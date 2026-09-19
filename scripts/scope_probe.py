"""越权探针矩阵：把「全部带 id 的接口 × 全部角色」跑一遍，找漏判的那几处。

## 为什么要有这个脚本

第二十 / 二十一轮的横向越权扫描**只覆盖了医院角色**（读侧、写侧各一轮），
其余角色是抽查。抽查的通病是：**没查的那些「没报错」不等于「没问题」**。

本脚本把方法固化成工具，做到「**枚举式覆盖**」：
带 id 的接口一个不漏，每个接口的每个允许角色都探一次，
并把「探不了」和「探了没问题」**分开报告**。

## 三种判定（必须分开，否则就是自欺）

| 判定 | 含义 |
|---|---|
| `REFUSED` | 被拒（403 / 404）—— 这是期望结果 |
| `**SUSPECT**` | **返回 200** —— 越权成功，必须人工确认 |
| `INCONCLUSIVE` | 400 / 405 / 500 —— 请求**没走到判据**（多半是 payload 不对）。**不算通过** |

`INCONCLUSIVE` 是这套探针最容易自欺的地方：如果把它当成「被拒了」，
那么「payload 写错」就会伪装成「接口很安全」。所以它必须单独列出、
并且**必须逐条消掉**（补正确的 payload），否则本轮不算查完。

## 两种横向维度

- **跨区县**：攻击者区县 ≠ 对象区县（所有角色都适用）；
- **跨机构（同区县）**：需要同区县存在 ≥2 个同类机构。演示库里
  襄城区有两家医院（爱心 #3 / 瑞鹏 #4），所以医院侧能构造；
  捕捉点在襄城区只有 1 个，构造不出来（见报告 §2.27.7）。

## 安全

- 写操作**一律包在回滚事务里**（`transaction.atomic()` + `set_rollback(True)`），
  不会在真实库留下任何痕迹；
- 脚本只读地枚举路由与对象，不修改任何数据。

用法：

    .venv/bin/python scripts/scope_probe.py            # 全部
    .venv/bin/python scripts/scope_probe.py capture    # 只看路径含 capture 的
"""
import json
import os
import re
import sys
import pathlib

import django

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tnr_system.settings')
os.environ.setdefault('DEBUG', 'on')
os.environ.setdefault('SECRET_KEY', 'probe')
os.environ.setdefault('ALLOWED_HOSTS', 'testserver,localhost,127.0.0.1')
django.setup()

from django.db import transaction                                    # noqa: E402
from django.db.models import Q                                       # noqa: E402
from django.test import Client                                       # noqa: E402
from django.urls import get_resolver                                 # noqa: E402

from accounts.models import User                                     # noqa: E402
from business.models import (                                        # noqa: E402
    Adoption, AdoptionApplication, Blacklist, Capture, CheckIn, Euthanasia,
    MaterialTransaction, Message, Pet, Release, Transfer, Treatment,
)

ROLES = {'gov_city', 'gov_district', 'shelter', 'hospital', 'adopter'}
PASSWORD = '123456'

# 对照组的 id：必然不存在（表里不会有这个主键）
GHOST_ID = 99999999


# ---------------------------------------------------------------- 角色解析
def roles_of(callback):
    """从 `role_required` 的闭包里取允许角色集合。"""
    f = callback
    for _ in range(6):
        for cell in getattr(f, '__closure__', None) or []:
            try:
                v = cell.cell_contents
            except Exception:
                continue
            if isinstance(v, (tuple, list)) and v and all(
                    isinstance(x, str) and x in ROLES for x in v):
                return set(v)
        f = getattr(f, '__wrapped__', None)
        if f is None:
            break
    return None


def id_routes():
    """全部「路径里带 id 参数」的 `/api/` 路由。"""
    pairs = []

    def walk(resolver, prefix=''):
        for p in resolver.url_patterns:
            if hasattr(p, 'url_patterns'):
                walk(p, prefix + str(p.pattern))
            else:
                pairs.append(('/' + (prefix + str(p.pattern)).lstrip('/'), p.callback))

    walk(get_resolver())
    out = []
    for pat, cb in pairs:
        if '/api/' not in pat:
            continue
        if not re.search(r'<(?:int:)?(pk|pet_id)>', pat):
            continue
        rs = roles_of(cb)
        if rs is None:
            continue
        out.append((pat, getattr(cb, '__name__', ''), rs))
    return sorted(out)


# ---------------------------------------------------------------- 对象挑取
def district_lookup(model):
    """返回该模型「区县字段」的路径，用于 `exclude(<path>_id=...)`。

    三种情形（本项目都出现过）：
      - 模型自己有 `district` 外键 → `district`
      - 模型声明了 `DISTRICT_LOOKUP`（如 `pet__district`）→ 用它
      - 都没有 → 返回 None（调用方必须显式处理，**不能**默认成 district，
        否则会 `FieldError`）
    """
    names = {f.name for f in model._meta.get_fields()}
    if 'district' in names:
        return 'district'
    lookup = getattr(model, 'DISTRICT_LOOKUP', None)
    if lookup:
        return lookup
    if 'pet' in names:
        return 'pet__district'
    return None


def _by_district(model, district_id, exclude_ids=(), **extra):
    """取一个**不属于该区县**的对象（跨区县探针的目标）。"""
    qs = model.objects.filter(**extra).exclude(id__in=list(exclude_ids))
    path = district_lookup(model)
    if path is None or district_id is None:
        return qs.first()
    return qs.exclude(**{path + '_id': district_id}).first()


# 目标对象的**结构性**约束（只用来选出「类型正确」的目标）。
#
# ⚠ 这里**故意不按状态过滤**。
# 曾经加过 `status='pending'` 之类的过滤，想「让请求走到归属判据」——
# 结果适得其反：目标状态不对时探针直接**跳过**，把预言机**藏了起来**。
# 现在改用「与不存在的 id 对比」的自校准判据（见 main），
# 状态门禁造成的响应差异**本身就是要抓的预言机**，不该被过滤掉。
VICTIM_FILTERS = {
    # 结构性的：`material_receive` 只认 dispatch 行，选错类型会 404，测不出东西
    'material_receive': {'type': 'dispatch'},
}


def _f(name):
    return VICTIM_FILTERS.get(name, {})


def victim_for(name, attacker):
    """给一个路由挑「不该被这个攻击者碰到的」对象。

    返回 (对象, 说明) 或 (None, 原因)。
    """
    d = attacker.district_id
    extra = _f(name)

    if name in ('capture_detail', 'capture_update', 'capture_delete'):
        obj = _by_district(Capture, d, **extra)
        return (obj, '跨区县捕捉单') if obj else (None, '无跨区县捕捉单')

    if name in ('transfer_receive', 'transfer_reject', 'transfer_withdraw'):
        obj = _by_district(Transfer, d, **extra)
        return (obj, '跨区县转运单') if obj else (None, '无跨区县转运单')

    if name == 'treatment_detail':
        obj = _by_district(Treatment, d, **extra)
        return (obj, '跨区县诊疗单') if obj else (None, '无跨区县诊疗单')

    if name == 'material_receive':
        obj = _by_district(MaterialTransaction, d, **extra)
        return (obj, '跨区县下发单') if obj else (None, '无跨区县下发单')

    if name == 'release_confirm':
        obj = _by_district(Release, d, **extra)
        return (obj, '跨区县放养单') if obj else (None, '无跨区县放养单')

    if name == 'body_receive':
        obj = _by_district(Euthanasia, d, **extra)
        return (obj, '跨区县安乐死单') if obj else (None, '无跨区县安乐死单')

    if name in ('adoption_info_edit', 'adoption_confirm_claim', 'adoption_reclaim'):
        obj = _by_district(Pet, d, **extra)
        return (obj, '跨区县宠物') if obj else (None, '无跨区县宠物')

    if name == 'owner_return_create':
        # ⚠ 这条路由的 `<int:pk>` **被完全忽略** —— 真正的 pet 从请求体 `pet_id` 取。
        # 所以跨区县目标要放进 body，不能只改 URL。
        obj = _by_district(Pet, d, is_deleted=False)
        return (obj, '跨区县宠物（走 body 的 pet_id）') if obj else (None, '无跨区县宠物')

    if name == 'adoption_application_review':
        obj = _by_district(AdoptionApplication, d, **extra)
        return (obj, '跨区县领养申请') if obj else (None, '无跨区县领养申请')

    if name == 'checkin_review':
        obj = _by_district(CheckIn, d, **extra)
        return (obj, '跨区县回访打卡') if obj else (None, '无跨区县回访打卡')

    if name in ('blacklist_update', 'blacklist_delete'):
        obj = _by_district(Blacklist, d, **extra)
        return (obj, '跨区县黑名单') if obj else (None, '无跨区县黑名单')

    if name == 'pet_lifecycle':
        obj = _by_district(Pet, d, **extra)
        return (obj, '跨区县宠物档案') if obj else (None, '无跨区县宠物')

    if name == 'mark_message_read':
        obj = Message.objects.exclude(user=attacker).first()
        return (obj, '他人的站内消息') if obj else (None, '无他人消息')

    if name in ('institution_edit', 'institution_toggle_status'):
        obj = _by_district(_Inst(), d)
        return (obj, '跨区县机构') if obj else (None, '无跨区县机构')

    if name in ('district_edit', 'district_toggle_status', 'district_delete'):
        obj = _by_district(_District(), d, is_city=False)
        return (obj, '跨区县区县') if obj else (None, '无跨区县区县')

    if name == 'user_toggle_status':
        obj = User.objects.exclude(id=attacker.id)
        if d is not None:
            obj = obj.exclude(district_id=d)
        obj = obj.first()
        return (obj, '跨区县账号') if obj else (None, '无跨区县账号')

    return None, '未登记的对象挑取规则（新接口？请补上）'


def _Inst():
    from core.models import Institution
    return Institution


def _District():
    from core.models import District
    return District


# ---------------------------------------------------------------- 请求 payload
# 目的：让请求能**走到判据**。填错会变成 INCONCLUSIVE，必须逐条消掉。
PAYLOADS = {
    'capture_update': {'community': '', 'address': '探针'},
    'capture_delete': {},
    'owner_return_create': {'owner_name': '探针', 'owner_phone': '13900000000'},
    'transfer_receive': {},
    'transfer_reject': {'reason': '探针'},
    'transfer_withdraw': {},
    'material_receive': {},
    'release_confirm': {},
    'adoption_info_edit': {'intro': '探针', 'is_active': True},
    'adoption_confirm_claim': {},
    'adoption_reclaim': {'reason': '探针'},
    'adoption_application_review': {'action': 'approve'},
    'checkin_review': {'status': 'approved'},
    'blacklist_update': {'name': '探针', 'phone': '13900000000'},
    'blacklist_delete': {},
    'euthanasia_body_receive': {'receiver_name': '探针'},
    'mark_message_read': {},
    'institution_edit': {'name': '探针机构'},
    'institution_toggle_status': {},
    'district_edit': {'name': '探针区县'},
    'district_toggle_status': {},
    'district_delete': {},
    'user_toggle_status': {},
}


def payload_for(name, obj):
    """请求体。个别接口的目标 id 走 body 而不是 URL，这里补上。"""
    base = dict(PAYLOADS.get(name, {}))
    if name == 'owner_return_create' and obj is not None:
        base['pet_id'] = obj.id
    return base


def url_for(pattern, obj):
    """把 `<int:pk>` / `<int:pet_id>` 换成真实 id。"""
    if not obj:
        return None
    return re.sub(r'<(?:int:)?(pk|pet_id)>', str(obj.id), pattern)


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    routes = id_routes()

    # ⚠ 攻击者必须选「**区县被收窄**」的账号。
    # 账号区县挂「全市（市级）」的（`district.is_city=True`）本来就该看到全部 ——
    # 用它做探针**必然 200**，那是假阳性，不是洞。演示库实测：
    # `hd_shelter` / `xc_shelter` / `dc_shelter` 三个捕捉点账号都是这种。
    # 领养人没有区县（`district` 为 NULL），同样属于「被收窄」的一类。
    def pick_attacker(role):
        qs = User.objects.filter(role=role, is_active=True)
        narrow = qs.filter(Q(district__isnull=True) | Q(district__is_city=False))
        if narrow.exists():
            return narrow.first(), None
        if qs.exists():
            return None, ('该角色只有「账号区县=全市」的账号 —— 用它探针必然 200，'
                          '是假阳性，跳过（不是洞）')
        return None, '演示库无该角色的可用账号'

    print('=' * 100)
    print('越权探针矩阵 —— 攻击者取「区县被收窄」的账号，目标取「外区县」对象')
    print('=' * 100)

    suspects, inconclusive, refused, skipped, oracles = [], [], [], [], []

    for pattern, name, roles in routes:
        if only and only not in pattern:
            continue
        for role in sorted(roles):
            attacker, why_not = pick_attacker(role)
            if attacker is None:
                skipped.append((pattern, role, why_not))
                continue
            obj, why = victim_for(name, attacker)
            if obj is None:
                skipped.append((pattern, role, why))
                continue

            url = url_for(pattern, obj)
            method = 'GET' if name.endswith(('_detail', '_lifecycle')) else 'POST'
            payload = payload_for(name, obj)

            # 对照组：同一接口、同一攻击者，但 id **一定不存在**。
            # 「跨区县 id」与「不存在的 id」响应不同 = **存在性预言机** ——
            # 攻击者能靠枚举 id 判断哪些记录存在、甚至读出业务状态。
            # 这条判据是**自校准**的：不依赖猜错误消息文本，也不依赖状态码约定。
            ghost_url = re.sub(r'<(?:int:)?(pk|pet_id)>', str(GHOST_ID), pattern)
            ghost_payload = payload_for(name, None)
            if name == 'owner_return_create':
                ghost_payload['pet_id'] = GHOST_ID

            client = Client()
            ok_login = client.login(username=attacker.username, password=PASSWORD)
            if not ok_login:
                skipped.append((pattern, role, f'{attacker.username} 登录失败'))
                continue

            def fire(u, p):
                try:
                    with transaction.atomic():
                        if method == 'GET':
                            resp = client.get(u)
                        else:
                            # 必须发 JSON：`parse_json_body` 只解析 JSON 体，
                            # 用 test client 的 dict 会变成 multipart → 视图读到空 dict。
                            resp = client.post(
                                u, json.dumps(p), content_type='application/json')
                        out = (resp.status_code, resp.content[:160].decode('utf-8', 'replace'))
                        transaction.set_rollback(True)   # 真实库不留痕
                        return out
                except Exception as exc:                 # noqa: BLE001
                    return 500, f'异常 {exc!r}'

            status, body = fire(url, payload)
            ghost_status, ghost_body = fire(ghost_url, ghost_payload)

            row = (pattern, role, attacker.username, f'{obj.__class__.__name__}#{obj.id}',
                   why, status, body, ghost_status, ghost_body)
            if status == 200:
                suspects.append(row)
            elif status != ghost_status:
                oracles.append(row)
            elif status in (403, 404):
                refused.append(row)
            else:
                inconclusive.append(row)

    def dump(title, rows, mark):
        print(f'\n{mark} {title}（{len(rows)}）')
        print('-' * 100)
        for pattern, role, who, target, why, status, body, gs, gb in rows:
            print(f'  {mark} [{role}] {pattern}')
            print(f'     账号={who:14s} 目标={target:24s} {why}')
            print(f'     跨区县 HTTP {status}  {body[:120]}')
            print(f'     不存在 HTTP {gs}  {gb[:120]}')

    dump('越权成功 —— 必须人工确认', suspects, '🔴')
    dump('存在性预言机 —— 「跨区县 id」与「不存在的 id」响应不同，泄露记录存在/状态',
         oracles, '🟠')
    dump('请求没走到判据（payload / 方法不对）—— **不算通过**', inconclusive, '🟡')
    dump('正确拒绝（且与「不存在」的响应一致，无预言机）', refused, '✅')

    print(f'\n⚪ 跳过（{len(skipped)}）')
    print('-' * 100)
    for pattern, role, why in skipped:
        print(f'  ⚪ {pattern}  [{role}]  {why}')

    print('\n' + '=' * 100)
    print(f'合计：越权嫌疑 {len(suspects)}｜存在性预言机 {len(oracles)}｜'
          f'结论不明 {len(inconclusive)}｜正确拒绝 {len(refused)}｜跳过 {len(skipped)}')
    return 1 if (suspects or oracles or inconclusive) else 0


if __name__ == '__main__':
    sys.exit(main())
