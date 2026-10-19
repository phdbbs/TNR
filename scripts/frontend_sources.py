"""门户前端源码清单 —— 静态闸门共用的**唯一真源**（第五十二轮 重构 B）。

## 为什么需要它

判据文件里到处硬编码 `templates/portal/<role>/portal.html`，一共 8 个文件、30 多处。
把内联脚本抽成 `static/js/portals/<role>.js` 之后，每一个都得改；
**漏掉的那个不报错**，只会让那条闸门看不见它本该看见的代码 ——
`core/tests.py` 的 `FRONTEND_SOURCE_FILES` 注释里写警告，正是因为已经发生过一次
「新增门户忘了登记，覆盖面无声明地缩水」。

所以这里只登记一次，两类消费方各取所需：

| 取什么 | 用于 |
|---|---|
| `read_html(role)` | 只关心 **HTML** 的判据（侧栏可达性、粘性表头、注释泄漏） |
| `read_js(role)` | 只关心 **JS** 的判据（静默读、死 catch、键契约、锚点存在性） |
| `read_portal(role)` | 两者混在同一段断言里的老用例：模板 + JS 拼起来，**逐字节**保留合并前的可匹配文本 |
| `iter_js_paths()` / `all_js_source()` | 「扫全量前端 JS」的闸门用它们，别自己拼列表 |
| `all_templates()` | 「扫全量模板」的闸门用它（HTML/CSS 类判据） |

`read_portal()` 把模板和 JS 拼起来是有意的兼容层：判据写的是「这段代码里必须
出现 `TNR_API.getTreatmentBoard`」，它不该关心代码住在哪个文件里。但**新写的**
判据请直接用语义更窄的 `read_js()` / `read_html()`。

## 抽出去的文件必须存在

`read_js()` 找不到文件就抛 `AssertionError`，**不返回空串**。
返回空串等于「这个门户没有 JS」，所有基于它的 `assertIn` 会全红、
而基于它的 `assertNotIn`（「不得用静默读」这类**禁止式**判据）会**全绿** ——
后者是危险的那一半：闸门看起来过了，其实什么都没看。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 五个业务端。新增一端必须同时登记模板与 JS —— 这里漏一个，
#: 等于那一端的 JS 从此没有任何静态校验。
ROLES = ('shelter', 'hospital', 'gov', 'platform', 'adopter')

#: 共享的运行时脚本（不属于某个门户，但同属前端源码全量）。
SHARED_JS = (
    ROOT / 'static' / 'js' / 'tnr-common.js',
    ROOT / 'static' / 'js' / 'tnr-api.js',
)

#: 外壳与角色 base：只有 HTML/CSS 与侧栏开关，不含业务 JS。
SHELL_HTML = ROOT / 'templates' / 'portal' / '_portal_shell.html'


def template_path(role):
    return ROOT / 'templates' / 'portal' / role / 'portal.html'


def js_path(role):
    return ROOT / 'static' / 'js' / 'portals' / f'{role}.js'


def _read(path):
    assert path.exists(), f'前端源码文件缺失：{path}（判据会失真，先把它补回来）'
    return path.read_text(encoding='utf-8')


def read_html(role):
    """只取模板文本（HTML/CSS 判据用；**不含**已抽出去的 JS）。"""
    return _read(template_path(role))


def read_js(role):
    """只取该端的门户 JS（`static/js/portals/<role>.js`）。"""
    return _read(js_path(role))


def read_portal(role):
    """模板 + 该端 JS，中间空一行 —— 给「HTML 与 JS 混在同一段断言」的老用例。"""
    return read_html(role) + '\n' + read_js(role)


def all_templates():
    """五份门户模板（HTML/CSS 判据的全量）。"""
    return tuple(template_path(r) for r in ROLES)


def iter_js_paths():
    """全量前端 JS：5 个门户 + 2 个共享运行时。

    「扫全量 JS」的闸门用它，别自己拼列表 —— 本项目的历史故障里有整整一类是
    「闸门只扫 `templates/portal/*.html`，JS 搬到 `static/` 之后覆盖面无声明地
    少掉 12,783 行」。`core/tests.py` 里那句
    「⚠ 新增门户 / JS 时必须加进来，否则闸门看不见它读的键」说的就是这件事，
    而它当时靠人记得；现在只剩一处，新增一端改 `ROLES` 一个词。
    """
    return [js_path(r) for r in ROLES] + list(SHARED_JS)


def all_js_source():
    """`iter_js_paths()` 的文本拼接（给只需要「一段源码」的判据用）。"""
    return '\n'.join(_read(p) for p in iter_js_paths())
