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
from business.services import MANAGEABLE_ROLES

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


def strip_html_comments(source):
    """把 HTML 注释 `<!-- ... -->` 替换成等长空白（保留换行）。

    与 `strip_js_comments` 同因同果，只是**同一个坑换了个马甲**：
    第二十二轮我在 `shelter_base.html` 补「黑名单管理」入口时，顺手在它上方
    写了说明注释，注释里引用了 ``Shelter.navigate('blacklist')`` 这个词组。
    `strip_js_comments` 只管 JS 注释，HTML 注释原样留着 ——
    于是「页面可达性」检查把**我自己的说明注释**当成了真实的导航入口，
    「侧栏 + navMap 同时删掉」的变异体照样通过（变异验证报 WEAK 才暴露）。

    教训：只要判据是「源码里有没有出现 X」，就必须先把**所有形态的注释**
    剥干净 —— 注释里写「这里曾经写错了 X」，不该让 X 被当成存在。

    等长替换的理由同 `strip_js_comments`：保持索引与行号一一对应。
    """
    return re.sub(
        r'<!--.*?-->',
        lambda m: re.sub(r'[^\n]', ' ', m.group(0)),
        source, flags=re.S)


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


# ---------------------------------------------------------------------------
# 行内事件绑定：必须走事件委托
# ---------------------------------------------------------------------------
#
# `TNR_UI.mountTable` 在「点表头排序 / 翻页 / 改每页条数」时会整体重建 tbody
# （内部再次调用 `mountTable` → `el.innerHTML = html`）。此前各端普遍写成
#
#     TNR_UI.mountTable('xxxWrap', {...});
#     document.querySelectorAll('[data-act]').forEach(b => {
#       b.addEventListener('click', () => this.doSomething(b.dataset.act));
#     });
#
# 监听器直接绑在**行元素**上，随旧节点一起被丢弃 —— 表现是「点单号/编号/按钮
# 没反应，且控制台不报任何错」。本轮把四端共 33 处这类绑定全部改为
#
#     TNR_UI.delegateClick('xxxWrap', { 'data-act': (v, el) => ... });
#
# 绑在**不会随重渲染消失的容器**上。下面两个用例锁死这个约定。
# ---------------------------------------------------------------------------

class RowActionEventDelegationTest(SimpleTestCase):
    """行内可点元素一律走 `TNR_UI.delegateClick`，不得直接绑在行元素上。

    判定方式：若某个方法内出现过 `mountTable`，那么该方法内**任何**
    `querySelectorAll('...')` + `addEventListener` 都视为行内绑定 —— 因为
    mountTable 会重建它渲染出来的整棵子树，绑在其中的监听器都可能丢失。

    确实不需要委托的场景（绑定目标不在任何会被重建的容器内），在该行或
    前 400 字符内标注 `direct-bind-ok` 即可豁免。
    """

    BIND = re.compile(
        r"querySelectorAll\(\s*'([^']+)'\s*\)[\s\S]{0,220}?addEventListener", re.M)
    MOUNT = re.compile(r'mountTable\s*\(')
    METHOD_NAME = re.compile(
        r'(?m)^  (?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{')
    EXEMPT = 'direct-bind-ok'

    @staticmethod
    def _method_of(text, pos):
        name = '?'
        for m in RowActionEventDelegationTest.METHOD_NAME.finditer(text):
            if m.start() < pos:
                name = m.group(1)
            else:
                break
        return name

    def test_no_direct_row_binding_inside_mounttable_methods(self):
        problems = []
        for name, rel in PORTALS.items():
            source = strip_js_comments(read(rel))   # 等长替换，索引与行号不变
            mounts = [m.start() for m in self.MOUNT.finditer(source)]
            if not mounts:
                continue
            for m in self.BIND.finditer(source):
                pos = m.start()
                fn = self._method_of(source, pos)
                if not any(self._method_of(source, p) == fn for p in mounts):
                    continue          # 该方法不渲染表格，不属于本约定管辖
                if self.EXEMPT in source[max(0, pos - 400):pos]:
                    continue
                line_no = source.count('\n', 0, pos) + 1
                problems.append(
                    f'{name}:{line_no} {fn}() 里 querySelectorAll(\'{m.group(1)}\') '
                    '直接绑了监听器；mountTable 重建 tbody 后它会失效'
                    '（点按钮没反应且不报错），请改用 TNR_UI.delegateClick(稳定容器, {...})')
        self.assertFalse(
            problems,
            '行内绑定未走事件委托：\n  ' + '\n  '.join(problems)
        )

    def test_delegate_click_helper_exists_and_dedups(self):
        """委托助手必须存在，且按「容器 + 标识」去重，重复调用不会叠加监听器。"""
        source = read('static/js/tnr-common.js')
        self.assertIn('delegateClick(container, handlers, tag)', source,
                      'TNR_UI.delegateClick 不见了，各端的行内委托会全部失效')
        self.assertIn('_delegatedTags: new WeakMap()', source,
                      'delegateClick 的去重表不见了：重复调用会叠加多份监听器，'
                      '一次点击触发多次动作')

    def test_bind_photo_zoom_is_delegated(self):
        """照片放大同样必须走委托，否则重渲染出来的新图片点不开。"""
        source = read('static/js/tnr-common.js')
        body = re.search(r'bindPhotoZoom\(container\)\s*\{([\s\S]{0,500}?)\n  \}', source)
        self.assertIsNotNone(body, '未找到 bindPhotoZoom 实现')
        self.assertIn('delegateClick', body.group(1),
                      'bindPhotoZoom 必须基于事件委托实现；'
                      '若退回「遍历元素逐个绑定」，mountTable 重渲染后的图片将无法点击放大')


# ---------------------------------------------------------------------------
# 门户对象自洽性：侧栏导航 → 页面容器 → 顶栏标题
# ---------------------------------------------------------------------------
#
# 第十轮 GUI 实测抓到的形态（政府端「全业务监管」）：
#
#   - 侧栏 `gov_base.html` 里有「全业务监管」这个导航项；
#   - `GovPortal.navMap` 也把它映射到 `'supervision'`；
#   - 但 `{% block content %}` 里**根本没有 `#page-supervision` 容器**。
#
# 点进去时 `render_supervision()` 第一行 `document.getElementById(...)`
# 返回 null → `.innerHTML` 抛 TypeError。异常发生在点击回调里，页面停在原处、
# **不报任何可见错误**，用户只会觉得「点了没反应」——静态语法检查、
# 模板渲染、API 测试全都覆盖不到。同一函数里还有一处 `this.state.supTab`
# （`GovPortal` 根本没有 `state` 属性）会在表格渲染时再抛一次。
# ---------------------------------------------------------------------------

BASE_TEMPLATES = {
    'shelter': 'templates/portal/shelter_base.html',
    'hospital': 'templates/portal/hospital_base.html',
    'gov': 'templates/portal/gov_base.html',
    'adopter': 'templates/portal/adopter_base.html',
}

# 各端门户对象名（adopter 端是散落的全局函数，没有统一对象）
PORTAL_OBJECTS = {
    'shelter': 'Shelter',
    'hospital': 'Hospital',
    'gov': 'GovPortal',
}

NAV_LABEL = re.compile(r'<span class="nav-item-label">([^<]+)</span>')
NAV_DATA_ATTR = re.compile(r'data-nav="([^"]+)"')
PAGE_CONTAINER = re.compile(r'id="page-([A-Za-z0-9_-]+)"')
NAV_MAP_ENTRY = re.compile(r"'([^']+)'\s*:\s*'([^']+)'")


def portal_object_body(source, var_name):
    """返回 `const <var_name> = { ... };` 的完整文本。

    结束位置取**列 0 的 `};`**，而不是靠配平花括号：JS 里的正则字面量
    （如 `replace(/\\d{3}/g, '')`）会让朴素的括号计数失步，从而把整个
    对象体截断或越界。
    """
    m = re.search(r'(?m)^(?:const|let|var)\s+' + re.escape(var_name) + r'\s*=\s*\{', source)
    if not m:
        return None
    end = re.search(r'(?m)^\};', source[m.end():])
    if not end:
        return None
    return source[m.start():m.end() + end.end()]


def property_literal(source, prop):
    """返回 `prop: { ... }` 的 `{...}` 文本（用于对象内的属性字面量，如 titles）。"""
    m = re.search(r'(?m)^\s*' + re.escape(prop) + r'\s*:\s*\{', source)
    if not m:
        return None
    return brace_body(source, m.end() - 1)


class SidebarNavTargetTest(SimpleTestCase):
    """侧栏上的每个导航项都必须指向真实存在的页面容器。

    三种实现方式都要覆盖：`navMap` 标签映射（shelter/gov）、
    侧栏 `data-nav` 属性（hospital）。
    """

    def test_nav_targets_have_page_containers(self):
        problems = []
        checked = 0
        for name, rel in PORTALS.items():
            source = read(rel)
            containers = set(PAGE_CONTAINER.findall(source))
            nav_map = find_object_literal(source, 'navMap')
            if nav_map:
                for label, page in NAV_MAP_ENTRY.findall(nav_map):
                    checked += 1
                    if page not in containers:
                        problems.append(
                            f'{name}: 侧栏「{label}」→ {page}，但源码里没有 '
                            f'id="page-{page}" 容器（点进去抛 TypeError，页面无任何提示）')
            for page in NAV_DATA_ATTR.findall(read(BASE_TEMPLATES[name])):
                checked += 1
                if page not in containers:
                    problems.append(
                        f'{name}: 侧栏 data-nav="{page}" 没有对应的 id="page-{page}" 容器')
        self.assertGreater(checked, 0, '没有解析到任何导航映射，测试本身可能已失效')
        self.assertFalse(
            problems,
            '侧栏导航指向了不存在的页面容器：\n  ' + '\n  '.join(problems))

    def test_sidebar_labels_and_navmap_are_in_sync(self):
        """侧栏标签 ↔ navMap 必须双向一致。

        只改一侧的两种后果：侧栏多了标签 → 点击无反应；navMap 多了条目 →
        该页面在界面上**点不进去**（功能不可达）。
        """
        problems = []
        checked = 0
        for name, rel in PORTALS.items():
            nav_map = find_object_literal(read(rel), 'navMap')
            if not nav_map:
                continue          # 该端不用 navMap 做导航
            checked += 1
            mapped = {label for label, _ in NAV_MAP_ENTRY.findall(nav_map)}
            labels = {t.strip() for t in NAV_LABEL.findall(read(BASE_TEMPLATES[name]))}
            self.assertTrue(labels, f'{name}: 侧栏里没有解析到任何 nav-item-label')
            self.assertFalse(
                labels - mapped,
                f'{name}: 侧栏有 {sorted(labels - mapped)}，但 navMap 没有对应去向 → 点击无反应')
            self.assertFalse(
                mapped - labels,
                f'{name}: navMap 里的 {sorted(mapped - labels)} 在侧栏上找不到入口 → 该页面点不进去')
        self.assertGreater(checked, 0, '没有找到任何 navMap，测试本身可能已失效')

    def test_titles_map_covers_every_nav_target(self):
        """顶栏标题映射必须覆盖全部导航目标，否则顶栏标题是空白。"""
        problems = []
        for name, rel in PORTALS.items():
            source = read(rel)
            titles = property_literal(source, 'titles')
            if not titles:
                continue
            keys = object_keys(titles)
            targets = set()
            nav_map = find_object_literal(source, 'navMap')
            if nav_map:
                targets |= {page for _, page in NAV_MAP_ENTRY.findall(nav_map)}
            targets |= set(NAV_DATA_ATTR.findall(read(BASE_TEMPLATES[name])))
            missing = sorted(targets - keys)
            if missing:
                problems.append(f'{name}: titles 缺少 {missing}（顶栏标题会显示为空）')
        self.assertFalse(problems, '顶栏标题映射不完整：\n  ' + '\n  '.join(problems))


class UndeclaredInstancePropertyTest(SimpleTestCase):
    """门户对象里 `this.<name>` 读取的属性必须存在，否则运行时 TypeError。

    「声明」的三种合法来源：
      1. 对象字面量的键（`state: {...}`、`titles: {...}`）；
      2. 对象字面量的方法（`render_xxx() {...}`）；
      3. **运行期赋值**（`this._captureRows = list;`）——这是本项目里
         「渲染时把数据挂到实例上供委托闭包实时读取」的常规写法，不算缺陷。

    只读、既没声明也没赋值的名字才是缺陷。真实案例：`GovPortal` 里写了
    `this.state.supTab`，但整个对象没有 `state` 属性 —— 表格渲染时
    `Cannot read properties of undefined (reading 'supTab')`，标签页空白。
    """

    def test_this_properties_are_declared_or_assigned(self):
        problems = []
        checked = 0
        for name, var in PORTAL_OBJECTS.items():
            source = strip_js_comments(read(PORTALS[name]))   # 等长替换，行号不变
            body = portal_object_body(source, var)
            self.assertIsNotNone(body, f'{name}: 未找到门户对象 {var}')
            checked += 1
            methods = set(re.findall(
                r'(?m)^  (?:async\s+)?([A-Za-z_$][\w$]*)\s*\(', body))
            declared = object_keys(body) | methods
            assigned = set(re.findall(r'\bthis\.([A-Za-z_$][\w$]*)\s*=[^=]', body))
            for prop in sorted(set(re.findall(r'\bthis\.([A-Za-z_$][\w$]*)', body))):
                if prop in declared or prop in assigned:
                    continue
                line_no = source.count('\n', 0, body.find('this.' + prop)) + 1
                problems.append(
                    f'{name}:{line_no} {var} 里用了 `this.{prop}`，'
                    '但它既不是对象的键/方法，也没有被赋值 → 运行时 TypeError')
        self.assertGreater(checked, 0, '没有找到任何门户对象，测试本身可能已失效')
        self.assertFalse(
            problems,
            '门户对象引用了未声明的属性：\n  ' + '\n  '.join(problems))


# ============================================================
# 写接口失败可见性：`try { ... } catch` 的 catch 不能是死代码
# ============================================================

RAW_BACKED_CALL = re.compile(r'await\s+TNR_API\.(?!post\b|get\b)(\w+)\s*\(')
# 注意：`res && res.message` **不算**成功判定 —— 它只是「有消息就用消息」，
# 服务端返回 `{success:false, message:'用户名已存在'}` 时同样会走进「成功」分支。
SUCCESS_CHECK = re.compile(
    r'assertOk|res\.success|res\.ok|success\s*===|success\s*!==|\bjson\.success')
# toast 文案里可能带括号（`toast(res.message || '已保存', 'success')`），
# 所以只匹配结尾的 `'success')`，不要试图用 `[^)]*` 吃掉整个参数列表。
SUCCESS_TOAST = re.compile(r"'success'\s*\)")


def raw_backed_methods():
    """返回 `TNR_API` 里**不会抛异常**的具名方法（实现走 `_post` / `_get`）。

    只有通用的 `TNR_API.post()` / `TNR_API.get()` 走 `_handle`（失败即抛）。
    「catch 能不能被触发」完全取决于这个区分，所以从源码解析而不是手写清单 ——
    手写清单会在新增接口时悄悄失效。
    """
    source = read('static/js/tnr-api.js')
    return {m.group(1) for m in re.finditer(
        r'async\s+(\w+)\s*\([^)]*\)\s*\{\s*return\s+this\.(_post|_get)\b', source)}


class WriteFailureVisibilityTest(SimpleTestCase):
    """写接口失败时界面必须报错，不能弹绿色「成功」。

    `TNR_API._post` / `_get` **不抛异常**（把响应体原样返回），而本项目的业务
    错误一律是 **HTTP 200 + `{success:false, message}`**（`json_fail`；
    只有越权才用 404）。于是下面这种写法里的 catch 永远不执行：

        try {
          await TNR_API.createUser({...});            // 不抛
          TNR_UI.toast('账号创建成功', 'success');     // ← 服务端拒绝时照样弹
        } catch (e) { TNR_UI.toast(e.message, 'danger'); }

    真实案例：政府端重复创建同名账号，服务端返回「用户名已存在」，
    界面却弹绿色「账号创建成功」并**关闭弹窗** —— 用户以为存上了，
    实际什么都没写。这类「谎报成功」比直接报错更糟：用户不会去核对。

    修法：拿到响应后过一遍 `TNR_UI.assertOk(res, '创建失败')`，把失败转成异常。
    """

    def test_try_catch_around_raw_api_checks_success(self):
        raw = raw_backed_methods()
        self.assertIn('createUser', raw,
                      '没能从 tnr-api.js 解析出 _post 驱动的接口，测试本身可能已失效')
        problems = []
        for name, rel in PORTALS.items():
            lines = strip_js_comments(read(rel)).split('\n')
            for i, line in enumerate(lines, 1):
                if 'try {' not in line:
                    continue
                # 取 try → catch 之间的块（找不到 catch 就取到窗口末尾）
                block = []
                for j in range(i, min(i + 60, len(lines))):
                    if re.search(r'\bcatch\b', lines[j]):
                        block = lines[i - 1:j]
                        break
                body = '\n'.join(block)
                calls = sorted(set(RAW_BACKED_CALL.findall(body)))
                if not calls or not SUCCESS_TOAST.search(body):
                    continue
                if SUCCESS_CHECK.search(body):
                    continue
                problems.append(
                    f'{name}:{i} try 块调用了 {", ".join(calls)}'
                    '（走 _post，失败不抛异常），块内却直接弹「成功」且没有任何'
                    '成功判定 —— catch 是死代码，服务端拒绝时界面会谎报成功')
        self.assertFalse(
            problems,
            '写接口的失败被界面吞掉了：\n  ' + '\n  '.join(problems))

    def test_assert_ok_helper_exists_and_throws(self):
        """`TNR_UI.assertOk` 必须存在，且 `success === false` 时抛异常。"""
        source = strip_js_comments(read('static/js/tnr-common.js'))
        self.assertIn('assertOk(', source,
                      'TNR_UI 缺少 assertOk —— 写接口的失败判定没有统一入口')
        self.assertRegex(
            source,
            r'assertOk\s*\([^)]*\)\s*\{[^}]*success\s*===\s*false[^}]*throw',
            'assertOk 必须在 success === false 时抛异常，'
            '否则调用方的 catch 仍然是死代码')

    def test_blacklist_check_does_not_use_silent_get(self):
        """黑名单校验是**安全控制**的前端预检，**不能用 `_get`**（第二十二轮）。

        上面那条管的是「写接口失败被吞」，这条是它的**读侧对应物**：
        `_get` 把 403 / 500 / 断网静默变成 `[]`，而三处调用点都写成

            if (bl && (bl.name || bl.idCard)) { ...警告... } else { ...放行... }

        于是**校验没做成时一律走「不在黑名单」分支** ——
        「黑名单查询拦截」还会明晃晃显示「✓ 通过：未在黑名单中」，
        主人领回不弹警告、线下登记直接放行。

        服务端在 `owner_return` / `adoption_create` 里会**硬拦**，所以这不是绕过；
        但「没查成」被渲染成「通过」是**错误结论**，比报错更难发现。
        改用 `get()`（失败抛错），由调用方显式提示「校验失败，请重试」。
        """
        source = strip_js_comments(read('static/js/tnr-api.js'))
        m = re.search(r'async checkBlacklist\([^)]*\)\s*\{(.*?)\n  \}', source, re.S)
        self.assertIsNotNone(
            m, 'tnr-api.js 里找不到 checkBlacklist，测试本身可能已失效')
        body = m.group(1)
        self.assertNotIn(
            '_get(', body,
            'checkBlacklist 又用回 _get 了 —— 它会把校验失败静默变成「不在黑名单」，'
            '界面于是显示「✓ 通过」')
        self.assertIn(
            'this.get(', body,
            'checkBlacklist 必须走 get()：失败抛错，调用方才能提示「校验失败」')


class PageReachabilityTest(SimpleTestCase):
    """每个 `#page-<id>` 都必须能从导航点到（第二十二轮）。

    真实案例：`#page-blacklist` 与 `Shelter.render_blacklist()` **一直都在**，
    但侧栏从 `renderSidebar()`（按 JS 里的 `navGroups` 渲染）改成
    `shelter_base.html` 里的**静态 HTML** 时漏掉了这一项 —— 于是
    **整页在界面上不可达**，只能靠控制台 `Shelter.navigate('blacklist')` 进去。
    依赖它的「黑名单登记 / 解除 / 查询拦截」随之全部作废。

    `BusinessFlowEntryPointTest` **抓不到**这种：它检查「API 方法有没有被调用」，
    而 `createBlacklist(` 确实被调用了 —— 只是在一个**点不到的页面**里。
    「功能实现了」和「功能能点到」是两件事。

    判据：页面 id 必须出现在导航目标的任一形态里 ——
    `navMap` 的值、`data-nav` / `data-page` / `data-tab`、或 `navigate('x')` 调用。
    （四端机制不同：捕捉点/政府端用 `navMap` 按标签文本映射，
    医院端用 `data-nav`，领养人端用 `data-tab` 的移动端底栏。）
    """

    # 已知不可达但**有意保留**的页面 → 理由。加进这里必须写清原因。
    EXEMPT = {
        ('shelter', 'owner-return'):
            '主人领回已改为「动物去向 → 回收」标签（首页快捷操作指向 '
            'navigate("release","recovery")），独立页保留为死代码，删留待业务确认',
    }

    @staticmethod
    def _page_ids(source):
        return set(re.findall(r'id="page-([A-Za-z0-9_-]+)"', source))

    @staticmethod
    def _nav_targets(source):
        targets = set()
        for m in re.finditer(r'navMap\s*=\s*\{(.*?)\n\s*\};', source, re.S):
            targets |= set(re.findall(r"'([A-Za-z0-9_-]+)'", m.group(1)))
        for attr in ('data-nav', 'data-page', 'data-tab', 'data-quick'):
            targets |= set(re.findall(rf'{attr}="([A-Za-z0-9_-]+)"', source))
        targets |= set(re.findall(r"navigate\('([A-Za-z0-9_-]+)'", source))
        targets |= set(re.findall(r"page:\s*'([A-Za-z0-9_-]+)'", source))
        return targets

    @staticmethod
    def _sources(name):
        """返回 (portal 源码, base 源码)，**各自先剥掉注释**（HTML + JS）。

        不剥注释会踩「注释把测试骗过」的老坑，本项目**踩过两次**：
          1. 我在 `render_owner_return` 的 JS 注释里写了
             ``navigate('owner-return')``，`_nav_targets` 把它当真实调用，
             `owner-return` 被判成「已可达」，豁免复核断言直接报「豁免已过期」；
          2. 我在 `shelter_base.html` 的 **HTML 注释**里写了
             ``Shelter.navigate('blacklist')`` —— `strip_js_comments` 只管
             JS 注释，于是「黑名单管理」被判成「有导航入口」，
             「侧栏 + navMap 同时删掉」的变异体照样通过（变异验证报 WEAK）。
        所以这里要**两种注释都剥**，且 HTML 先剥（HTML 注释边界无歧义，
        先剥掉就不会让注释里的引号把 JS 扫描器的字符串配对带偏）。

        两个文件要**分别**剥离再拼 —— 拼起来再剥会破坏引号配对。
        """
        def clean(text):
            return strip_js_comments(strip_html_comments(text))

        portal = clean(read(PORTALS[name]))
        base_path = f'templates/portal/{name}_base.html'
        base = ''
        if os.path.exists(os.path.join(ROOT, base_path)):
            base = clean(read(base_path))
        return portal, base

    def test_every_page_view_is_reachable_from_navigation(self):
        problems = []
        scanned = 0
        for name in PORTALS:
            portal, base = self._sources(name)
            pages = self._page_ids(portal)
            self.assertTrue(
                pages, f'{PORTALS[name]} 里一个 #page-* 都没扫到 —— 扫描逻辑失效了')
            scanned += len(pages)
            targets = self._nav_targets(portal + base)
            for pid in sorted(pages - targets):
                if (name, pid) in self.EXEMPT:
                    continue
                problems.append(
                    f'{name}: #page-{pid} 在导航里没有任何入口'
                    f'（侧栏 / 快捷操作 / navigate 调用都没有）')
        self.assertGreaterEqual(scanned, 20, '扫到的页面数偏少，扫描逻辑可能失效')
        self.assertEqual(
            problems, [],
            '有页面在界面上点不到（功能实现了但用户到不了）：\n  '
            + '\n  '.join(problems))

    def test_exemptions_are_still_unreachable(self):
        """豁免清单要**定期复核** —— 页面被恢复或删除后，豁免就该删掉。

        不做这条断言的话，豁免会永久留着，把真问题挡住。
        """
        stale = []
        for (name, pid), reason in self.EXEMPT.items():
            portal, base = self._sources(name)
            if pid not in self._page_ids(portal):
                stale.append(f'{name}.{pid} 已不存在（页面被删了？）→ 删掉豁免：{reason}')
            elif pid in self._nav_targets(portal + base):
                stale.append(f'{name}.{pid} 已经可达了 → 删掉豁免：{reason}')
        self.assertEqual(stale, [], '豁免清单已过期：\n  ' + '\n  '.join(stale))

    def test_comments_cannot_fake_reachability(self):
        """守卫的守卫：注释里的 `navigate('x')` 不能算导航入口。

        这条断言本身不长，但它防的是**最阴的一类失效**：判据靠源码文本匹配，
        而修复时顺手写的说明注释正好包含了那个文本 —— 于是判据被自己骗过，
        而且**变异验证会报 WEAK 而不是 FAIL**，很容易被当成「小瑕疵」放过。
        本项目已经踩过两次（JS 注释一次、HTML 注释一次），所以钉死在这里。
        """
        probe = (
            '<script>\n'
            "  // 说明：以前靠 Shelter.navigate('ghost-a') 进页面\n"
            "  /* 块注释里也有 navigate('ghost-b') */\n"
            '  const real = 1;\n'
            '</script>\n'
            "<!-- HTML 注释：也可以 Shelter.navigate('ghost-c') -->\n"
        )
        cleaned = strip_js_comments(strip_html_comments(probe))
        self.assertEqual(
            self._nav_targets(cleaned), set(),
            '注释里的 navigate 被当成了导航入口 —— 剥离逻辑失效')

        # 正向对照：真代码必须仍被认出来，否则说明剥离器把有效代码也剥了
        # （那样守卫会变成「一律报不可达」的误报机器，同样不能用）。
        self.assertIn('ghost-d', self._nav_targets("  this.navigate('ghost-d');"))
        self.assertIn('ghost-e', self._nav_targets('  <a data-nav="ghost-e">'))
        self.assertIn(
            'ghost-f',
            self._nav_targets("  const navMap = {\n    '标签': 'ghost-f'\n  };"))


class StaticAssetCacheBustTest(SimpleTestCase):
    """静态资源的 `?v=` 必须**全局同一个值**。

    改 `static/` 下任何文件都要同时升 5 个模板里的 **6 处** `?v=`
    （`base.html` 的 CSS + `tnr-common.js`，以及 4 个门户各自引入的 `tnr-api.js`）。
    漏升任何一处，那个页面就会继续读浏览器缓存里的旧文件 ——
    表现为「改了没生效」，而且**只在某一个端上不生效**，极难排查。
    第十轮给 4 处补版本号、第二十二轮升 `20260918k` → `20260919a` 时都靠人工核对，
    这条断言把它变成机器检查。
    """

    def test_all_version_query_params_are_identical(self):
        seen = {}   # 版本号 → [文件:行]
        for rel in ['templates/base.html'] + list(PORTALS.values()):
            for i, line in enumerate(read(rel).split('\n'), 1):
                for v in re.findall(r'\?v=([A-Za-z0-9_.-]+)', line):
                    seen.setdefault(v, []).append(f'{rel}:{i}')
        self.assertTrue(seen, '一个 ?v= 都没扫到 —— 扫描逻辑失效了')
        self.assertEqual(
            len(seen), 1,
            '静态资源版本号不一致（漏升会导致某个端继续读缓存）：\n  '
            + '\n  '.join(f'{v} → {", ".join(locs)}' for v, locs in seen.items()))

    def test_tnr_api_js_is_versioned_in_every_portal(self):
        """4 个门户都必须给 `tnr-api.js` 带版本号。

        （第十轮之前 4 处**都没有**版本号，改 `tnr-api.js` 后用户永远读旧文件。）
        """
        missing = []
        for name, rel in PORTALS.items():
            source = read(rel)
            # 注意 `{% static '...' %}` 的 `%}` 在路径与 `?v=` 之间 ——
            # 漏掉它会让正则永远匹配不到版本号，把 4 个端全报成「缺版本号」
            # （第一版就踩了，幸好它报错而不是静默通过）。
            m = re.search(r"static\s+'js/tnr-api\.js'\s*%\}\s*(\?v=)?", source)
            if m is None:
                missing.append(f'{name}: 找不到 tnr-api.js 的引入语句')
            elif not m.group(1):
                missing.append(f'{name}: 引入了 tnr-api.js 但没带 ?v=')
        self.assertEqual(missing, [], '门户引入 tnr-api.js 缺版本号：\n  '
                                      + '\n  '.join(missing))


# ============================================================
# 业务流程闭环：每个业务环节都必须有界面入口
# ============================================================

# 业务环节 → 界面里的调用特征（具名方法名，或直接写 URL 片段）。
#
# 这张表是**人工维护**的，价值在于：后端接口写好了但界面没接，流程就是死的，
# 而机器扫描「API 方法有没有被调用」误报太多 —— 很多页面直接走
# `TNR_API.post(url, ...)` / `TNR_API._postForm(url, fd)`，根本不经过具名方法。
#
# 真实案例：`/api/business/releases/create/` 一直存在，两端门户却都没调用，
# 「动物去向 → 放养」里的「待放养确认」列表永远是空的 ——
# **整个放养流程在界面上不可达**（只能靠直接调接口或脚本造数据）。
FLOW_ENTRY_POINTS = {
    '捕捉登记': '/api/business/captures/create/',
    '主人领回（回收）': 'ownerReturn(',
    '转运下发': 'createTransfer(',
    '转运签收': 'receiveTransfer(',
    '转运驳回': 'rejectTransfer(',
    # 「转运重新下发」已移除：它会把被驳回的单再复制成一张 pending 单，
    # 原单永远停在 rejected，于是同一单可反复下发、同一动物挂在多张未结单上。
    # 现在被驳回的动物自动回到「待转运」备选框，由操作员重新勾选下发新单。
    '转运撤回': 'withdrawTransfer(',
    '诊疗登记': 'createTreatment(',
    '物料采购入库': 'purchaseMaterial(',
    '物料下发': 'dispatchMaterial(',
    '物料签收': 'receiveMaterial(',
    '物料库存异动': 'adjustStock(',
    '放养发起': 'createRelease(',
    '放养确认': 'confirmRelease(',
    '领养登记': 'registerAdoption(',
    '领养资料编辑/上下架': 'editAdoptionInfo(',
    '领养确认领出': 'confirmAdoptionClaim(',
    '领养撤销': 'reclaimAdoption(',
    '死亡登记': 'createEuthanasia(',
    '遗体移交（捕捉点领取）': 'receiveBody(',
    '黑名单登记': 'createBlacklist(',
    '黑名单解除': 'deleteBlacklist(',
    '回访打卡审核': 'reviewCheckin(',
}


class BusinessFlowEntryPointTest(SimpleTestCase):
    """每个业务环节都必须能在界面上点到，不能只有接口没有入口。

    接口存在、界面没接 = 流程死掉，而且**不会报错** —— 页面只是永远空着，
    看起来像「还没有数据」。靠人工走查很容易漏（放养流程就这样漏了很久）。
    """

    def test_every_flow_has_a_ui_entry(self):
        corpus = '\n'.join(read(rel) for rel in PORTALS.values())
        missing = [name for name, token in FLOW_ENTRY_POINTS.items()
                   if token not in corpus]
        self.assertFalse(
            missing,
            '以下业务环节在所有门户模板里都找不到调用入口（流程不可达）：\n  '
            + '\n  '.join(missing))





# ============================================================
# 页签 id 一致性：JS 里 `renderXxxTab('id')` 的 id 必须真实存在
# ============================================================

TAB_CONTAINER = re.compile(r'id="(\w+Tabs)"')
TAB_ITEM = re.compile(r'data-tab="([\w-]+)"')
TAB_CALL = re.compile(r"render(\w+)Tab\('([\w-]+)'\)")


class TabIdConsistencyTest(SimpleTestCase):
    """`renderXxxTab('id')` 引用的页签 id 必须真实存在于对应的 `#xxxTabs` 容器里。

    真实案例：捕捉端 `showReleaseConfirmModal` 在「确认放养成功」之后调的是
    `this.renderRelTab('released')`，而 `#relTabs` 里的 id 是 **`release`**
    —— `released` 是放养记录的**状态**，不是页签名。`renderRelTab` 没有匹配分支，
    紧接着的 `document.querySelector('[data-tab="released"]').click()` 直接对
    null 取属性，抛**未捕获 TypeError**：界面上提示「放养确认成功」，但列表不刷新、
    停留在旧状态，控制台里才看得到报错。用户会以为操作没生效而反复重试。

    这类拼写错误没有任何静态约束（模板里没有 `released` 这个页签，JS 里也没有
    任何地方引用它），只能靠本用例兜住。
    """

    def test_tab_ids_used_in_js_exist_in_markup(self):
        problems = []
        for name, rel in PORTALS.items():
            source = read(rel)
            body = strip_js_comments(source)   # 注释里提到旧 id 不算引用
            for m in TAB_CONTAINER.finditer(source):
                container = m.group(1)              # 例如 relTabs
                prefix = container[:-len('Tabs')]   # rel
                # 页签容器后面紧跟的是内容容器 `<div id="...">`，以此作为边界
                seg = source[m.end():]
                nxt = seg.find('<div id=')
                if nxt >= 0:
                    seg = seg[:nxt]
                ids = set(TAB_ITEM.findall(seg))
                if not ids:
                    continue
                for call in TAB_CALL.finditer(body):
                    if call.group(1).lower() != prefix.lower():
                        continue
                    if call.group(2) not in ids:
                        problems.append(
                            f'{name}: render{call.group(1)}Tab("{call.group(2)}") '
                            f'不在 #{container} 的页签里（实际有 {sorted(ids)}）')
        self.assertFalse(
            problems,
            '以下页签 id 在 JS 里被引用但模板里并不存在 —— '
            '切换页签会静默失败（渲染无分支）或抛 TypeError：\n  '
            + '\n  '.join(problems))


class LocalDateDefaultTest(SimpleTestCase):
    """表单日期默认值必须取**本地时间**，不能用 `toISOString()`（UTC）。

    真实案例：医院端的接种/用药/植入/手术/处置日期、捕捉端「回收时间」
    此前都用 `new Date().toISOString().substr(0, 10)` 作默认值。
    `toISOString()` 返回 **UTC**，北京时间 00:00–08:00 这段 UTC 还停在前一天
    —— 9/18 凌晨打开表单，默认日期显示 9/17；`datetime-local` 更会整体差 8 小时。

    这与后端 `generate_pet_codes()` 曾用 `timezone.now()` 取日期是**同一类坑**
    （那边表现为编号 `TNR260917xxx`），正确做法是 `TNR_UI.todayStr()` /
    `TNR_UI.nowLocalStr()`。
    """

    # toISOString() 后紧跟日期截取 → 说明是拿它当「今天」用
    ISO_DATE_CUT = re.compile(
        r'toISOString\(\)\s*\.\s*(?:substr|substring|slice)\s*\(\s*0\s*,')
    UTC_GETTER = re.compile(r'\bgetUTC(?:FullYear|Month|Date|Hours|Minutes)\s*\(')

    def test_no_utc_based_date_defaults(self):
        problems = []
        for rel in FRONTEND_FILES:
            # 剔除注释再扫：正确写法的注释里会举反例（`toISOString().substr(0,10)`），
            # 不剥离会把「说明为什么不能这么写」的注释本身判成违规。
            source = strip_js_comments(read(rel))
            for m in self.ISO_DATE_CUT.finditer(source):
                line = source[:m.start()].count('\n') + 1
                problems.append(
                    f'{rel}:{line} 用 toISOString() 截日期当默认值'
                    '（UTC，凌晨 00:00–08:00 会退回前一天）')
            for m in self.UTC_GETTER.finditer(source):
                line = source[:m.start()].count('\n') + 1
                problems.append(
                    f'{rel}:{line} 用 getUTC*() 取日期分量（应改用本地方法）')
        self.assertFalse(
            problems,
            '以下位置把 UTC 当本地时间用 —— 北京时间 00:00–08:00 会得到前一天：\n  '
            + '\n  '.join(problems)
            + '\n请改用 TNR_UI.todayStr() / TNR_UI.nowLocalStr()。')


class LocalDateInterpretationTest(SimpleTestCase):
    """把后端返回的日期串当日期用时，必须按**本地时区**取日期。

    与上一个类同源（都是「把 UTC 当本地用」），但触发点不同：
    `LocalDateDefaultTest` 管**表单默认值**（`toISOString()`）；
    这里管**解读后端返回的串**。

    后端 `serialize_instance` 对 `DateTimeField` 用 `isoformat()`，`USE_TZ=True`
    下是 **UTC 带偏移**（`2026-09-22T06:05:50+00:00`）。裸截断
    （`.slice(0, 10)` / `.substr(0, 10)` / `.substring(0, 10)`）拿到的是
    **UTC 日期**，后果分两类：

      * **显示**：医院端转运台账 / 诊疗详情原来自己实现 `formatDateTime`，
        裸截断 → 时间差 8 小时；UTC ≥ 16:00（本地已跨日）时**日期差一天**。
        实测：`2026-09-22T20:30:00+00:00` 旧实现给 `2026-09-22 20:30`，
        正确是 `2026-09-23 04:30`。
      * **筛选**：跟用户选的**本地日期**比较时，北京时间 00:00–08:00 的记录
        会被算到**前一天** —— 用户选「今天」看不到它们。而后端筛选口径是
        `timezone.localdate()` / `__date` 查询（本地日期），两边必须一致。

    正确写法：`TNR_UI.localDateStr()`（筛选键）/ `TNR_UI.formatDate()`（显示）。
    两者对 `DateField` 的纯日期串**原样返回**，对 `DateTimeField` 的 UTC 串
    按浏览器本地时区换算。

    ⚠ 对纯日期串短路是**必需的**：ES 规范把 date-only form
    （`new Date('2026-09-22')`）当 **UTC 午夜**，负时区下会退到 21 日。
    """

    # 任何「取前 10 位当日期」的写法
    NAKED_DATE_CUT = re.compile(
        r'\.\s*(?:substr|substring|slice)\s*\(\s*0\s*,\s*10\s*\)')
    # date-only form 的短路判断（纯日期不参与时区换算）
    DATE_ONLY_GUARD = r'\d{4}-\d{2}-\d{2}'

    def test_no_naked_utc_date_truncation(self):
        problems = []
        for rel in FRONTEND_FILES:
            source = strip_js_comments(read(rel))
            for m in self.NAKED_DATE_CUT.finditer(source):
                line = source[:m.start()].count('\n') + 1
                problems.append(f'{rel}:{line}')
        self.assertFalse(
            problems,
            '以下位置直接截取日期串前 10 位 —— 后端 `DateTimeField` 序列化出来的是 '
            '**UTC 带偏移**的串，裸截断得到 UTC 日期：显示差 8 小时（UTC ≥ 16:00 连'
            '日期都差一天），筛选会把本地 00:00–08:00 的记录算到前一天。\n  '
            + '\n  '.join(problems)
            + '\n请改用 TNR_UI.localDateStr()（筛选键）或 TNR_UI.formatDate()（显示）。')

    def test_hospital_formatters_delegate_to_shared_implementation(self):
        """医院端不得再自己实现 formatDate/formatDateTime（裸截断 UTC）。"""
        source = strip_js_comments(read(PORTALS['hospital']))
        self.assertIn(
            'formatDate(iso) { return TNR_UI.formatDate(iso); }', source,
            '医院端 formatDate 应委托公共实现 —— 自己写就是又一份裸截断。')
        self.assertIn(
            'formatDateTime(iso) { return TNR_UI.formatDateTime(iso); }', source,
            '医院端 formatDateTime 应委托公共实现 —— 自己写会显示 UTC（差 8 小时）。')

    def test_shared_local_date_helper_exists_with_date_only_guard(self):
        """`TNR_UI.localDateStr` 是筛选口径的唯一实现，且必须对纯日期串短路。"""
        source = strip_js_comments(read('static/js/tnr-common.js'))
        self.assertIn('localDateStr(date) {', source,
                      'TNR_UI.localDateStr 不见了 —— 日期筛选口径会退回裸截断。')
        self.assertIn(
            self.DATE_ONLY_GUARD, source,
            '`localDateStr` / `formatDate` 缺少 `YYYY-MM-DD` 短路判断 —— '
            'ES 把 date-only form 当 UTC 午夜，负时区下 `new Date("2026-09-22")` '
            '会退到 21 日。')

    def test_format_date_time_does_not_invent_time_for_date_only(self):
        """`formatDateTime` 收到纯日期串时不得补出 `08:00`。"""
        source = strip_js_comments(read('static/js/tnr-common.js'))
        m = re.search(r'formatDateTime\(date\)\s*\{', source)
        self.assertIsNotNone(m, '找不到 TNR_UI.formatDateTime')
        body = source[m.end():m.end() + 600]
        self.assertIn(
            self.DATE_ONLY_GUARD, body,
            '`formatDateTime` 收到 `DateField` 的纯日期串时，旧实现会按 UTC 午夜再取'
            '本地小时 → UTC+8 下渲染成 `2026-09-22 08:00`（一个日期字段凭空多出 '
            '08:00）。必须在函数开头对 date-only form 短路。')


# ---------------------------------------------------------------------------
# 筛选条（搜索条件）的取值来源与选项覆盖
#
# 第十二轮给十几个列表补了「常用搜索条件」。补筛选条有两类**静默**缺陷
# （界面完全不报错，只是筛不出东西）：
#   1. 选项值与后端 choices 脱节 —— 下拉里混进「选了必定 0 条」的死选项。
#      真实案例：领养审核的状态筛选按「想当然的审批流」写成
#      pending/pending_claim/completed/rejected，而后端只有
#      pending_claim/completed/cancelled —— 两个死选项 + 漏掉真实存在的「已取消」。
#   2. 筛选值不回 getFilterValues 取，而是回 DOM 里捞。真实案例：政府端台账
#      用 `document.querySelector('[data-filter="district"]')` 读
#      `selectedOptions[0].textContent` —— 各 `.page-view` 常驻 DOM，全局裸查
#      命中的是**文档里第一个**区县下拉（机构管理页那个），于是「选任何区县都是空表」。
# ---------------------------------------------------------------------------

SELECT_FILTER_RE = re.compile(
    r"key:\s*'([a-zA-Z_]+)'\s*,\s*label:\s*'([^']*)'\s*,"
    r"\s*type:\s*'select'\s*,\s*options:\s*")


def select_filters(source):
    """列出源码里全部下拉筛选条：[(key, label, 字面量取值集合, options 表达式, 起始下标)]。

    `options` 是动态表达式（`districts.filter(...)`）时，字面量取值集合为空 ——
    那类筛选条的取值来自接口数据，不能用字面量比对。
    """
    out = []
    for m in SELECT_FILTER_RE.finditer(source):
        i = m.end()
        if i < len(source) and source[i] == '[':
            depth, j = 0, i
            while j < len(source):
                if source[j] == '[':
                    depth += 1
                elif source[j] == ']':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            expr = source[i:j + 1]
            values = set(re.findall(r"value:\s*'([^']*)'", expr))
        else:
            # 动态 options：这些筛选条都是单行写法，取到行末即可
            end = source.find('\n', i)
            expr = source[i:end]
            values = set()
        out.append((m.group(1), m.group(2), values, expr, m.start()))
    return out


class FilterBarOptionCoverageTest(SimpleTestCase):
    """下拉筛选条的选项必须与后端 choices **双向**对齐。

    「缺项」让用户筛不到某一类；「多项」制造死选项。两个方向都要断言。
    """

    def _find(self, rel, must_have):
        """按「选项里必然包含的取值」定位某一个下拉筛选条。

        同一个 portal 里 label 为「状态」的筛选条有四五条，靠 key/label 无法区分；
        用「某个只有该实体才有的枚举值」做指纹最稳（如 `partial` 只属于 Capture）。
        """
        for key, label, values, expr, pos in select_filters(read(rel)):
            if values and must_have <= values:
                return key, label, values
        self.fail(f'{rel} 中未找到选项包含 {sorted(must_have)} 的下拉筛选条，'
                  f'检查测试本身是否失效')

    def _assert_covers(self, rel, must_have, model, field):
        expected = {c[0] for c in model._meta.get_field(field).choices}
        self.assertTrue(expected, f'{model.__name__}.{field} 没有定义 choices')
        key, label, values = self._find(rel, must_have)
        missing = expected - values
        extra = values - expected
        self.assertFalse(
            missing,
            f'{rel} 的「{label}」筛选（key={key}）缺少 {sorted(missing)}，'
            f'用户无法按这些取值过滤。后端 {model.__name__}.{field} 全部取值：{sorted(expected)}')
        self.assertFalse(
            extra,
            f'{rel} 的「{label}」筛选（key={key}）含后端不存在的取值 {sorted(extra)} —— '
            f'选中它们永远是 0 条（死选项）。后端 {model.__name__}.{field} 全部取值：'
            f'{sorted(expected)}')

    def test_shelter_capture_status_filter_covers_all(self):
        """捕捉单状态含 `void`（已作废），筛选下拉必须能选到它。"""
        self._assert_covers(PORTALS['shelter'], {'partial'}, Capture, 'status')

    def test_shelter_transfer_status_filter_covers_all(self):
        self._assert_covers(PORTALS['shelter'], {'received'}, Transfer, 'status')

    def test_shelter_adoption_status_filter_covers_all(self):
        """领养状态只有 pending_claim/completed/cancelled，没有 pending/rejected。"""
        self._assert_covers(PORTALS['shelter'], {'pending_claim'}, Adoption, 'status')

    def test_shelter_checkin_status_filter_covers_all(self):
        self._assert_covers(PORTALS['shelter'], {'approved'}, CheckIn, 'status')


class DistrictFilterExcludesCityTest(SimpleTestCase):
    """区县筛选下拉不得包含「全市（市级）」。

    业务记录归属区县一律落到具体区县（`_resolve_district_scope` 明确拒绝市级），
    所以「全市（市级）」永远筛不出任何一行。它还不是「无害的空选项」——
    它排在第一位，用户很容易把它当成「全部」，选完看到空表会以为数据丢了。
    """

    def test_district_filter_options_exclude_city_level(self):
        problems = []
        for name, rel in PORTALS.items():
            source = read(rel)
            for key, label, values, expr, pos in select_filters(source):
                if key != 'district':
                    continue
                # 只查「取值来自区县接口」的下拉。从业务数据里现推出来的
                # （如政府端物料监管按流水 district_name 去重生成）天然不含市级。
                if not re.search(r'\b(activeDistricts|districts)\b', expr):
                    continue
                if 'is_city' not in expr:
                    line = source[:pos].count('\n') + 1
                    problems.append(f'{rel}:{line} 「{label}」下拉未过滤市级区县')
        self.assertFalse(
            problems,
            '以下区县筛选下拉含「全市（市级）」死选项，请补 `.filter(d => !d.is_city)`：\n  '
            + '\n  '.join(problems))


class FilterValueSourceTest(SimpleTestCase):
    """筛选值必须走 `TNR_UI.getFilterValues()`，不得回 DOM 里捞。

    这不是风格问题：各 portal 的 `.page-view` **全部常驻 DOM**（切页只切 `.active`），
    所以任何「全局选择器 + 读控件当前值」的写法都会命中别的页面里的控件，
    而那个控件此时是默认态 —— 表现为「筛选条怎么选都不变」或「选什么都空」。
    """

    # 全局裸查：document.querySelector('[data-filter=...]')
    BARE_GLOBAL = re.compile(r"document\.querySelector(?:All)?\(\s*['\"]\[data-filter")
    # 把下拉的显示文本当数据源（而非 getFilterValues 给出的 value）
    DOM_TEXT = re.compile(r'\.selectedOptions\b')

    def test_filter_values_not_read_from_dom(self):
        problems = []
        for name, rel in PORTALS.items():
            source = strip_js_comments(read(rel))
            for rx, why in (
                (self.BARE_GLOBAL,
                 '全局裸查 [data-filter] —— 会命中文档里第一个同类控件（往往是别的'
                 '页面那个），筛选结果与用户实际所选无关'),
                (self.DOM_TEXT,
                 '用 selectedOptions 读下拉显示文本当数据源 —— 应改用 '
                 'getFilterValues() 的 value，并自行完成 id→名称换算'),
            ):
                for m in rx.finditer(source):
                    line = source[:m.start()].count('\n') + 1
                    problems.append(f'{rel}:{line} {why}')
        self.assertFalse(
            problems,
            '以下位置把 DOM 当成筛选数据源（各 .page-view 常驻 DOM，必然串页）：\n  '
            + '\n  '.join(problems))


class TableHeightFloorTest(SimpleTestCase):
    """列表高度下限（**固定 10 行**）必须**实测**前 10 行的累计高度，不得用「首行行高 × 10」估算。

    表格里长文本会折行，同一张表的行高并不均匀 —— 实测「全量台账中心 / 捕捉台账」
    首行 69.78px、第 5/7/10 行 76.19px：按首行估出「10 行刚好 742px」，
    而实际需要 767px，于是第 10 行被裁掉、只完整显示 9 行，还凭空多出一条 25px 的
    内部滚动条。**界面上完全不报错**（表格照常渲染），只有量「可视行数」才发现得了 ——
    渲染侧的守卫是 `gui-test-scripts/42_lists_inventory.js` 的「被压扁」栏
    （该栏原先留了 1 行容差，恰好把这一行放过去，已一并收紧）。
    """

    # 估算写法：`const MIN = ... rowH * <数字|变量>`
    ESTIMATED_FLOOR = re.compile(r'\bMIN\s*=\s*[^;]*\browH\s*\*')
    # 正确写法：某一行的 `.bottom` 减去 tbody 的 `.top` —— 即前 N 行的真实累计高度。
    # ⚠ 不能只写 `getBoundingClientRect().bottom`：同方法里 `cardBottom` 那行也含它，
    # 断言会被无关代码满足（本测试第二版就踩到，变异测试才发现咬不住）。
    MEASURED = re.compile(
        r'\.bottom\s*-\s*[^;]{0,80}?getBoundingClientRect\(\)\s*\.top')

    def _fit_body(self):
        """返回 (源码, `_fitTableHeight` 方法体, 方法体在源码里的起始下标)。

        第三个值是**必需的**：方法体的匹配偏移是相对方法体的，直接拿去索引整份源码
        会算出离谱的行号（本测试第一版就踩到 —— 494 行报成 76 行），
        把看报错的人带到完全无关的位置。注释已剥离，避免说明文字把断言骗过。
        """
        source = strip_js_comments(read('static/js/tnr-common.js'))
        m = re.search(r'(?m)^  _fitTableHeight\s*\([^)]*\)\s*\{', source)
        self.assertIsNotNone(
            m, '未在 static/js/tnr-common.js 找到 _fitTableHeight —— 本测试需要同步更新')
        base = source.index('{', m.start())
        body = brace_body(source, base)
        self.assertIsNotNone(body, '_fitTableHeight 方法体的花括号不配对')
        return source, body, base

    def test_floor_is_measured_not_estimated(self):
        source, body, base = self._fit_body()
        self.assertTrue(
            self.MEASURED.search(body),
            '列表高度下限必须**实测**前 N 行的累计高度：取第 N 行的 '
            '`getBoundingClientRect().bottom` 减去 tbody 顶部。\n'
            '不能用「首行行高 × N」估算 —— 行高随内容折行而变，'
            '估算会少算几十像素，把最后一行裁掉且不报错。')
        m = self.ESTIMATED_FLOOR.search(body)
        if m:
            self.fail(
                'static/js/tnr-common.js:%d 仍在用「行高 × N」估算列表高度下限。\n'
                '表格行高不齐时（长文本折行）会少算几十像素 → 最后一行被裁、'
                '并多出一条内部滚动条；实测「全量台账中心 / 捕捉台账」少显示 1 行。\n'
                '请改成实测第 N 行的下沿。'
                % (source[:base + m.start()].count('\n') + 1))

    # 下限的行数常量：`const FLOOR_ROWS = 10`
    FLOOR_CONST = re.compile(r'\b(?:FLOOR_ROWS|MIN_ROWS)\s*=\s*(\d+)')

    def test_floor_is_fixed_ten_rows(self):
        """下限是**固定的 10 行**，不得随分页的「条/页」控件变化。

        曾误写成「取当前每页条数」，后果很隐蔽：**一页渲染的行数 = 每页条数**，
        于是「自然高」恒等于「一页的行高」→ `MIN ≈ natural` → 容差判断永远判成
        「内容放得下」→ **下限彻底失效**（实测 20 条/页 时列表长到 1017px，比视口还高）。
        行数 > 50 的表在 50 条/页 下更会算出 50 行的下限（约 2500px），列表被顶出屏幕。
        「每页条数」只决定一页渲染多少行，**不决定列表该有多高**。
        """
        _, body, _ = self._fit_body()
        # 用 assertFalse 而不是 assertNotIn —— 后者会把整个方法体打进报错，
        # 几百行代码把真正的提示信息埋掉，看报错的人根本找不到重点。
        self.assertFalse(
            'dt-pagesize' in body,
            '列表高度下限读了分页的「条/页」控件 —— 下限会随每页条数一起变大，\n'
            '而「自然高」恒等于「一页的行高」，两者几乎相等 → 容差判断永远判成\n'
            '「内容放得下」→ 下限彻底失效（实测 20 条/页 时列表长到 1017px，比视口还高）。\n'
            '「每页条数」只决定一页渲染多少行，不决定列表该有多高。')
        m = self.FLOOR_CONST.search(body)
        self.assertIsNotNone(
            m, '未找到列表高度下限的行数常量（如 `const FLOOR_ROWS = 10;`）'
               '—— 本测试需要同步更新')
        self.assertEqual(
            m.group(1), '10',
            f'列表高度下限是 {m.group(1)} 行，应为 **10 行**'
            '（要求：「列表的高度按照默认 10 行来设置」）。')

    def test_floor_covers_wrapper_border(self):
        """限高值要含 wrapper 自身边框：max-height 作用在**边框盒**上。"""
        _, body, _ = self._fit_body()
        self.assertRegex(
            body, r'borderTopWidth|offsetHeight\s*-\s*wrap\.clientHeight',
            '列表高度下限没把 wrapper 自身的上下边框算进去。\n'
            'max-height 作用在**边框盒**上（实测 maxHeight=744 → clientHeight=742），'
            '少算这 2px 会让最后一行被裁掉一点点，并造出假滚动条。')


class ManageableRolesContractTest(SimpleTestCase):
    """「谁能建哪些角色的账号」前后端必须同一套，后端是**唯一真源**。

    这里锁的是一类真实漏洞：`GovPortal.canCreateRole()` 早就把规则写在界面上
    （只是把角色下拉的选项过滤掉），而 `supervision.user_create`
    **没有任何对应校验** —— 实测区级管理员直接 POST `role=gov_city` +
    市级 `district_id` 就建出了一个**市级管理员**账号（口令还是他自己填的），
    登录后 `get_district_scope()` 返回 `None`，**全部区县的数据都看得到**。

    这类「前端做了、后端没做」的校验特别难被发现：界面上根本点不出这个选项，
    所以怎么点都正常，**只有直接打接口才会暴露**。所以两侧必须用一条断言绑死：
    改一侧不改另一侧就红。
    """

    # `if (this.currentUser.role === 'gov_city') return ['a', 'b'].includes(role);`
    PATTERN = re.compile(
        r"this\.currentUser\.role\s*===\s*'(\w+)'\)\s*return\s*\[([^\]]*)\]")

    def _frontend_roles(self):
        source = read(PORTALS['gov'])
        m = re.search(r'canCreateRole\(role\)\s*\{', source)
        self.assertIsNotNone(m, 'gov portal 里找不到 canCreateRole —— 本测试需要同步更新')
        body = brace_body(source, source.index('{', m.start()))
        self.assertIsNotNone(body, 'canCreateRole 方法体的花括号不配对')
        return {
            role: [x.strip().strip('\'"') for x in roles.split(',') if x.strip()]
            for role, roles in self.PATTERN.findall(body)
        }

    def test_frontend_role_lists_match_backend(self):
        self.assertEqual(
            self._frontend_roles(),
            {role: list(roles) for role, roles in MANAGEABLE_ROLES.items()},
            '前端 `canCreateRole()` 与后端 `services.MANAGEABLE_ROLES` 不一致。\n'
            '后端才是真正生效的那一份 —— 前端只过滤下拉选项，改错了不会有任何提示，\n'
            '只会让界面上少一个（或凭空多一个）角色选项。')

    def test_backend_roles_cover_every_non_adopter_role(self):
        """市级管理员必须能建出全部非领养人角色，否则会「建不出第二个管理员」。"""
        self.assertEqual(
            set(MANAGEABLE_ROLES['gov_city']),
            {'gov_city', 'gov_district', 'shelter', 'hospital'},
            '市级管理员可建角色集合变了 —— 领养人不在政府端创建，其余四种都应可建。')


class InstitutionFormScopeContractTest(SimpleTestCase):
    """「机构表单」前后端必须同一套区县规则。

    锁的是一类真实缺陷：`institution_create` 与 `institution_edit` 是**一对**
    孪生接口，校验却各写一份，然后漂移 —— 本轮实测的三处都是这个形态：

    1. `create` 拒绝「医院挂市级」，`edit` 没有 → 把医院改挂「全市（市级）」后
       `cascade_operator_district()` 把它的操作员一起搬过去，那个账号
       `district.is_city` 变 True → `get_district_scope()` 返回 None →
       **全市所有区县的数据都看得到**；
    2. `create` 没有区县范围校验，`edit` 有 → 区级管理员能在他区建机构；
    3. 前端机构表单的「所属区县」下拉排除了市级，而挂市级的捕捉点确实存在
       （现场两个捕捉点就挂在「全市（市级）」下）→ 编辑时 `<select>` 退化成
       选中第一项，**不改区县直接点保存**就把机构静默搬到了具体区县。

    这些都是「界面上点不出来、只有直接打接口（或误点保存）才暴露」的形态。
    """

    # `if new_type == 'hospital' and ... is_city:` —— create / edit 两处都要有
    HOSPITAL_CITY_GUARD = re.compile(
        r"==\s*'hospital'\s*and[\s\S]{0,120}?is_city")

    def _func_body(self, source, name):
        """取 Python 顶层函数的整段源码。

        不能用 `brace_body`：Python 函数体不靠花括号界定，函数签名之后的
        第一个 `{` 往往落在某个 dict 字面量里，配出来的是一段无关片段
        （第一版就是这么写的，四个用例全红）。
        """
        m = re.search(r'^def ' + re.escape(name) + r'\(', source, re.M)
        self.assertIsNotNone(m, f'supervision/views.py 里找不到 {name} —— 本测试需要同步更新')
        nxt = re.search(r'^(?:def |@|# =)', source[m.end():], re.M)
        end = len(source) if nxt is None else m.end() + nxt.start()
        return source[m.start():end]

    def test_create_and_edit_both_reject_city_level_hospital(self):
        source = read('supervision/views.py')
        for name in ('institution_create', 'institution_edit'):
            body = self._func_body(source, name)
            self.assertRegex(
                body, self.HOSPITAL_CITY_GUARD,
                f'`{name}` 缺少「医院不能挂市级」校验。\n'
                'create 与 edit 是一对孪生接口，校验必须两边都有 ——\n'
                '只写一边就等于另一边没写，而界面上点不出来，测不出来。')

    def test_create_and_edit_both_check_district_scope(self):
        source = read('supervision/views.py')
        for name in ('institution_create', 'institution_edit'):
            body = self._func_body(source, name)
            self.assertIn(
                '_district_out_of_scope(request', body,
                f'`{name}` 没有走 `_district_out_of_scope()` 做区县范围校验。\n'
                '两个接口必须共用同一个判据，各写一份必然会漂移。')

    def _institution_modal(self):
        source = read(PORTALS['gov'])
        m = re.search(r'showInstitutionModal\(id\)\s*\{', source)
        self.assertIsNotNone(m, 'gov portal 里找不到 showInstitutionModal —— 本测试需要同步更新')
        # 用 m.start() 定位方法体的那个 `{`：用 m.end() 会**跳过**它，
        # 从方法体内部的下一个 `{` 开始配对，取到的是一段无关片段。
        body = brace_body(source, source.index('{', m.start()))
        self.assertIsNotNone(body, 'showInstitutionModal 方法体的花括号不配对')
        return body

    def test_frontend_narrows_district_options_for_district_admin(self):
        """区级管理员的区县下拉必须收敛到本区县（服务端会拒，前端别让人白填）。"""
        body = self._institution_modal()
        self.assertRegex(
            body, r"isCityLevel\(\)[\s\S]{0,200}?currentUser\.district_id",
            '机构表单的区县下拉没有按操作员区县收敛 ——\n'
            '区级管理员会看到全部区县，选了他区才被服务端拒绝，白填一遍。\n'
            '（账号表单 `showAccountModal` 早就这么做了，机构表单漏了。）')

    def test_frontend_preserves_institution_current_district(self):
        """编辑时必须把机构当前区县兜底加回选项。

        少了这一步，挂市级的捕捉点（下拉里被 `!d.is_city` 排除）会让
        `<select>` 退化成选中第一项，**不改区县直接点保存**就把它搬到具体区县。
        """
        body = self._institution_modal()
        self.assertIn('inst.district_id', body,
                      '机构表单没有把「机构当前区县」加回下拉选项 ——\n'
                      '挂市级的捕捉点编辑保存时会被静默改判到某个具体区县。')


class NarrowedOptionContractTest(SimpleTestCase):
    """「前端把选项收窄了、后端不校验」这一类（第十七轮）。

    前两轮把 `disabled` / `readonly` 两种形态扫过一遍，本轮补第三种：
    **下拉里根本没有这个选项** —— 前端按某个规则过滤掉了候选值，
    而服务端对同一个字段不做任何校验。界面上点不出来，只有直接打接口才暴露；
    反过来，界面上**能**改的（下拉没禁用）却又是服务端要拒的，则是另一种坑：
    用户改一下就撞 400，看不出原因。

    本轮实测的三处都属于这个形态，且都带「跨模块孪生漂移」的性质：
    同一个判据在一个模块里写了、在另一个模块里漏了。
    """

    def _py_func(self, source, name):
        """取 Python 顶层函数的整段源码（不能用花括号配对，见上一轮的教训）。"""
        m = re.search(r'^def ' + re.escape(name) + r'\(', source, re.M)
        self.assertIsNotNone(m, f'找不到 {name} —— 本测试需要同步更新')
        nxt = re.search(r'^(?:def |@|# =)', source[m.end():], re.M)
        end = len(source) if nxt is None else m.end() + nxt.start()
        return source[m.start():end]

    def _js_method(self, source, pattern, label):
        m = re.search(pattern, source)
        self.assertIsNotNone(m, f'找不到 {label} —— 本测试需要同步更新')
        body = brace_body(source, source.index('{', m.start()))
        self.assertIsNotNone(body, f'{label} 的花括号不配对')
        return body

    # ---------- 一、区县级别（is_city）改动 ----------

    def test_district_edit_guards_is_city_change(self):
        """`district_edit` 改 `is_city` 必须先做引用审计。

        `get_district_scope()` 对「所属区县 is_city=True」的账号返回 None = 全部数据。
        所以把已有账号/数据的区县改成市级，等于一次性给该区县所有账号发放全市读权限，
        而且不需要碰任何账号、没有任何请求看起来像越权。
        前端这个下拉就在区县表单里（`#dist-is-city`），后端原本零守卫。
        """
        body = self._py_func(read('supervision/views.py'), 'district_edit')
        self.assertRegex(
            body, r"is_city[\s\S]{0,600}?_district_references\(",
            '`district_edit` 调整 `is_city` 时没有做引用审计。\n'
            '区县级别决定其下所有账号的可见范围，改它等于批量改权限。')

    def test_district_delete_and_edit_share_reference_helper(self):
        """删除与改级别必须共用同一份引用清单，否则会出现「删不掉但能改级别」的缝隙。"""
        source = read('supervision/views.py')
        for name in ('district_delete', 'district_edit'):
            self.assertIn(
                '_district_references(', self._py_func(source, name),
                f'`{name}` 没有走共用的 `_district_references()` —— '
                '两处各列一份清单必然会漂移。')

    # ---------- 二、停用区县 ----------

    def test_institution_create_and_edit_reject_inactive_district(self):
        """机构创建/编辑都要拒绝停用区县。

        `user_create` 早有「所选区县已停用」，机构侧没有 —— 同一模块内两条创建
        路径各写一份判据，必然漂移。前端所有区县下拉都写了 `status === 'active'`，
        但只过滤选项等于没校验。
        """
        source = read('supervision/views.py')
        for name in ('institution_create', 'institution_edit'):
            self.assertIn(
                '_inactive_district_error(', self._py_func(source, name),
                f'`{name}` 没有校验「区县已停用」。\n'
                '与 `user_create` 是同一条判据，必须共用 `_inactive_district_error()`。')

    # ---------- 三、捕捉点 ↔ 归属区县 ----------

    def test_capture_create_and_update_share_shelter_district_judgement(self):
        """捕捉单的创建与编辑必须共用同一条「捕捉点 ↔ 归属区县」判据。

        捕捉点本身挂在具体区县时，归属区县是派生值。填成他区会让这张单从
        **执行机构所在区县**的可见范围里消失（区县隔离按 `Capture.district` 过滤），
        捕捉点操作员看不到自己登记的单，既不能转运也不能作废。
        """
        source = read('business/views_capture.py')
        for name in ('capture_create', 'capture_update'):
            self.assertIn(
                '_shelter_district_conflict(', self._py_func(source, name),
                f'`{name}` 没有校验捕捉点与归属区县的一致性。\n'
                'create 与 update 是一对孪生接口，必须共用同一个判据。')

    def test_capture_create_form_locks_district_to_shelter(self):
        """新建捕捉单：捕捉点挂在具体区县时，归属区县下拉必须**禁用**。

        只做「change 时自动同步」不够 —— 同步完还能手改，等于留了一个必撞 400 的入口。
        """
        body = self._js_method(
            read(PORTALS['shelter']),
            r'function _syncDistrictFromShelter\(\)',
            'shelter portal 的 _syncDistrictFromShelter')
        # 断言必须带上右侧的取值：只写 `districtSelect.disabled =` 的话，
        # `= false` 这种「写了但没生效」的写法照样能过 —— 变异验证时它没跟着变红，
        # 才发现第一版是空转的。
        self.assertRegex(
            body, r'districtSelect\.disabled\s*=\s*locked\b',
            '捕捉单表单没有按捕捉点锁定归属区县下拉 ——\n'
            '服务端会拒绝不一致的提交，界面必须跟着禁用。')

    def test_capture_edit_form_locks_district_when_shelter_is_concrete(self):
        """编辑捕捉单：同样要锁，但历史错配数据必须留一个改回来的入口。"""
        body = self._js_method(
            read(PORTALS['shelter']),
            r'async showCaptureEdit\(',
            'shelter portal 的 showCaptureEdit')
        self.assertIn(
            'districtLocked', body,
            '编辑弹窗没有计算 `districtLocked` —— 归属区县下拉会留着可改，'
            '用户一改就撞 400。')
        self.assertRegex(
            body, r'ed_districtId[\s\S]{0,80}?\$\{districtLocked \? \'disabled\'',
            '编辑弹窗的 `#ed_districtId` 没有按 `districtLocked` 禁用。')


class InactiveEntityContractTest(SimpleTestCase):
    """「停用」状态必须在前后端两侧都真的生效（第十八轮）。

    `Institution.status` 曾在整个后端**没有任何读取点** —— 只有
    `institution_create` 写 `status='active'` 与 `institution_toggle_status`
    翻转这两处写入，业务侧一次都没读过。停用一家医院只改了列表里的一个徽标：
    新建转运单的医院下拉照旧列出它、`transfer_create` 照旧接受。

    而前端只有**政府端**的机构下拉过滤了 `status === 'active'`，捕捉点端三处
    漏了 —— 同一条规则只写了一半，就成了跨端漂移。服务端两侧都没有，
    所以「停用」在整条链路上等于没写。
    """

    def _py_func(self, source, name):
        m = re.search(r'^def ' + re.escape(name) + r'\(', source, re.M)
        self.assertIsNotNone(m, f'找不到 {name} —— 本测试需要同步更新')
        nxt = re.search(r'^(?:def |@|# =)', source[m.end():], re.M)
        end = len(source) if nxt is None else m.end() + nxt.start()
        return source[m.start():end]

    def test_business_create_paths_reject_inactive_institution(self):
        """新建捕捉单 / 新建转运单都要走共用的 `inactive_institution_error()`。"""
        for path, name in (('business/views_capture.py', 'capture_create'),
                           ('business/views_transfer.py', 'transfer_create')):
            self.assertIn(
                'inactive_institution_error(', self._py_func(read(path), name),
                f'`{name}` 没有校验机构是否已停用 ——\n'
                '「停用」必须真的拦住新业务，否则它只是个徽标。')

    def test_inactive_judgement_has_single_implementation(self):
        """判据只能有一份实现，其余是薄封装。"""
        services = read('business/services.py')
        self.assertIn('def inactive_institution_error(', services,
                      '服务层没有 `inactive_institution_error()`')
        self.assertIn('def inactive_district_error(', services,
                      '服务层没有 `inactive_district_error()`')
        body = self._py_func(read('supervision/views.py'), '_inactive_district_error')
        # 断言必须落在**真正的委托语句**上。第一版只写了
        # `assertIn('inactive_district_error(', body)` —— 结果**文档字符串里那句
        # `services.inactive_district_error()`** 就把它命中了：把实现改回各写一份
        # 照样能过（变异验证时 M8 没跟着变红才发现）。断言越像「检查某个写法
        # 出现过」越容易空转。
        self.assertRegex(
            body, re.compile(r'^\s+return inactive_district_error\(district\)', re.M),
            '`_inactive_district_error` 没有委托给服务层的同一份实现 ——\n'
            '同一个概念散在多处就是漂移的温床。')
        self.assertNotIn(
            "district.status != 'active'", body,
            '`_inactive_district_error` 里又出现了就地判断 ——\n'
            '它应当只是服务层判据的薄封装。')

    def test_shelter_portal_excludes_inactive_institutions(self):
        """捕捉点端的机构下拉必须与政府端一样排除停用机构。"""
        html = read(PORTALS['shelter'])
        found = (html.count("i.status === 'active'")
                 + html.count("h.status === 'active'"))
        self.assertGreaterEqual(
            found, 3,
            '捕捉点端至少三处机构下拉要排除停用机构：新建捕捉单的捕捉点、\n'
            '转运单与下发出库的接收医院。政府端早就过滤了，只写一半就是跨端漂移。')

    def test_capture_district_dropdowns_exclude_inactive_district(self):
        """区县下拉要排除停用区县，且必须留「当前值」的兜底。"""
        html = read(PORTALS['shelter'])
        self.assertGreaterEqual(
            html.count("d.status === 'active'"), 2,
            '捕捉点端的区县下拉（新建 / 编辑）没有排除停用区县 ——\n'
            '服务端会拒，界面上却选得到。')
        # 只过滤会让「当前值」从下拉里消失 → select 退到第一个选项 →
        # 提交的区县被静默改掉，或者撞 400 而用户无法修正。
        self.assertRegex(
            html,
            r"d\.status === 'active' \|\| d\.id === "
            r"(myInstitution\?\.districtId|full\.districtId)",
            '区县下拉过滤停用区县时没有保留「当前值」的兜底 ——\n'
            '存量记录的下拉会退到第一个选项，归属区县被静默改掉。')

class ShelterStockAdjustmentEntryTest(SimpleTestCase):
    """捕捉点必须**有**库存异动入口（第三十七轮补的权限缺口）。

    背景：`expiry_date` 判据上线后，捕捉点库存里的过期物料既不能用于诊疗、
    也不能下发（下发时判据会拦）；而 `stock_adjustment` 原先的角色白名单里
    **没有 shelter** —— 那些物料成了「既不能报废、也不能用」的死库存。

    服务端放开角色只是**一半**：没有界面入口，捕捉点操作员依然无从下手。
    所以这里同时钉住两端，且**都从源码取证**（不靠人记得改过）。
    """

    def test_backend_decorator_includes_shelter(self):
        src = read('business/views_material.py')
        idx = src.find('def stock_adjustment(request):')
        self.assertGreater(idx, 0, '找不到 stock_adjustment 视图')
        # 只看紧邻的装饰器区（往上 400 字符足够覆盖三层装饰器）
        decorators = src[max(0, idx - 400):idx]
        self.assertIn(
            "@role_required('shelter', 'hospital', 'gov_city', 'gov_district')",
            decorators,
            'stock_adjustment 的角色白名单里没有 shelter —— 捕捉点的过期物料'
            '就没有任何正规出口（既不能报废、也不能下发）。')

    def test_shelter_portal_has_adjustment_entry(self):
        html = read(PORTALS['shelter'])
        self.assertIn(
            'data-tab="adjustment"', html,
            '捕捉点门户缺少「库存异动」标签页 —— 服务端开了权限，'
            '操作员却没有任何入口可用。')
        self.assertIn(
            'saveStockAdjustment', html,
            '捕捉点门户缺少提交异动的处理函数')
        self.assertIn(
            'TNR_API.adjustStock(', html,
            '捕捉点门户没有真正调用异动接口')


def css_rule(css, selector):
    """取某条 CSS 规则的花括号内容（只看精确匹配的选择器）。

    用 `re.escape(selector) + r'\\s*\\{'` 而不是 `find`：`.sig-preview` 还出现在
    `.sig-preview img` / `.sig-preview .sig-placeholder` 里，用 find 会取错规则。
    """
    m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
    return m.group(1) if m else None


class SignatureModalFieldTest(SimpleTestCase):
    """手写签名：页面只预览，手写在弹窗里（第三十九轮）。

    磊哥反馈的真实故障：手机端手指落在签名区想上下滑动翻页，结果全被当成笔画。
    根因是画布**必须**写 `touch-action: none`（不写就一个笔画也画不出来），
    而这一条会整块吞掉触屏滚动手势。修法是把画布搬进弹窗 —— 弹窗里本来就不需要
    滚动，那里才是 `touch-action: none` 的正确位置。

    所以这些用例锁的是「**位置**」而不只是「有没有写」：
    同一行 `touch-action: none` 放在页面预览块上就是缺陷，放在弹窗画布上才对。
    """

    SHELTER = PORTALS['shelter']
    COMMON_JS = 'static/js/tnr-common.js'
    CSS = 'static/css/tnr-traditional.css'
    FIELD_IDS = ('ca_signature', 'ed_signature', 'or_signature',
                 'rc_signature', 'cf_signature')

    # --- 模板：不能再有页面内直画的画布 ---

    def test_no_template_keeps_an_inline_canvas(self):
        """页面上不允许再出现直画的 canvas —— 它就是吞掉滚动手势的那一块。"""
        for name, rel in PORTALS.items():
            html = read(rel)
            self.assertNotIn('class="signature-pad"', html,
                             f'{name} 门户还有页面内直画的签名画布；'
                             '它带 touch-action:none，会吞掉手机端的上下滑动手势。')
            self.assertNotIn('TNR_UI.initSignaturePad(', html,
                             f'{name} 门户还在调旧的 initSignaturePad')

    def test_every_signature_field_target_exists_and_is_empty(self):
        html = read(self.SHELTER)
        targets = re.findall(
            r"initSignatureField\(\s*document\.getElementById\('([^']+)'\)", html)
        for tid in self.FIELD_IDS:
            self.assertIn(tid, targets, f'捕捉点门户缺少签名字段 #{tid}')
        for tid in targets:
            # 组件会整块重写容器的 innerHTML，容器里预置的标记会被**静默抹掉**
            self.assertIn(f'<div class="sig-field" id="{tid}"></div>', html,
                          f'#{tid} 不是空的 sig-field 容器')

    def test_no_orphan_clear_buttons(self):
        """5 个独立的「清除签名」按钮已随组件移除。

        如果只删按钮、留下 `getElementById(...).addEventListener`，
        页面初始化时会**抛未捕获 TypeError**，后面所有绑定全部不执行 ——
        表现是「整个捕捉页的按钮都没反应」，且控制台之外看不到任何提示。
        """
        html = read(self.SHELTER)
        for old in ('btnClearSig', 'btnClearEditSig', 'orClearSig',
                    'rcClearSig', 'cfClearSig'):
            self.assertNotIn(old, html,
                             f'{old} 已移除；若仍被 getElementById 引用会中断页面初始化')

    # --- 组件：API 与旧接口兼容 ---

    def test_old_api_is_gone_and_new_one_is_present(self):
        src = read(self.COMMON_JS)
        self.assertNotIn('initSignaturePad(', src, '旧的 initSignaturePad 应已删除')
        for token in ('initSignatureField(el, opts)',
                      '_bindSigCanvas(canvas)',
                      '_openSignatureModal(',
                      '_closeSignatureModal(',
                      '_drawSigImage('):
            self.assertIn(token, src, f'tnr-common.js 缺少 {token}')

    def test_component_keeps_backward_compatible_api(self):
        """调用点靠 isEmpty/getDataURL 提交，名字变了就会静默传空。"""
        src = read(self.COMMON_JS)
        idx = src.find('initSignatureField(el, opts)')
        self.assertGreater(idx, 0)
        body = src[idx:idx + 4000]
        for method in ('isEmpty()', 'getDataURL()', 'isDirty()', 'clear()', 'setValue('):
            self.assertIn(method, body, f'签名组件缺少 {method}')

    def test_edit_path_only_uploads_when_actually_resigned(self):
        """「留空则保留」：没重新签就不该重传 —— 否则每存一次就多一份签名图。"""
        html = read(self.SHELTER)
        self.assertIn("if (sigPad.isDirty() && !sigPad.isEmpty()) fd.append('signature'",
                      html,
                      '编辑捕捉记录时会无条件重传签名，media/ 里会不断堆积重复图片')

    # --- CSS：手势归属是关键判据 ---

    def test_preview_does_not_swallow_touch_scroll(self):
        css = read(self.CSS)
        rule = css_rule(css, '.sig-preview')
        self.assertIsNotNone(rule, 'CSS 里找不到 .sig-preview 规则')
        self.assertNotIn('touch-action: none', rule,
                         '.sig-preview 带 touch-action:none —— 手机端在签名区上滑'
                         '会变成画笔画，正是磊哥反馈的那个故障。')

    def test_modal_canvas_owns_the_touch_gestures(self):
        css = read(self.CSS)
        rule = css_rule(css, '.sig-canvas')
        self.assertIsNotNone(rule, 'CSS 里找不到 .sig-canvas 规则')
        self.assertIn('touch-action: none', rule,
                      '弹窗画布必须写 touch-action:none，否则画不出笔画')

    def test_signature_modal_sits_above_the_page_modal(self):
        """签名弹窗是从「编辑捕捉记录」等弹窗里开出来的。

        `TNR_UI.modal` 把 overlay 的 z-index 设成 **1500**（内联样式），
        签名弹窗低于它就会被压在下面 —— 用户点得到、但看不见，也点不到按钮。
        """
        css = read(self.CSS)
        rule = css_rule(css, '.sig-modal')
        self.assertIsNotNone(rule, 'CSS 里找不到 .sig-modal 规则')
        m = re.search(r'z-index:\s*(\d+)', rule)
        self.assertIsNotNone(m, '.sig-modal 没写 z-index')
        self.assertGreater(int(m.group(1)), 1500,
                           '签名弹窗的 z-index 必须高于 TNR_UI.modal 的 1500')

