#!/usr/bin/env python3
"""把 `'<div id="X">' + TNR_UI.renderFilterBar([...]) + '</div>'` 这种
「同高包裹层」写法改成 `TNR_UI.renderFilterBar([...], '', { id: 'X' })`。

为什么要改：那一层包裹 div 与 `.filter-bar` 同高，`position: sticky` 的包含块
就是它自己 —— 筛选条**一点可移动空间都没有**，冻结直接失效、跟着内容滚走。
（实测：滚后 top=-401，而正常应为 102。）

用括号配平扫描参数体，不靠正则去猜 —— 参数体里有 `[` `]` `{` `}` 和字符串。
默认 dry-run，只打印将要做的替换；加 --apply 才写盘。
"""
import re
import sys
import os

FILES = [
    'templates/portal/hospital/portal.html',
    'templates/portal/shelter/portal.html',
]

PREFIX = re.compile(r"'<div id=\"(\w+)\">'(\s*\+\s*)TNR_UI\.renderFilterBar\(")
# ⚠ 不要写 `^`：`pattern.match(s, pos)` 里的 `pos` 不影响 `^` 的锚点
# （`^` 永远锚在**整个字符串**开头），带 pos 用 `^` 会永远匹配不上。
TAIL = re.compile(r"\s*\+\s*'</div>'")


def match_paren(s, open_idx):
    """从 s[open_idx] == '(' 起找配对的 ')'，跳过字符串字面量与注释。"""
    assert s[open_idx] == '(', s[open_idx - 20:open_idx + 20]
    depth = 0
    quote = None
    i = open_idx
    while i < len(s):
        c = s[i]
        if quote:
            if c == '\\':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in '"\'`':
            quote = c
        elif c == '/':
            if s[i:i + 2] == '//':
                j = s.find('\n', i)
                i = len(s) if j < 0 else j
                continue
            if s[i:i + 2] == '/*':
                j = s.find('*/', i)
                i = len(s) if j < 0 else j + 2
                continue
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise RuntimeError('括号不配平')


def transform(src):
    out = src
    edits = []
    pos = 0
    while True:
        m = PREFIX.search(out, pos)
        if not m:
            break
        name = m.group(1)
        open_idx = m.end() - 1
        close_idx = match_paren(out, open_idx)
        body = out[open_idx + 1:close_idx]
        tail_m = TAIL.match(out, close_idx + 1)
        if not tail_m:
            print(f'  !! 跳过 {name}：结尾不是 + \'</div>\'（实际 {out[close_idx + 1:close_idx + 40]!r}）')
            pos = close_idx + 1
            continue
        new = f"TNR_UI.renderFilterBar({body}, '', {{ id: '{name}' }})"
        out = out[:m.start()] + new + out[tail_m.end():]
        edits.append((name, new.replace('\n', ' ')[:110]))
        pos = m.start() + len(new)
    return out, edits


def main():
    apply = '--apply' in sys.argv
    total = 0
    for path in FILES:
        src = open(path, encoding='utf-8').read()
        out, edits = transform(src)
        print(f'\n===== {path}：命中 {len(edits)} 处 =====')
        for name, snippet in edits:
            print(f'  - {name}: {snippet}…')
        total += len(edits)
        if apply and out != src:
            open(path, 'w', encoding='utf-8').write(out)
            print(f'  ✅ 已写盘')
    print(f'\n合计 {total} 处；{"已应用" if apply else "dry-run（加 --apply 写盘）"}')


if __name__ == '__main__':
    main()
