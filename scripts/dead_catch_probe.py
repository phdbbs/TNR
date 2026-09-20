"""清点「死 catch」：`try { …静默读… } catch { …有代码… }` —— 作者写了失败提示，
但提示**覆盖不到服务端拒绝**。

## 背景：`TNR_API` 有三类方法，失败可见性完全不同

| 类别 | 判据（方法体） | 失败时 | catch 能否接住 |
|---|---|---|---|
| `get-silent` | 含 `this._get(` | 返回 `[]`（**忽略 HTTP 状态**） | ❌ 接不住 |
| `post-silent` | 含 `this._post(` / `this._postForm(` | 返回 `res.json()`（不校验 `success`） | ❌ 接不住**除非**外面包了 `TNR_UI.assertOk` |
| `throwing` | 含 `this.get(` / `this.post(` / `this._handle(`，或显式 `fetch(` + `throw` | 抛异常 | ✅ 接得住 |
| `pure` | 都不含（如 `getPetStatusText`） | 不发请求 | —（不参与判定） |
| `fallback` | 含 `fetch(` 且有本地兜底（如 `generatePetCodes`） | 静默降级到本地 | ⚠ 有意为之，单独列 |

`TNR_UI.assertOk(res, msg)` 只在 `res.success === false` 时抛错 —— 所以它**只对
`post-silent` 有效**：`get-silent` 返回的是 `data`（数组），`data.success` 是
`undefined`，断言永远不触发。

## 准确的措辞（**别说过头**）

catch **不是永远走不到**。它仍会触发于：
  * `fetch` 网络层失败（断网 / 连接被拒）；
  * 响应体不是 JSON（`res.json()` 抛错，例如网关返回 HTML 错误页）。
准确的结论是：**catch 覆盖不到「服务端返回了合法 JSON 的失败」**，
而那恰好是权限 / 参数 / 业务拒绝 / 内部错误的主要形态。

## 分类

| 分类 | 条件 | 是不是缺陷 |
|---|---|---|
| `死` | 只有静默读 + catch 有代码 | ✅ **缺陷**：提示永远不会出现 |
| `半死` | 静默读与抛错混用 + catch 有代码 | ⚠ 部分：静默读的失败仍被吞 |
| `静默吞` | 只有静默读 + catch **无代码**（含只有注释） | ❌ 不是：多为**有意降级**（注释会说明） |
| `活` | 只有抛错调用 | ❌ 不是：catch 有效 |

用法：python3 scripts/dead_catch_probe.py [--all]
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jslex import SCRIPT_RE, match_brace, strip_js  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
API_PATH = ROOT / 'static/js/tnr-api.js'
PORTALS = ('shelter', 'gov', 'hospital', 'adopter')

CALL_RE = re.compile(r'TNR_API\.([A-Za-z_]\w*)\s*\(')
CATCH_RE = re.compile(r'\s*catch\s*(\([^)]*\))?\s*\{')
SILENT_KINDS = ('get-silent', 'post-silent', 'silent-primitive')
PRIMITIVES = ('_get', '_post', '_postForm')


def api_methods(api_src):
    """方法名 -> 类别（见模块 docstring 的表）。

    ⚠ **同步方法也要扫**：`getPetStatusText` / `petAttrText` 这些纯文案函数是
    同步的。只扫 `async` 的话它们压根不在表里，`survey` 只能靠「未知即 pure」
    的默认值兜着 —— 那是**隐式**的，一旦默认值被改坏就没人发现。
    这里显式登记，让「纯函数」是个**结论**而不是**巧合**。
    """
    st = strip_js(api_src)
    out = {}
    patterns = (
        r'\basync\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*\{',   # async 方法
        r'(?m)^  ([A-Za-z_]\w*)\s*\([^)]*\)\s*\{',       # 同步方法（对象字面量里缩进 2 空格）
    )
    for pat in patterns:
        for m in re.finditer(pat, st):
            name = m.group(1)
            body = st[m.end() - 1:match_brace(st, st.index('{', m.start())) + 1]
            if name in PRIMITIVES:
                out[name] = 'silent-primitive'
            elif 'this._get(' in body:
                out[name] = 'get-silent'
            elif 'this._post(' in body or 'this._postForm(' in body:
                out[name] = 'post-silent'
            elif ('this.get(' in body or 'this.post(' in body or 'this._handle(' in body
                  or 'this.getData(' in body):
                out[name] = 'throwing'
            elif 'fetch(' in body:
                out[name] = 'throwing' if 'throw ' in body else 'fallback'
            elif 'throw new Error' in body:
                out[name] = 'throwing'     # `_handle` 这种「不发请求但负责抛错」的
            else:
                out[name] = 'pure'
    return out


# 这些是控制流关键字，不是方法名 —— 否则 `if (x) {` 会被当成方法
METHOD_KEYWORDS = {'if', 'for', 'while', 'switch', 'catch', 'do', 'else', 'function'}


def enclosing_method(body_st, offset):
    """向上找最近的 `xxx(...) {`，返回 (方法名, 体起点, 体终点)。

    ⚠ 必须跳过 `if` / `for` / `while` 这些控制流 —— 它们的形状也是
    `关键字 (… ) {`，不跳会报出一堆方法名叫 `if` 的条目（报告没法看）。
    """
    best = None
    for m in re.finditer(r'([A-Za-z_]\w*)\s*\([^)]*\)\s*\{', body_st[:offset]):
        if m.group(1) in METHOD_KEYWORDS:
            continue
        ob = body_st.index('{', m.start())
        oe = match_brace(body_st, ob)
        if oe >= offset:
            best = (m.group(1), ob, oe)
    return best or ('?', offset, offset)


SUCCESS_RE = re.compile(r'\.success\b')

# 「catch 体里向用户呈现了失败」的判据。
# 只有这些才算「作者承诺了失败提示」—— 承诺落空才是缺陷。
# `pets = []`（降级为空）或只有注释，都属于**有意降级**，不在缺陷之列。
PRESENT_RE = re.compile(r'\b(toast|alert)\s*\(|\.innerHTML\b|\.textContent\b')


def survey(src, api):
    rows = []
    for m in SCRIPT_RE.finditer(src):
        if re.search(r'\bsrc\s*=', m.group('attrs'), re.IGNORECASE):
            continue
        a = m.start('body')
        body_st = strip_js(src[a:m.end('body')])
        for t in re.finditer(r'\btry\s*\{', body_st):
            try_end = match_brace(body_st, body_st.index('{', t.start()))
            if try_end < 0:
                continue
            cm = CATCH_RE.match(body_st[try_end + 1:try_end + 200])
            if not cm:
                continue
            cb = body_st.index('{', try_end + 1 + cm.start())
            catch_end = match_brace(body_st, cb)
            if catch_end < 0:
                continue
            try_body = body_st[t.start():try_end + 1]
            calls = sorted(set(CALL_RE.findall(try_body)))
            if not calls:
                continue
            mname, mob, moe = enclosing_method(body_st, t.start())
            method_body = body_st[mob:moe + 1]
            # 写接口的失败**可观测**：`_post` 返回完整信封，调用方可以查
            # `res.success` / 用 `assertOk`。只要方法体里查了，就不算静默。
            observed = ('TNR_UI.assertOk(' in method_body
                        or bool(SUCCESS_RE.search(method_body)))
            sil, thr, pure = [], [], []
            for c in calls:
                kind = api.get(c, 'pure')
                if kind == 'get-silent':
                    # `_get` 只返回 `data`，信封被丢掉 —— **构造上不可观测**，
                    # `assertOk(data)` / `data.success` 都永远不触发。
                    sil.append(c)
                elif kind in SILENT_KINDS:
                    (thr if observed else sil).append(
                        c + ('(已查success)' if observed else ''))
                elif kind == 'throwing':
                    thr.append(c)
                elif kind == 'fallback':
                    pure.append(c + '(本地兜底)')
                else:
                    pure.append(c)
            code = body_st[cb + 1:catch_end]
            orig = src[a + cb + 1:a + catch_end]
            comments = re.findall(r'/\*(.*?)\*/|//([^\n]*)', orig, re.DOTALL)
            rows.append({
                'line': src.count('\n', 0, a + t.start()) + 1,
                'method': mname,
                'sil': sil, 'thr': thr, 'pure': pure,
                'presents': bool(PRESENT_RE.search(code)),
                'comment': ' '.join((c[0] or c[1] or '').strip()
                                    for c in comments).strip(),
            })
    return rows


def classify(r):
    """`死` / `半死` 只判「**catch 里呈现了失败** 而 try 里全是静默读」这一种。

    理由：`catch (e) { pets = []; }` 或只写注释的 catch，是**有意降级**
    （项目明确接受「读接口失败降级成空」）。而 `catch (e) { toast('加载失败') }`
    是作者**承诺了用户可见的失败提示**，却永远兑现不了 —— 那才是缺陷。
    """
    if not r['presents']:
        return '静默吞' if r['sil'] else '活'
    if not r['sil']:
        return '活'
    return '半死' if r['thr'] else '死'


def main():
    show_all = '--all' in sys.argv
    api = api_methods(API_PATH.read_text(encoding='utf-8'))
    counts = {}
    for k in ('get-silent', 'post-silent', 'throwing', 'fallback', 'pure'):
        counts[k] = sorted(n for n, v in api.items() if v == k)
    print(f'接口方法共 {len(api)} 个：'
          f'get-silent {len(counts["get-silent"])} / post-silent {len(counts["post-silent"])}'
          f' / throwing {len(counts["throwing"])} / fallback {len(counts["fallback"])}'
          f' / pure {len(counts["pure"])}')
    print(f'  get-silent ：{" ".join(counts["get-silent"])}')
    print(f'  post-silent：{" ".join(counts["post-silent"])}')
    print(f'  fallback   ：{" ".join(counts["fallback"])}')
    print()

    tally = {}
    need = []
    for name in PORTALS:
        src = (ROOT / f'templates/portal/{name}/portal.html').read_text(encoding='utf-8')
        rows = survey(src, api)
        print(f'== {name}：含 API 调用的 try/catch {len(rows)} 处')
        for r in rows:
            kind = classify(r)
            tally[kind] = tally.get(kind, 0) + 1
            if kind in ('死', '半死'):
                need.append((name, r, kind))
            if show_all or kind in ('死', '半死'):
                detail = []
                if r['sil']:
                    detail.append('静默=' + ' '.join(r['sil']))
                if r['thr']:
                    detail.append('抛错=' + ' '.join(r['thr']))
                if r['pure']:
                    detail.append('纯函数=' + ' '.join(r['pure']))
                print(f'   L{r["line"]:<6} {r["method"]:<26} [{kind}] {" ; ".join(detail)}')
    print(f'\n分类汇总：{tally}')
    print(f'\n需要处理（死 + 半死）：{len(need)} 处')
    for name, r, kind in need:
        print(f'  {name:9s} L{r["line"]:<6} {r["method"]:<26} [{kind}]')
        print(f'             静默读：{" ".join(r["sil"])}')


if __name__ == '__main__':
    main()
