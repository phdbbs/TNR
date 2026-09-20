"""剥离 HTML 内联 `<script>` 里的 JS 注释 / 字符串 / 正则字面量（**长度不变**）。

给审计脚本与测试共用：扫描「模板里到底写了什么 JS」时，必须先把字符串、模板、
注释、正则抹成等长空白，否则中文提示语里的 `try`、注释里的 `catch`、
正则里的引号都会把判据带偏。

## 为什么必须**只在 `<script>` 区域内**剥

整个 HTML 文件里到处是属性引号（`class="…"`、`data-page-node-id="…"`），
拿 JS 的词法器去剥它们会产生上千个失配区段；一旦某处失步还会**跨越区域**污染
后面的脚本，扫描结果变成「时对时错」，比错更糟。

## JS 词法器要处理的三个坑（全部实测踩过，别再简化）

1. **正则字面量**：`.replace(/"/g, '""')` 里的双引号会让「只认字符串」的剥离器
   误判成字符串起点，从此整段失步 —— 实测漏掉了 shelter 的 `showPetArchive`。
2. **模板字符串的 `${}` 嵌套**：`` ${dist ? `<p …>` : ''} `` —— 内层反引号会被
   误当成外层结束，状态整体错位一个反引号。**必须用上下文栈**：模板字符串里
   遇 `${` 进入「表达式」上下文，`}` 深度归零才回到模板。
3. **`</button>` 被误判成正则**：`<` 曾被放进 `REGEX_PRECEDERS`，于是代码里的
   `</xxx>` 让 `/` 被当成正则起点，**吞掉后面一大段代码**。
   → 标准启发式**不含 `< > + - * % ~ ^`**，只留 `( , = : [ ! & | ? { } ;`。
   → 再加**前瞻校验**：闭 `/` 之后若紧跟字母，必须落在 `REGEX_FLAGS` 里。

## 另外两个「看起来对」的陷阱（`audit` 就是为它们写的）

* **`${` 抹了、闭 `}` 没抹** → 大括号**多出一个闭括号** → `match_brace` 提前收尾
  → 方法体被**静默截断**（实测把 `getHospitalPets` 判成「非静默」）。
* 只查「`try {` 偏移一致」**抓不到**上面这条 —— 那时所有 `try {` 位置都是对的。
  所以 `audit` 必须**同时**查长度、`try {` 偏移、**大括号 / 圆括号配平**。

⚠ 原则：「宁可漏剥一个正则，也不能吞掉代码」—— 吞代码 = 扫描漏报 = 静默失败。
"""
import re

# 能出现在正则字面量之前的字符（标准启发式，**故意不含 < > 与算术运算符**）
REGEX_PRECEDERS = set('(,=:[!&|?{};')

# 合法正则标志。闭 `/` 之后若紧跟字母，必须落在这一集合里，否则判否。
REGEX_FLAGS = set('dgimsuvy')

MAX_REGEX_LEN = 200

SCRIPT_RE = re.compile(
    r'<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>',
    re.IGNORECASE | re.DOTALL,
)


def _regex_end(src, i, n):
    """`i` 指向候选的起始 `/`。是正则则返回结束 `/` 之后的位置，否则 None。

    判否条件（任一命中即判否）：
      * 跨行 / 超长 / 到 EOF 都没找到闭 `/`；
      * 闭 `/` 之后紧跟一个**不是合法正则标志**的字母
        （`</button>` 那种误判，闭 `/` 后跟的是 `b` 等普通字母）。

    ⚠ **不能**用「体里出现引号就判否」—— `/"/g` 是合法正则，实测这么写会把它
    误杀，然后整段 `.replace(/"/g, '""')` 失步（shelter 第 3462 行）。
    """
    j = i + 1
    in_class = False
    while j < n and j - i <= MAX_REGEX_LEN:
        d = src[j]
        if d == '\\':
            j += 2
            continue
        if d == '\n':
            return None
        if d == '[':
            in_class = True
        elif d == ']':
            in_class = False
        elif d == '/' and not in_class:
            k = j + 1
            if k < n and src[k].isalpha() and src[k] not in REGEX_FLAGS:
                return None          # 闭 `/` 后跟非法标志字母 → 不是正则
            return k
        j += 1
    return None


def lex(src):
    """核心词法器。返回 `(stripped, kinds)`。

    `stripped` 与 `src` **等长**，被抹掉的位置是空白；
    `kinds` 是等长列表，逐字符标出类别，取值：
    `'code'` / `'string'` / `'template'` / `'expr'` / `'regex'` / `'comment'`。
    """
    out = []
    kinds = []
    i, n = 0, len(src)
    # 上下文栈。栈底永远是普通代码。
    #   ('code', None)      普通代码
    #   ('string', q)       字符串字面量（' 或 "）
    #   ('template', None)  模板字符串的**字面量**部分
    #   ('expr', depth)     模板字符串 `${}` 里的表达式，depth 为 {} 嵌套深度
    stack = [('code', None)]
    prev = None          # 上一个**有效**字符（仅在 code/expr 里更新）

    def emit(text, kind):
        out.append(text)
        kinds.extend([kind] * len(text))

    while i < n:
        c = src[i]
        kind, arg = stack[-1]

        # ---------- 字符串字面量 ----------
        if kind == 'string':
            if c == '\\' and i + 1 < n:
                emit('\n' if c == '\n' else ' ', 'string')
                emit('\n' if src[i + 1] == '\n' else ' ', 'string')
                i += 2
                continue
            emit('\n' if c == '\n' else ' ', 'string')
            if c == arg:
                stack.pop()
                prev = arg
            i += 1
            continue

        # ---------- 模板字符串的字面量部分 ----------
        if kind == 'template':
            if c == '\\' and i + 1 < n:
                emit('\n' if c == '\n' else ' ', 'template')
                emit('\n' if src[i + 1] == '\n' else ' ', 'template')
                i += 2
                continue
            if c == '$' and i + 1 < n and src[i + 1] == '{':
                emit('  ', 'template')
                i += 2
                stack.append(('expr', 0))
                prev = '{'
                continue
            emit('\n' if c == '\n' else ' ', 'template')
            if c == '`':
                stack.pop()
                prev = '`'
            i += 1
            continue

        # ---------- 代码 / 表达式 ----------
        if c == '/' and i + 1 < n and src[i + 1] == '/':
            while i < n and src[i] != '\n':
                emit(' ', 'comment')
                i += 1
            continue
        if c == '/' and i + 1 < n and src[i + 1] == '*':
            while i < n and not (src[i] == '*' and i + 1 < n and src[i + 1] == '/'):
                emit('\n' if src[i] == '\n' else ' ', 'comment')
                i += 1
            emit('  ', 'comment')
            i += 2
            continue
        if c in ('"', "'"):
            stack.append(('string', c))
            emit(' ', 'code')
            prev = c
            i += 1
            continue
        if c == '`':
            stack.append(('template', None))
            emit(' ', 'code')
            prev = c
            i += 1
            continue
        if c == '/' and (prev is None or prev in REGEX_PRECEDERS):
            end = _regex_end(src, i, n)
            if end is not None:
                emit(' ' * (end - i), 'regex')
                i = end
                prev = '/'
                continue
            # 前瞻判否 → 当普通字符处理，落到下面
        if kind == 'expr':
            if c == '{':
                stack[-1] = ('expr', arg + 1)
            elif c == '}':
                if arg == 0:
                    # ⚠ 必须**抹成空白**，不能原样输出 `}`。进入 `${` 时已经把
                    # `${` 抹成两个空格；若这里输出 `}`，大括号计数就**多出一个
                    # 闭括号** → `match_brace` 提前收尾 → 方法体被静默截断。
                    emit(' ', 'template')
                    stack.pop()          # 回到模板字符串
                    prev = '}'
                    i += 1
                    continue
                stack[-1] = ('expr', arg - 1)

        emit(c, 'code')
        if not c.isspace():
            prev = c
        i += 1

    return ''.join(out), kinds


def strip_js(src):
    """把 JS 源码里的字符串 / 模板 / 正则 / 注释全部替换成等长空白。"""
    return lex(src)[0]


def strip_html_js(src):
    """只剥**内联** `<script>` 的 JS，其余部分原样保留（长度不变）。"""
    return strip_html_js_kinds(src)[0]


def strip_html_js_kinds(src):
    """同 `strip_html_js`，但同时返回逐字符类别。"""
    out = list(src)
    kinds = ['code'] * len(src)
    for m in SCRIPT_RE.finditer(src):
        if re.search(r'\bsrc\s*=', m.group('attrs'), re.IGNORECASE):
            continue                       # 外链脚本，没有可剥的源码
        a, b = m.start('body'), m.end('body')
        s, k = lex(src[a:b])
        out[a:b] = s
        kinds[a:b] = k
    return ''.join(out), kinds


def stripped_kinds(src):
    """按「是 HTML 就只剥 script，否则整体当 JS」自动选择。"""
    return strip_html_js_kinds(src) if '<script' in src.lower() else lex(src)


def match_brace(stripped, open_idx):
    """从 `open_idx`（指向 `{`）按大括号配对找到匹配的 `}` 偏移，找不到返回 -1。"""
    depth = 0
    for k in range(open_idx, len(stripped)):
        if stripped[k] == '{':
            depth += 1
        elif stripped[k] == '}':
            depth -= 1
            if depth == 0:
                return k
    return -1


def brace_balance(stripped):
    """大括号 / 圆括号必须配平。返回 (是否配平, 说明)。"""
    for op, cl, tag in (('{', '}', '大括号'), ('(', ')', '圆括号')):
        depth = 0
        for k, c in enumerate(stripped):
            if c == op:
                depth += 1
            elif c == cl:
                depth -= 1
                if depth < 0:
                    return False, f'{tag}第 {stripped.count(chr(10), 0, k) + 1} 行多出闭括号'
        if depth != 0:
            return False, f'{tag}有 {depth} 个未闭合'
    return True, 'OK'


def audit(src, stripped=None):
    """自检。**三条不变量**，缺一不可：

    1. **长度不变** —— 否则偏移不能复用；
    2. **每个（非注释里的）`try {` 在剥离后仍在同一偏移** —— 检测「整段被吞」；
    3. **大括号 / 圆括号配平** —— 检测「`${` 抹了闭 `}` 没抹」这类
       位置全对但结构被静默截断的情况。

    ⚠ 第 2 条必须**跳过注释里的 `try {`**：`tnr-common.js` 第 33 行的注释
    正好在讲「这种写法里的 catch 是死代码」，里面就写着一个 `try {`。
    不跳过就会**被自己的说明文字打红**（假阳性）。

    返回 (是否通过, 说明)。
    """
    if stripped is None:
        stripped = stripped_kinds(src)[0]
    if len(src) != len(stripped):
        return False, f'长度变了：{len(src)} -> {len(stripped)}'
    _, kinds = stripped_kinds(src)
    for m in re.finditer(r'\btry\s*\{', src):
        o = m.start()
        if kinds[o] == 'comment':
            continue
        if not re.match(r'try\s*\{', stripped[o:o + 8]):
            line = src.count('\n', 0, o) + 1
            return False, f'失步：偏移 {o}（第 {line} 行）剥离后 = {stripped[o:o + 12]!r}'
    return brace_balance(stripped)
