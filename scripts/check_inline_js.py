#!/usr/bin/env python3
"""渲染各门户页面，把内联 <script> 抽出来做语法检查。

存在的理由：模板里的内联 JS **没有任何静态校验** —— Django 模板标签会让
`node --check` 直接失败，所以「改了模板 JS 但写错一个括号」只能等浏览器
报错才发现，而浏览器里的表现是**整页白屏**（脚本解析失败 → 后续
`DOMContentLoaded` 回调根本不注册），不是一句可读的错误。
这里把渲染结果抽出来交给 node 解析，把这类错误挡在部署前。

用法（在项目根，用本地 venv）::

    .venv/bin/python scripts/check_inline_js.py

node 位置：优先环境变量 `NODE_BIN`，其次托管运行时，最后回落 PATH 里的 `node`。

退出码：有语法错误 → 1；全部通过 → 0。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tnr_system.settings')
import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.test import Client  # noqa: E402

# 测试客户端默认用 Host: testserver，生产配置的 ALLOWED_HOSTS 里没有它
settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ['testserver']

#: 要覆盖的门户页。**新增门户必须加进来** —— 漏一个端就是漏一个端的校验，
#: 而脚本照样打印「全部通过」。
PAGES = [
    ('cy_shelter', '/shelter/'),
    ('aixin_hosp', '/hospital/'),
    ('cy_gov', '/gov/'),
    ('adopter1', '/adopter/'),
    ('platform', '/platform/'),
]

SCRIPT_RE = re.compile(r'<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.S)


def resolve_node():
    cand = [os.environ.get('NODE_BIN'),
            '/Users/wl/.workbuddy-ai/binaries/node/versions/22.22.2-3/bin/node']
    for c in cand:
        if c and os.path.exists(c):
            return c
    found = shutil.which('node')
    if found:
        return found
    print('✗ 找不到 node —— 请设置 NODE_BIN=/path/to/node 后重试')
    sys.exit(2)


def main():
    node = resolve_node()
    failures = 0
    checked = 0
    for user, url in PAGES:
        c = Client()
        if not c.login(username=user, password='123456'):
            print(f'  ✗ 登录失败：{user}（先跑 seed_data）')
            failures += 1
            continue
        resp = c.get(url)
        if resp.status_code != 200:
            print(f'  ✗ {url} -> HTTP {resp.status_code}')
            failures += 1
            continue
        html = resp.content.decode('utf-8')
        blocks = SCRIPT_RE.findall(html)
        if not blocks:
            print(f'  ✗ {url} 没有内联脚本？')
            failures += 1
            continue
        page_fail = 0
        for i, code in enumerate(blocks):
            checked += 1
            with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                             encoding='utf-8') as f:
                f.write(code)
                path = f.name
            r = subprocess.run([node, '--check', path], capture_output=True, text=True)
            if r.returncode != 0:
                failures += 1
                page_fail += 1
                print(f'  ✗ {url} 第 {i + 1} 段内联脚本语法错误：')
                print('    ' + (r.stderr or '').strip().replace('\n', '\n    ')[:800])
            os.unlink(path)
        # ⚠ 「✓」必须只在**本页全部通过**时打 —— 无条件打会让「有语法错误的页」
        # 也显示一个 ✓，是最典型的假绿（本项目已多次栽在这类信号上）。
        if page_fail:
            print(f'  ✗ {url}（{len(blocks)} 段内联脚本，{page_fail} 段语法错误）')
        else:
            print(f'  ✓ {url}（{len(blocks)} 段内联脚本）')

    print()
    print(f'  检查了 {checked} 段内联脚本，{failures} 处失败')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
