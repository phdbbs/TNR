"""把单据打印页的**真实渲染结果**落盘成 HTML，用于肉眼核对 A4 版式。

用法：
    .venv/bin/python scripts/render_print_preview.py

结果固定写到仓库内 `gui-test-screenshots/print_preview/`（已在 .gitignore，
本地产物不入库）。不接受命令行指定输出目录 —— 单据内容来自真实库数据，
落盘路径必须是脚本可控的固定位置，避免「参数拼路径」这类穿越面。

为什么要有这个脚本
------------------
第四十六轮发现「诊疗单打出来是『单据号：—』」**不是靠断言发现的** ——
`test_renders_doc_no_qr_and_related` 当时是绿的（它断言的是「页面里有单据号
字符串」，而那条单据恰好有号）。是**把打印页用真实库数据渲染成 HTML、
拿眼睛看**才发现的：顺着查下去挖出 66 条消耗流水一个号都没有。

所以这是一个**验收手段**，不是测试：它不进 `manage.py test`，
因为它打的是**真实 `db.sqlite3`**（不在 `TestCase` 里就不走测试库），
结果随库里数据变，不适合当判据。

⚠ 两个坑（都踩过）：
  1. `Client()` 不传 `SERVER_NAME` 会因 `testserver` 不在 `ALLOWED_HOSTS`
     拿到 **400 页**（而且是个很大的 Django DEBUG 页，看着不像出错）；
  2. `build_absolute_uri()` 生成的二维码内容会带 `http://localhost/...` ——
     这是 `Client` 的 host，**不是**真实部署域名，看版式时忽略即可。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tnr_system.settings')

import django  # noqa: E402
django.setup()

from django.test import Client                      # noqa: E402
from accounts.models import User                    # noqa: E402
from business.models import Capture, MaterialTransaction, Treatment, Transfer  # noqa: E402

OUTDIR = os.path.join(ROOT, 'gui-test-screenshots', 'print_preview')


def _write(filename, content):
    """把渲染结果写入固定输出目录内的白名单文件名。

    路径穿越守卫：无论 filename 将来怎么扩展，落盘目标 resolve 后
    必须**仍在本脚本固定的 OUTDIR 内**，否则直接拒绝 —— 输出位置
    永远不出这个目录。
    """
    import pathlib
    base = pathlib.Path(OUTDIR).resolve()
    target = (base / filename).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f'非法输出路径：{filename}')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return str(target)


def _best_capture():
    """挑关联单据最多的捕捉单 —— 这样关联链区块也会被渲染出来。"""
    best, best_n = None, -1
    for cap in Capture.objects.filter(is_deleted=False):
        codes = {c.strip() for c in (cap.pet_codes or '').split(',') if c.strip()}
        n = sum(Treatment.objects.filter(pet_code=c).count() for c in codes)
        n += sum(1 for t in Transfer.objects.all()
                 if codes & {x.strip() for x in (t.pet_codes or '').split(',')})
        if n > best_n:
            best, best_n = cap, n
    return best


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    user = (User.objects.filter(role='gov_city', is_active=True).first()
            or User.objects.filter(is_superuser=True).first())
    if user is None:
        print('库里没有任何可用的 gov_city / 超管账号 —— 先跑 seed_data')
        return 1
    print('登录身份：%s（%s）' % (user.username, user.role))

    # ⚠ 必须传 SERVER_NAME，否则 DisallowedHost → 400 页
    client = Client(SERVER_NAME='localhost')
    client.force_login(user)

    jobs = []
    cap = _best_capture()
    if cap:
        jobs.append(('capture', cap.id, 'capture'))
    tr = Treatment.objects.first()
    if tr:
        jobs.append(('treatment', tr.id, 'treatment'))
    mt = MaterialTransaction.objects.filter(type='consume').first()
    if mt:
        jobs.append(('material', mt.id, 'material_consume'))

    written = []
    for doc, pk, name in jobs:
        resp = client.get('/print/%s/%s/' % (doc, pk))
        path = _write('print_preview_%s.html' % name, resp.content)
        # 「单据号：—」= 这条单据没有号（归档时无法编号）—— 这是本脚本最该盯的一行
        blank_no = b'<td class="lbl">\xe5\x8d\x95\xe6\x8d\xae\xe5\x8f\xb7\xef\xbc\x9a</td><td>\xe2\x80\x94</td>' in resp.content
        print('  %-20s /print/%s/%s/  %s  %s  %s'
              % (name, doc, pk, resp.status_code,
                 resp['Content-Type'].split(';')[0],
                 '⚠ 单据号为空！' if blank_no else 'ok'))
        written.append(path)

    resp = client.get('/print/nothing/1/')
    path = _write('print_preview_404.html', resp.content)
    print('  %-20s /print/nothing/1/  %s（未知单据类型）' % ('404', resp.status_code))
    written.append(path)

    print('\n已写出：')
    for p in written:
        print('  ' + p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
