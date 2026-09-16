"""前后端契约一致性回归测试。

这些用例不启动浏览器，而是把「模板/JS 源码」当作被测对象做静态断言。
它们锁定的是第五轮走查发现的一类缺陷——**前端自己维护的枚举映射表与后端
模型 choices 脱节**，表现为界面上直接漏出 `pending_claim`、`receive` 这样的
英文枚举码，或状态缺失时被静默兜底成某个具体业务状态（「状态全是已完成」）。

新增后端枚举值 / 新增接口时，这些测试会失败并指向需要同步的前端文件。
"""
import os
import re

from django.test import SimpleTestCase

from business.models import Adoption, Capture, CheckIn, MaterialTransaction, Pet, Treatment, Transfer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PORTALS = {
    'shelter': 'templates/portal/shelter/portal.html',
    'hospital': 'templates/portal/hospital/portal.html',
    'gov': 'templates/portal/gov/portal.html',
    'adopter': 'templates/portal/adopter/portal.html',
}

FRONTEND_FILES = list(PORTALS.values()) + [
    'static/js/tnr-api.js',
    'static/js/tnr-common.js',
]


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def find_object_literal(source, var_name, occurrence=0):
    """返回 `const/let/var <var_name> = { ... }` 里 `{...}` 的完整文本。

    :param occurrence: 同名变量第几次出现（模板里 typeMap 会被多次声明）
    """
    pattern = re.compile(
        r'(?:const|let|var)\s+' + re.escape(var_name) + r'\s*=\s*\{')
    matches = list(pattern.finditer(source))
    if len(matches) <= occurrence:
        return None
    start = matches[occurrence].end() - 1
    depth, i, n = 0, start, len(source)
    while i < n:
        c = source[i]
        if c in '\'"`':
            q = c
            i += 1
            while i < n and source[i] != q:
                if source[i] == '\\':
                    i += 1
                i += 1
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    return None


def object_keys(body):
    """抽取对象字面量的**顶层**键名（兼容 `'key':` 与 `key:` 两种写法）。

    只取深度为 1 的键，避免把嵌套对象的键（如 `{text:…, badge:…}`）也算进来。
    """
    keys = set()
    depth = 0
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c in '\'"`':
            q = c
            j = i + 1
            while j < n and body[j] != q:
                if body[j] == '\\':
                    j += 1
                j += 1
            k = j + 1
            while k < n and body[k] in ' \t\n\r':
                k += 1
            if depth == 1 and k < n and body[k] == ':':
                keys.add(body[i + 1:j])
            i = j + 1
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif depth == 1 and (c.isalpha() or c == '_'):
            j = i
            while j < n and (body[j].isalnum() or body[j] == '_'):
                j += 1
            k = j
            while k < n and body[k] in ' \t\n\r':
                k += 1
            if k < n and body[k] == ':':
                keys.add(body[i:j])
            i = j
            continue
        i += 1
    return keys


class MaterialTypeMapCoverageTest(SimpleTestCase):
    """物料流水类型映射表必须覆盖 MaterialTransaction 的全部 choices。

    `receive`（医院签收下发物料时生成）曾被四张映射表同时漏掉，
    导致捕捉点/医院/监管三端的物料台账里「签收入库」显示成英文 `receive`，
    捕捉点端的类型筛选下拉里也选不到这一类流水。
    """

    def _material_type_maps(self, rel):
        """找出文件里所有「物料类型映射表」及其键集合。"""
        source = read(rel)
        found = []
        for var in ('typeMap', 'txnTypeMap'):
            idx = 0
            while True:
                body = find_object_literal(source, var, idx)
                if body is None:
                    break
                keys = object_keys(body)
                # 只认物料类型映射：必须含 purchase/dispatch 这类物料流水取值
                if {'purchase', 'dispatch'} <= keys:
                    found.append((var, idx, keys))
                idx += 1
        return found

    def test_every_portal_map_covers_all_material_types(self):
        expected = {c[0] for c in MaterialTransaction._meta.get_field('type').choices}
        self.assertTrue(expected, '后端未定义物料流水类型 choices')

        checked = 0
        for name, rel in PORTALS.items():
            for var, idx, keys in self._material_type_maps(rel):
                checked += 1
                missing = expected - keys
                self.assertFalse(
                    missing,
                    f'{rel} 的 {var}[{idx}] 缺少物料流水类型映射 {sorted(missing)}；'
                    f'这些流水在界面上会直接显示英文枚举码。'
                    f'（后端全部取值：{sorted(expected)}）'
                )
        self.assertGreater(checked, 0, '没有找到任何物料类型映射表，检查测试本身是否失效')

    def test_shelter_material_type_filter_covers_all_types(self):
        """捕捉点端「物资流向」的类型筛选下拉必须能选到全部流水类型。"""
        source = read(PORTALS['shelter'])
        anchor = re.search(r"key:\s*'type',\s*label:\s*'类型'", source)
        self.assertIsNotNone(anchor, '未找到捕捉点端的物料类型筛选定义')
        # 用「起点 + 结束标记」取块，避免依赖脆弱的括号转义
        end = source.find('];', anchor.start())
        self.assertGreater(end, anchor.start(), '物料类型筛选定义没有正常的结束标记')
        block = source[anchor.start():end]
        expected = {c[0] for c in MaterialTransaction._meta.get_field('type').choices}
        missing = {v for v in expected if f"'{v}'" not in block}
        self.assertFalse(
            missing,
            f'捕捉点端物料类型筛选下拉缺少 {sorted(missing)}，用户无法按这些类型过滤流水'
        )


class StatusEnumMappingTest(SimpleTestCase):
    """各端状态徽章映射表必须覆盖对应实体的全部状态取值。"""

    def _assert_covers(self, rel, var, expected, occurrence=0, extra_vars=()):
        body = find_object_literal(read(rel), var, occurrence)
        self.assertIsNotNone(body, f'{rel} 中未找到 {var}[{occurrence}]')
        keys = object_keys(body)
        for v in extra_vars:
            keys |= object_keys(find_object_literal(read(rel), v) or '{}')
        missing = set(expected) - keys
        self.assertFalse(
            missing,
            f'{rel} 的 {var}[{occurrence}] 缺少状态映射 {sorted(missing)}，'
            f'界面上会显示英文枚举码。后端取值：{sorted(expected)}'
        )

    def test_shelter_status_badge_covers_adoption_and_treatment(self):
        """捕捉点端通用状态徽章：领养「待领出/已撤销」、诊疗「进行中」。"""
        adoption = {c[0] for c in Adoption._meta.get_field('status').choices}
        treatment = {c[0] for c in Treatment._meta.get_field('status').choices}
        transfer = {c[0] for c in Transfer._meta.get_field('status').choices}
        self._assert_covers(
            PORTALS['shelter'], 'map', adoption | treatment | transfer, occurrence=0)

    def test_shelter_capture_status_badge_covers_capture(self):
        expected = {c[0] for c in Capture._meta.get_field('status').choices}
        self._assert_covers(PORTALS['shelter'], 'map', expected, occurrence=1)

    def test_shelter_pet_status_badge_covers_pet(self):
        """捕捉点端放养/回收页用 TNR_API 的宠物状态映射。"""
        expected = {c[0] for c in Pet._meta.get_field('status').choices}
        source = read('static/js/tnr-api.js')
        self._assert_covers('static/js/tnr-api.js', 'map', expected, occurrence=0)
        # 徽章样式映射同样要齐全
        badges = re.findall(r"const map = \{(.*?)\};", source, re.S)
        self.assertGreaterEqual(len(badges), 2, 'tnr-api.js 中未找到状态映射表')
        for idx, body in enumerate(badges[:2]):
            keys = set(re.findall(r"'([a-z_]+)':", body))
            missing = expected - keys
            self.assertFalse(missing, f'tnr-api.js 第 {idx + 1} 张状态映射表缺少 {sorted(missing)}')

    def test_gov_pet_status_map_covers_pet(self):
        expected = {c[0] for c in Pet._meta.get_field('status').choices}
        self._assert_covers(PORTALS['gov'], 'petStatus', expected)

    def test_hospital_status_maps_cover_transfer_and_treatment(self):
        transfer = {c[0] for c in Transfer._meta.get_field('status').choices}
        treatment = {c[0] for c in Treatment._meta.get_field('status').choices}
        self._assert_covers(PORTALS['hospital'], 'm', transfer, occurrence=0)
        self._assert_covers(PORTALS['hospital'], 'm', treatment, occurrence=1)


class NoHardcodedStatusFallbackTest(SimpleTestCase):
    """模板中不得把缺失状态静默兜底成某个具体业务状态。

    `r.status || 'completed'` 会让状态为空的记录在界面上显示成「已完成」——
    这正是「状态不能全是已完成」这类问题的根因。允许的兜底只有占位符
    （`'—'` / `'未知'`），不能是真实的业务状态码。
    """

    # 允许的兜底值：占位符，不是任何业务状态
    ALLOWED = {'—', '-', '未知', '--'}

    def test_no_business_status_as_fallback(self):
        pattern = re.compile(r"""\bstatus\s*\|\|\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]""")
        offenders = []
        for rel in FRONTEND_FILES:
            for m in pattern.finditer(read(rel)):
                if m.group(1) not in self.ALLOWED:
                    line = read(rel)[:m.start()].count('\n') + 1
                    offenders.append(f'{rel}:{line} → {m.group(0)}')
        self.assertFalse(
            offenders,
            '发现把缺失状态兜底成具体业务状态的写法，会让空状态被误显示为某个真实状态：\n  '
            + '\n  '.join(offenders)
        )


class ApiRouteCoverageTest(SimpleTestCase):
    """后端每个 /api/ 路由都必须至少有一处前端调用点。

    反过来（前端调用了不存在的路由）由本测试同时覆盖：先把前端源码里的
    「字符串拼接缝隙」和「模板插值」统一替换成标记符，再与归一化后的路由匹配，
    因此 `'/api/x/' + id + '/y/'` 与 `` `/api/x/${id}/y/` `` 都能正确识别。
    """

    MARK = '\x00'
    GAP = re.compile(r"""(['"`])\s*\+\s*[^+'"`]*?\s*\+\s*\1""")
    INTERP = re.compile(r'\$\{[^}]*\}')

    def _normalized_sources(self):
        out = {}
        for rel in FRONTEND_FILES:
            text = self.INTERP.sub(self.MARK, read(rel))
            text = self.GAP.sub(self.MARK, text)
            out[rel] = text
        return out

    def _route_regex(self, route):
        parts = re.split(r'\{X\}|<[^>]+>', route)
        return re.compile(self.MARK.join(re.escape(p) for p in parts))

    def _backend_routes(self):
        from django.urls import get_resolver

        def walk(resolver, prefix=''):
            for p in resolver.url_patterns:
                if hasattr(p, 'url_patterns'):
                    yield from walk(p, prefix + str(p.pattern))
                else:
                    yield prefix + str(p.pattern)

        routes = set()
        for pat in walk(get_resolver()):
            if not pat.startswith('api/'):
                continue
            norm = re.sub(r'<[^>]+>', '{X}', pat)
            if not norm.endswith('/'):
                norm += '/'
            routes.add('/' + norm)
        return routes

    def test_every_backend_route_is_reachable_from_frontend(self):
        sources = self._normalized_sources()
        missing = []
        for route in sorted(self._backend_routes()):
            rx = self._route_regex(route)
            if any(rx.search(t) for t in sources.values()):
                continue
            # 兼容 TNR_API.KEYS 的相对路径写法（BASE + 'portal/messages/'）
            rel = route
            for pfx in ('/api/business/', '/api/supervision/'):
                if rel.startswith(pfx):
                    rel = rel[len(pfx) - 1:]
                    break
            variants = {rel, rel.lstrip('/')}
            if any(self._route_regex(v).search(t)
                   for v in variants for t in sources.values()):
                continue
            missing.append(route)
        self.assertFalse(
            missing,
            '以下后端接口没有任何前端调用点（功能不可达或前端漏接）：\n  '
            + '\n  '.join(missing)
        )

    def test_no_frontend_call_to_unknown_route(self):
        """前端不得调用后端未注册的接口（会 404，表现为按钮「不生效」）。"""
        sources = self._normalized_sources()
        route_res = [self._route_regex(r) for r in self._backend_routes()]

        called = set()
        for text in sources.values():
            for m in re.finditer(r'/api/[A-Za-z0-9_/{}\-.]*', text):
                seg = m.group(0)
                if seg.count(self.MARK) or seg.endswith('/'):
                    called.add(seg)
        # 前缀本身（'/api/business/'）是 BASE 常量，不是调用目标
        called = {c for c in called if c.rstrip('/') not in
                  ('/api/business', '/api/supervision')}
        unknown = sorted(
            c for c in called if not any(rx.fullmatch(c) for rx in route_res))
        self.assertFalse(
            unknown,
            '前端调用了后端未注册的接口，点击后会 404：\n  ' + '\n  '.join(unknown)
        )


class CaptureDistrictDefaultTest(SimpleTestCase):
    """「新增捕捉登记」的归属区县默认值。

    这里锁的是一个已经造成线上数据错误的写法：表单原先把区县默认成
    `user.district_id`，而两个捕捉点操作员都被挂在「全市（市级）」下，
    于是登记出来的捕捉单全部落到市级，本区县政府在区县隔离下看不到它们
    （现场 7 条）。默认值必须来自「捕捉点所在区县」，且市级不能作为选项。
    """

    def _capture_add_district_select(self):
        source = read(PORTALS['shelter'])
        # 定位「新增捕捉登记」里的区县下拉，用 id 锚点避免匹配到编辑弹窗
        anchor = source.find('id="ca_districtId"')
        self.assertGreater(anchor, 0, '未找到新增捕捉登记表单的区县下拉')
        # 往前找到所在 <select> 的起始，往后找到该 select 的结束
        start = source.rfind('<select', 0, anchor)
        end = source.find('</select>', anchor)
        self.assertGreater(end, anchor, '区县下拉没有正常的结束标签')
        return source[start:end]

    def test_options_exclude_city_level_districts(self):
        block = self._capture_add_district_select()
        self.assertIn('realDistricts', block,
                      '区县下拉必须过滤掉「全市（市级）」——捕捉单不能归属到市级')

    def test_default_is_not_operator_district(self):
        block = self._capture_add_district_select()
        self.assertNotIn(
            'user?.district_id', block,
            '默认区县不能取操作员所属区县：操作员可能被挂在「全市（市级）」下，'
            '会把捕捉单错误地落到市级。应取捕捉点（机构）所在区县。'
        )
        self.assertIn('defaultDistrictId', block, '区县下拉应使用 defaultDistrictId 作为默认选中值')


class CheckInStatusMappingTest(SimpleTestCase):
    """回访打卡状态在捕捉点端是内联三元表达式，单独断言其覆盖全部取值。

    `pending` 走的是最后的 else 分支，因此这里断言的是「三个取值各自都有
    对应中文文案」，而不是断言每个枚举码都字面出现。
    """

    def test_shelter_checkin_status_covers_all(self):
        source = read(PORTALS['shelter'])
        anchor = re.search(r"r\.status === 'approved'", source)
        self.assertIsNotNone(anchor, '未找到捕捉点端的打卡状态渲染')
        end = source.find('\n', anchor.start())
        line = source[anchor.start():end]
        for token, label in (("'approved'", '已通过'),
                             ("'rejected'", '未通过'),
                             ('待审核', '待审核')):
            self.assertIn(token, line, f'打卡状态渲染未处理 {token}')
        # 三个分支各自的中文文案必须齐备，且不能漏出英文枚举码
        for label in ('已通过', '未通过', '待审核'):
            self.assertIn(label, line, f'打卡状态渲染缺少「{label}」文案')
        for code in (c[0] for c in CheckIn._meta.get_field('status').choices):
            self.assertNotIn(
                f'>{code}<', line,
                f'打卡状态渲染把枚举码 {code} 直接当成文案输出了'
            )


# ---------------------------------------------------------------------------
# 渲染时序 / 作用域
#
# 下面两个用例锁的是**只有真实浏览器渲染才会暴露**的一类缺陷：模板语法完全合法、
# 静态检查也看不出问题，但页面一渲染就抛错或静默空白。GUI 实测（Playwright）
# 在捕捉点端「动物回收放养 → 待回收」页签上抓到两处：
#
#   1. `TNR_UI.mountTable('[data-mt="shelter.release.recovery"]', ...)` 写在
#      `wrap.innerHTML = html` **之前** —— 选择器查不到元素，mountTable 直接
#      return，表格永远不出现，而且**不报任何错**，只是页面空白。
#   2. 同一分支用了简写选项 `{ ..., rowActions }`，但 `rowActions` 只声明在
#      同函数的另一个分支里 —— 渲染时抛 `ReferenceError: rowActions is not defined`。
#
# 这类缺陷 API 测试与模板静态断言都覆盖不到，只能靠「跑一遍渲染」或下面这种
# 源码层面的时序/作用域断言。
# ---------------------------------------------------------------------------

MOUNT_CALL = re.compile(r'TNR_UI\.mountTable\s*\(\s*([\'"])(.+?)\1\s*,\s*')
METHOD_START = re.compile(r'(?m)^  (?:async\s+)?[A-Za-z_$][\w$]*\s*\([^)]*\)\s*\{')


def brace_body(source, open_idx):
    """从 `{` 的位置返回配对完整的 `{...}` 文本（跳过字符串内的花括号）。"""
    depth, i, n = 0, open_idx, len(source)
    while i < n:
        c = source[i]
        if c in '\'"`':
            q = c
            i += 1
            while i < n and source[i] != q:
                if source[i] == '\\':
                    i += 1
                i += 1
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return source[open_idx:i + 1]
        i += 1
    return None


def shorthand_identifiers(body):
    """返回对象字面量里**简写**的顶层标识符（`{ ..., rowActions }` → `rowActions`）。

    三个条件缺一不可：
      1. 位于顶层（深度 1）；
      2. 前一个非空白字符是 `{` 或 `,` —— 说明它是某个属性的**开头**。
         这条把两类噪音挡在外面：`title: title` 里的**值** `title`
         （前面是 `:`）、`this.state.supTab` 里的 `this`/`state`
         （前面是 `+` / `.`）；
      3. 后一个非空白字符是 `,` 或 `}` —— 说明它**就是整个属性**，没有跟冒号。
    """
    out = set()
    depth = 0
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c in '\'"`':
            q = c
            j = i + 1
            while j < n and body[j] != q:
                if body[j] == '\\':
                    j += 1
                j += 1
            i = j + 1
            continue
        if c in '([{':
            depth += 1
            i += 1
            continue
        if c in ')]}':
            depth -= 1
            i += 1
            continue
        if depth == 1 and (c.isalpha() or c in '_$'):
            # 前一个非空白字符
            p = i - 1
            while p >= 0 and body[p] in ' \t\r\n':
                p -= 1
            prev = body[p] if p >= 0 else ''
            j = i
            while j < n and (body[j].isalnum() or body[j] in '_$'):
                j += 1
            # 后一个非空白字符
            k = j
            while k < n and body[k] in ' \t\r\n':
                k += 1
            nxt = body[k] if k < n else ''
            if prev in '{,' and nxt in ',}':
                out.add(body[i:j])
            i = j
            continue
        i += 1
    return out


def split_top_commas(text):
    """按顶层逗号切分（忽略括号/引号内的逗号）。"""
    parts, buf, depth = [], [], 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in '\'"`':
            q = c
            buf.append(c)
            i += 1
            while i < n and text[i] != q:
                buf.append(text[i])
                if text[i] == '\\':
                    i += 1
                    if i < n:
                        buf.append(text[i])
                i += 1
            if i < n:
                buf.append(text[i])
            i += 1
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == ',' and depth == 0:
            parts.append(''.join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append(''.join(buf))
    return parts


DECL_KEYWORD = re.compile(r'\b(?:const|let|var)\s+')


def declares_name(text, ident):
    """`text` 里是否存在「声明 ident」的语句。

    两个必须处理的写法（否则会误判成「未声明」）：
      - 多重声明：`let data = [], columns = [], title = '';`
      - 嵌套箭头函数：`const renderTable = () => { ... const columns = [...] ... }`
        —— 外层箭头函数体在窗口内可能尚未闭合，因此**不能**用「扫到语句末尾再
        从末尾继续找」的跳进写法，否则会把函数体内后续的声明全吞掉。
        这里改为对每个 `const/let/var` 关键字独立取一段窗口。
    """
    n = len(text)
    for m in DECL_KEYWORD.finditer(text):
        # 取到该声明语句的结束（顶层分号或换行）；限制窗口长度防止无限前进
        # 注意必须跳过字符串：`el.includes('(')` 这类写法里的括号会把深度计数带偏。
        j, depth, limit = m.end(), 0, min(n, m.end() + 4000)
        while j < limit:
            c = text[j]
            if c in '\'"`':
                q = c
                j += 1
                while j < limit and text[j] != q:
                    if text[j] == '\\':
                        j += 1
                    j += 1
            elif c in '([{':
                depth += 1
            elif c in ')]}':
                depth -= 1
            elif c == ';' and depth == 0:
                break
            elif c == '\n' and depth == 0:
                break
            j += 1
        for part in split_top_commas(text[m.end():j]):
            part = part.strip()
            if not part:
                continue
            name = re.match(r'([A-Za-z_$][\w$]*)', part)
            if name:
                if name.group(1) == ident:
                    return True
            else:
                # 解构：const { a, b } = obj
                head = part.split('=')[0]
                if re.search(r'(?<![\w$.])' + re.escape(ident) + r'\b', head):
                    return True
    return False


def strip_js_comments(source):
    """把 JS 行注释与块注释替换成等长空白（保留换行），返回**等长**文本。

    两个要点：
      - 必须从文件开头扫描：若从中间片段开始，引号配对会失步，注释就剥不掉。
      - 必须保持长度不变：这样剥离后的文本与原文本**索引一一对应**，
        行号与偏移量都不受影响，调用方可以放心复用。

    必要性：注释里出现的 `wrap.innerHTML = html` 字样会让「渲染时序检查」
    误判为通过 —— 这是真实踩过的坑（修复说明的注释本身把测试骗过了）。
    """
    chars = list(source)
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c in '\'"`':
            q = c
            i += 1
            while i < n and source[i] != q:
                if source[i] == '\\':
                    i += 1
                i += 1
            i += 1
            continue
        if c == '/' and i + 1 < n and source[i + 1] == '/':
            while i < n and source[i] != '\n':
                chars[i] = ' '
                i += 1
            continue
        if c == '/' and i + 1 < n and source[i + 1] == '*':
            j = i + 2
            while j + 1 < n and not (source[j] == '*' and source[j + 1] == '/'):
                j += 1
            j = min(j + 2, n)
            for k in range(i, j):
                if chars[k] != '\n':
                    chars[k] = ' '
            i = j
            continue
        i += 1
    return ''.join(chars)


class RenderContainerOrderingTest(SimpleTestCase):
    """`mountTable` 的目标容器必须先写进 DOM，否则表格静默不渲染。"""

    def test_mount_target_container_inserted_before_mount(self):
        problems = []
        for name, rel in PORTALS.items():
            source = strip_js_comments(read(rel))   # 等长替换，索引与行号不变
            for m in MOUNT_CALL.finditer(source):
                marker_hit = re.search(r'data-mt="([^"]+)"', m.group(2))
                if not marker_hit:
                    continue          # 目标不是 [data-mt=...] 选择器，无需容器
                marker = f'data-mt="{marker_hit.group(1)}"'
                idx = source.rfind(marker, 0, m.start())
                if idx < 0:
                    problems.append(f'{name}: {marker} 的容器从未出现在源码里')
                    continue
                if not re.search(r'\.innerHTML\s*=', source[idx:m.start()]):
                    line_no = source.count('\n', 0, m.start()) + 1
                    problems.append(
                        f'{name}:{line_no} {marker} 在容器插入 DOM 之前就被 mountTable 调用'
                        '（选择器查不到元素 → 表格静默不渲染）')
        self.assertFalse(
            problems,
            'mountTable 时序错误：\n  ' + '\n  '.join(problems)
        )


class UndeclaredOptionIdentifierTest(SimpleTestCase):
    """`mountTable` 选项里的简写标识符必须在同一方法作用域内声明。

    跨分支误用是最典型的形态：`rowActions` 声明在 `released` 分支，
    却在 `recovery` 分支被引用 —— 同文件内有声明，但不在同一作用域。
    因此这里按「方法边界」切分作用域，而不是全文件搜索。
    """

    ALLOWED = {'true', 'false', 'null', 'undefined'}

    def test_shorthand_options_declared_within_method_scope(self):
        problems = []
        for name, rel in PORTALS.items():
            source = strip_js_comments(read(rel))   # 等长替换，索引与行号不变
            for m in MOUNT_CALL.finditer(source):
                rest = source[m.end():m.end() + 300]
                head = re.match(r'\s*\{', rest)
                if not head:
                    continue          # 目标不是对象字面量（如 mountTable(el, opts)）
                brace = m.end() + head.end() - 1
                body = brace_body(source, brace)
                if not body:
                    continue
                line_no = source.count('\n', 0, m.start()) + 1
                # 调用点所属的方法（最近的 2 空格缩进方法声明）
                scope_start = 0
                for ms in METHOD_START.finditer(source, 0, m.start()):
                    scope_start = ms.start()
                for ident in shorthand_identifiers(body):
                    if ident in self.ALLOWED:
                        continue
                    if not declares_name(source[scope_start:m.start()], ident):
                        problems.append(
                            f'{name}:{line_no} mountTable 选项用了未声明的 `{ident}`'
                            '（渲染时会抛 ReferenceError）')
        self.assertFalse(
            problems,
            '未声明的 mountTable 选项：\n  ' + '\n  '.join(problems)
        )


class ModalMethodOuterScopeLeakTest(SimpleTestCase):
    """弹窗/详情类方法体内不得引用「外层渲染函数的局部变量」。

    第八轮 GUI 实测抓到两例，形态完全相同——把列表页的局部变量直接搬进
    独立方法里用：

      - `showCaptureEdit` 里用 `pets`（那是 `render_capture_list` 的局部变量）
        → 弹窗渲染时 `ReferenceError: pets is not defined`，编辑窗根本打不开；
      - 同一个方法里用 `wrap.querySelectorAll('[data-pet-photo]')`
        → 保存时 `ReferenceError`，单只照片永远提交不上去。

    要点：这两个名字在**文件别处都有声明**，所以「全文件搜索」查不出来，
    必须按**方法边界**切分作用域。API 测试也覆盖不到（异常发生在浏览器里）。

    只检查 `showXxx` 方法，且只认「选择器/数组方法」这两种用法
    （`ident.querySelector*`、`ident.forEach|map|filter|find|length`），
    这样 `'form-wrap'`、`flex-wrap:wrap` 这类字符串/CSS 不会误报。
    """

    # 列表页渲染函数的典型局部变量：弹窗方法里要用就必须自己声明
    OUTER_LOCALS = (
        'wrap', 'pets', 'all', 'list', 'records', 'releases', 'transfers',
        'captures', 'districts', 'treatments', 'hospitals', 'communities',
    )

    MODAL_METHOD = re.compile(r'(?:async\s+)?(show[A-Z]\w*)\s*\(([^)]*)\)\s*\{')
    DANGEROUS_USE = (
        r'(?<![\w$.])({ident})\s*\.\s*'
        r'(?:querySelector(?:All)?|forEach|map|filter|find|length)\b'
    )

    def test_modal_methods_do_not_leak_outer_locals(self):
        problems = []
        for name, rel in PORTALS.items():
            source = strip_js_comments(read(rel))   # 等长替换，索引与行号不变
            # 用**任意方法**作边界切分：只按 showXxx 切会让方法体越界，
            # 把后续方法的内容吞进来，产生误报。
            methods = list(METHOD_START.finditer(source))
            bounds = [m.start() for m in methods] + [len(source)]
            for i, m in enumerate(methods):
                header = source[m.start():m.end()]
                hit = self.MODAL_METHOD.search(header)
                if not hit:
                    continue
                body = source[m.end() - 1:bounds[i + 1]]
                params = hit.group(2)
                line_no = source.count('\n', 0, m.start()) + 1
                for ident in self.OUTER_LOCALS:
                    pattern = re.compile(self.DANGEROUS_USE.format(ident=re.escape(ident)))
                    if not pattern.search(body):
                        continue
                    # 形参同名也算「已声明」（如 showXxx(rec, releases)）
                    if re.search(r'(?<![\w$.])' + re.escape(ident) + r'\b', params):
                        continue
                    if declares_name(body, ident):
                        continue
                    problems.append(
                        f'{name}:{line_no} {hit.group(1)}() 用了外层变量 `{ident}`'
                        '（本方法作用域内未声明 → 运行时 ReferenceError）')
        self.assertFalse(
            sorted(set(problems)),
            '弹窗方法泄漏外层作用域变量：\n  ' + '\n  '.join(sorted(set(problems)))
        )

