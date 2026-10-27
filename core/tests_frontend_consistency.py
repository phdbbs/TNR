"""前后端契约一致性回归测试。

这些用例不启动浏览器，而是把「模板/JS 源码」当作被测对象做静态断言。
它们锁定的是第五轮走查发现的一类缺陷——**前端自己维护的枚举映射表与后端
模型 choices 脱节**，表现为界面上直接漏出 `pending_claim`、`receive` 这样的
英文枚举码，或状态缺失时被静默兜底成某个具体业务状态（「状态全是已完成」）。

新增后端枚举值 / 新增接口时，这些测试会失败并指向需要同步的前端文件。
"""
import ast
import glob
import os
import re
import sys

from django.template import Context, Engine
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from html.parser import HTMLParser

from accounts.models import User
from accounts.views import ROLE_PORTAL_MAP
from business.models import (Adoption, AdoptionApplication, Capture, CheckIn, Chip,
                             MaterialTransaction, Pet, Release, Treatment, Transfer)
from business.services import MANAGEABLE_ROLES
from business.services_treatment import (
    STERILIZATION_FIELDS, STERILIZATION_REQUIRED,
)
from core.models import Institution, STATUS_ACTIVE, STATUS_LABELS, STATUS_VALUES
from core.passwords import DEMO_INITIAL_PASSWORD
from supervision.doc_maintenance import DOC_LABELS, REASON_MAX

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `scripts/jslex.py` 提供「只抹注释、保留字符串字面量」的剥离器。本文件里
# 断言**用户可见文案**的用例都必须用它 —— 见 `MarkdownLeakInUiCopyTest`。
_SCRIPTS = os.path.join(ROOT, 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from jslex import blank_comments  # noqa: E402

PORTALS = {
    'shelter': 'templates/portal/shelter/portal.html',
    'hospital': 'templates/portal/hospital/portal.html',
    'gov': 'templates/portal/gov/portal.html',
    'adopter': 'templates/portal/adopter/portal.html',
    # 第四十二轮新增：全局设置（机构/区县/账号/编号规则/公告/日志）迁到平台端
    'platform': 'templates/portal/platform/portal.html',
}

#: 各端**应用逻辑**的位置（第五十二轮 重构 B：从模板里的内联 `<script>` 抽出来）。
PORTAL_JS = {
    'shelter': 'static/js/portals/shelter.js',
    'hospital': 'static/js/portals/hospital.js',
    'gov': 'static/js/portals/gov.js',
    'adopter': 'static/js/portals/adopter.js',
    'platform': 'static/js/portals/platform.js',
}

FRONTEND_FILES = list(PORTALS.values()) + list(PORTAL_JS.values()) + [
    'static/js/tnr-api.js',
    'static/js/tnr-common.js',
]

#: 模板路径 → 同端 JS 路径（`read()` 的兼容层用）
_JS_SIBLING = {v: PORTAL_JS[k] for k, v in PORTALS.items()}


def _read_raw(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def read(rel):
    """读前端源码；给的是**门户模板**时自动拼上同端的 JS。

    兼容层的理由：本文件 25 处 `read(PORTALS[...])` 断言的是「这段代码必须存在」，
    它不该关心代码住在模板还是 `static/` 里。JS 拼在**末尾**，
    所以模板自身的行号与 `strip_js_comments` 的等长替换都不受影响。

    ⚠ 只关心 HTML 的判据（例如「角色 base 里不得再有 `<style>`」）请改用
    `read_html()`；只关心 JS 的请用 `read_js()`。新写的判据优先用这两个窄口，
    别默认走 `read()` —— 合并视图会让「X 不得出现」这类断言跨文件生效。
    """
    text = _read_raw(rel)
    sibling = _JS_SIBLING.get(rel)
    return text + '\n' + _read_raw(sibling) if sibling else text


def read_html(role):
    """只读门户模板（不含 JS）。"""
    return _read_raw(PORTALS[role])


def read_js(role):
    """只读该端的应用逻辑（`static/js/portals/<role>.js`）。"""
    return _read_raw(PORTAL_JS[role])


def _close_brace(source, start):
    """从 `source[start] == '{'` 找到配对的 `}` 下标；找不到返回 -1。

    跳过字符串字面量里的括号（`'{'` 不算开、`'}'` 不算合）。
    """
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
                return i
        i += 1
    return -1


def find_object_literal(source, var_name, occurrence=0):
    """返回 `const/let/var <var_name> = { ... }` 里 `{...}` 的完整文本。

    :param occurrence: 同名变量第几次出现（模板里 typeMap 会被多次声明）
    """
    pattern = re.compile(
        r'(?:const|let|var)\s+' + re.escape(var_name) + r'\s*=\s*\{')
    matches = list(pattern.finditer(source))
    if len(matches) <= occurrence:
        return None
    end = _close_brace(source, matches[occurrence].end() - 1)
    if end < 0:
        return None
    return source[matches[occurrence].end() - 1:end + 1]


def find_property_literal(source, prop_name, occurrence=0):
    """返回对象里 `<prop_name>: { ... }` 这条属性的字面量文本。

    给「挂在 `TNR_UI` 上的枚举真源」用 —— 它不是 `const X = {`，
    `find_object_literal` 那种写法匹配不到，而匹配不到 == 判据静默全绿。
    """
    pattern = re.compile(
        r'(?:^|[{,\s])' + re.escape(prop_name) + r'\s*:\s*\{', re.M)
    matches = list(pattern.finditer(source))
    if len(matches) <= occurrence:
        return None
    start = matches[occurrence].end() - 1        # 正则已吃到那个 `{`
    end = _close_brace(source, start)
    if end < 0:
        return None
    return source[start:end + 1]


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
    """物料流水类型的显示口径必须**只有一份**，且覆盖后端全部 choices。

    原始缺陷：`receive`（医院签收下发物料时生成）曾被四张映射表同时漏掉，
    捕捉点/医院/监管三端的物料台账把它显示成英文 `receive`，筛选下拉也选不到。
    修法原本是「把四张表都补一遍」—— 但只要还是 N 份，下一次新增取值同样会漏。

    第五十二轮 重构 B 把它收成一份真源（`TNR_UI.MATERIAL_TXN_TYPES`，
    在 `static/js/tnr-common.js`），所以这里的判据也跟着换了方向：
      - 正向：真源覆盖后端全部 choices（缺一个 = 界面漏英文码）；
      - 反向：**各端不得再自己声明**一张物料类型表（出现即红，见
        `test_no_portal_redeclares_material_type_map`）—— 没有这条，
        「同源」会在下一次顺手写个局部 `typeMap` 时悄悄失效，
        而三端叫法不一的老问题（`adjustment` 在监管端曾显示成「调整」）就回来了。
    """

    #: 唯一真源所在文件（共享运行时，五端都加载）
    ENUMS_REL = 'static/js/tnr-common.js'
    #: 真源里的表名
    ENUMS_VAR = 'MATERIAL_TXN_TYPES'
    #: 视角差异表：只有 `dispatch` 在医院端换说法，键必须 ⊂ 真源键
    VIEW_VAR = 'MATERIAL_TXN_VIEW_LABELS'

    def _literal(self, var, source=None):
        """取真源里 `VAR: { … }` 这条属性字面量（不是 `const VAR = {`）。"""
        src = self._enums_source() if source is None else source
        return find_property_literal(src, var)

    def _enums_source(self):
        return _read_raw(self.ENUMS_REL)

    def _backend_types(self):
        return {c[0] for c in MaterialTransaction._meta.get_field('type').choices}

    def test_single_source_covers_all_material_types(self):
        body = self._literal(self.ENUMS_VAR)
        self.assertIsNotNone(
            body, f'{self.ENUMS_REL} 里找不到 `{self.ENUMS_VAR}: {{…}}` —— '
                  '真源被改名或搬走时，所有同源判据都会变成「查不到所以全绿」，'
                  '所以这里必须红')
        keys = object_keys(body)
        expected = self._backend_types()
        self.assertTrue(expected, '后端未定义物料流水类型 choices')
        missing = expected - keys
        self.assertFalse(
            missing,
            f'物料流水类型真源缺 {sorted(missing)}；这些流水在界面上会直接显示'
            f'英文枚举码。（后端全部取值：{sorted(expected)}）')
        extra = keys - expected
        self.assertFalse(
            extra,
            f'物料流水类型真源多出后端没有的取值 {sorted(extra)}：筛选下拉会给出'
            '一个永远查不到、且提交即被拒的选项')

    def test_single_source_entries_are_fully_populated(self):
        """每条都要有 `long` / `short` / `badge` —— 缺一个就退回显示枚举码。

        `long`/`short` 各自有用到（台账用全称、紧凑列与详情抽屉用短称），
        只写一半会在另一个调用点渲染成 `undefined`。
        """
        body = self._literal(self.ENUMS_VAR)
        self.assertIsNotNone(body, f'{self.ENUMS_VAR} 没找到，判据失效')
        for typ in sorted(self._backend_types()):
            m = re.search(r'\b' + re.escape(typ) + r'\s*:\s*\{', body)
            self.assertIsNotNone(m, f'真源里没有 {typ} 这一条')
            span = body[m.end() - 1:_close_brace(body, m.end() - 1) + 1]
            for field in ('long', 'short', 'badge'):
                self.assertRegex(
                    span, r'\b' + field + r'\s*:\s*[\'"`][^\'"`]+[\'"`]',
                    f'真源的 {typ} 缺 {field}（或写成了空串）：{span.strip()}')

    def test_perspective_override_only_renames_known_types(self):
        """视角表里的键必须是真源已有的类型。

        写错一个字母（`dispath`）不会报错，只会**静默不生效** ——
        医院端又显示成捕捉点视角的叫法，而没人会去查这张表。
        """
        view = self._literal(self.VIEW_VAR)
        self.assertIsNotNone(
            view, f'{self.ENUMS_REL} 里找不到 `{self.VIEW_VAR}` —— '
                  '医院端的「捕捉点下发」视角差异必须有显式登记处')
        known = object_keys(self._literal(self.ENUMS_VAR))
        # 结构是 { hospital: { dispatch: {…} } }：object_keys 只取顶层（视角名），
        # 再对每个视角内部取一层键。
        for view_name in object_keys(view):
            inner = re.search(view_name + r"?\s*:\s*\{", view)
            self.assertIsNotNone(inner, f'视角 {view_name} 没有对象字面量')
            span = view[inner.end() - 1:_close_brace(view, inner.end() - 1) + 1]
            for typ in object_keys(span):
                self.assertIn(
                    typ, known,
                    f'{self.VIEW_VAR}.{view_name}.{typ} 在真源里没有这个类型'
                    '（多半是键名写错），它不会报错，只会静默不生效')

    def test_no_portal_redeclares_material_type_map(self):
        """反向对照：任何一端的应用 JS 里都不许再出现自己的物料类型表。

        历史形态有三种（都在这条判据里堵住）：
          - `const typeMap = { purchase: '采购入库', … }`
          - `const typeMap = { purchase: ['采购入库', 'success'], … }`
          - `const matTxnTypes = [ { value: 'purchase', label: '采购入库' }, … ]`
        """
        offenders = []
        for role in PORTALS:
            src = blank_comments(read_js(role))     # 注释里提旧写法不算抄
            for var, idx, keys in self._material_type_maps(src):
                offenders.append(f'{role}: {var}[{idx}] = {sorted(keys)}')
            # 选项表形态：同一文件里出现 ≥3 个 `value: '<类型>'` 就是又抄了一份
            values = set(re.findall(r"value:\s*'(purchase|dispatch|receive|"
                                    r"consume|adjustment|outbound)'", src))
            if len(values) >= 3:
                offenders.append(f'{role}: 自带类型下拉 {sorted(values)}')
            for label in ('采购入库', '捕捉点下发', '签收入库'):
                if re.search(r"[\[{]\s*'?%s'?\s*[:,]" % label, src) or \
                        re.search(r"label:\s*'%s'" % label, src):
                    offenders.append(f'{role}: 硬编码类型文案「{label}」')
        self.assertEqual(
            offenders, [],
            '物料流水类型又出现了第二份真源（应统一走 '
            'TNR_UI.materialTxn / materialTxnBadge / materialTxnOptions）：\n  '
            + '\n  '.join(offenders))

    def _material_type_maps(self, source):
        """找出**给定源码**里所有「物料类型映射表」及其键集合。"""
        found = []
        for var in ('typeMap', 'txnTypeMap', 'matTxnTypes'):
            idx = 0
            while True:
                body = find_object_literal(source, var, idx)
                if body is None:
                    body = find_property_literal(source, var)
                if body is None:
                    break
                keys = object_keys(body)
                # 只认物料类型映射：必须含 purchase/dispatch 这类物料流水取值
                if {'purchase', 'dispatch'} <= keys:
                    found.append((var, idx, keys))
                idx += 1
        return found

    def test_material_call_sites_pass_a_known_form_and_view(self):
        """调用真源时传的 `form` / `view` 必须是已登记的那几个值。

        判据管住了「真源里每条都有 long/short」，却管不住调用点写错：
        `materialTxn(t, 'shot')` 以前会渲染成字面量 `undefined`
        （node 里实跑出来的），现在退回显示枚举码 —— 但枚举码漏在界面上
        同样是被禁止过的缺陷（`receive` 就漏过一次）。所以参数也得机器查。
        """
        known_forms = {'long', 'short'}
        known_views = set(PORTALS)
        bad = []
        for role in PORTALS:
            for call in re.finditer(
                    r"materialTxn(?:Badge|Options)?\s*\(([^)]*)\)", read_js(role)):
                args = re.findall(r"'([^']*)'", call.group(1))
                # 第一个字面量参数按出口不同：Badge 是 type（变量，不在此列），
                # Options 是 form。这里只看形如 long/short/视角名 的那几个。
                for a in args:
                    if a and a not in known_forms and a not in known_views:
                        bad.append(f'{role}: {call.group(0).strip()} 里的 {a!r}')
        self.assertEqual(
            bad, [],
            '物料类型真源的调用点传了未登记的 form/view（拼错的 form 会让整列'
            '退回显示枚举码）：\n  ' + '\n  '.join(bad))

    def test_material_filter_uses_the_single_source(self):
        """用到这个枚举的端都必须走真源的三个出口之一。

        筛选是用户唯一能主动枚举流水类型的入口：它和表格列不同源时，
        「选得到却显示成英文码」或「显示得对却筛不到」都会出现。
        每端该出现哪些出口是显式登记的 —— 少一个就是有人改回了本地表。
        """
        expected = {
            # 捕捉点：物资流向有类型下拉 + 台账/抽屉两种列
            'shelter': ('materialTxnOptions', 'materialTxnBadge', 'materialTxn('),
            # 监管：类型下拉 + 台账徽章
            'gov': ('materialTxnOptions', 'materialTxnBadge'),
            # 医院：只有出入库台账的徽章（它的物料区用页签分门别类，没有类型下拉）
            'hospital': ('materialTxnBadge',),
        }
        for role, names in expected.items():
            src = read_js(role)
            for n in names:
                self.assertRegex(
                    src, n.replace('(', r'\s*\('),
                    f'{role}: 没有走 TNR_UI.{n}（真源）—— '
                    '要么是被改回本地表，要么是判据登记的出口清单过期了')


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

    # 捕捉点端「通用状态徽章」那张本地表已在第五十二轮巡检（续）逐字段拆光：
    # 宠物 → `PET_STATUS`、领养单 → `ADOPTION_STATUS`、转运/诊疗/捕捉 →
    # `TRANSFER_STATUS` / `TREATMENT_STATUS` / `CAPTURE_STATUS`（见
    # `CaptureTransferTreatmentSingleSourceTest`），`Shelter.statusBadge` 本身也已删除。
    # 本类原来那三条 `_assert_covers(..., 'map'/'m')` 判据锚的就是这些本地表，
    # 故一并删除 —— 覆盖性判据搬到真源上，比在六份抄本里各查一遍更有效。

    def test_shelter_pet_status_badge_covers_pet(self):
        """宠物状态徽章由真源给出：取值函数必须读 `TNR_UI.PET_STATUS`。

        原先这里查的是 `tnr-api.js` 里那两张 `const map`（文案一张、配色一张）。
        真源合并后那两张表已删除，取值函数随后也搬进了 `tnr-common.js`
        （`petStatusText` / `petStatusBadge`）—— 判据跟着盯这两个函数：
        读不到真源就等于徽章退回英文码，正是要防的那一类静默。

        各端直接调 `TNR_UI.petStatusBadge`，「别处不许再自带宠物状态表」由
        `PetStatusSingleSourceTest.test_no_other_frontend_file_maps_pet_status`
        守住，手拼徽章的旧写法由 `test_portals_use_the_badge_helper` 守住。
        """
        common = strip_js_comments(_read_raw('static/js/tnr-common.js'))
        for fn in ('petStatusText', 'petStatusBadge'):
            m = re.search(r'\b' + fn + r'\(status\)\s*\{', common)
            self.assertIsNotNone(m, f'{fn} 不在了 —— 各端取宠物状态的入口丢了')
            body = brace_body(common, common.index('{', m.start()))
            self.assertIsNotNone(body, f'{fn} 的花括号不配对')
            self.assertIn(
                'TNR_UI.PET_STATUS', body,
                f'{fn} 不再读取 TNR_UI.PET_STATUS —— 状态显示又分叉成两份真源了')

    def test_gov_pet_status_map_covers_pet(self):
        """监管端不得再自带宠物状态表：同一只动物三端只能有一种叫法。"""
        gov = strip_js_comments(read_js('gov'))
        for pat in (r'\bconst\s+petStatus\s*=', r'\bpetStatus\s*:\s*\{'):
            self.assertIsNone(
                re.search(pat, gov),
                'gov.js 里又出现本地宠物状态表 —— 第五十二轮合并的 TNR_UI.PET_STATUS '
                '被绕过，监管端与捕捉点端会再次漂移（曾经：待诊疗 / 诊疗中 / 待诊疗·诊疗中）')
        self.assertIn('TNR_UI.petStatusBadge(', gov,
                      '监管端一宠一档的当前状态没有走共享徽章函数')


class CaptureTransferTreatmentSingleSourceTest(SimpleTestCase):
    """捕捉单 / 转运单 / 诊疗单状态：显示与「待签收」口径各只许有一处定义。

    第五十二轮电脑端巡检（续）在监管端一格找到三处一起的漏：

      1. **监管端三张转运表都漏了 `partial`（部分签收）**，兜底写的是
         `m[r.status] || { t: r.status }` —— 医院签收了一部分动物的转运单，
         状态列直接**印出英文枚举码 `partial`**；领养人端的生命周期时间线漏的是
         同一档，但兜底写 `|| '待签收'`，于是显示成「还没签」，
         看不出已经签收了一部分。两种错法都**不报错**。
      2. **同一个词监管端与医院/捕捉点端对不上账**：后端 label 是
         待签收 / 已签收，监管端三处都写「待接收 / 已接收」。
      3. **同一份真源在同一个文件里还有两种写法**：`Treatment.status` 只有
         进行中 / 已完成，监管端全业务列表写「诊疗中」（那是 `STAGE_CHOICES`
         的**阶段**文案），台账列表写「进行中」，配色还一黄一蓝。

    「待签收」口径此前散在六处（后端签收/驳回两道门禁、医院端待办计数、标签页、
    行内按钮、详情可签判断、捕捉点可选动物），少算 `partial` 的那几处会让
    部分签收的单子**从待办里消失**，剩余动物再没人去签。
    """

    CSS = 'static/css/tnr-traditional.css'
    COMMON = 'static/js/tnr-common.js'

    TABLES = {
        'CAPTURE_STATUS': Capture,
        'TRANSFER_STATUS': Transfer,
        'TREATMENT_STATUS': Treatment,
    }

    def _entries(self, table):
        body = find_property_literal(strip_js_comments(_read_raw(self.COMMON)), table)
        self.assertIsNotNone(body, f'TNR_UI.{table} 真源不在了')
        return {m.group(1): (m.group(2), m.group(3))
                for m in re.finditer(
                    r"(\w+):\s*\{\s*text:\s*'([^']*)'\s*,\s*badge:\s*'([^']*)'\s*,?\s*\}", body)}

    def test_sources_cover_backend_choices(self):
        """三张真源都必须与后端 choices 双向对齐（缺档=漏英文码，多档=幽灵态）。"""
        for table, model in self.TABLES.items():
            expected = {c[0] for c in model._meta.get_field('status').choices}
            got = set(self._entries(table))
            self.assertTrue(expected, f'{model.__name__}.status 没有 choices，本条判据会空转')
            self.assertEqual(expected - got, set(),
                             f'{table} 缺状态 {sorted(expected - got)}，界面会漏出英文枚举码')
            self.assertEqual(got - expected, set(),
                             f'{table} 里有后端不存在的状态 {sorted(set(got) - expected)}（幽灵态）')

    def test_source_texts_equal_backend_labels(self):
        """文案必须逐字等于后端 label：同一个 code 两端两种叫法就没法对账。"""
        for table, model in self.TABLES.items():
            labels = dict(model._meta.get_field('status').choices)
            drift = [(code, text, labels[code])
                     for code, (text, _) in self._entries(table).items()
                     if code in labels and text != labels[code]]
            self.assertFalse(
                drift,
                f'{table} 的显示文案与后端 label 不一致：\n  '
                + '\n  '.join(f'{c}: 前端「{a}」 vs 后端「{b}」' for c, a, b in drift))

    def test_source_badges_exist_in_css(self):
        """配色名必须真存在：拼错的徽章**没有底色**，看着像普通文本而不显眼。"""
        css = _read_raw(self.CSS)
        for table in self.TABLES:
            missing = [badge for _, badge in self._entries(table).values()
                       if css_rule(css, '.badge-' + badge) is None]
            self.assertFalse(
                missing,
                f'{table} 的配色名在 CSS 里没有对应规则，徽章会渲染成没有底色：{sorted(set(missing))}')

    def test_open_transfer_status_matches_backend(self):
        """`OPEN_TRANSFER_STATUS` 必须等于后端 `Transfer.OPEN_STATUSES`。

        两边都只许有一处定义：后端那道「可签收 / 可驳回」门禁与前端六处
        待办判定读同一个集合。少一档 = 部分签收的单子从待办消失。
        """
        m = re.search(r'OPEN_TRANSFER_STATUS:\s*\[([^\]]*)\]',
                      strip_js_comments(_read_raw(self.COMMON)))
        self.assertIsNotNone(m, 'TNR_UI.OPEN_TRANSFER_STATUS 不在了 —— 前端又开始各自判 pending')
        got = {s.strip().strip("'\"") for s in m.group(1).split(',') if s.strip()}
        self.assertEqual(got, set(Transfer.OPEN_STATUSES),
                         f'前端「待签收」口径 {sorted(got)} 与后端 Transfer.OPEN_STATUSES '
                         f'{sorted(Transfer.OPEN_STATUSES)} 不一致')

    def test_no_frontend_reimplements_open_transfer(self):
        """前端不许再手写 `status === 'pending' || status === 'partial'`。

        这条判据盯的是**判定**而不是显示：字面量组合看着与真源等价，
        但后端加一档（或某一处漏写 `|| partial`）时，只有真源会跟着变。
        """
        offenders = []
        pattern = re.compile(r"status\s*===\s*'pending'\s*(\|\||&&)[\s\S]{0,40}?status\s*===\s*'partial'")
        for rel in FRONTEND_FILES:
            src = strip_js_comments(_read_raw(rel))
            for m in pattern.finditer(src):
                offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(
            offenders,
            '这些地方又开始各自拼「待签收 = pending + partial」了（应走 '
            'TNR_UI.isOpenTransferStatus / OPEN_TRANSFER_STATUS）：\n  ' + '\n  '.join(offenders))

    def test_renderers_use_the_sources(self):
        """三端每一处展示点位都必须取真源，不再各写一份表。"""
        required = {
            'static/js/portals/gov.js': ('captureStatusBadge', 'transferStatusBadge',
                                         'treatmentStatusBadge', 'captureStatusText',
                                         'transferStatusText'),
            'static/js/portals/shelter.js': ('captureStatusBadge', 'transferStatusBadge',
                                             'treatmentStatusBadge', 'isOpenTransferStatus'),
            'static/js/portals/hospital.js': ('transferStatusBadge', 'treatmentStatusBadge',
                                              'isOpenTransferStatus'),
            'static/js/portals/adopter.js': ('transferStatusText', 'treatmentStatusText'),
        }
        for rel, tokens in required.items():
            src = strip_js_comments(_read_raw(rel))
            for token in tokens:
                self.assertIn(
                    token, src,
                    f'{rel} 不再取用 TNR_UI 的 {token} —— 状态显示/待办判定又分叉成本地表')

    def test_shelter_lifecycle_timeline_uses_the_sources(self):
        """捕捉点端「流转全过程」时间线的转运/诊疗状态必须取真源。

        这里曾留着本轮最后一张本地转运表（只有 pending / received / rejected）：
        `partial` 落到兜底直接印出英文码。诊疗则是写死二分
        `completed ? '已完成' : '进行中'`，与后端 choices 平行维护。
        两处都不报错 —— 状态错了只是「看着不对」。

        第五十六轮：时间线随 `showPetArchive` 从捕捉点端迁到了
        `TNR_UI.showPetArchive`（`tnr-common.js`），四处门户共用一份。
        """
        src = strip_js_comments(_read_raw('static/js/tnr-common.js'))
        self.assertIn('TNR_UI.transferStatusText(ev.status)', src,
                      '流转时间线的转运状态不再取真源 —— 部分签收又会印出英文 partial')
        self.assertIn('TNR_UI.treatmentStatusText(ev.status)', src,
                      '流转时间线的诊疗状态不再取真源 —— 又会退回「已完成 / 进行中」二分')
        for banned in ("const transferStatus = {", "? '已完成' : '进行中'"):
            self.assertNotIn(
                banned, src,
                f'公共时间线又出现了本地状态表（应走 TNR_UI.*StatusText）：{banned!r}')

    def test_no_legacy_transfer_wording_in_status_maps(self):
        """「待接收 / 已接收」这两个自创叫法不许回到状态映射里。

        只禁**映射字面量**（`pending: …待接收`），不限口语说明 ——
        医院端「待接收物料」讲的是物料，不是转运单状态（转运单卡片已改称
        「待签收交接单」：那个数字本来就是交接单张数，不是动物只数）。
        """
        offenders = []
        pattern = re.compile(r"(pending|received)['\"]?\s*:\s*[^,\n}]*[待已]接收")
        for rel in FRONTEND_FILES:
            src = strip_js_comments(_read_raw(rel))
            for m in pattern.finditer(src):
                offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(
            offenders,
            '这些地方又把转运单状态写成「待接收 / 已接收」（后端 label 待签收 / 已签收）：\n  '
            + '\n  '.join(offenders))

    def test_treatment_status_not_labelled_by_stage(self):
        """`in_progress` 不许显示成阶段文案「诊疗中」。

        `Treatment.status`（进行中/已完成）与 `Treatment.stage`（待接诊/诊疗中/
        诊疗完毕）是两个字段：把阶段词当状态名，同一张单在监管端两张表里
        会一个说「诊疗中」一个说「进行中」。
        """
        offenders = []
        for rel in FRONTEND_FILES:
            src = strip_js_comments(_read_raw(rel))
            for m in re.finditer(r"in_progress[^,\n}]*诊疗中", src):
                offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(
            offenders,
            '这些地方把诊疗单状态 in_progress 写成阶段文案「诊疗中」：\n  ' + '\n  '.join(offenders))

    def test_backend_open_statuses_is_the_only_predicate(self):
        """后端签收/驳回两道门禁也必须读 `Transfer.OPEN_STATUSES`。

        前端读真源、后端仍写两遍字面量的话，加一档时两边各漏一半 ——
        界面把单子列进待办、后端却回「当前状态(partial)不可签收」。
        """
        src = _read_raw('business/views_transfer.py')
        self.assertEqual(
            src.count('Transfer.OPEN_STATUSES'), 2,
            '签收/驳回两道门禁应有两处引用 Transfer.OPEN_STATUSES')
        self.assertNotIn(
            "in ('pending', 'partial')", src,
            'views_transfer.py 又出现了字面量 pending+partial 判定')

    def test_no_status_fallback_invents_a_state(self):
        """查不到真源时不许兜底成某个具体状态（`|| '待签收'` 这类）。

        领养人端时间线原来写 `{…}[ev.status] || '待签收'`：漏掉的 `partial`
        被静默说成「待签收」，比显示英文码更糟 —— 英文码至少刺眼。
        """
        offenders = []
        for rel in FRONTEND_FILES:
            src = strip_js_comments(_read_raw(rel))
            for m in re.finditer(r"\|\|\s*'(待签收|已签收|待审核|已完成|进行中)'", src):
                offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(
            offenders,
            '这些状态兜底把未知取值猜成某个具体业务状态（应显示原始枚举码）：\n  '
            + '\n  '.join(offenders))


class TreatmentStageSingleSourceTest(SimpleTestCase):
    """诊疗「阶段」的两套取值（三态 + 看板五态）各自只许有一处显示定义。

    第五十二轮电脑端巡检（续四）遗留：`Treatment.stage`（三态）与医院看板的
    `board_stage`（五态）都是后端**派生值**，前端却各写了三份互不相干的表 ——
    `hospital.js` 的 `txStageBadge()` 一张 `[文案, badge]`、`renderBoard()` 的页签
    又一张、`txStageText()` 第三张。后果不是难看：

      - 同一个 code `checking` 在看板页签叫**「待检」**、在详情抽屉的「当前阶段」
        叫**「待接诊」**（后端 label），同一只动物同一时刻两种说法，
        医院与监管对账时像差了流程；
      - 页签、状态列、操作按钮三处各自迭代一份固定列表，后端 `_board_stage()`
        加一态时，最坏的表现是那一动物的单子在**五个桶里都不出现**（看板看着没数据），
        而后端 `_board_stage()` 已经会返回它。

    所以这里同时钉：三态文案 = `STAGE_CHOICES` 的 label / 五态取值与顺序 =
    `Treatment.BOARD_STAGES` / 两张表里同名的 code 必须同名 / 别处不许再抄一份。
    """

    CSS = 'static/css/tnr-traditional.css'
    COMMON = 'static/js/tnr-common.js'
    HOSPITAL = 'static/js/portals/hospital.js'

    def _entries(self, table):
        body = find_property_literal(strip_js_comments(_read_raw(self.COMMON)), table)
        self.assertIsNotNone(body, f'TNR_UI.{table} 真源不在了')
        return {m.group(1): (m.group(2), m.group(3))
                for m in re.finditer(
                    r"(\w+):\s*\{\s*text:\s*'([^']*)'\s*,\s*badge:\s*'([^']*)'\s*,?\s*\}", body)}

    def _order(self):
        m = re.search(r'BOARD_STAGE_ORDER:\s*\[([\s\S]*?)\]',
                      strip_js_comments(_read_raw(self.COMMON)))
        self.assertIsNotNone(m, 'TNR_UI.BOARD_STAGE_ORDER 不在了 —— 页签又开始各写一份列表')
        return [s.strip().strip("'\"")
                for s in m.group(1).split(',') if s.strip()]

    def test_treatment_stage_matches_backend_labels(self):
        """三态必须与 `Treatment.STAGE_CHOICES` 双向对齐，文案逐字等于 label。"""
        expected = dict(Treatment.STAGE_CHOICES)
        got = {code: text for code, (text, _) in self._entries('TREATMENT_STAGE').items()}
        self.assertEqual(set(expected) - set(got), set(),
                         f'TREATMENT_STAGE 缺阶段 {sorted(set(expected) - set(got))}')
        self.assertEqual(set(got) - set(expected), set(),
                         f'TREATMENT_STAGE 有后端不存在的阶段 {sorted(set(got) - set(expected))}')
        drift = [(c, got[c], expected[c]) for c in got if got[c] != expected[c]]
        self.assertFalse(drift, '阶段文案与后端 label 不一致：'
                         + ', '.join(f'{c}: 前端「{a}」vs 后端「{b}」' for c, a, b in drift))

    def test_board_stage_order_matches_backend_stages(self):
        """五态的**取值与顺序**都必须等于 `Treatment.BOARD_STAGES`。

        比较用列表而不是集合：看板的页签顺序就是流程顺序，
        顺序错了医生会以为「待出院」排在「诊疗中」前面。
        """
        self.assertEqual(self._order(), list(Treatment.BOARD_STAGES),
                         '前端页签顺序与后端 BOARD_STAGES 不一致')

    def test_board_stage_keys_and_badges_are_complete(self):
        """五态表的键必须与页签顺序一一对应，配色名在 CSS 里真存在。"""
        entries = self._entries('BOARD_STAGE')
        order = self._order()
        self.assertEqual(set(order) - set(entries), set(),
                         f'BOARD_STAGE 缺 {sorted(set(order) - set(entries))}，页签会漏出英文码')
        self.assertEqual(set(entries) - set(order), set(),
                         f'BOARD_STAGE 有幽灵态 {sorted(set(entries) - set(order))}')
        css = _read_raw(self.CSS)
        missing = [badge for _, badge in entries.values()
                   if css_rule(css, '.badge-' + badge) is None]
        self.assertFalse(missing, f'BOARD_STAGE 配色名在 CSS 里没有规则：{sorted(set(missing))}')

    def test_shared_stage_code_has_one_name(self):
        """三态与五态里同名的 code，必须叫同一个名字。

        `checking`/`treating` 两个 code 同时出现在两张表里：以前看板把 `checking`
        叫「待检」、详情叫「待接诊」，`board_stage` 与 `stage` 的对应关系在界面上
        就断了（同一个时刻两种文案）。
        """
        coarse = {c: t for c, (t, _) in self._entries('TREATMENT_STAGE').items()}
        fine = {c: t for c, (t, _) in self._entries('BOARD_STAGE').items()}
        clash = [(c, fine[c], coarse[c]) for c in sorted(set(fine) & set(coarse))
                 if fine[c] != coarse[c]]
        self.assertFalse(clash, '同一个阶段码在两张表里两种叫法：\n  '
                         + '\n  '.join(f'{c}: 看板「{a}」vs 三态「{b}」' for c, a, b in clash))

    def test_no_portal_restates_stage_labels(self):
        """除真源以外，任何前端文件都不许把阶段码映射到**阶段文案**。

        判据盯的是「码 → 真源里的某条文案」这种复制：医院端的悬停提示
        （『已交接、尚未提交检查单』）与操作按钮名（『检查』『诊断』）是医院自己的
        操作语，不算冒名；把 `checking: ['待检', 'badge-warning']` 或
        `pending_diagnose: '待诊'` 再写一遍才算。
        """
        labels = {text for table in ('TREATMENT_STAGE', 'BOARD_STAGE')
                  for text, _ in self._entries(table).values()}
        codes = '|'.join(Treatment.BOARD_STAGES)
        pat = re.compile(r'(?:%s)\s*:\s*(\[[^\]]*\]|"[^"]*"|\'[^\']*\')' % codes)
        offenders = []
        for rel in FRONTEND_FILES:
            if rel.endswith('tnr-common.js'):
                continue
            src = strip_js_comments(_read_raw(rel))
            for m in pat.finditer(src):
                hit = next((lb for lb in labels if lb in m.group(1)), None)
                if hit:
                    offenders.append(
                        f'{rel}:{src[:m.start()].count(chr(10)) + 1} '
                        f'把阶段码写成「{hit}」')
        self.assertFalse(offenders,
                         '这些地方又抄了一份阶段文案（应调用 TNR_UI.boardStageText/'
                         'boardStageBadge/treatmentStageText）：\n  ' + '\n  '.join(offenders))

    def test_hospital_board_reads_the_stage_source(self):
        """医院端看板、页签、详情三处都必须走真源。"""
        src = strip_js_comments(_read_raw(self.HOSPITAL))
        for token in ('TNR_UI.BOARD_STAGE', 'TNR_UI.BOARD_STAGE_ORDER',
                      'TNR_UI.boardStageText', 'TNR_UI.boardStageBadge',
                      'TNR_UI.treatmentStageText'):
            self.assertIn(token, src,
                          f'医院端没有用 {token} —— 阶段显示又开始自写一份')

    def test_backend_buckets_and_filter_read_the_tables(self):
        """后端两处也不许各写一遍：看板空桶、`?stage=` 筛选判据。

        空桶字面量原来在「账号未绑机构」兜底与正常分桶各一份；`?stage=` 的
        时间戳判据原来手写、与 `Treatment.stage` 属性推两遍。
        """
        src = _read_raw('business/views_treatment.py')
        for literal in ("'pending_diagnose': []",
                        'checked_at__isnull=False, done_at__isnull=True'):
            at = src.find(literal)
            self.assertEqual(
                at, -1,
                f'views_treatment.py 第 {src[:at].count(chr(10)) + 1} 行又手写了 {literal} '
                '—— 阶段判据/桶名的第二处定义（应读 Treatment.BOARD_STAGES / STAGE_FILTERS）')
        self.assertIn('Treatment.BOARD_STAGES', src,
                      '看板空桶没有读 Treatment.BOARD_STAGES')
        self.assertIn('Treatment.STAGE_FILTERS', src,
                      '?stage= 筛选没有读 Treatment.STAGE_FILTERS')


class PlatformEnumSingleSourceTest(SimpleTestCase):
    """平台端四张枚举表各只许有一处定义：实体状态 / 角色 / 机构类型 / 芯片状态。

    第五十二轮电脑端巡检（续五）盘点出来的重复：

      - **启用/停用** 8 份：徽章表（还多写一个后端**永远不会产出**的
        `disabled` 幽灵态）、三个状态筛选下拉、三行「停用/启用」按钮、
        以及捕捉点端个人中心直接写死绿色「启用」（账号真被停用也照显示启用）；
      - **角色名** 5 份：名称表、徽章表、筛选下拉、建号下拉、权限说明卡。
        其中名称表把后端 label「市级政府管理员 / 区级政府管理员」简写成
        「市级管理员 / 区级管理员」，而同页 `role_display` 与打印单据用全称；
      - **机构类型** 3 份：页面 tab、弹窗标题、账号表单里的机构提示；
      - **芯片状态** 4 份：两份还把元组顺序写反、把「已使用」写成「已植入」。

    这些不是审美问题：同一个账号/机构在列表与单据上两种职务名，授权说明与
    监管台账就对不上账；一个后端不存在的状态名会让排查时误以为是另一种停用。
    """

    COMMON = 'static/js/tnr-common.js'
    CSS = 'static/css/tnr-traditional.css'
    PLATFORM = 'static/js/portals/platform.js'
    SHELTER = 'static/js/portals/shelter.js'

    #: 实体状态的取值域只能来自 `core.models` 常量；这些文件里出现裸字符串即为漂移。
    BACKEND_STATUS_FILES = (
        'supervision/views.py',
        'business/services.py',
        'business/views_material.py',
        'business/management/commands/seed_data.py',
        'accounts/management/commands/ensure_superuser.py',
        'accounts/management/commands/ensure_platform_admin.py',
    )

    def _entries(self, table):
        body = find_property_literal(strip_js_comments(_read_raw(self.COMMON)), table)
        self.assertIsNotNone(body, f'TNR_UI.{table} 真源不在了')
        return {m.group(1): (m.group(2), m.group(3))
                for m in re.finditer(
                    r"(\w+):\s*\{\s*text:\s*'([^']*)'\s*,\s*badge:\s*'([^']*)'\s*,?\s*\}",
                    body)}

    def _code_string_literals(self, rel, values):
        """源码里（不含注释与 docstring）取值为 values 之一的字符串字面量。

        用 AST 而不是正则：注释里引用历史取值是合法的（本仓库大量注释在解释
        「以前写的是 'active'」），docstring 里也可能引用；只有**参与执行**的
        字面量才是第二处真源。
        """
        tree = ast.parse(_read_raw(rel), filename=rel)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, 'body', [])
                first = body[0] if body else None
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstrings.add(id(first.value))
        return [(node.lineno, node.value) for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and node.value in values]

    def test_entity_status_matches_backend_domain(self):
        """`ENTITY_STATUS` 的键序与文案 = `core.models.STATUS_VALUES/LABELS`。"""
        entries = self._entries('ENTITY_STATUS')
        self.assertEqual(list(entries), list(STATUS_VALUES),
                         '实体状态键/顺序与后端取值域不一致（下拉顺序也读它）')
        self.assertEqual({k: v[0] for k, v in entries.items()}, STATUS_LABELS,
                         '实体状态文案与后端 STATUS_LABELS 不一致')
        self.assertNotIn('disabled', entries,
                         'ENTITY_STATUS 又出现了后端不会产出的 `disabled` 幽灵态')
        css = _read_raw(self.CSS)
        missing = [badge for _, badge in entries.values()
                   if css_rule(css, '.badge-' + badge) is None]
        self.assertFalse(missing, f'实体状态配色在 CSS 里没有规则：{sorted(set(missing))}')

    def test_role_keys_and_labels_match_backend_choices(self):
        """角色表与下拉顺序逐项等于 `User.ROLE_CHOICES`，文案逐字等于 label。"""
        expected = dict(User.ROLE_CHOICES)
        entries = self._entries('ROLE')
        self.assertEqual(list(entries), [k for k, _ in User.ROLE_CHOICES],
                         'ROLE 的键/顺序与 User.ROLE_CHOICES 不一致')
        drift = [(k, v[0], expected[k]) for k, v in entries.items()
                 if k in expected and v[0] != expected[k]]
        self.assertFalse(drift, '角色文案与后端 label 不一致：'
                         + ', '.join(f'{k}: 前端「{a}」vs 后端「{b}」'
                                     for k, a, b in drift))
        m = re.search(r'ROLE_ORDER:\s*\[([\s\S]*?)\]',
                      strip_js_comments(_read_raw(self.COMMON)))
        self.assertIsNotNone(m, 'TNR_UI.ROLE_ORDER 不在了 —— 下拉顺序又开始各写一份')
        order = [s.strip().strip("'\"") for s in m.group(1).split(',') if s.strip()]
        self.assertEqual(order, [k for k, _ in User.ROLE_CHOICES],
                         'ROLE_ORDER 与 User.ROLE_CHOICES 顺序不一致')

    def test_inst_type_matches_backend_choices(self):
        """机构类型文案 = `Institution.TYPE_CHOICES` 的 label（房型 tab 不再自写）。"""
        entries = self._entries('INST_TYPE')
        expected = dict(Institution.TYPE_CHOICES)
        self.assertEqual({k: v[0] for k, v in entries.items()}, expected,
                         '机构类型文案与 Institution.TYPE_CHOICES 不一致')

    def test_chip_status_matches_backend_choices(self):
        """芯片四态 = `Chip.STATUS_CHOICES`，顺序也一致（选芯片下拉按它排）。"""
        entries = self._entries('CHIP_STATUS')
        expected = dict(Chip.STATUS_CHOICES)
        self.assertEqual(list(entries), list(expected),
                         'CHIP_STATUS 的键/顺序与 Chip.STATUS_CHOICES 不一致')
        self.assertEqual({k: v[0] for k, v in entries.items()}, expected,
                         '芯片状态文案与后端 label 不一致（已使用 vs 已植入）')

    def test_platform_portal_reads_the_enum_sources(self):
        """平台端四个枚举的显示/下拉/切换按钮都必须走 `TNR_UI` 真源。"""
        src = strip_js_comments(_read_raw(self.PLATFORM))
        for token in ('TNR_UI.entityStatusBadge(r.status)',
                      'TNR_UI.entityStatusOptions()',
                      'TNR_UI.entityToggleLabel(r.status)',
                      'TNR_UI.entityToggleBtnClass(r.status)',
                      'TNR_UI.roleBadge(r.role)',
                      "TNR_UI.roleOptions(['adopter'])",
                      'TNR_UI.roleText(this.currentUser.role)',
                      'TNR_UI.instTypeText('):
            self.assertIn(token, src,
                          f'platform.js 没有用 {token} —— 枚举显示又开始自写一份')
        for legacy in ('市级管理员', '区级管理员', 'disabled'):
            self.assertNotIn(legacy, src,
                             f'platform.js 又出现了旧枚举值「{legacy}」')
        for pat, why in (
                (r"\{\s*value:\s*'(?:active|inactive)'", '自写启停下拉选项'),
                (r"'停用'\s*:\s*'启用'", '自写切换按钮文案'),
                (r"status\s*:\s*\{\s*text:", '自写实体状态徽章表')):
            m = re.search(pat, src)
            self.assertIsNone(
                m, f'platform.js 第 {src[:m.start()].count(chr(10)) + 1} 行又{why}'
                   '（应读 TNR_UI.ENTITY_STATUS / ROLE / INST_TYPE）' if m else '')

    def test_shelter_account_status_reads_the_source(self):
        """捕捉点端个人中心的角色/账号状态也必须走真源，不许写死「启用」。"""
        src = strip_js_comments(_read_raw(self.SHELTER))
        self.assertIn('TNR_UI.entityStatusBadge(user?.status)', src,
                      '捕捉点端账号状态又写死了（账号停用后仍显示启用）')
        self.assertIn('TNR_UI.roleText(user?.role)', src,
                      '捕捉点端角色名没有走真源')
        self.assertNotIn("'启用'", src, '捕捉点端仍在字面量里写「启用」')
        self.assertIn("'status': user.status", _read_raw('business/views_portal.py'),
                      '门户 user_data 没有带 status —— 个人中心徽章会显示「—」')

    def test_backend_status_writes_use_the_constants(self):
        """实体状态的写侧不得再散落裸字符串，默认值必须引用常量。"""
        self.assertEqual(STATUS_VALUES, ('active', 'inactive'))
        self.assertEqual(STATUS_LABELS, {'active': '启用', 'inactive': '停用'})
        offenders = []
        for rel in self.BACKEND_STATUS_FILES:
            for lineno, value in self._code_string_literals(
                    rel, {'active', 'inactive', 'disabled'}):
                offenders.append(f'{rel}:{lineno} 写了 {value!r}')
        self.assertFalse(offenders,
                         '实体状态取值域应使用 core.models.STATUS_* 常量：\n  '
                         + '\n  '.join(offenders))
        self.assertIn('default=STATUS_ACTIVE', _read_raw('core/models.py'),
                      'District/Institution.status 默认值没有引用 STATUS_ACTIVE')
        self.assertIn('default=STATUS_ACTIVE', _read_raw('accounts/models.py'),
                      'User.status 默认值没有引用 STATUS_ACTIVE')


class PetStatusSingleSourceTest(SimpleTestCase):
    """宠物状态的文案与配色只许有一处定义：`TNR_UI.PET_STATUS`。

    第五十二轮电脑端巡检时同一个 `Pet.status='in_treatment'` 有三种叫法
    （监管端「待诊疗」、捕捉点端「诊疗中」、医院端「待诊疗/诊疗中」），
    配色也各挑各的（info / warning）。这不是审美问题：监管对账时，
    同一只动物在三端看起来像处在三个不同流转阶段，而纸质单据用的是
    第四种（后端 choices 的 label）。物料流水类型在重构 B 里已经踩过同一脚，
    这次把宠物状态按同一口径收口。

    判据是**元数据驱动**的，不写死任何一条中文：
      - 覆盖 = `Pet.STATUS_CHOICES` 的取值集合（后端加一态，前端漏了直接红）；
      - 文案 = 后端 label 逐项相等（改后端措辞会打到前端来，反之也是）；
      - 配色 = CSS 里真有 `.badge-<type>` 这条规则（拼错的色名渲染成无底色）。
    """

    CSS = 'static/css/tnr-traditional.css'

    def _source(self):
        return strip_js_comments(read('static/js/tnr-common.js'))

    def _entries(self):
        """{状态码: (文案, 配色)}，取自真源字面量本身。"""
        body = find_property_literal(self._source(), 'PET_STATUS')
        self.assertIsNotNone(body, 'TNR_UI.PET_STATUS 真源不在了')
        return {m.group(1): (m.group(2), m.group(3))
                for m in re.finditer(r"(\w+):\s*\{\s*text:\s*'([^']*)'\s*,\s*badge:\s*'([^']*)'", body)}

    def test_pet_status_covers_every_backend_choice(self):
        expected = {c[0] for c in Pet._meta.get_field('status').choices}
        got = set(self._entries())
        self.assertEqual(expected - got, set(),
                         f'真源缺状态 {sorted(expected - got)}，界面会漏出英文枚举码')
        self.assertEqual(got - expected, set(),
                         f'真源里有后端不存在的状态 {sorted(got - expected)}（幽灵态）')

    def test_pet_status_text_equals_backend_label(self):
        labels = dict(Pet._meta.get_field('status').choices)
        drift = [(code, text, labels[code])
                 for code, (text, _) in self._entries().items()
                 if code in labels and text != labels[code]]
        self.assertFalse(
            drift,
            '这些状态的显示文案与后端 choices 的 label 不一致（同一状态两种叫法，'
            '单据与界面会对不上）：\n  '
            + '\n  '.join(f'{c}: 前端「{a}」 vs 后端「{b}」' for c, a, b in drift))

    def test_pet_status_badge_exists_in_css(self):
        css = _read_raw(self.CSS)
        missing = [badge for _, badge in self._entries().values()
                   if css_rule(css, '.badge-' + badge) is None]
        self.assertFalse(missing,
                         f'配色名在 CSS 里没有对应规则，徽章会渲染成没有底色：{sorted(set(missing))}')

    def test_no_other_frontend_file_maps_pet_status(self):
        """别的前端文件不许再自带宠物状态表（`in_treatment:` 作键只出现在真源）。

        挑 `in_treatment` 当哨兵：它是宠物独有的取值，而 `owner_returned`、
        `released` 同样出现在**办结去向**表里（`hospital.js` 的 `TX_DISPOSAL_*`、
        出院单据映射），那些是另一份枚举，不该被这条判据误伤。
        """
        offenders = []
        for rel in FRONTEND_FILES:
            if rel == 'static/js/tnr-common.js':
                continue
            src = strip_js_comments(_read_raw(rel))
            for m in re.finditer(r'(?m)^\s*in_treatment\s*:', src):
                line = src[:m.start()].count('\n') + 1
                offenders.append(f'{rel}:{line}')
        self.assertFalse(
            offenders,
            '这些文件又写了一份宠物状态映射，会与真源各自漂移：\n  ' + '\n  '.join(offenders))

    def test_portals_use_the_badge_helper(self):
        """各端渲染宠物状态必须走 `TNR_UI.petStatusBadge`，不许再手拼徽章。

        此前散着两种手拼：`'<span class="badge ' + TNR_API.getPetStatusBadge(…)`
        与 `statusBadge(text, badge.replace('badge-',''))`。前者把 CSS 类名
        当字符串往外传，后者再把前缀拆回来；配色真源一改，这两种写法
        都得各改一遍，漏一处就是「这只动物没有底色」。
        取值函数 `getPetStatusText/Badge` 已随迁移从 `tnr-api.js` 删除。
        """
        offenders = []
        for rel in FRONTEND_FILES:
            src = strip_js_comments(_read_raw(rel))
            for pat in (r"getPetStatusBadge", r"getPetStatusText",
                        r"\.replace\(\s*'badge-'"):
                for m in re.finditer(pat, src):
                    offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(
            offenders,
            '这些地方又在手拼宠物状态徽章（应走 TNR_UI.petStatusBadge）：\n  '
            + '\n  '.join(offenders))
        for rel in ('static/js/portals/shelter.js', 'static/js/portals/hospital.js',
                    'static/js/portals/gov.js', 'static/js/portals/adopter.js'):
            self.assertIn(
                'TNR_UI.petStatusBadge(', strip_js_comments(_read_raw(rel)),
                f'{rel} 不再使用共享宠物状态徽章 —— 文案/配色又各拼一份')


class ReleaseStatusSingleSourceTest(SimpleTestCase):
    """放归单状态的文案/配色与「未办结」口径，前后端各只许有一处定义。

    第五十二轮电脑端巡检发现：放归在第四十八轮被拆成两段
    （`pending` 待转运回小区 → `transferred` 已回小区待放归 → `released`），
    后端改完了，**前端两份表根本没跟上**：
      - `shelter.js` 放归页签 `filter(r => r.status === 'pending')` ——
        `transferred` 的单子既不进「待放归」也不进「已放归」，整张单从界面消失，
        办理人再也点不到「确认放归」，单据永远停在中间态；
      - `gov.js` 放归监管 `released ? 已放归 : 待放归` 二值兜底 ——
        动物已回小区只差签字，监管端仍显示「待放归」，两段流程走到哪看不出；
      - 后端的重复放归判据同样只认 `pending`，动物一登记转运就又能开一张
        放归单、也能被领养流程接手（双重承诺）。

    所以这里同时钉三件事：显示真源覆盖 / 文案 = 后端 label /
    「未办结」口径 = `Release.OPEN_STATUSES`，且别处不许再各写一份。
    """

    CSS = 'static/css/tnr-traditional.css'
    COMMON = 'static/js/tnr-common.js'

    def _entries(self):
        body = find_property_literal(strip_js_comments(read(self.COMMON)), 'RELEASE_STATUS')
        self.assertIsNotNone(body, 'TNR_UI.RELEASE_STATUS 真源不在了')
        return {m.group(1): (m.group(2), m.group(3))
                for m in re.finditer(
                    r"(\w+):\s*\{\s*text:\s*'([^']*)'\s*,\s*badge:\s*'([^']*)'", body)}

    def _open_status(self):
        """`OPEN_RELEASE_STATUS: ['pending', 'transferred']` 里的取值。"""
        m = re.search(r'OPEN_RELEASE_STATUS:\s*\[([^\]]*)\]',
                      strip_js_comments(read(self.COMMON)))
        self.assertIsNotNone(m, 'TNR_UI.OPEN_RELEASE_STATUS 不在了 —— 前端又开始各自判 pending')
        return {s.strip().strip("'\"") for s in m.group(1).split(',') if s.strip()}

    def test_release_status_covers_every_backend_choice(self):
        expected = {c[0] for c in Release._meta.get_field('status').choices}
        got = set(self._entries())
        self.assertEqual(expected - got, set(),
                         f'真源缺状态 {sorted(expected - got)}，界面会漏出英文枚举码')
        self.assertEqual(got - expected, set(),
                         f'真源里有后端不存在的状态 {sorted(got - expected)}（幽灵态）'
                         f'——放归单没有撤销接口，别照着领养/转运抄一条 cancelled')

    def test_release_status_text_equals_backend_label(self):
        labels = dict(Release._meta.get_field('status').choices)
        drift = [(code, text, labels[code])
                 for code, (text, _) in self._entries().items()
                 if code in labels and text != labels[code]]
        self.assertFalse(
            drift,
            '放归状态的显示文案与后端 label 不一致（单据与界面两种叫法）：\n  '
            + '\n  '.join(f'{c}: 前端「{a}」 vs 后端「{b}」' for c, a, b in drift))

    def test_release_status_badge_exists_in_css(self):
        css = _read_raw(self.CSS)
        missing = [badge for _, badge in self._entries().values()
                   if css_rule(css, '.badge-' + badge) is None]
        self.assertFalse(missing,
                         f'配色名在 CSS 里没有对应规则，徽章会渲染成没有底色：{sorted(set(missing))}')

    def test_open_release_status_matches_backend(self):
        """前端的「待放归」集合必须等于后端 `Release.OPEN_STATUSES`。

        这条是跨语言的一致性判据：后端把中间态并入未办结（`release_confirm`
        接受它），前端漏了 `transferred` 就是把单子藏起来；前端多写了终态，
        已放归的单会同时出现在两张表里。
        """
        self.assertEqual(self._open_status(), set(Release.OPEN_STATUSES),
                         '前端 OPEN_RELEASE_STATUS 与后端 Release.OPEN_STATUSES 口径不同：'
                         '「待放归」= 未办结的放归单，只有一处定义')

    def test_no_other_frontend_file_maps_release_status(self):
        """别处不许再自带放归状态表（`transferred:` 作键只出现在真源）。

        挑 `transferred` 当哨兵：它是放归单独有的取值，宠物/领养/转运都没有，
        而 `released`、`pending` 到处都是（各流程自己的枚举），拿来判会满屏误伤。
        """
        offenders = []
        for rel in FRONTEND_FILES:
            if rel == self.COMMON:
                continue
            src = strip_js_comments(_read_raw(rel))
            for m in re.finditer(r'(?m)^\s*transferred\s*:', src):
                offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(offenders,
                         '这些文件又写了一份放归状态映射，会与真源各自漂移：\n  '
                         + '\n  '.join(offenders))

    def test_release_lists_use_the_open_status(self):
        """捕捉点端的「待放归」列表与医院端出院看板都必须按未办结口径取单。

        写回 `=== 'pending'` 的话，`transferred` 的单子不会报错、不会红，
        只是**从页签里消失** —— 这正是本类要防的那种静默缺陷。
        """
        for rel in ('static/js/portals/shelter.js', 'static/js/portals/hospital.js',
                    'static/js/portals/gov.js'):
            src = strip_js_comments(_read_raw(rel))
            self.assertTrue(
                'RELEASE_STATUS' in src or 'releaseStatusBadge' in src,
                f'{rel} 没有取用 TNR_UI 的放归状态真源（状态列/阶段又各写一份）')
        shelter = strip_js_comments(_read_raw('static/js/portals/shelter.js'))
        self.assertIn('OPEN_RELEASE_STATUS', shelter,
                      '捕捉点端放归页签不再按「未办结」取单，已回小区待签字的放归单会从界面消失')
        # 任何「拿 releases 列表比未办结单个状态」的写法都会漏掉一段（看板的
        # 待放归统计就中过：只数 pending，动物登记转运后单子从待办里消失）。
        # `=== 'released'` 允许 —— 那是「已放归」表要的就是终态。
        bad = [m.group(0) for m in re.finditer(
            r"releases\.filter\(\s*\w+\s*=>\s*\w+\.status\s*[!=]==\s*'(?!released)[a-z_]+'",
            shelter)]
        self.assertFalse(
            bad,
            '这些放归列表又开始各自判单个状态了（应为 TNR_UI.OPEN_RELEASE_STATUS，'
            '见 Release.OPEN_STATUSES）：\n  ' + '\n  '.join(bad))


    def test_shelter_adoption_surfaces_use_the_source(self):
        """捕捉点端每一处领养单列表/详情都从真源取状态。

        这里逐个点位检查，而不是「文件里出现过 `adoptionStatusBadge`」就行：
        捕捉点端有四处领养展示（领养登记表、动物去向的领养页签、全量台账、
        一宠一档详情），漏掉任何一处，那一处就会退回兜底表 —— 而兜底表里的
        `cancelled` 恰好叫「已撤销」（后端是「已取消」），四处显示三种说法。
        """
        src = strip_js_comments(_read_raw('static/js/portals/shelter.js'))
        for anchor in ("tab === 'adoptions'", "tab === 'adoption'",
                       'showAdoptionDetail(rec) {',
                       "mountTable('adoptionTableWrap'"):
            hits = list(re.finditer(re.escape(anchor), src))
            self.assertTrue(hits, f'捕捉点端找不到 {anchor}，判据会空转')
            for m in hits:
                line = src[:m.start()].count(chr(10)) + 1
                window = src[m.start():m.start() + 1600]
                self.assertIn(
                    'adoptionStatusBadge', window,
                    f'shelter.js:{line} 起的领养单展示没取用状态真源（退回通用兜底表 = 旧叫法）')

    def test_pet_archive_release_detail_shows_stage(self):
        """每一处放归列表/明细都要按状态取徽章，不许写死终态。

        捕捉点端有两处中过：
          - 全量台账「放归台账」的状态列写死 `statusBadge('已放归', 'success')` ——
            待转运、待小区签字的单子在台账里也一律显示绿色「已放归」；
          - 一宠一档的放归明细只列「放归时间」，而 `released_at` 在办结之前是空的，
            于是这两类单子看着跟「没填」一样，看不出动物在车上还是在小区等签字。
        """
        src = strip_js_comments(_read_raw('static/js/portals/shelter.js'))
        hits = list(re.finditer(r"tab === 'releases'", src))
        self.assertTrue(hits, '捕捉点端不再有任何放归列表/明细，判据会空转')
        for m in hits:
            line = src[:m.start()].count(chr(10)) + 1
            window = src[m.start():m.start() + 1400]
            self.assertNotIn(
                "statusBadge('已放归'", window,
                f'shelter.js:{line} 的放归状态写死成「已放归」（待转运/待签字的单子也是绿的）')
            self.assertIn('releaseStatusBadge', window,
                          f'shelter.js:{line} 的放归列表/明细没取用放归状态真源')

    def test_adopter_adoption_card_uses_status(self):
        """领养人端「我的领养」卡片不许再写死绿色「已领养」。"""
        src = strip_js_comments(_read_raw('static/js/portals/adopter.js'))
        self.assertIn('adoptionStatusBadge(adoption.status)', src,
                      '领养人端领养卡片的状态徽章不再取自 `Adoption.status`')

    def test_no_binary_release_status_fallback(self):
        """不许再写 `status === 'released' ? '已放归' : '待放归'`。

        二值兜底会把未办结的两段压成同一档，监管端/时间线看不出动物
        是还没送出、还是已经回小区只差物业签字；将来后端再加一段也会
        被静默归进「待放归」。取值一律走 `TNR_UI.RELEASE_STATUS`。
        """
        offenders = []
        for rel in FRONTEND_FILES:
            src = strip_js_comments(_read_raw(rel))
            for m in re.finditer(r"status\s*===\s*'released'\s*\?", src):
                offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(
            offenders,
            '这些地方的放归状态又退回「released / 其余」二值判断了：\n  '
            + '\n  '.join(offenders))


class CheckInStatusSingleSourceTest(SimpleTestCase):
    """回访打卡审核状态（`CheckIn.status`）的文案/配色只许有一处定义。

    第五十二轮电脑端巡检（续）在同一类缺陷上又撞到一个点位：
      - `gov.js` 领养业务详情的打卡时间线写的是
        `c.status === 'approved' ? '已审核' : '待审核'` ——
        **已驳回**的回访在监管端显示成「待审核」，监管看不出这条回访
        已经判过不合格，也就看不出该走「违规收回」；文案还和后端 label
        （已通过）不一致；
      - 同一状态在 `shelter.js` 叫「未通过」、在 `adopter.js` 叫「已驳回」，
        领养人和机构对同一条记录用两种说法，沟通成本全砸在纠纷上；
      - 监管端领养列表的「回访打卡 N 次」只看条数：三条全驳回的领养
        照样显示绿色徽章，跟「回访良好」完全相反。
    """

    CSS = 'static/css/tnr-traditional.css'
    COMMON = 'static/js/tnr-common.js'
    RENDERERS = ('static/js/portals/shelter.js', 'static/js/portals/gov.js',
                 'static/js/portals/adopter.js')

    def _entries(self):
        body = find_property_literal(strip_js_comments(_read_raw(self.COMMON)),
                                     'CHECKIN_STATUS')
        self.assertIsNotNone(body, 'TNR_UI.CHECKIN_STATUS 真源不在了')
        return {m.group(1): (m.group(2), m.group(3))
                for m in re.finditer(
                    r"(\w+):\s*\{\s*text:\s*'([^']*)'\s*,\s*badge:\s*'([^']*)'", body)}

    def test_checkin_status_covers_every_backend_choice(self):
        expected = {c[0] for c in CheckIn._meta.get_field('status').choices}
        got = set(self._entries())
        self.assertEqual(expected - got, set(),
                         f'真源缺状态 {sorted(expected - got)}，界面会漏出英文枚举码')
        self.assertEqual(got - expected, set(),
                         f'真源里有后端不存在的状态 {sorted(got - expected)}（幽灵态）'
                         f'—— 打卡没有「已取消」，别照着领养申请抄一行')

    def test_checkin_status_text_equals_backend_label(self):
        labels = dict(CheckIn._meta.get_field('status').choices)
        drift = [(code, text, labels[code])
                 for code, (text, _) in self._entries().items()
                 if code in labels and text != labels[code]]
        self.assertFalse(
            drift,
            '打卡状态的显示文案与后端 label 不一致（单据与界面两种叫法）：\n  '
            + '\n  '.join(f'{c}: 前端「{a}」 vs 后端「{b}」' for c, a, b in drift))

    def test_checkin_status_badge_exists_in_css(self):
        css = _read_raw(self.CSS)
        missing = [badge for _, badge in self._entries().values()
                   if css_rule(css, '.badge-' + badge) is None]
        self.assertFalse(missing,
                         f'配色名在 CSS 里没有对应规则，徽章会渲染成没有底色：{sorted(set(missing))}')

    def test_renderers_use_the_source(self):
        """三处打卡展示都必须取真源，不再各写一份。"""
        for rel in self.RENDERERS:
            src = strip_js_comments(_read_raw(rel))
            self.assertTrue(
                'CHECKIN_STATUS' in src or 'checkinStatus' in src,
                f'{rel} 没有取用 TNR_UI 的打卡状态真源（徽章/时间线又各写一份）')

    def test_no_binary_checkin_status_fallback(self):
        """不许再写 `status === 'approved' ? '中文文案' : …`。

        二值兜底会把 `rejected`（已驳回）压成「待审核」，监管端看不出回访
        已经判过不合格；后端将来加一档也会被同样吞掉。
        取状态文案一律走 `TNR_UI.checkinStatusText/Badge`。
        （`? 'done' : ''` 这种选 CSS 类名的不算 —— 所以判据要求真分支是中文。）
        """
        offenders = []
        for rel in FRONTEND_FILES:
            src = strip_js_comments(_read_raw(rel))
            for m in re.finditer(r"===\s*'approved'\s*\?\s*'[^']*[\u4e00-\u9fff]", src):
                offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(
            offenders,
            '这些地方的打卡状态退回「approved / 其余」二值判断了：\n  '
            + '\n  '.join(offenders))

    def _window(self, rel, anchor, size=700):
        src = strip_js_comments(_read_raw(rel))
        i = src.find(anchor)
        self.assertNotEqual(i, -1, f'{rel} 里找不到 {anchor}（展示点位被删掉了？）')
        return src[i:i + size]

    def test_gov_checkin_count_shows_review_result(self):
        """监管端的「回访打卡」必须带审核结果，不能只报一个条数。

        以前是 `checkIns.filter(...).length` + `cnt > 0 ? badge-success : badge-warning`：
        三条回访**全部驳回**的领养照样显示绿色「3次」，监管看板把
        「回访全不合格」读成「回访良好」—— 方向完全相反，而且不报错。
        """
        for anchor in ("title: '回访打卡'", "label: '回访打卡'"):
            win = self._window('static/js/portals/gov.js', anchor)
            self.assertIn('CHECKIN_STATUS', win,
                          f'监管端 {anchor} 又开始只数条数：待审/驳回与通过会被压成同一个绿色徽章')
        self.assertNotIn(
            "cnt > 0 ? 'badge-success'",
            strip_js_comments(_read_raw('static/js/portals/gov.js')),
            '监管端回访打卡列恢复了「有条数就绿」的旧判据')

    def test_shelter_checkin_words_match_backend_label(self):
        """机构端不许再自创「未通过」。

        筛选下拉的选项以前手写成 待审核/已通过/**未通过**，
        后端 label 与领养人端都是「已驳回」：同一条驳回记录，
        机构台账写「未通过」、领养人手机显示「已驳回」，纠纷时两头对不上话。
        """
        src = strip_js_comments(_read_raw('static/js/portals/shelter.js'))
        self.assertNotIn(
            '未通过', src,
            '捕捉点端又出现了自创的打卡状态文案「未通过」（应取 TNR_UI.CHECKIN_STATUS）')
        self.assertIn("options: Object.keys(TNR_UI.CHECKIN_STATUS)", src,
                      '回访筛选下拉不再由真源生成，后端加一档这里就选不到')


class AdoptionStatusSingleSourceTest(SimpleTestCase):
    """领养单 / 在线领养申请单的状态显示，前后端各只许有一处定义。

    第五十二轮电脑端巡检（续）在领养这一格发现的是一整套四种叫法：
      - 后端 `Adoption.STATUS_CHOICES`：待领出 / **已完成** / **已取消**；
      - `shelter.js` 筛选下拉：与后端一致（本轮起改为从真源生成）；
      - `shelter.js` 徽章兜底表：`cancelled` 叫「已撤销」；
      - `hospital.js` 出院看板：`completed` 叫「已领出」、`cancelled` 叫「已撤销」；
      - `adopter.js` 我的领养卡片：徽章**硬编码绿色「已领养」**，完全不看
        `adoption.status` —— `pending_claim`（医院还没确认领出）和
        `cancelled`（已取消）的单子也显示「已领养」，领养人据此以为手续办完。
    `AdoptionApplication.status` 另外还多一档 `cancelled`（领养人自己撤回），
    此前在 `adopter.js` 里手抄了文本、徽章两张表。
    """

    CSS = 'static/css/tnr-traditional.css'
    COMMON = 'static/js/tnr-common.js'

    def _entries(self, table):
        body = find_property_literal(strip_js_comments(_read_raw(self.COMMON)), table)
        self.assertIsNotNone(body, f'TNR_UI.{table} 真源不在了')
        return {m.group(1): (m.group(2), m.group(3))
                for m in re.finditer(
                    r"(\w+):\s*\{\s*text:\s*'([^']*)'\s*,\s*badge:\s*'([^']*)'", body)}

    def _assert_source(self, table, model):
        expected = {c[0] for c in model._meta.get_field('status').choices}
        labels = dict(model._meta.get_field('status').choices)
        got = self._entries(table)
        self.assertEqual(expected - set(got), set(),
                         f'{table} 缺状态 {sorted(expected - set(got))}，界面会漏出英文枚举码')
        self.assertEqual(set(got) - expected, set(),
                         f'{table} 里有后端不存在的状态 {sorted(set(got) - expected)}（幽灵态）')
        drift = [(code, text, labels[code])
                 for code, (text, _) in got.items()
                 if code in labels and text != labels[code]]
        self.assertFalse(
            drift,
            f'{table} 的显示文案与后端 label 不一致（单据与界面两种叫法）：\n  '
            + '\n  '.join(f'{c}: 前端「{a}」 vs 后端「{b}」' for c, a, b in drift))
        css = _read_raw(self.CSS)
        missing = [badge for _, badge in got.values()
                   if css_rule(css, '.badge-' + badge) is None]
        self.assertFalse(missing,
                         f'{table} 的配色名在 CSS 里没有对应规则，徽章会渲染成没有底色：'
                         f'{sorted(set(missing))}')

    def test_adoption_status_source_matches_backend(self):
        self._assert_source('ADOPTION_STATUS', Adoption)

    def test_apply_status_source_matches_backend(self):
        self._assert_source('APPLY_STATUS', AdoptionApplication)

    def test_renderers_use_the_source(self):
        """领养单的三个展示点位都必须取真源。"""
        for rel, token in (('static/js/portals/shelter.js', 'adoptionStatusBadge'),
                           ('static/js/portals/hospital.js', 'adoptionStatusBadge'),
                           ('static/js/portals/adopter.js', 'adoptionStatusBadge')):
            src = strip_js_comments(_read_raw(rel))
            self.assertIn(token, src,
                          f'{rel} 不再取用领养单状态真源（状态列/看板/卡片又各写一份）')
        src = strip_js_comments(_read_raw('static/js/portals/adopter.js'))
        self.assertTrue('APPLY_STATUS' in src or 'applyStatus' in src,
                        '领养人端不再取用申请单状态真源（文本/徽章又各抄一份）')

    def test_no_hardcoded_adoption_badge(self):
        """不许再写死 `badge-success">已领养`。

        「已领养」是**动物**（`Pet.status=adopted`）的文案，不是领养单的；
        写死在卡片上就等于无视 `Adoption.status`，待领出/已取消都会显示成绿色已完成。
        """
        offenders = []
        for rel in FRONTEND_FILES:
            src = strip_js_comments(_read_raw(rel))
            for m in re.finditer(r'badge-\w+">[^<]*已领养', src):
                offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(
            offenders,
            '这些地方把领养单状态写死成「已领养」了（应取 TNR_UI.adoptionStatusBadge）：\n  '
            + '\n  '.join(offenders))

    def test_no_legacy_adoption_wording(self):
        """「已撤销」「已领出」这两个自创叫法不许回来。

        后端与领养人端对同一档都叫「已取消」；把 `completed` 说成「已领出」
        则与动物状态「已领养」混成一件 —— 单据办结与动物出栏是两个字段。
        """
        offenders = []
        for rel in FRONTEND_FILES:
            src = strip_js_comments(_read_raw(rel))
            for m in re.finditer(r'cancelled[^:]{0,40}:\s*[^,\n]*已撤销|已领出', src):
                offenders.append(f'{rel}:{src[:m.start()].count(chr(10)) + 1}')
        self.assertFalse(
            offenders,
            '这些文件又出现了领养单的旧叫法（已撤销 / 已领出）：\n  '
            + '\n  '.join(offenders))

    def test_shelter_adoption_filter_uses_source(self):
        """捕捉点领养筛选下拉必须由真源生成。

        以前这里逐条手写 `{ value, label }`，与 `ADOPTION_STATUS` 平行维护：
        后端加一档或改一个叫法时，筛选条必定漏。取值与文案的正确性
        由 `test_adoption_status_source_matches_backend` 对真源与后端 choices 守着。
        """
        src = strip_js_comments(_read_raw('static/js/portals/shelter.js'))
        self.assertIn('TNR_UI.adoptionStatusOptions()', src,
                      '捕捉点领养筛选下拉不再由真源生成，后端加一档这里就选不到')
        self.assertNotIn("value: 'pending_claim'", src,
                         '领养筛选下拉又改回逐条手写了（应走 TNR_UI.adoptionStatusOptions）')


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

    def _route_matchers(self):
        routes = self._backend_routes()
        route_res = [self._route_regex(r) for r in routes]
        # 去掉动态段后的字面量：`/api/business/print/{X}/{X}/` → `/api/business/print//`
        route_prefixes = [re.sub(r'\{X\}', '', r) for r in routes]
        return route_res, route_prefixes

    @staticmethod
    def _is_known(seg, route_res, route_prefixes):
        """这个前端路径片段是否对应某个已注册路由。

        两级判定：① 整体 `fullmatch`（写死的完整路径）；
        ② 是某个路由**字面量的前缀**（参数化路径被截断后的形态）。
        """
        return (any(rx.fullmatch(seg) for rx in route_res)
                or any(p.startswith(seg) for p in route_prefixes))

    def test_no_frontend_call_to_unknown_route(self):
        """前端不得调用后端未注册的接口（会 404，表现为按钮「不生效」）。

        ⚠ 归一化会**在第一个动态段处把路径截断**：取出片段用的字符类
        `[A-Za-z0-9_/{}\\-.]` 不含标记符 `MARK`（`\\x00`），所以
        `` `/api/business/xxx/${id}/yyy/` `` 只能取出 `/api/business/xxx/`
        这个**前缀**。原先只做 `fullmatch`，于是这个前缀能不能过关
        **完全取决于它自己恰好是不是一个路由** ——
        `/api/business/captures/` 恰好是列表接口，所以一直蒙混过关。

        第四十六轮加单据打印时暴露了这个巧合：打印路径是
        `/api/business/print/<doc>/<pk>/`，而 `/api/business/print/` 本身
        不是路由 → 10 例**假红**。（顺带说明：那条打印路由本身后来被
        移出了 `/api/` —— 它返回整页 HTML，违反了 `/api/` 的 JSON 契约，
        是**真问题**，见 `tnr_system/urls.py`。两件事独立。）
        这里改为「前缀必须能作为某个已注册路由的前缀」，
        既保住「拼错路径根」的检出能力，又不再依赖巧合。

        顺带说明 `seg.count(self.MARK)` 是个**恒为 0 的死条件** ——
        片段由不含 `MARK` 的字符类取出，永远数不到标记符。保留它是因为
        字符类一旦放宽（让片段跨过标记符）它就会开始生效，但那样会误伤
        `` `.../${qs}` ``（`qs` 带 `?` 查询串）这类写法，所以**不动字符类**。
        """
        sources = self._normalized_sources()
        route_res, route_prefixes = self._route_matchers()

        called = set()
        for text in sources.values():
            for m in re.finditer(r'/api/[A-Za-z0-9_/{}\-.]*', text):
                seg = m.group(0)
                if seg.count(self.MARK) or seg.endswith('/'):
                    called.add(seg)
        # 前缀本身（'/api/business/'）是 BASE 常量，不是调用目标
        called = {c for c in called if c.rstrip('/') not in
                  ('/api/business', '/api/supervision')}
        unknown = sorted(c for c in called
                         if not self._is_known(c, route_res, route_prefixes))
        self.assertFalse(
            unknown,
            '前端调用了后端未注册的接口，点击后会 404：\n  ' + '\n  '.join(unknown)
        )

    def test_unknown_route_predicate_rejects_both_ways(self):
        """反向对照：判据既不能假红、也不能宽到「一律通过」。

        上面那条断言放宽成了「前缀匹配」—— 放宽本身有把闸门放松到
        形同虚设的风险（`一律 return True` 也能让上一条变绿）。
        所以这里成对地钉住两个方向。

        ⚠ 用**手工构造**的路由表做纯谓词单测，**不引用真实 URLconf**：
        真实路由会被增删，把某个具体路径写进断言就会变成
        「改路由就红」的假警报（第四十六轮把打印路由移出 `/api/` 时，
        第一版反向对照正是这样自己把自己写红了）。
        """
        route_res = [self._route_regex('/api/business/reports/{X}/')]
        route_prefixes = ['/api/business/reports//']   # 动态段被抹掉后的字面量
        # ① 参数化路径被截断后的**合法前缀** → 必须放行，否则假红
        self.assertTrue(
            self._is_known('/api/business/reports/', route_res, route_prefixes),
            '合法前缀被判成未知路由 —— 会产生假红')
        # ② 拼错的路径根 → 必须照样被拒，否则闸门失效
        self.assertFalse(
            self._is_known('/api/business/reportzzz/', route_res, route_prefixes),
            '拼错的路径根没被拦住 —— 闸门失效')


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


class BlacklistFormDistrictTest(SimpleTestCase):
    """「加入黑名单」必须能选归属区县。

    服务端 `blacklist_create` 走 `resolve_district_scope()`，**拒绝「全市（市级）」**
    （放宽等于让市级归属渗进业务记录，区县政府看不到本区数据）。但旧表单里
    根本没有区县控件：市级捕捉点操作员（机构挂在市级）点「保存」恒定 400
    「归属区县不能是市级，请选择具体区县」——**提示让用户做的事在界面里做不到**，
    该功能对市级捕捉点 100% 不可用（`business_blacklist` 长期 0 行就是这么来的）。
    """

    def _form_source(self):
        source = read(PORTALS['shelter'])
        anchor = source.find('id="bl_name"')
        self.assertGreater(anchor, 0, '未找到「加入黑名单」表单')
        # 到下一个方法为止（提交体在保存按钮之后，不能只截到按钮）
        end = source.find('showBlacklistCheck', anchor)
        self.assertGreater(end, anchor, '未找到黑名单表单的结束位置')
        return source[anchor:end]

    def test_form_has_district_select_excluding_city(self):
        block = self._form_source()
        self.assertIn('id="bl_district"', block,
                      '「加入黑名单」必须提供归属区县下拉：服务端拒「市级」，'
                      '没有这个控件时市级捕捉点永远提交不了')
        self.assertIn('blRealDistricts', block,
                      '区县下拉必须过滤掉「全市（市级）」——黑名单不能归属到市级')

    def test_create_payload_submits_district_id(self):
        block = self._form_source()
        self.assertIn('district_id', block,
                      '创建黑名单时必须一并提交 district_id，'
                      '否则市级捕捉点操作员恒定 400')


# （原 `CheckInStatusMappingTest` 已删除）它钉的是「捕捉点端那段内联三元表达式
# 要把 approved/rejected/pending 都翻成中文」，并把其中的「未通过」当作正确文案。
# 第五十二轮巡检（续）把打卡状态迁到 `TNR_UI.CHECKIN_STATUS` 之后，
# 这段判据既锚不住（内联表达式没了）、又会把自创的「未通过」钉成标准 ——
# 由 `CheckInStatusSingleSourceTest` 取代：文案以 `CheckIn.STATUS_CHOICES` 的
# label 为准，且要求三端都取真源。


# ---------------------------------------------------------------------------
# 渲染时序 / 作用域
#
# 下面两个用例锁的是**只有真实浏览器渲染才会暴露**的一类缺陷：模板语法完全合法、
# 静态检查也看不出问题，但页面一渲染就抛错或静默空白。GUI 实测（Playwright）
# 在捕捉点端「动物回收放归 → 待回收」页签上抓到两处：
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
            raw = read(rel)
            source = strip_js_comments(raw)   # 等长替换，索引与行号不变
            mounts = [m.start() for m in self.MOUNT.finditer(source)]
            if not mounts:
                continue
            for m in self.BIND.finditer(source):
                pos = m.start()
                fn = self._method_of(source, pos)
                if not any(self._method_of(source, p) == fn for p in mounts):
                    continue          # 该方法不渲染表格，不属于本约定管辖
                # ⚠ 豁免标记必须去**未剥离注释**的原文里找。这里曾经写的是
                # `self.EXEMPT in source[...]`，而 `source` 是剥掉注释后的文本
                # —— 注释已经被替换成等长空白，标记**永远找不到**，
                # 于是「标注 direct-bind-ok 即可豁免」这条承诺是**死代码**：
                # 照着文档加注释的人会一直撞 FAIL，却查不出为什么。
                # 剥离是等长的，偏移量在 raw 与 source 之间一一对应。
                if self.EXEMPT in raw[max(0, pos - 400):pos]:
                    continue
                line_no = source.count('\n', 0, pos) + 1
                problems.append(
                    f'{name}:{line_no} {fn}() 里 querySelectorAll(\'{m.group(1)}\') '
                    '直接绑了监听器；mountTable 重建 tbody 后它会失效'
                    '（点按钮没反应且不报错），请改用 TNR_UI.delegateClick(稳定容器, {...})；'
                    '确认不需要委托的，在上一行注释里写 direct-bind-ok 并说明理由')
        self.assertFalse(
            problems,
            '行内绑定未走事件委托：\n  ' + '\n  '.join(problems)
        )

    def test_direct_bind_exemption_actually_works(self):
        """`direct-bind-ok` 豁免必须真的生效。

        这条用例是给上面那个「死代码」缺陷上的锁：豁免标记写在注释里，
        而分析走的是**剥掉注释**的文本 —— 一旦有人把 `raw` 改回 `source`，
        豁免机制会再次静默失效（表现是「照文档加注释也没用」，而不是报错）。
        """
        source = read('core/tests_frontend_consistency.py')
        self.assertIn('if self.EXEMPT in raw[max(0, pos - 400):pos]:', source,
                      '豁免判断又回到剥注释后的文本上了 —— direct-bind-ok 会失效')

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
    'platform': 'templates/portal/platform_base.html',
}

# 各端门户对象名（adopter 端是散落的全局函数，没有统一对象）
PORTAL_OBJECTS = {
    'shelter': 'Shelter',
    'hospital': 'Hospital',
    'gov': 'GovPortal',
    'platform': 'PlatformPortal',
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
            # ⚠ `body` 是 `source` 的切片：`body.find(...)` 是**相对偏移**，
            # 必须加上切片起点才是 `source` 里的位置，否则报出的行号退回
            # 「对象体内部的行号」，把排查者送到错误的位置。
            offset = source.index(body)
            methods = set(re.findall(
                r'(?m)^  (?:async\s+)?([A-Za-z_$][\w$]*)\s*\(', body))
            declared = object_keys(body) | methods
            assigned = set(re.findall(r'\bthis\.([A-Za-z_$][\w$]*)\s*=[^=]', body))
            for prop in sorted(set(re.findall(r'\bthis\.([A-Za-z_$][\w$]*)', body))):
                if prop in declared or prop in assigned:
                    continue
                line_no = source.count('\n', 0, offset + body.find('this.' + prop)) + 1
                problems.append(
                    f'{name}:{line_no} {var} 里用了 `this.{prop}`，'
                    '但它既不是对象的键/方法，也没有被赋值 → 运行时 TypeError')
        self.assertGreater(checked, 0, '没有找到任何门户对象，测试本身可能已失效')
        self.assertFalse(
            problems,
            '门户对象引用了未声明的属性：\n  ' + '\n  '.join(problems))


class DeadPortalStateTest(SimpleTestCase):
    """门户对象里声明的顶层数据键必须**至少有一个读取点**。

    第四十二轮实测的四个（都是「声明了、但零引用」）：

    | 端 | 键 | 为什么是死的 |
    |---|---|---|
    | gov | `instState` / `settingsState` | 对应的两个页已迁到平台端 |
    | gov | `currentUser` | 冗余副本 —— `TNR_API.getCurrentUser()` 直接读 `window.TNR_USER` |
    | shelter | `navGroups` | 28 行导航结构，而侧边栏是 `shelter_base.html` **服务端渲染**的 |

    这与后端「字段存在但无人读」是同一个洞（`Institution.status` 曾零读取点，
    见 `InactiveEntityContractTest`）。危害不是报错，而是**误导**：
    读者会以为改这里能改行为，改完发现没反应，再去查别处。

    判据：键名在对象体内出现次数 < 2（= 只有声明那一次）。
    正常键至少是「声明 + 一次读取」= 2 次（如 `this.titles[pageId]`）。
    """

    def test_declared_keys_are_read(self):
        problems = []
        checked = 0
        for name, var in PORTAL_OBJECTS.items():
            source = strip_js_comments(read(PORTALS[name]))   # 等长替换，行号不变
            body = portal_object_body(source, var)
            self.assertIsNotNone(body, f'{name}: 未找到门户对象 {var}')
            # ⚠ `body` 是 `source` 的**切片**，`body.find(key)` 是相对偏移 ——
            # 少了这一步，报出来的行号会退回「对象体内部的行号」（如 gov:1），
            # 把排查者送到错误的位置。
            offset = source.index(body)
            for key in sorted(object_keys(body)):
                checked += 1
                if len(re.findall(r'\b' + re.escape(key) + r'\b', body)) >= 2:
                    continue
                line_no = source.count('\n', 0, offset + body.find(key)) + 1
                problems.append(
                    f'{name}:{line_no} {var}.{key} 只有声明、没有任何读取点'
                    ' → 死状态（改了不会有任何效果）')
        self.assertGreater(checked, 0, '没有解析到任何顶层键，测试本身可能已失效')
        self.assertFalse(
            problems,
            '门户对象里有「声明了但无人读」的键：\n  ' + '\n  '.join(problems))


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

    def test_non_json_write_response_is_reported_as_failure(self):
        """404/502 的 HTML 错误页不能被吞成 `{}` 当成写成功。

        真实案例（第四十九轮本地实测）：`reset-password/` 路由尚未加载时
        dev server 回 404 HTML 页，`_post` 里 `res.json().catch(() => ({}))`
        把它变成 `{}`，`assertOk({})` 判 `undefined !== false` 放行 ——
        弹窗提示「密码已重置」，旧密码仍能登录，数据库里什么都没变。

        业务失败是 HTTP 200 + `{success:false}`；**非 2xx 且解析不出信封**
        必然是路由/网关/CSRF 拦截等基础设施错误，必须折成失败信封。
        """
        source = strip_js_comments(read('static/js/tnr-api.js'))
        self.assertIn('_envelope(res, data)', source,
                      '_post / _postForm 没有走统一的响应收口')
        start = source.find('_envelope(res, data) {')
        self.assertGreaterEqual(start, 0, '找不到 _envelope 的实现')
        body = source[start:start + 1200]
        self.assertIn('typeof data !== \'object\'', body,
                      '_envelope 必须识别「解析不出 JSON」的响应')
        self.assertIn('!res.ok && data.success !== false', body,
                      '_envelope 必须把没有明确信封的非 2xx 判为失败')
        self.assertGreaterEqual(body.count('success: false'), 3,
                                '_envelope 的 401 / 非 JSON / 非 2xx 三个分支'
                                '都必须返回 success:false')
        for method in ('_post(url, body)', '_postForm(url, formData)'):
            start = source.find('async ' + method)
            self.assertGreaterEqual(start, 0, f'找不到 {method}')
            block = source[start:start + 600]
            self.assertIn('return this._envelope(res, data);', block,
                          f'{method} 绕过了 _envelope —— 非 JSON 错误又会被当成成功')
            self.assertNotIn('catch(() => ({}))', block,
                             f'{method} 又把非 JSON 响应吞成空对象 —— '
                             '404/502 会被当成写成功')

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
            '主人领回已改为「动物去向 → 领回」标签（首页快捷操作指向 '
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
    """静态资源引入一律走 `{% asset_url %}`，缓存键由内容哈希给出。

    原先模板里写的是**人工日期串**（`?v=20261019b`），12 个引入点全靠人记得同时升，
    而本类老判据只能查「这些值彼此相等」，查不出「有没有人忘记升」。
    第五十二轮电脑端巡检把代价实盘打了出来：改了 `static/js/portals/shelter.js`
    的个人中心渲染，浏览器仍在跑旧文件，页面显示「改了没生效」，
    而所有源码判据全绿 —— 因为它们查的是磁盘上的文件，不是浏览器里跑的那一份。

    现在的口径：**不许出现人工版本号**。缓存键 = 文件内容哈希，
    内容没变继续用缓存，内容一变自动失效，没有任何需要人维护的清单。
    """

    def _templates_with_static(self):
        #: 需要缓存键的引入点（以 `PORTALS` 为准，别照抄注释里的旧数字）。
        return ['templates/base.html'] + [PORTALS[r] for r in PORTALS]

    def test_no_manual_version_stamp_survives_in_templates(self):
        bad = []
        for rel in self._templates_with_static():
            for i, line in enumerate(_read_raw(rel).split('\n'), 1):
                # `{% static 'x.js' %}?v=...` 这种「人工拼版本号」的写法必须绝迹
                if re.search(r"\{%\s*static\s+'[^']+'\s*%\}\s*\?v=", line):
                    bad.append(f'{rel}:{i}')
        self.assertFalse(
            bad,
            '这些引入点还在用手写版本号，改了 static/ 下的文件后浏览器会继续跑旧版：\n  '
            + '\n  '.join(bad))

    @staticmethod
    def _asset_tag(path):
        """模板里期望出现的写法：`{% asset_url 'js/...' %}`。

        用拼接而不是 `%` 格式化 —— 标签本身的 `{%` / `%}` 会被 `%`-format
        当成转换符读掉（`'% }'` 报「not enough arguments」），f-string 同理要
        把花括号写成 `{{`。拼接最不容易看错。
        """
        return '{' + "% asset_url '" + path + "' %" + '}'

    def test_every_static_include_goes_through_asset_url(self):
        """每个引入点都用 `{% asset_url %}`，且所在模板 `{% load ... assets %}`。"""
        missing = []
        for role, rel in PORTALS.items():
            src = _read_raw(rel)
            for path in (f'js/portals/{role}.js', 'js/tnr-api.js'):
                if self._asset_tag(path) not in src:
                    missing.append(f"{rel}: 没有 {self._asset_tag(path)} 引入")
        base = _read_raw('templates/base.html')
        for path in ('css/tnr-traditional.css', 'js/tnr-common.js'):
            if self._asset_tag(path) not in base:
                missing.append(f"templates/base.html: 没有 {self._asset_tag(path)} 引入")
        self.assertEqual(missing, [], '静态资源没走 asset_url：\n  ' + '\n  '.join(missing))

        for rel in self._templates_with_static():
            src = _read_raw(rel)
            if 'asset_url' not in src:
                continue
            load = re.search(r'\{%\s*load\s+([^%]*?)%\}', src)
            self.assertIsNotNone(load, f'{rel}: 用了 asset_url 却没有任何 {{% load %}}')
            self.assertIn(
                'assets', load.group(1).split(),
                f'{rel}: {{% load %}} 里没有 `assets`，`asset_url` 会渲染成空串')

    def test_stamp_tracks_file_content_not_a_hand_typed_date(self):
        """同一秒内连改两次也必须换哈希 —— 失效判据取 `mtime_ns` + 字节数。

        这条是本次改造的**全部意义**：取整到秒的 mtime 会让「编辑器保存后立刻
        再存一次」命中旧哈希，于是「改了没生效」换个形式回来。
        """
        from core.templatetags.assets import asset_stamp

        probe_rel = 'js/__cache_probe__.js'
        # ⚠ 必须落在 `static/js/` 下：`STATICFILES_DIRS` 登记的是 `static/` 本身，
        # 前缀为空串 —— 写到 `static/__cache_probe__.js` 时 `finders.find()` 永远找不到，
        # 哈希恒为 `'0'`，本条判据会静默退化成空转（实测踩过）。
        probe_abs = os.path.join(ROOT, 'static', 'js', '__cache_probe__.js')
        self.addCleanup(lambda: os.path.exists(probe_abs) and os.remove(probe_abs))

        with open(probe_abs, 'w', encoding='utf-8') as f:
            f.write('window.__probe = 1;')
        first = asset_stamp(probe_rel)
        self.assertRegex(first, r'^[0-9a-f]{12}$', f'哈希形态不对：{first}')

        # 同尺寸改写：字节数相同，只有 mtime 变 —— 光比尺寸不够。
        with open(probe_abs, 'w', encoding='utf-8') as f:
            f.write('window.__probe = 2;')
        second = asset_stamp(probe_rel)
        self.assertNotEqual(first, second,
                            '内容变了哈希没变（缓存判据失效），浏览器会继续跑旧文件')

        with open(probe_abs, 'w', encoding='utf-8') as f:
            f.write('window.__probe = 22;')
        third = asset_stamp(probe_rel)
        self.assertNotEqual(second, third, '同秒内连改两次被合并成同一个哈希')

    def test_missing_file_degrades_to_placeholder_not_crash(self):
        """文件缺失时返回 `'0'`：缓存键不该把整页渲染打成 500。"""
        from core.templatetags.assets import asset_stamp
        self.assertEqual(asset_stamp('js/__definitely_not_here__.js'), '0')



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
# 「动物去向 → 放归」里的「待放归确认」列表永远是空的 ——
# **整个放归流程在界面上不可达**（只能靠直接调接口或脚本造数据）。
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
    # 第四十七轮：原「快速登记」整块删除，诊疗登记入口换成三步流程的
    # `openTreatment()`（进入流程即开单），绝育病历并入「绝育」项目卡片。
    '诊疗登记': 'openTreatment(',
    # 第五十一轮：病历改到**术后**才填，而 `plan/` 在已诊断后一律拒绝，
    # 所以补录必须走独立接口。这条判据盯的就是「接口有了、界面没接」——
    # 少了它，医生做完手术点「补录病历」提交不了，只能撤回诊断（会把
    # 已执行项目的库存扣动一起打乱），而系统不会报任何错。
    '绝育病历术后补录': 'treatmentSterilization(',
    '物料采购入库': 'purchaseMaterial(',
    '物料下发': 'dispatchMaterial(',
    '物料签收': 'receiveMaterial(',
    '物料库存异动': 'adjustStock(',
    '放归发起': 'createRelease(',
    '放归确认': 'confirmRelease(',
    '领养登记': 'registerAdoption(',
    '领养资料编辑/上下架': 'editAdoptionInfo(',
    # 第五十六轮：系统照片从捕捉/诊疗档案自动带入、只决定显隐；
    # 医院新补拍的说明类照片走 multipart 的 saveAdoptionInfo。
    '领养补充照片维护': 'saveAdoptionInfo(',
    # 第五十五轮：领养不再「确认领出」即改动物状态，必须走「领养出库单」，
    # 留下领养人交接签字与出库日期。判据也跟着换成出库入口。
    '领养出库交接': 'outboundAdoption(',
    '领养撤销': 'reclaimAdoption(',
    # 第四十六轮新增。`AdoptionApplication.status` 里的 `cancelled` 此前
    # **没有任何产生端**（模型声明了、领养人端也映射了标签与徽章，但没有
    # 接口也没有按钮），属于「枚举值存在、写入路径缺失」。
    '领养申请撤回': 'cancelApply(',
    '死亡登记': 'createEuthanasia(',
    '遗体移交（捕捉点领取）': 'receiveBody(',
    '黑名单登记': 'createBlacklist(',
    '黑名单解除': 'deleteBlacklist(',
    '回访打卡审核': 'reviewCheckin(',
}


class BusinessFlowEntryPointTest(SimpleTestCase):
    """每个业务环节都必须能在界面上点到，不能只有接口没有入口。

    接口存在、界面没接 = 流程死掉，而且**不会报错** —— 页面只是永远空着，
    看起来像「还没有数据」。靠人工走查很容易漏（放归流程就这样漏了很久）。
    """

    def test_every_flow_has_a_ui_entry(self):
        corpus = '\n'.join(read(rel) for rel in PORTALS.values())
        missing = [name for name, token in FLOW_ENTRY_POINTS.items()
                   if token not in corpus]
        self.assertFalse(
            missing,
            '以下业务环节在所有门户模板里都找不到调用入口（流程不可达）：\n  '
            + '\n  '.join(missing))


class SterilizationRecordTimingTest(SimpleTestCase):
    """绝育手术病历的**填写时机**搬到术后之后，前端必须跟着变的四处判据。

    第五十一轮把「必须六栏齐全」从「医嘱与处方」挪到「执行绝育项目」那一刻
    （术前主刀/麻醉/过程/恢复在物理上还不存在，硬要填只能编一份假病历）。
    这一轮漏任何一处都**不会报错**，只会让医生在界面里绕不出去：
      ① 已诊断后要提交到独立接口 —— 漏了就是「补录病历点了没反应」
        （`plan/` 在已诊断后一律拒绝，草稿永远提交不上去）；
      ② 执行清单缺病历时要有补录入口 —— 漏了就是点「治疗完成」必吃 400；
      ③ 计划阶段允许整份留空 —— 漏了就是逼医生术前编假病历。

    ⚠ 断言一律**收窄到方法体内**（用 `METHOD_START` + `brace_body` 抠出来）：
    在整份模板里搜关键词的话，随便一句注释就能让它通过。
    """

    def _body(self, name):
        source = blank_comments(read(PORTALS['hospital']))
        for m in METHOD_START.finditer(source):
            head = m.group(0).lstrip()
            if re.match(r'(?:async\s+)?' + re.escape(name) + r'\s*\(', head):
                return brace_body(source, m.end() - 1)
        self.fail(f'医院端找不到方法 {name}()（改名要同步这条判据）')

    def _literal_list(self, var):
        m = re.search(var + r':\s*\[([^\]]*)\]',
                      blank_comments(read(PORTALS['hospital'])))
        if not m:
            self.fail(f'医院端没有 {var} 常量（病历栏名的前端真源）')
        return re.findall(r"'([^']+)'", m.group(1))

    def test_modal_submits_directly_once_diagnosed(self):
        body = self._body('openSterilizationModal')
        self.assertRegex(body, r'postOp\s*=\s*!!detail\.diagnosed_at',
                         '弹窗要按「是否已诊断」决定提交方式：术前只写草稿，'
                         '术后必须直接落库（plan/ 那时已经关门）')
        self.assertIn('await TNR_API.treatmentSterilization(', body)
        # 提交调用必须在「未诊断就早退」之后 —— 顺序反了等于术前也走直提交，
        # 而服务端 `save_plan` 才是术前那条路的真源，两处写会互相覆盖。
        self.assertLess(body.index("if (!postOp)"),
                        body.index('treatmentSterilization('))
        self.assertIn('draft.sterilization', body, '术前那条路（只存草稿）不能丢')
        self.assertIn('is_disposed', body, '已办结要只读：病历是归档事实')

    def test_modal_allows_an_empty_record_before_surgery(self):
        """术前必须留一条「整份留空」的出路，且清空是真清空（六栏一起归零）。
        判据里点名 `STER_DRAFT_FIELDS` —— 与服务端 `sterilization_draft_filled()`
        同一份口径，少一栏都会把「留空」判成「填了一半」而拦住。"""
        body = self._body('openSterilizationModal')
        self.assertIn('sterClearBtn', body)
        self.assertIn('STER_DRAFT_FIELDS', body)
        self.assertIn("filled < vals.length", body,
                      '缺栏要按栏点名，不能只说「请填写所有必填项」')

    def test_exec_list_gates_on_the_stored_record(self):
        body = self._body('step3HTML')
        self.assertIn('sterRecordMissing(', body)
        self.assertIn('tx-record-open', body)
        # 缺栏判断必须在渲染「治疗完成」按钮之前算出来（三元表达式取 miss）
        self.assertLess(body.index('sterRecordMissing('),
                        body.index('data-act="tx-exec"'))

    def test_plan_stage_may_leave_the_record_empty(self):
        """「医嘱与处方」的预检只能拦**半份**病历，不能拦空白。"""
        body = self._body('txPlanProblem')
        self.assertIn('needs_record', body)
        self.assertRegex(body, r'filled\s*>\s*0\s*&&\s*filled\s*<',
                         '计划阶段的判据必须是「填了才要求填全」；'
                         '写成「一律要求填全」会逼医生术前编假病历')

    def test_must_predicate_matches_backend(self):
        """绝育算不算「必执行」：排了时间**或**填了病历都算（服务端同口径）。

        两边不一致的后果是界面显示「0 项必执行」、点「诊疗完成」却被服务端
        拦住 —— 这是第五十一轮之前一直存在的错法（旧口径只认病历）。
        """
        body = self._body('txItemState')
        self.assertRegex(body, r'must\s*=\s*isRec\s\?\s*\(hasDate\s*\|\|\s*recAny\)\s*:\s*hasRx')
        self.assertIn('STER_DRAFT_FIELDS.some(', body)

    def test_frontend_field_keys_are_the_backend_ones(self):
        """病历栏名的前端清单必须与服务端逐字相等（顺序也要一致 ——
        缺栏提示按这份顺序点名）。

        栏名错一个（`surgeon` 写成 `doctor`）的表现是**保存成功但那一栏永远空**：
        前端发的是错键，服务端读不到就当没填，六列留空，打印页缺主刀医生。
        `STER_DRAFT_FIELDS` 对应服务端「算不算填过」的判据
        （`sterilization_draft_filled()` 不看麻醉方式：下拉默认值不算医生填过），
        两边不一致 → 一边算必执行、另一边不算。
        """
        backend = [key for key, _ in STERILIZATION_FIELDS]
        self.assertEqual(self._literal_list('STER_REC_FIELDS'), backend)
        self.assertEqual(
            self._literal_list('STER_DRAFT_FIELDS'),
            ['surgery_date'] + list(STERILIZATION_REQUIRED))
        labels = re.search(r'STER_REC_LABELS:\s*\{([^}]*)\}',
                           blank_comments(read(PORTALS['hospital'])))
        self.assertTrue(labels, '缺栏要点名是哪一栏，得有一份中文标签表')
        self.assertEqual(
            [k for k, _ in re.findall(r'(\w+):\s*\'([^\']+)\'',
                                       labels.group(1))], backend)

    def test_exec_gate_reads_the_stored_columns(self):
        """执行前的病历闸门读的是**库里六列**（`detail.sterilization_*`），
        不是前端草稿 —— 「草稿说填了、库里没有」放行就等于放着一份空病历过去。
        """
        body = self._body('sterRecordMissing')
        self.assertEqual(sorted(re.findall(r"'(sterilization_\w+)'", body)),
                         sorted(col for _, col in STERILIZATION_FIELDS))



# ============================================================
# 单据打印入口：DOC_SPECS 里每个单据类型都必须有界面入口
# ============================================================
#
# 第四十六轮「全局单据化」给 8 类单据都做了 A4 打印视图
# （`business/views_print.py` + `templates/print/doc.html`），
# 但**打印功能前端零入口**：按钮不在任何页面上，用户根本不知道有这功能。
#
# 这与 `FLOW_ENTRY_POINTS` 是同一类缺陷（接口/页面写好了、界面没接），
# 区别是这次缺的是「读」而不是「写」—— 更难被发现：
# 一个**不存在**的功能，没人会来报告「打不开」。
# 而且后端 `DOC_SPECS` 是「枚举值」的典型形态：加一个键只改一行，
# 忘了挂入口不会有任何报错，只有真的去点才发现没有按钮。
#
# 所以这里**不写死单据清单**，直接从 `DOC_SPECS` 解析键名 ——
# 以后新增单据类型忘了挂入口，这条用例会自己亮。
#
# 前端有**三种**挂载写法，都要认（只认第一种会漏掉另外两种）：
#   1. 字面量：`printDocBtn('treatment', t.id)`（医院端）
#   2. 映射表：`PRINT_DOC_KEY = { captures: 'capture', ... }`（捕捉端台账页签）
#   3. 动态透传：`printDocBtn(tab, r.id)`，`tab` 取自 `r.business_type`，
#      合法取值就在抽屉标题那张表里（政府端全局台账中心）
PRINT_DOC_BTN = re.compile(r"printDocBtn\(\s*'([a-z_]+)'")
PRINT_DOC_MAP = re.compile(r"PRINT_DOC_KEY\s*=\s*\{([^}]*)\}", re.S)
PRINT_DOC_MAP_VAL = re.compile(r":\s*'([a-z_]+)'")
GOV_LEDGER_TITLE = re.compile(r"台账详情 · ' \+ \((\{[^}]*\})\[tab\]")
DOC_SPEC_KEY = re.compile(r"^    '([a-z_]+)': \{", re.M)


class PrintEntryPointTest(SimpleTestCase):
    """`views_print.DOC_SPECS` 的每个单据类型都必须能在界面上点到「打印」。"""

    def test_every_doc_spec_has_a_print_entry(self):
        keys = sorted(set(DOC_SPEC_KEY.findall(read('business/views_print.py'))))
        self.assertGreaterEqual(
            len(keys), 8, '没从 DOC_SPECS 解析到单据键 —— 解析逻辑失效了')

        used = set()
        for rel in PORTALS.values():
            src = read(rel)
            used |= set(PRINT_DOC_BTN.findall(src))
            for body in PRINT_DOC_MAP.findall(src):
                used |= set(PRINT_DOC_MAP_VAL.findall(body))
            for body in GOV_LEDGER_TITLE.findall(src):
                used |= set(re.findall(r"(\w+)\s*:", body))

        missing = [k for k in keys if k not in used]
        self.assertEqual(
            missing, [],
            '以下单据类型后端有打印视图、但**界面上没有任何入口**'
            '（用户看不到「打印」按钮 = 功能等于不存在）：\n  '
            + '\n  '.join(missing)
            + '\n（已挂载：' + ', '.join(sorted(used)) + '）')

    def test_print_button_is_wired_in_both_ends(self):
        """打印入口不能只挂一个端。

        `printDocBtn` 由 `tnr-common.js` 提供，**所有门户都能用**。
        捕捉端负责台账归档、政府端负责监管归档 —— 只挂一端时，
        另一端的工作人员会觉得「这系统没有打印」。
        """
        for name in ('shelter', 'gov'):
            self.assertIn(
                'printDocBtn', read(PORTALS[name]),
                f'{name} 门户没有任何打印入口（单据打印在该端不可达）')


# ============================================================
# 台账导出与一宠一档 PDF 入口（第六十三轮）
# ============================================================
#
# 事故：捕捉点端「全量台账中心 → 一宠一档 → 导出」，打开 CSV 只有表头。
# 那是 `shelter.js` 里自拼的导出器：取值写的是
# `typeof c.render === 'function' ? '' : r[c.key]`，而**一宠一档 11 列全部
# 用 `render`**（编号是可点链接、状态是徽章），于是每行都是 11 个空单元格。
# 政府端更彻底：按钮只弹一句「导出成功（模拟）」，**没有任何文件**。
#
# 两个错都来自「各端各写一份导出」。所以判据不是「能导出」，而是
# **`text/csv` 只有一处实现**，且两个有导出的端都调到它。
class LedgerExportConsistencyTest(SimpleTestCase):
    """台账 CSV 导出只允许有 `tnr-common.js` 一份实现。"""

    def test_csv_blob_is_implemented_only_in_common_layer(self):
        offenders = []
        for rel in FRONTEND_FILES:
            if rel == 'static/js/tnr-common.js':
                continue
            if 'text/csv' in _read_raw(rel):
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            '以下文件自己拼了 CSV：`text/csv` 只允许出现在 '
            '`static/js/tnr-common.js` 的 `exportCsv` 里。\n'
            '各端各写一份的后果是「导出只有表头 / 少一列」这类缺陷逐端复发，'
            '而且**不报错** —— 用户打开 CSV 才发现。\n  ' + '\n  '.join(offenders))

    def test_ledger_pages_call_the_shared_exporter(self):
        """捕捉点端与政府端的「导出」按钮都必须走 `TNR_UI.exportCsv`。"""
        for role in ('shelter', 'gov'):
            src = strip_js_comments(read_js(role))
            self.assertIn(
                'TNR_UI.exportCsv(', src,
                f'{role} 端的台账导出没有调用公共实现 `TNR_UI.exportCsv` '
                '（自拼 CSV 会丢掉 `render` 列的内容）')


class PetArchivePdfEntryTest(SimpleTestCase):
    """一宠一档详情必须能「导出 PDF」，且打印路由不被单据路由吞掉。"""

    def test_archive_drawer_has_pdf_export_button(self):
        src = strip_js_comments(_read_raw('static/js/tnr-common.js'))
        self.assertIn(
            "printDocBtn('pet-archive'", src,
            '一宠一档档案抽屉里没有「导出 PDF」入口：'
            '`print_pet_archive` 写好了，但用户在界面上点不到 = 功能不存在')

    def test_pet_archive_route_precedes_generic_print_route(self):
        """`print/pet-archive/<pk>/` 必须排在 `print/<str:doc>/<int:pk>/` 之前。

        Django 按顺序匹配：写在后面会被当成 doc='pet-archive' 命中单据打印，
        用户看到「单据不存在或无权查看」——看起来像越权，实际是路由没抢到。
        """
        src = _read_raw('tnr_system/urls.py')
        archive = src.index("'print/pet-archive/<int:pk>/'")
        generic = src.index("'print/<str:doc>/<int:pk>/'")
        self.assertLess(
            archive, generic,
            '`print/pet-archive/<int:pk>/` 排在 `print/<str:doc>/<int:pk>/` 之后，'
            '会被单据路由抢先匹配掉')






# ============================================================
# 页签 id 一致性：JS 里 `renderXxxTab('id')` 的 id 必须真实存在
# ============================================================

TAB_CONTAINER = re.compile(r'id="(\w+Tabs)"')
TAB_ITEM = re.compile(r'data-tab="([\w-]+)"')
TAB_CALL = re.compile(r"render(\w+)Tab\('([\w-]+)'\)")


class TabIdConsistencyTest(SimpleTestCase):
    """`renderXxxTab('id')` 引用的页签 id 必须真实存在于对应的 `#xxxTabs` 容器里。

    真实案例：捕捉端 `showReleaseConfirmModal` 在「确认放归成功」之后调的是
    `this.renderRelTab('released')`，而 `#relTabs` 里的 id 是 **`release`**
    —— `released` 是放归记录的**状态**，不是页签名。`renderRelTab` 没有匹配分支，
    紧接着的 `document.querySelector('[data-tab="released"]').click()` 直接对
    null 取属性，抛**未捕获 TypeError**：界面上提示「放归确认成功」，但列表不刷新、
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
    """状态筛选下拉的选项必须由真源生成，不再逐条手写。

    以前这里用 `select_filters()` 解析字面量取值、再与后端 choices 双向比对。
    改成生成式选项后，「能筛到全部取值」已经**等价于**「真源覆盖全部 choices」，
    由 `CaptureTransferTreatmentSingleSourceTest` 与
    `AdoptionStatusSingleSourceTest` 各自钉住；本类改为钉住另外两件事：

      1. 筛选条确实调用生成器（`*StatusOptions()`），没有退回手写；
      2. 生成器本身确实从 `text` 取文案、按键序输出 —— 否则「有真源」也可能
         渲染出一列英文码或空标签。

    ⚠ 生成式选项的 `options` 是函数调用，`select_filters()` 看不到取值，
    再用它做覆盖性比对必然误报（与打卡状态筛选同一处理，见
    `CheckInStatusSingleSourceTest` 的注释）。
    """

    def test_shelter_status_filters_use_generators(self):
        src = strip_js_comments(_read_raw('static/js/portals/shelter.js'))
        for token in ('TNR_UI.captureStatusOptions()',
                      'TNR_UI.transferStatusOptions()',
                      'TNR_UI.adoptionStatusOptions()'):
            self.assertIn(token, src,
                          f'捕捉点端状态筛选没有走 {token} —— 手写选项会漏掉后端新增的档位')
        offenders = []
        for pat in (r"value:\s*'partial'", r"value:\s*'pending_claim'",
                    r"value:\s*'void'", r"value:\s*'received'"):
            for m in re.finditer(pat, src):
                offenders.append(f'第 {src[:m.start()].count(chr(10)) + 1} 行')
        self.assertFalse(
            offenders,
            '捕捉点端又出现了手写的状态筛选项（应调用 TNR_UI.*StatusOptions）：\n  '
            + '\n  '.join(offenders))

    def test_status_options_reads_text_in_key_order(self):
        """生成器必须按键序、取真源的 `text`；只给 value 等于把英文码摆进下拉。"""
        common = strip_js_comments(_read_raw('static/js/tnr-common.js'))
        m = re.search(r'statusOptions\(table\)\s*\{([\s\S]*?)\n  \},', common)
        self.assertIsNotNone(m, 'TNR_UI.statusOptions() 不在了 —— 筛选选项又各写一份')
        body = m.group(1)
        self.assertIn('Object.keys(TNR_UI[table])', body,
                      'statusOptions 没有按键序遍历真源（下拉顺序会与后端 choices 脱节）')
        self.assertIn('TNR_UI[table][value].text', body,
                      'statusOptions 没有取真源的 text（下拉会漏出英文枚举码）')


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

    这里锁的是一类真实漏洞：`canCreateRole()` 早就把规则写在界面上
    （只是把角色下拉的选项过滤掉），而 `supervision.user_create`
    **没有任何对应校验** —— 实测区级管理员直接 POST `role=gov_city` +
    市级 `district_id` 就建出了一个**市级管理员**账号（口令还是他自己填的），
    登录后 `get_district_scope()` 返回 `None`，**全部区县的数据都看得到**。

    这类「前端做了、后端没做」的校验特别难被发现：界面上根本点不出这个选项，
    所以怎么点都正常，**只有直接打接口才会暴露**。所以两侧必须用一条断言绑死：
    改一侧不改另一侧就红。

    ⚠ 第四十二轮：账号管理迁到**平台端**，前端真源随之变成
    `platform/portal.html` 的 `canCreateRole()`，判据也从
    `currentUser.role === 'gov_city'` 改成 `this.isGlobalScope()`。
    """

    # `if (this.isGlobalScope()) return ['a', 'b'].includes(role);`
    PATTERN = re.compile(r"isGlobalScope\(\)\)\s*return\s*\[([^\]]*)\]")

    def _frontend_roles(self):
        source = read(PORTALS['platform'])
        m = re.search(r'canCreateRole\(role\)\s*\{', source)
        self.assertIsNotNone(m, 'platform portal 里找不到 canCreateRole —— 本测试需要同步更新')
        body = brace_body(source, source.index('{', m.start()))
        self.assertIsNotNone(body, 'canCreateRole 方法体的花括号不配对')
        lists = self.PATTERN.findall(body)
        self.assertTrue(lists, 'canCreateRole 里没解析到角色白名单 —— 本测试需要同步更新')
        return [x.strip().strip('\'"') for x in lists[0].split(',') if x.strip()]

    def test_frontend_role_lists_match_backend(self):
        self.assertEqual(
            self._frontend_roles(),
            list(MANAGEABLE_ROLES['platform_admin']),
            '平台端 `canCreateRole()` 与后端 `services.MANAGEABLE_ROLES` 不一致。\n'
            '后端才是真正生效的那一份 —— 前端只过滤下拉选项，改错了不会有任何提示，\n'
            '只会让界面上少一个（或凭空多一个）角色选项。')

    def test_backend_roles_cover_every_non_adopter_role(self):
        """平台管理员必须能建出全部非领养人角色，否则会「建不出第二个管理员」。"""
        self.assertEqual(
            set(MANAGEABLE_ROLES['platform_admin']),
            {'platform_admin', 'gov_city', 'gov_district', 'shelter', 'hospital'},
            '平台管理员可建角色集合变了 —— 领养人不在管理端创建，其余五种都应可建。')


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
        # 第四十二轮：机构管理迁到平台端
        source = read(PORTALS['platform'])
        m = re.search(r'showInstitutionModal\(id\)\s*\{', source)
        self.assertIsNotNone(m, 'platform portal 里找不到 showInstitutionModal —— 本测试需要同步更新')
        # 用 m.start() 定位方法体的那个 `{`：用 m.end() 会**跳过**它，
        # 从方法体内部的下一个 `{` 开始配对，取到的是一段无关片段。
        body = brace_body(source, source.index('{', m.start()))
        self.assertIsNotNone(body, 'showInstitutionModal 方法体的花括号不配对')
        return body

    def test_frontend_narrows_district_options_only_for_district_scoped_operator(self):
        """区县下拉的收窄必须以「**不是**全局角色」为前提。

        平台管理员没有区县归属。若把政府端的 `isCityLevel()` 判据照抄过来
        （`!isCityLevel()` 对平台管理员恒为真），收窄分支会**永远生效**，
        把下拉过滤成空集 —— 界面上「所属区县」一个选项都没有，而服务端并不拒绝，
        用户纯粹白填一遍。
        """
        body = self._institution_modal()
        self.assertRegex(
            body, r"!this\.isGlobalScope\(\)[\s\S]{0,200}?currentUser\.district_id",
            '机构表单的区县下拉收窄没有以「非全局角色」为前提 ——\n'
            '平台管理员会被误判成区级管理员，下拉退化成空集。')

    def test_frontend_preserves_institution_current_district(self):
        """编辑时必须把机构当前区县兜底加回选项。

        少了这一步，挂市级的捕捉点（下拉里被 `!d.is_city` 排除）会让
        `<select>` 退化成选中第一项，**不改区县直接点保存**就把它搬到具体区县。
        """
        body = self._institution_modal()
        self.assertIn('inst.district_id', body,
                      '机构表单没有把「机构当前区县」加回下拉选项 ——\n'
                      '挂市级的捕捉点编辑保存时会被静默改判到某个具体区县。')


class AccountFormCascadeContractTest(SimpleTestCase):
    """「所属区县 → 关联机构」必须**逐级联动**（第四十六轮实测两处）。

    两个缺陷形态不同，根子都是「下拉选项与实际规则脱钩」：

    1. **前端收窄 = 静默做不到**。捕捉点操作员的区县下拉只列了市级，
       而服务端 `user_create` 明确允许 `shelter` 挂「市级或具体区县」。
       于是「区县级捕捉点操作员」在界面上根本建不出来 —— 不报错、不 400，
       用户只会以为系统不支持。
    2. **前端放开 = 白填一遍**。两个角色的机构下拉都是全量列出，
       选了襄城区还能挑樊城的医院，提交时才被 `validate_operator_district()`
       拦下。更隐蔽的是「市级 + 区县捕捉点」：所属区县是市级 ⇒ 读取范围是全市，
       代表区县捕捉点就等于「看全市档案、以区县机构身份操作」——
       不报错、不 500，数据隔离已经破了。

    ⚠ 本判据锁的是**结构**（选项来源 + 是否存在全量 append 的退路）。
    真正的行为验证靠浏览器实测：市级→只列市级捕捉点、襄城区→只列襄城的
    捕捉点与医院、无机构的区县→明确提示而不是空下拉。
    """

    def _modal(self):
        source = read(PORTALS['platform'])
        m = re.search(r'showAccountModal\(\)\s*\{', source)
        self.assertIsNotNone(
            m, 'platform portal 里找不到 showAccountModal —— 本测试需要同步更新')
        body = brace_body(source, source.index('{', m.start()))
        self.assertIsNotNone(body, 'showAccountModal 方法体的花括号不配对')
        return body

    def _refresh_fn(self):
        body = self._modal()
        m = re.search(r'refreshInstitutionOptions\s*=\s*async\s*\(\)\s*=>\s*\{', body)
        self.assertIsNotNone(
            m, '找不到 refreshInstitutionOptions —— 机构按区县筛选的逻辑被移走了')
        fn = brace_body(body, body.index('{', m.start()))
        self.assertIsNotNone(fn, 'refreshInstitutionOptions 的花括号不配对')
        return fn

    def test_shelter_district_options_cover_city_and_counties(self):
        """捕捉点操作员的区县下拉必须**同时**有市级与具体区县。

        只列 `cityDistricts` 就是「区县级捕捉点操作员建不出来」——
        服务端允许、前端藏起来，比撞 400 更难发现。

        ⚠ 锚点必须先收进 `updateDistrictOptions()` 内部再找 shelter 分支：
        `refreshInstitutionOptions()` 里也有一处 `role === 'shelter'`
        （三元表达式选机构类型），从整个模态框里搜会命中那一处，
        断言落在机构筛选的代码上，**区县分支改了也发现不了**。

        ⚠ 截取范围必须卡在**本分支的 `}`** 上，不能用固定长度窗口：
        实测 `+2000` 会越过 shelter 分支、抓到后面
        `gov_district || hospital` 分支里的 `realDistricts.forEach`，
        于是「把 shelter 的区县选项整块删掉」这个回归**判据仍然是绿的**。
        """
        body = self._modal()
        m = re.search(r'updateDistrictOptions\s*=\s*async\s*\(\)\s*=>\s*\{', body)
        self.assertIsNotNone(m, '找不到 updateDistrictOptions —— 角色联动逻辑被移走了')
        upd = brace_body(body, body.index('{', m.start()))
        self.assertIsNotNone(upd, 'updateDistrictOptions 的花括号不配对')
        seg_m = re.search(r"role === 'shelter'\)\s*\{", upd)
        self.assertIsNotNone(seg_m, 'updateDistrictOptions 里找不到 shelter 分支')
        start = seg_m.start()
        nxt = re.search(r'\}\s*else if \(role === ', upd[start:])
        end = start + nxt.start() if nxt else len(upd)
        seg = upd[start:end]
        self.assertIn(
            'cityDistricts', seg,
            '捕捉点操作员的区县下拉没有市级选项 —— 市级捕捉点操作员建不出来')
        self.assertIn(
            'realDistricts', seg,
            '捕捉点操作员的区县下拉没有具体区县选项 ——\n'
            '服务端 `user_create` 允许 shelter 挂区县，前端藏起来等于「静默做不到」')

    def test_institution_options_are_filtered_by_selected_district(self):
        """机构下拉必须按 `district_id` 过滤，且**不能留全量 append 的退路**。

        反向对照（`all.forEach` 必须不存在）是关键：只断言「出现了
        district_id 字样」抓不到「过滤写了但全量 append 还留着」——
        那种代码照样把所有机构列出来，判据却是绿的。
        """
        fn = self._refresh_fn()
        self.assertRegex(
            fn, r"all\.filter\([\s\S]{0,120}?i\.district_id",
            '机构下拉没有按 `district_id` 过滤 —— 选了区县仍能挑到别区的机构')
        self.assertNotRegex(
            fn, r"\ball\.forEach\b",
            '机构下拉存在全量 append 的写法 —— 按区县筛选会被它架空，\n'
            '用户在襄城区仍能选到樊城的机构，提交时才撞 400')

    def test_district_change_refreshes_institution_options(self):
        """区县变化必须重刷机构，否则会留下「区县=樊城、机构=襄城医院」的组合。"""
        body = self._modal()
        self.assertRegex(
            body, r"districtSelect\.addEventListener\(\s*'change'\s*,\s*refreshInstitutionOptions\s*\)",
            '区县下拉没有绑定机构刷新 —— 换区县后机构清单还是旧的')

    def test_empty_institution_list_is_explained(self):
        """该区县没有可用机构时必须给提示，不能只留一个空下拉。

        「没报错的地方最危险」：空下拉会被读成「系统里没有机构」，
        而实际是该区县没有启用的捕捉点/医院 —— 两种情况的处置完全不同。
        """
        fn = self._refresh_fn()
        self.assertIn(
            '暂无启用', fn,
            '机构下拉为空时没有提示 —— 用户会以为系统里根本没有机构')


class VetLicenseEntryContractTest(SimpleTestCase):
    """执业兽医师证号的**整条录入链路**（第四十九轮补的录入口）。

    业务口径（2026-10-19 确认）：执业身份的验证点就在**平台端「新增医院账号」**
    这一步 —— 建号时录入证号，之后每张诊疗单靠它归属到人。

    这条链路跨 6 个文件（模型 → 服务层判据 → 建号/改号视图 → 路由 → API 封装 →
    平台端界面 → 开单快照 → 打印），任何一环「只是没接上」都不会报错：
      - 界面上没这个输入框 → 证号恒为空 → 签名归属失效，而系统里一切正常；
      - 服务端漏了必填 → 前端填得再认真也挡不住直接 POST；
      - 改号接口不回写历史单据是**要求**，不是缺陷 —— 但「新单子也不更新」是真缺陷。
    所以四类形态各钉一条。
    """

    def _js(self, rel, pattern, label):
        source = read(rel)
        m = re.search(pattern, source)
        self.assertIsNotNone(m, f'找不到 {label} —— 本测试需要同步更新')
        body = brace_body(source, source.index('{', m.start()))
        self.assertIsNotNone(body, f'{label} 的花括号不配对')
        return body

    def _py_func(self, source, name):
        """取顶层函数的整段源码（花括号配对在 Python 上不适用）。"""
        m = re.search(r'^def ' + re.escape(name) + r'\(', source, re.M)
        self.assertIsNotNone(m, f'找不到 {name} —— 本测试需要同步更新')
        nxt = re.search(r'^(?:def |@|# =)', source[m.end():], re.M)
        end = len(source) if nxt is None else m.end() + nxt.start()
        return source[m.start():end]

    def test_service_holds_the_only_rule(self):
        """判据只在 `services.validate_vet_license` 里写一遍。

        视图里出现证号长度/形状的就地校验就是第二真源 ——
        同一个概念写多遍，就会有一遍没人改（本轮补录入口时最容易复制出去）。
        """
        body = self._py_func(read('business/services.py'), 'validate_vet_license')
        self.assertIn("role != 'hospital'", body, '非医院角色填证号的分支没了')
        self.assertIn('len(license_no)', body, '长度/形状判据没了')
        for name in ('user_create', 'user_update'):
            seg = self._py_func(read('supervision/views.py'), name)
            self.assertIn('validate_vet_license(', seg,
                          f'{name}() 没有调用服务层判据 —— 直接 POST 能绕过界面')
            self.assertIn('normalize_vet_license(', seg,
                          f'{name}() 没有先规范化，首尾空格会进证号')
            self.assertNotRegex(
                seg, r'vet_license[\s\S]{0,40}\.isdigit\(\)|len\(vet_license\)',
                f'{name}() 里出现了就地形状校验 —— 与服务层重复，会各自漂移')

    def test_create_and_edit_views_store_it(self):
        src = read('supervision/views.py')
        self.assertRegex(
            src, r'user\.vet_license\s*=\s*vet_license',
            '`user_create` 校验完没写库 —— 建号时填了证号，账号上还是空的')
        self.assertRegex(
            src, r"if 'vet_license' in data:",
            '`user_update` 的证号分支必须按**键是否出现**决定改不改 ——\n'
            '改成真值判断会让「只改姓名」的提交把证号清空。')

    def test_route_and_api_wrapper_exist(self):
        urls = read('supervision/urls.py')
        self.assertRegex(
            urls, r"users/<int:pk>/edit/'[^)]*views\.user_update",
            '`users/<pk>/edit/` 路由没了 —— 抄错的证号没有纠正入口，\n'
            '现场的下一步就是直连数据库改（绕过校验，也不进审计）。')
        api = strip_js_comments(read('static/js/tnr-api.js'))
        self.assertRegex(
            api, r'updateUser\(id,\s*data\)[\s\S]{0,120}users/\$\{id\}/edit/',
            '`TNR_API.updateUser` 没打到 `users/${id}/edit/` —— 界面上的保存是空操作。')

    def test_platform_modal_has_required_field(self):
        """新增账号模态框：证号输入框存在、只给医院角色看、提交前挡空。"""
        body = self._js(PORTALS['platform'], r'showAccountModal\(\)\s*\{',
                        'showAccountModal')
        self.assertIn(
            'id="acct-vet-license"', body,
            '平台端「新增账号」里没有执业证号输入框 —— 业务口径要求建医院账号时\n'
            '就录入证号（这是执业身份唯一的录入口），缺了它等于让现场去改数据库。')
        self.assertIn(
            "licenseGroup.style.display = role === 'hospital'", body,
            '证号输入框没有随角色收起 —— 非医院角色也填得到，提交必被服务端拒。')
        self.assertRegex(
            body, r"role === 'hospital' && !vetLicense",
            '医院角色提交前没挡空证号 —— 界面放行人，服务端拒 400，用户不知道为什么。')
        self.assertRegex(
            body, r'createUser\(\{[\s\S]{0,300}vet_license',
            '提交时没把 vet_license 发给接口 —— 输入框成了摆设。')
        # 反向对照：把上面这条锚点改掉，判据必须跟着红
        planted = body.replace('vet_license: vetLicense', 'license: vetLicense')
        self.assertNotEqual(
            planted, body,
            '反向对照锚点失效：`createUser` 的 payload 里已经不是 `vet_license: vetLicense` 了，\n'
            '请确认上面那条 `createUser(...)` 断言是否已经抓不到真实字段名。')

    def test_accounts_table_shows_and_edits_it(self):
        # 判据收在 `renderAccountsTable()` 方法体内：`data-edit` 这个属性名
        # 机构表也在用，拿整个模板搜会命中机构那一处，账号表删掉编辑按钮照样绿。
        html = self._js(PORTALS['platform'], r'(?:async\s+)?renderAccountsTable\(\)\s*\{',
                        'renderAccountsTable')
        # 渲染器集中到 `cell.vetLicense` 后，列定义与本体的字符距离变远；
        # 判据改为「列引用了共用渲染器」+「渲染器里有未登记标记」两段，
        # 无论以后把渲染器挪到哪里都不会误红。
        self.assertRegex(
            html, r"key: 'vet_license',[^\n]*render: cell\.vetLicense",
            '账号列表的「执业证号」列没有引用共用渲染器 cell.vetLicense')
        self.assertRegex(
            html, r"vetLicense: r =>[\s\S]{0,220}未登记",
            '账号列表没有「执业证号」列或未登记标记 ——\n'
            '平台端看不见空证号，就会以为医院都登记过了。')
        self.assertIn('data-edit="${r.id}"', html, '账号列表没有编辑按钮。')
        self.assertIn(
            "'data-edit': (id) => this.showAccountEditModal", html,
            '账号列表的编辑按钮没绑到编辑模态框 —— 点了没反应。')
        edit = self._js(PORTALS['platform'], r'showAccountEditModal\(row\)\s*\{',
                        'showAccountEditModal')
        self.assertIn(
            'TNR_API.updateUser(', edit,
            '编辑模态框没走 updateUser —— 保存按钮点了没接口。')
        self.assertNotIn(
            'acctEdit-role', edit,
            '编辑模态框又能改角色了 —— 服务端 `user_update` 只收姓名/电话/证号，\n'
            '界面给出去而接口不认，等于一个永远保存不了的输入框。')
        self.assertNotRegex(
            edit, r'payload\.(role|district_id|institution_id|password|is_active)\s*=',
            '编辑模态框提交了**改权限**的字段 —— 与 `user_update` 的收窄范围不一致，\n'
            '这些字段的服务端守卫是被测不到的死代码。')

    def test_open_treatment_snapshots_instead_of_reading_live(self):
        """开单时快照进单据，打印时读单据 —— 反过来（打印实时读账号）是缺陷。"""
        svc = read('business/services_treatment.py')
        m = re.search(r'^def open_treatment\(', svc, re.M)
        self.assertIsNotNone(m, '找不到 open_treatment —— 快照位置需要同步')
        seg = svc[m.start():m.start() + 3000]
        self.assertRegex(
            seg, r'vet_license\s*=\s*getattr\(user,\s*.vet_license.',
            '开单时没把账号证号快照进诊疗单 —— 单据上的签名归属为空。')
        printer = read('business/views_print.py')
        self.assertIn(
            "_v('vet_license')", printer,
            '打印模板不再读单据快照字段。如果它改成实时读 `User.vet_license`，\n'
            '历史单据会跟着账号一起变 —— 那张单子就无法再证明当时是谁签的字。')

    def test_hospital_profile_shows_license_readonly_and_changes_own_password(self):
        """医院个人中心：证号只读展示（平台维护）+ 自助改密（第五十轮）。

        证号在个人中心若做成可编辑输入框，医院就能自证执业身份；而「改密请
        联系管理员」在只有平台一个管理员时会把人锁死 —— 两者各自钉一条。
        """
        body = self._js(PORTALS['hospital'], r'render_profile\(\)\s*\{',
                        'hospital.render_profile')
        self.assertIn('vet_license', body, '个人中心没有读取执业证号')
        self.assertNotRegex(
            body, r'<input[^>]*vet',
            '个人中心把执业证号做成了输入框 —— 证号只能由平台端维护')
        self.assertIn('/api/me/password/', body,
                      '个人中心没有自助改密入口 —— 用户只能找平台管理员')
        self.assertNotIn('联系系统管理员', body,
                         '个人中心仍在提示「联系系统管理员」改密')
        self.assertRegex(
            read('business/views_portal.py'), r"'vet_license':\s*user\.vet_license",
            '门户载荷没下发 vet_license —— 个人中心只能显示空值')
        self.assertRegex(
            read('accounts/views.py'), r"'vet_license':\s*u\.vet_license",
            '/api/me/ 没下发 vet_license —— 接口与门户载荷两套口径')


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
        """捕捉点端的机构下拉必须与政府端一样排除停用机构。

        ⚠ 第四十九轮起只剩**一处**：转运单的接收医院。
        第二处（新建捕捉单的「执行机构」）不是「过滤被删了」，而是**整个下拉被删了**：
        它原本是给监管账号代录用的（政府端转只读后这条路径整体消失，
        判据见 `core/tests_gov_readonly.py`），现在捕捉点提交时
        `shelter_id` 恒等于自己绑定的捕捉点。
        第四十五轮起第三处（下发出库的接收医院）**过滤责任转移到了服务端** ——
        那个下拉改成由 `materials/dispatch-targets/` 提供，服务端在
        `dispatch_targets()` 里以 `STATUS_ACTIVE` 收口（见下一条用例）。
        闸门跟着责任走，而不是钉在「前端要出现 N 次这个写法」上。

        区分「删了过滤」和「删了下拉」的办法是把两者都钉住：
        若有人把执行机构下拉加回来却忘了排除停用机构，
        下面两条断言会一起变红。
        """
        html = read(PORTALS['shelter'])
        found = (html.count("i.status === 'active'")
                 + html.count("h.status === 'active'"))
        self.assertEqual(
            found, 1,
            '捕捉点端排除停用机构的下拉数量变了（现在 %d 处，应为转运单的接收医院 1 处）。\n'
            '少了：说明接收医院的过滤被删；多了：说明有新的机构下拉没进这份口径。\n'
            '政府端早就过滤了，只写一半就是跨端漂移。' % found)
        self.assertNotIn(
            'ca_shelterId', html,
            '「执行机构」下拉又回来了 —— 它属于第四十九轮删掉的「监管代录」入口。\n'
            '要恢复就得先回答：谁替哪个捕捉点录，以及他有没有这个权限。')
        # 反向对照：把接收医院那句过滤拆掉，计数器必须跟着归零。
        # 不然这条判据可能是「数字恰好等于 1」的空转 —— 比如两种写法都被
        # 改成 `status !== 'inactive'`，数量不变而口径已经变了。
        # 锚点只认判据本身（第五十轮后过滤器还叠了区县条件，整句匹配会失效）。
        planted = html.replace("h.status === 'active'", 'true')
        self.assertNotEqual(html, planted, '反向对照的锚点字符串失效了（模板里那句话被改写）')
        self.assertEqual(
            0, planted.count("i.status === 'active'") + planted.count("h.status === 'active'"),
            '植入的缺陷没被计数器看见 —— 上面那条断言是假绿')

    def test_shelter_dispatch_targets_come_from_server_and_exclude_inactive(self):
        """「下发出库」的接收方下拉改由服务端给，服务端必须自己排除停用机构。

        第四十五轮：这个下拉从「前端拼全市医院」改成 `materials/dispatch-targets/`
        （与 `dispatch_create` 的校验**共用同一个判据** `dispatch_targets()`）。
        前端不再过滤 `status === 'active'` —— 过滤责任**转移**了，
        所以闸门必须跟着转移：只把上面那条的计数从 3 改成 2，
        等于把「少了一处过滤」这件事一起删掉。
        """
        html = read(PORTALS['shelter'])
        self.assertIn(
            'TNR_API.getDispatchTargets(', html,
            '捕捉点端「下发出库」的接收方下拉没有走服务端接口 ——\n'
            '前端自己拼列表必然与服务端校验漂移：用户**选得到、发不出去**（撞 400），\n'
            '而且从下拉里看不出哪个能选。')
        self.assertNotIn(
            'id="ob_hospital"', html,
            '接收方下拉还叫 `ob_hospital` —— 第四十五轮起接收方是**医院/捕捉点二选一**，\n'
            '写死成医院会把捕捉点 id 当医院 id 提交，服务端报「医院不存在」。')
        body = self._py_func(read('business/views_material.py'), 'dispatch_targets')
        self.assertIn(
            'status=STATUS_ACTIVE', body,
            '`dispatch_targets()` 没有排除停用机构 —— 停用机构仍会出现在下拉里，\n'
            '且因为下拉已经不看前端过滤，这一处漏了就是**整条链路**漏了。')
        self.assertIn('STATUS_ACTIVE', read('business/views_material.py'),
                      'views_material.py 没有使用 core.models.STATUS_ACTIVE')

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
        m = re.search(r"@role_required\(([^)]*)\)", decorators)
        self.assertIsNotNone(m, 'stock_adjustment 上找不到 @role_required')
        roles = set(re.findall(r"'([a-z_]+)'", m.group(1)))
        # 逐角色断言，而不是把整串白名单抄进本用例 —— 抄死的那一份会在
        # 每次调整角色时被顺手改掉，从而失去「钉住 shelter」的原意。
        self.assertIn('shelter', roles,
                      'stock_adjustment 的角色白名单里没有 shelter —— 捕捉点的'
                      '过期物料就没有任何正规出口（既不能报废，也不能下发）。')
        self.assertIn('hospital', roles, '医院侧库存异动入口消失')
        self.assertFalse(roles & {'gov_city', 'gov_district'},
                         '政府端只读（第四十九轮）：监管不该再能异动库存')

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


class InventoryAdjustmentBatchPickerTest(SimpleTestCase):
    """库存异动必须能按批选择（过期报废按批处理）。

    同一药品不同批号效期不同。只给「不指定」时，异动按 FEFO 扣减 ——
    想报废过期批，系统可能先扣了另一批还有效的货，过期批反而留在账上。
    因此两端的异动表单都要列出**所有仍有库存的批次**，选中后只扣该批。
    """

    def test_hospital_adjustment_lists_batches_with_stock(self):
        js = strip_js_comments(read_js('hospital'))
        start = js.find('async renderMaterialAdjustment(c, txns, materials) {')
        self.assertGreater(start, 0, '找不到医院端库存异动渲染函数')
        end = js.find('\n  async saveStockAdjustment(', start)
        self.assertGreater(end, start, '找不到 renderMaterialAdjustment 的结尾')
        body = js[start:end]
        self.assertIn('id="adjBatch"', body, '异动表单没有批号下拉')
        self.assertIn('getMaterialBatches(m.id)', body,
                      '异动表单没有拉取该物料的批次')
        self.assertIn('(b.quantity || 0) > 0', body,
                      '批次下拉必须过滤掉零库存批次')
        self.assertIn("'adjustment',", body,
                      '批次预览没有按 adjustment 口径（过期批不跳过）')

        start = js.find('async saveStockAdjustment(materials) {')
        self.assertGreater(start, 0, '找不到医院端异动提交函数')
        end = js.find('\n  async renderMaterialOutbound(', start)
        save = js[start:end]
        self.assertIn('batch_no: batchNo', save,
                      '异动提交没有把选中的批号发给服务端')
        self.assertIn('adjBatch', save, '异动提交没有读取批号下拉')

    def test_shelter_adjustment_lists_batches_with_stock(self):
        js = strip_js_comments(read_js('shelter'))
        start = js.find("tab === 'adjustment'")
        self.assertGreater(start, 0, '找不到捕捉点库存异动页签')
        end = js.find("tab === 'outboundDecl'", start)
        self.assertGreater(end, start, '找不到 adjustment 页签的结尾')
        body = js[start:end]
        self.assertIn('id="adjBatch"', body, '异动表单没有批号下拉')
        self.assertIn('getMaterialBatches(m.id)', body,
                      '异动表单没有拉取该物料的批次')
        self.assertIn('(b.quantity || 0) > 0', body,
                      '批次下拉必须过滤掉零库存批次')
        self.assertIn("'adjustment',", body,
                      '批次预览没有按 adjustment 口径（过期批不跳过）')

        start = js.find('async saveStockAdjustment() {')
        self.assertGreater(start, 0, '找不到捕捉点异动提交函数')
        end = js.find('\n  async saveMaterialOutbound(', start)
        save = js[start:end]
        self.assertIn('batch_no: batchNo', save,
                      '异动提交没有把选中的批号发给服务端')
        self.assertIn('adjBatch', save, '异动提交没有读取批号下拉')


class TemplateCommentLeakTest(SimpleTestCase):
    """`{# … #}` 是**单行**注释 —— 跨行写会把注释正文渲染到页面上。

    Django 的 `{# #}` 只匹配**同一行**（跨行要用 `{% comment %}`）。跨行写
    既不报错也不 500：模板语法合法、接口全 200、测试全绿，只是那段中文
    被**原样输出**。第四十二轮实测两处 —— 政府端与平台端侧栏里都出现了
    `{# 第四十二轮：「机构基础管理 / 账号权限管理 …` 这样一行字。

    这类缺陷只有「真的打开页面看一眼」才会发现（本轮是 GUI 脚本读
    `body.innerText` 时顺带撞到的），所以必须机械化。
    """

    #: 允许的例外：(相对路径, 行号) → 理由。没有理由的例外就是下一个洞。
    EXEMPT = {}

    def test_no_multiline_django_comment(self):
        files = sorted(glob.glob(
            os.path.join(ROOT, 'templates', '**', '*.html'), recursive=True))
        self.assertGreater(len(files), 10,
                           '一个模板都没扫到 —— 扫描逻辑失效了，不是「全部通过」')
        offenders = []
        for path in files:
            rel = os.path.relpath(path, ROOT)
            in_comment_block = False
            with open(path, encoding='utf-8') as f:
                for lineno, line in enumerate(f, 1):
                    if '{% comment %}' in line:
                        in_comment_block = True
                    if '{% endcomment %}' in line:
                        in_comment_block = False
                        continue
                    if in_comment_block:
                        continue
                    # 先剥掉**同一行内**已经闭合的注释
                    if '{#' in re.sub(r'\{#.*?#\}', '', line):
                        if (rel, lineno) in self.EXEMPT:
                            continue
                        offenders.append(f'{rel}:{lineno}: {line.strip()[:80]}')
        self.assertEqual(
            offenders, [],
            '以下 `{#` 没有在同一行闭合 —— 注释内容会被当成正文渲染出来：\n  '
            + '\n  '.join(offenders))


class MarkdownLeakInUiCopyTest(SimpleTestCase):
    """UI 文案里不许出现 Markdown 的 `**加粗**` —— 星号会**原样显示**给用户。

    `TNR_UI.confirm()` / 各列表渲染走的是 **HTML**，不是 Markdown。作者在提示语
    里写 `**无法撤回**` 想表达加粗，但这里没有任何 Markdown 解析器：用户看到的
    就是一对星号。第四十六轮走查实测两处：

      * `platform/portal.html` 发布公告确认框：`公告一经发布**无法撤回**`
      * `hospital/portal.html` 旧口径提示条：`这是**旧口径**的一次性登记`

    两处都不报错、不 500、接口全 200 —— 只有真的把页面打开、把弹窗读出来
    才会发现（本轮是在平台端发布公告时顺带撞到的）。所以必须机械化。

    ⚠ 判据必须先**剥注释**：本项目里大量解释性注释（`/** … **/`、
    `<!-- … ** … -->`）会让「源码里出现 `**`」这条断言**恒真**。
    这里用 `scripts/jslex.blank_comments()` —— 它**只抹注释、保留字符串
    字面量**。反过来用 `strip_js` 会把字面量一起抹掉，于是 `assertIn`
    **恒假**，同样抓不到真缺陷（两个方向都踩过）。
    """

    #: HTML 注释。`blank_comments()` 只管 `<script>` 内的 JS 注释。
    HTML_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)
    #: `<style>` 整块 —— CSS 注释（`/* … */`）住在里面，同样是「解释性文字」。
    STYLE_BLOCK_RE = re.compile(r'<style\b.*?</style\s*>', re.DOTALL)

    @staticmethod
    def _blank(match):
        """等长抹白（保留换行）—— 抹掉的同时行号仍然可用。"""
        return re.sub(r'[^\n]', ' ', match.group(0))

    @classmethod
    def _visible_source(cls, src):
        """返回「用户可见文本」的等长副本：注释被抹白，正文原样保留。"""
        probe = blank_comments(src)
        probe = cls.HTML_COMMENT_RE.sub(cls._blank, probe)
        probe = cls.STYLE_BLOCK_RE.sub(cls._blank, probe)
        return probe

    def test_no_markdown_bold_in_ui_copy(self):
        files = sorted(glob.glob(
            os.path.join(ROOT, 'templates', '**', '*.html'), recursive=True))
        # `static/js/` 也必须扫：`TNR_UI` 的 toast / 批次预览 / 出库提示
        # 都住在这里，第四十八轮「物料出库」那条 `将**增加**库存` 就是
        # 只扫模板时漏掉的（`tnr-common.js`）。
        files += sorted(glob.glob(
            os.path.join(ROOT, 'static', 'js', '*.js'), recursive=True))
        self.assertGreater(len(files), 10,
                           '一个前端文件都没扫到 —— 扫描逻辑失效了，不是「全部通过」')
        offenders = []
        for path in files:
            rel = os.path.relpath(path, ROOT)
            with open(path, encoding='utf-8') as f:
                src = f.read()
            probe = self._visible_source(src)
            # 等长性自检：抹白若把整份文件抹没了，下面会「全部通过」。
            self.assertEqual(len(probe), len(src),
                             f'{rel}: 剥离器改变了长度，判据不可信')
            for m in re.finditer(r'\*\*', probe):
                lineno = src.count('\n', 0, m.start()) + 1
                line = src.split('\n')[lineno - 1].strip()
                offenders.append(f'{rel}:{lineno}: {line[:80]}')
        self.assertEqual(
            offenders, [],
            '以下 UI 文案里的 `**` 会被原样渲染（HTML 不解析 Markdown）——'
            ' 想加粗请写 <strong>：\n  ' + '\n  '.join(offenders))

    def test_scanner_can_actually_see_a_planted_defect(self):
        """反向对照：判据写成「一律通过」也能过，所以必须证明它抓得到。

        同时验证「注释里的 `**` 不算数」—— 否则这条判据会把本项目里上千条
        解释性注释全报出来，第一个真缺陷反而被淹没。
        """
        planted = (
            '<div>正常文案</div>\n'
            '<!-- 注释里的 **不该被抓** -->\n'
            '<style>/* CSS 注释里的 **也不该被抓** */</style>\n'
            '<script>\n'
            "  var tip = '这是**加粗**文案';\n"
            '  // JS 注释里的 **同样不该被抓**\n'
            '</script>\n'
        )
        probe = self._visible_source(planted)
        self.assertEqual(len(probe), len(planted), '剥离器必须等长')
        hits = [m.start() for m in re.finditer(r'\*\*', probe)]
        self.assertEqual(
            len(hits), 2,
            '应当且只应当抓到字符串字面量里的那一对星号，实得 %d 个' % len(hits))
        self.assertIn('加粗', probe[hits[0]:hits[1] + 2],
                      '抓到的位置不对 —— 不是那对包着「加粗」的星号')
        for word in ('不该被抓', '也不该被抓', '同样不该被抓'):
            self.assertNotIn(word, probe, '注释没有被抹白')


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


class LocationFlowTest(SimpleTestCase):
    """定位流程（第三十九轮）。

    磊哥反馈「显示无法获取定位」。**实测结论：后端完全正常** ——
    以真实湖北公网 IP 请求 `/api/business/geocode/ip/`，高德返回
    「湖北省襄阳市襄城区中原街道晨光路幸福社区」，`source=client_ip`。
    根因是站点跑在 `http://公网IP` 上：非安全上下文下浏览器**强制**禁止
    `navigator.geolocation` 且连权限框都不弹，网页代码绕不过去。

    代码侧真正能修的是「拿不到 GPS 时别白等 15 秒」和「把降级原因说清楚」。
    """

    SHELTER = PORTALS['shelter']

    def test_gps_is_requested_in_two_stages(self):
        """先高精度、失败再低精度。

        旧版只发一次 `enableHighAccuracy: true` + 15 秒硬超时：高精度会强制
        启用 GPS 芯片，室内要 20~30 秒才收敛，于是室内实测大量超时，
        直接掉到城市级兜底 —— 现场体感就是「定位没用」。
        """
        html = read(self.SHELTER)
        self.assertIn('_gpsOnce(true,', html, '缺少高精度阶段的请求')
        self.assertIn('_gpsOnce(false,', html, '缺少低精度兜底阶段的请求')

    def test_single_shot_high_accuracy_call_is_gone(self):
        html = read(self.SHELTER)
        self.assertNotIn('enableHighAccuracy: true, timeout: 15000', html,
                         '旧的一次性高精度请求还在：室内会白等 15 秒才降级')

    def test_denied_permission_does_not_retry(self):
        """用户点了「拒绝」再问一次还是拒绝，不该白白再等一轮。"""
        html = read(self.SHELTER)
        idx = html.find('_doGpsLocate()')
        self.assertGreater(idx, 0, '找不到 _doGpsLocate')
        body = html[idx:idx + 2200]
        self.assertIn('code === 1', body, '没有区分「用户拒绝」与「信号弱」')
        self.assertIn('denied', body)

    def test_idle_hint_is_a_single_constant(self):
        """初始化与「重置表单」两处文案必须同源，不能各写一份字面量。"""
        html = read(self.SHELTER)
        self.assertIn('LOC_HINT_IDLE', html)
        self.assertEqual(
            html.count('未定位（点击「定位」获取当前地址及经纬度）'), 1,
            '「未定位」提示出现多次 —— 两处字面量迟早会漂移，应收成常量')
        self.assertEqual(
            html.count('未定位（点击「定位」获取大致位置'), 1)

    def test_server_ip_location_is_never_autofilled(self):
        """`source === 'server_ip'` 拿到的**是服务器机房所在城市**，与用户无关。

        线上真实事故：襄阳的手机点「定位」，详细地址被填成
        「北京市东城区交道口街道辛安里南锣鼓巷」，用户直接提交。
        宁可留空让用户手填，也不要给一个看起来合理的错地址。
        """
        html = read(self.SHELTER)
        idx = html.find("info.source === 'server_ip'")
        self.assertGreater(idx, 0, '找不到 server_ip 分支')
        branch = html[idx:idx + 700]
        self.assertIn('pendingLocation = null', branch,
                      'server_ip 的结果被写进了待提交定位 —— 会把用户带到一个'
                      '与本人无关的城市地址')


# ---------------------------------------------------------------------------
# 「起止时间」搜索条件：每个列表（含数据概览）都要有，且判据必须唯一
# ---------------------------------------------------------------------------
#
# 需求原话：「每个列表的搜索条件包括『数据概览』，全部添加：起止时间 作为搜索条件。」
#
# 这类改动最容易留下三种**不报错**的缺陷：
#   1. 某个列表**没挂**起止时间控件 —— 只是少个控件，肉眼扫不出来；
#   2. 控件挂了但**没接判据** —— 填了日期列表纹丝不动，比没有控件更糟；
#   3. 判据**各写一份** —— 同一个区间在不同页面筛出不同结果，永远对不上账。
#
# 下面的用例分别锁这三件事。
# ---------------------------------------------------------------------------

DATE_RANGE_KEYS = ('start_date', 'end_date')

# 「控件存在、但判据在服务端」的列表渲染方法（需要明确理由才准豁免）。
BACKEND_DATE_FILTERED = {
    ('gov', 'renderLedgerTable'):
        '台账走服务端过滤：start_date / end_date 直接透传给 /api/supervision/ledger/，'
        '前端不再做内存过滤（否则会把服务端已分页的结果再筛一遍）',
}

# 领养人端不用 mountTable（移动端卡片列表），所以单独登记。
ADOPTER_DATE_LISTS = (
    ('renderHall', '领养大厅 —— 上架时间'),
    ('renderMyAdoption', '我的领养 —— 申请 applied_at / 记录 adopted_at'),
    ('renderCheckinHistory', '打卡历史 —— 提交时间'),
    ('renderMessages', '消息中心 —— 产生时间'),
)

PORTAL_METHOD_HEADER = re.compile(
    r'(?m)^  (?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{')
TOP_FN_HEADER = re.compile(
    r'(?m)^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{')
ENSURE_DATE_BAR = re.compile(r"ensureDateBar\(\s*'([\w]+)'\s*,\s*'([\w]+)'")


def call_first_arg(source, start):
    """返回 `(` 之后**第一个顶层实参**的文本（忽略括号/引号内的逗号）。

    用来判断 `inDateRange(<第一个实参>, start, end)` 的第一个实参里
    到底比的是哪个时间字段 —— 实参可能跨多行、可能嵌套函数调用。
    """
    i, n, depth = start, len(source), 0
    out = []
    while i < n:
        c = source[i]
        if c in '\'"`':
            q = c
            out.append(c)
            i += 1
            while i < n and source[i] != q:
                if source[i] == '\\':
                    out.append(source[i])
                    i += 1
                out.append(source[i])
                i += 1
            out.append(q)
            i += 1
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                break
            depth -= 1
        elif c == ',' and depth == 0:
            break
        out.append(c)
        i += 1
    return ''.join(out)


def method_spans(source, header_re, stop_re=None):
    """按「方法头」切分源码，返回 ``[(名字, 起, 止)]``。

    **不用花括号配平**：门户模板里有大量跨行模板字符串（整段 HTML），
    里面出现的 `{` / `}` 会让朴素计数失步，把方法体截断（假阴性）或越界
    （假阳性）。方法头本身就是天然分隔符，按它切更稳 —— 代价是方法体
    可能略微多算，方向上偏「宽松」，不会误报。
    """
    heads = [(m.group(1), m.start()) for m in header_re.finditer(source)]
    stop = len(source)
    if stop_re:
        m = stop_re.search(source)
        if m:
            stop = m.start()
    return [
        (name, start, heads[i + 1][1] if i + 1 < len(heads) else stop)
        for i, (name, start) in enumerate(heads)
    ]


def portal_body(source, var_name):
    """取 `const <var_name> = { ... };` 的文本；找不到返回 None。"""
    m = re.search(r'(?m)^(?:const|let|var)\s+' + re.escape(var_name) + r'\s*=\s*\{', source)
    if not m:
        return None
    end = re.search(r'(?m)^\};', source[m.end():])
    if not end:
        return None
    return source[m.start():m.end() + end.end()]


class _IdParentParser(HTMLParser):
    """收集模板里每个 ``id`` 的**最近的带 id 祖先**。

    用来断言「时间筛选条」与「列表容器」是**兄弟节点** —— 这正是本项目
    最防的那类静默缺陷：筛选条若被放进列表容器里，列表一重建它就被连同
    节点一起清掉，用户看到的是「选完日期，控件消失了」。
    """

    VOID = frozenset((
        'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
        'link', 'meta', 'param', 'source', 'track', 'wbr',
    ))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parent = {}
        self.stack = []

    def _parent_id(self):
        for _tag, el_id in reversed(self.stack):
            if el_id:
                return el_id
        return None

    def handle_starttag(self, tag, attrs):
        el_id = dict(attrs).get('id')
        if el_id:
            self.parent[el_id] = self._parent_id()
        if tag not in self.VOID:
            self.stack.append((tag, el_id))

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


class DateRangeFilterTest(SimpleTestCase):
    """起止时间：公共层契约。"""

    COMMON = 'static/js/tnr-common.js'

    def test_render_filter_bar_appends_date_range(self):
        """`renderFilterBar` 必须**默认追加**起止时间。

        抽成常量 + 统一追加，而不是在 30 多个调用点各写一遍：逐处手写必然漏，
        而漏掉的表现是「这一页没有起止时间」—— 不报错、只是少个控件。
        """
        source = read(self.COMMON)
        idx = source.find("renderFilterBar(filters, actions = '', opts = {})")
        self.assertGreater(idx, 0, 'renderFilterBar 签名变了，本用例需要同步')
        body = source[idx:idx + 1800]
        self.assertIn('DATE_RANGE_FILTERS', body,
                      'renderFilterBar 不再引用 DATE_RANGE_FILTERS —— '
                      '所有列表会一起丢掉起止时间控件')
        self.assertIn('concat(this.DATE_RANGE_FILTERS)', body,
                      '起止时间没有被拼进筛选条件列表')

    def test_date_range_filters_are_idempotent(self):
        """筛选条自己已经写了 `start_date` 的，不得再追加一组。

        重复追加会让同一页出现两组同名 `data-filter`，`getFilterValues`
        后者覆盖前者 —— 用户在**上面那组**输入的值被**静默抹掉**。

        ⚠ 这里必须断**行为**、不能断「标识符存在」：变异测试实测过，
        把 `hasDateRange` 的值改成常量 `false`（= 幂等判断被废掉）之后，
        `assertIn('hasDateRange', body)` 依然通过 —— 这种断言绿的原因和
        被删掉的逻辑毫无关系。
        """
        source = read(self.COMMON)
        idx = source.find("renderFilterBar(filters, actions = '', opts = {})")
        body = source[idx:idx + 1800]
        self.assertRegex(
            body,
            r"hasDateRange\s*=\s*list\.some\(\s*f\s*=>\s*f\s*&&\s*f\.key\s*===\s*'start_date'\s*\)",
            '幂等判断不再是「按 start_date 探测」了 —— 同一页会出现两组同名筛选控件')
        self.assertRegex(
            body, r'opts\.noDateRange\s*\|\|\s*hasDateRange',
            '幂等判断没有被真正接进追加条件')
        self.assertIn('noDateRange', body,
                      '缺少显式关闭的逃生口（留给没有时间字段的实体）')

    def test_date_range_keys_match_backend_params(self):
        """前端的 key 必须与后端读的查询参数同名。

        政府端台账把这两个 key **直接透传**给 `/api/supervision/ledger/`；
        前端改成 `dateFrom` 之类，后端只会收到空值 —— 筛选静默失效，
        接口照样 200 返回全量。
        """
        source = read(self.COMMON)
        idx = source.find('DATE_RANGE_FILTERS: [')
        self.assertGreater(idx, 0, '找不到 DATE_RANGE_FILTERS')
        block = source[idx:idx + 400]
        keys = re.findall(r"key:\s*'([^']+)'", block)
        self.assertEqual(keys, list(DATE_RANGE_KEYS),
                         f'DATE_RANGE_FILTERS 的 key 变成了 {keys}，'
                         f'与后端 parse_date_param 读的 {DATE_RANGE_KEYS} 不同名')

        backend = read('supervision/views.py')
        for key in DATE_RANGE_KEYS:
            self.assertIn(f"request.GET.get('{key}')", backend,
                          f'后端台账不再读 {key} —— 前端的起止时间会静默失效')

    def test_in_date_range_is_the_single_implementation(self):
        """全站**只有一个**时间区间判据，且换算本地日期。

        - 各写一份必然漂移，漂移的表现是「同一个区间在不同页面筛出不同结果」；
        - 裸截断 `.slice(0, 10)` 会把北京时间 00:00–08:00 的记录算到**前一天**。
        """
        source = read(self.COMMON)
        idx = source.find('inDateRange(dateValue, start, end) {')
        self.assertGreater(idx, 0, 'inDateRange 不见了')
        body = source[idx:idx + 700]
        self.assertIn('localDateStr', body,
                      'inDateRange 必须用 localDateStr 换算本地日期，'
                      '不能裸截断 ISO 串（UTC 串会让日期差一天）')
        self.assertNotIn('slice(0, 10)', body)

        # 除 tnr-common.js 外，任何前端文件都不得再**定义**一份同名判据。
        # （各端调用 `inDateRange(dateValue, ...)` 是正常的，这里只认定义体。）
        definition = re.compile(
            r'inDateRange\s*\(\s*dateValue\s*,\s*start\s*,\s*end\s*\)\s*\{')
        for rel in FRONTEND_FILES:
            if rel == self.COMMON:
                continue
            self.assertIsNone(definition.search(read(rel)),
                              f'{rel} 里又定义了一份 inDateRange —— 判据必须唯一')


class MobileLayoutTest(SimpleTestCase):
    """390px 真机视口下的可用性契约（第五十五轮）。

    这三类缺陷都是拿 390×844 视口实测出来的，静态断言锁定的是修法：
    - 标签栏 4–6 个标签时内容宽 497–707px、容器只有 336–342px，
      原先没有横向滚动，右侧标签永远点不到；
    - 日志行「时间+模块+描述」挤在一行，描述只剩 ~70px，单行 217–255px 高；
    - 分页按钮 32px、领养人端「清空」25px，低于 44px 触控热区。
    """

    CSS = 'static/css/tnr-traditional.css'
    COMMON = 'static/js/tnr-common.js'

    def test_tabs_scroll_horizontally_on_mobile(self):
        css = _read_raw(self.CSS)
        self.assertRegex(
            css, r'body \.page-view \.tabs\s*\{[^}]*overflow-x:\s*auto',
            '标签栏没有横向滚动 —— 手机上超过 3 个标签就被裁掉，右侧标签点不到')
        self.assertRegex(
            css, r'body \.page-view \.tabs \.tab-item\s*\{[^}]*flex:\s*0 0 auto',
            '标签项会在横向滚动区里被压缩，应固定原宽')

    def test_date_filters_are_rendered_as_a_pair(self):
        source = read(self.COMMON)
        idx = source.find("renderFilterBar(filters, actions = '', opts = {})")
        self.assertGreater(idx, 0, 'renderFilterBar 签名变了，本用例需要同步')
        body = source[idx:idx + 2400]
        self.assertIn('filter-date-pair', body,
                      '起止日期没有成对包裹 —— 手机端筛选条会多占一整行')
        self.assertRegex(
            body,
            r"f\.type === 'date' && all\[i \+ 1\] && all\[i \+ 1\]\.type === 'date'",
            '配对条件不再识别「连续两个日期控件」')
        rule = css_rule(_read_raw(self.CSS), '.filter-date-pair')
        self.assertIsNotNone(rule, '缺少 .filter-date-pair 的布局规则')
        self.assertRegex(rule, r'display:\s*flex')

    def test_mobile_filter_bar_uses_two_columns(self):
        """手机端筛选条两列并排，日期对与操作按钮跨两列。

        单列堆叠时 5 个条件占 5×70px，列表首屏被筛选条挤没；
        但日期对若被拆到两列，「开始/结束」会各占一行、反而更高。
        """
        css = _read_raw(self.CSS)
        self.assertRegex(
            css,
            r'body \.filter-bar\s*\{[^}]*display:\s*grid;'
            r'[^}]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)',
            '移动端筛选条仍是单列堆叠 —— 多个条件会挤占整屏')
        self.assertRegex(
            css,
            r'\.filter-date-pair,\s*\.filter-actions\s*\{[^}]*grid-column:\s*1 / -1',
            '日期对/操作按钮没有跨列 —— 开始、结束日期会被拆到两行')

    def test_mobile_table_actions_stay_visible(self):
        """宽表横向滚动时操作列必须钉在右侧。

        捕捉记录 7 列、平台账号 9 列，390px 下要横向滑动；操作列原先在
        最右端，用户每次都要滑到底才能点「详情/编辑」。
        """
        css = _read_raw(self.CSS)
        self.assertRegex(
            css,
            r'body \.table-wrapper \.data-table th\.dt-actions-th,\s*'
            r'body \.table-wrapper \.data-table td\.dt-actions-td\s*\{'
            r'[^}]*position:\s*sticky;[^}]*right:\s*0;',
            '移动端操作列没有固定在右侧 —— 宽表必须滑到最右才能操作')
        self.assertRegex(
            css,
            r'body \.table-wrapper \.data-table th\.dt-actions-th\s*\{'
            r'[^}]*background:\s*var\(--rice-warm\)',
            '固定后的表头操作列必须保持表头底色，否则滚动内容会透出')

    def test_mobile_wide_tables_collapse_low_priority_columns(self):
        """宽表在手机上收成「主列 + 操作」，低频列并入主列。

        390px 实测账号表宽 996px、可视 332px（横滑 664px），捕捉记录 613px
        （横滑 281px）；操作列吸附只让按钮「点得到」，每行仍要来回滑才能
        看全。列压缩必须两半同时成立：低频列隐藏 + 主列 `mobileRender`
        补回，缺一半就是信息消失或桌面重复。
        """
        css = _read_raw(self.CSS)
        self.assertRegex(
            css,
            r'body \.dt-table th\.dt-col-mobile-hide,\s*'
            r'body \.dt-table td\.dt-col-mobile-hide,\s*'
            r'body \.dt-table col\.dt-col-mobile-hide\s*\{[^}]*display:\s*none',
            '移动端没有隐藏低频列 —— 9 列账号表仍要横滑 664px')
        self.assertRegex(
            css,
            r'body \.dt-table \.dt-mobile-only\s*\{[^}]*display:\s*block',
            '主列的手机端详情没有显示 —— 低频列隐藏后信息会彻底丢失')
        common = _read_raw(self.COMMON)
        self.assertIn("col.mobile === 'hide'", common,
                      'mountTable 不再识别 mobile:hide —— 列压缩失效')
        self.assertIn('col.mobileRender', common,
                      'mountTable 不再渲染 mobileRender —— 隐藏列的信息无法补回')
        platform = _read_raw('static/js/portals/platform.js')
        # ⚠ 不能写 `assertIn('mobileRender: mobileCard', platform)`：platform.js 里
        # 有**两张**宽表用同一个写法（账号表主列 `key: 'username'`、单据表主列
        # `key: 'doc_label'`），删掉任意一张的 `mobileRender` 另一张照样让它通过
        #（变异 M170 实测：整条用例**空转**）。按表逐个点名，缺一处红一处。
        self.assertRegex(
            platform, r"\{ key: 'username'[^\n]*mobileRender: mobileCard",
            '账号表主列没有手机端详情 —— 姓名/角色/状态会消失')
        self.assertRegex(
            platform, r"\{ key: 'doc_label'[^\n]*mobileRender: mobileCard",
            '单据表主列没有手机端详情 —— 状态/单据号会消失')
        self.assertGreaterEqual(
            platform.count("mobile: 'hide'"), 8,
            '账号表应把姓名/角色/区县/机构/证号/电话/开户时间/状态在手机收起')
        shelter = _read_raw('static/js/portals/shelter.js')
        self.assertIn('mobileRender: mobileCard', shelter,
                      '捕捉记录主列没有手机端详情 —— 小区/数量/转运状态会消失')
        self.assertGreaterEqual(
            shelter.count("mobile: 'hide'"), 5,
            '捕捉记录应把小区/物业/数量/转运状态/时间在手机收起')

    def test_pagination_targets_are_touch_sized_on_mobile(self):
        css = _read_raw(self.CSS)
        self.assertRegex(
            css,
            r'@media \(max-width: 768px\)[\s\S]*?\.pagination-btn\s*\{'
            r'[^}]*min-width:\s*44px;[^}]*height:\s*44px;',
            '分页按钮在手机上仍是 32px —— 触控热区不足')

    def test_mobile_log_rows_stack_detail(self):
        for role in ('platform', 'gov'):
            html = read_html(role)
            self.assertRegex(
                html,
                r'@media \(max-width:768px\)\{\s*\.log-entry\{[^}]*flex-wrap:wrap[^}]*\}'
                r'[\s\S]*?\.log-detail\{[^}]*flex:1 1 100%',
                f'{role} 端日志描述没有独占一行 —— 手机上单条日志会撑到 217px 以上')

    def test_adopter_date_clear_is_touch_sized(self):
        rule = css_rule(read_html('adopter'), '.mobile-date-bar .date-bar-clear')
        self.assertIsNotNone(rule, '领养人端「清空」按钮的规则不见了')
        self.assertRegex(rule, r'min-width:\s*44px')
        self.assertRegex(rule, r'min-height:\s*44px')

    def test_date_pair_inputs_keep_desktop_width(self):
        """成对日期在桌面端必须保留 140px 最小宽度。

        第五十五轮引入 `.filter-date-pair` 时把 `min-width: 0` 写进了基础规则，
        1366×900 实测两个日期输入被压到 121px（日期串显示不全）。
        收缩只应发生在 768px 断点内。
        """
        rule = css_rule(
            _read_raw(self.CSS), '.filter-date-pair .filter-item .form-input')
        self.assertIsNotNone(rule, '成对日期输入的宽度规则不见了')
        self.assertNotRegex(
            rule, r'min-width:\s*0',
            '基础规则禁止 min-width:0 —— 桌面端日期输入会被压到 121px')
        self.assertRegex(rule, r'width:\s*100%')

    def test_gov_stat_grid_does_not_pin_three_columns_inline(self):
        js = read_js('gov')
        self.assertNotIn(
            'grid-template-columns:repeat(3,1fr);margin-bottom', js,
            '内联三列覆盖了移动端两列栅格 —— 390px 宽下每列只有 111px')
        self.assertIn(
            'stat-grid stat-grid-3', js,
            '三列统计区必须挂 .stat-grid-3 类：桌面三列、移动端两列由样式表统一裁决')
        css = _read_raw(self.CSS)
        self.assertRegex(
            css,
            r'body \.stat-grid\.stat-grid-3\s*\{[^}]*'
            r'repeat\(3, minmax\(0, 1fr\)\)',
            '桌面端缺少 .stat-grid-3 的三列规则')
        self.assertRegex(
            css,
            r'@media \(max-width: 768px\)[\s\S]*?'
            r'body \.stat-grid\.stat-grid-3\s*\{[^}]*repeat\(2,',
            '移动端缺少 .stat-grid-3 的两列回退')


class DateRangeCoverageTest(SimpleTestCase):
    """起止时间：每个列表都要真正接上判据。"""

    def _spans(self, name, source):
        """返回 ``(spans, body)``。

        ⚠ `spans` 里的偏移一律换算成**相对整个 source 的绝对值** ——
        门户端的方法定义在 `const Xxx = { ... }` 内部，若直接把「相对对象体」
        的偏移拿去索引整个文件，切出来的就是**错位的一段**（实测会切到别的
        方法上，报出一串根本不存在的缺陷）。
        """
        obj = PORTAL_OBJECTS.get(name)
        if not obj:
            return method_spans(source, TOP_FN_HEADER), source
        m = re.search(r'(?m)^(?:const|let|var)\s+' + re.escape(obj) + r'\s*=\s*\{', source)
        self.assertIsNotNone(m, f'{name}: 找不到门户对象 {obj}')
        body = portal_body(source, obj)
        self.assertIsNotNone(body, f'{name}: 门户对象 {obj} 未闭合')
        base = m.start()
        spans = [(n, base + s, base + e)
                 for n, s, e in method_spans(body, PORTAL_METHOD_HEADER)]
        return spans, source

    def test_every_table_renderer_uses_the_date_judgement(self):
        """凡调用 `mountTable(` 的方法，都必须**直接或间接**用到 `inDateRange`。

        间接 = 调用了某个含 `inDateRange` 的辅助方法（如医院端 `_matQuery()`）
        —— 只认直接调用会把「把判据抽成共用函数」这种**正确**做法判成缺陷。
        """
        problems = []
        checked = 0
        for name, rel in PORTALS.items():
            source = strip_js_comments(read(rel))   # 等长替换，行号不变
            spans, _body = self._spans(name, source)
            segs = [source[s:e] for _n, s, e in spans]

            # 含判据但不渲染表格的方法 = 可复用的时间判据助手
            helpers = {
                n for (n, _s, _e), seg in zip(spans, segs)
                if 'inDateRange' in seg and 'mountTable(' not in seg
            }

            for (n, s, _e), seg in zip(spans, segs):
                if 'mountTable(' not in seg:
                    continue
                if (name, n) in BACKEND_DATE_FILTERED:
                    continue
                checked += 1
                direct = 'inDateRange' in seg
                indirect = any(
                    re.search(r'\b' + re.escape(h) + r'\b', seg) for h in helpers)
                if not (direct or indirect):
                    line_no = source.count('\n', 0, s) + 1
                    problems.append(
                        f'{name}:{line_no} {n}() 渲染了表格却没有任何时间判据 —— '
                        '界面上有「开始/结束日期」，填了列表纹丝不动')

        self.assertGreaterEqual(checked, 20, '解析到的列表渲染方法太少，用例可能已失效')
        self.assertFalse(
            problems,
            '以下列表缺少起止时间判据：\n  ' + '\n  '.join(problems))

    def test_backend_filtered_lists_still_pass_dates_through(self):
        """豁免「前端不筛」的列表，必须真的把日期透传给服务端。

        豁免清单如果被写成「什么都不做」，就等于把这个列表的起止时间
        静默变成装饰品。

        ⚠ 同样要断**值从哪来**，不能只断 `start_date:` 这个 key 存在 ——
        变异测试实测过：把值改成字面量 `''` 之后，`assertIn('start_date:', seg)`
        照样通过。
        """
        source = strip_js_comments(read(PORTALS['gov']))
        spans, _ = self._spans('gov', source)
        segs = {n: source[s:e] for n, s, e in spans}
        seg = segs['renderLedgerTable']
        for key in DATE_RANGE_KEYS:
            self.assertRegex(
                seg, key + r"\s*:\s*rawFilters\." + key,
                f'政府端台账没有把用户填的 {key} 透传给后端（值被写成了常量）')

    def test_adopter_lists_have_date_filters(self):
        """领养人端 4 个列表：既要有筛选条，也要接判据。"""
        source = strip_js_comments(read(PORTALS['adopter']))
        spans = method_spans(source, TOP_FN_HEADER)
        segs = {n: source[s:e] for n, s, e in spans}
        for fn, desc in ADOPTER_DATE_LISTS:
            self.assertIn(fn, segs, f'领养人端找不到 {fn}（{desc}）')
            seg = segs[fn]
            self.assertIn('inDateRange', seg, f'{fn}（{desc}）没有接时间判据')
            self.assertIn('dateFilterOf', seg, f'{fn}（{desc}）没有读筛选状态')

    def test_adopter_date_bars_sit_outside_the_list_containers(self):
        """领养人端筛选条必须与列表容器**同级**。

        放进列表容器里的后果：`renderXxx()` 一重建 innerHTML，输入框连同节点
        一起被清掉 —— 用户看到「选完日期，控件没了」，而控制台干干净净。
        """
        html = read(PORTALS['adopter'])
        tree = _IdParentParser()
        tree.feed(html)
        parent = tree.parent

        expected_siblings = {
            'hallDateBar': ('page-hall', 'hallList'),
            'myAdoptionDateBar': ('page-my-adoption', 'myAdoptionList'),
            'messagesDateBar': ('page-messages', 'messagesList'),
            'checkinDateBar': ('checkinHistorySection', 'checkinHistory'),
        }
        for bar, (page, listing) in expected_siblings.items():
            self.assertIn(bar, parent, f'模板里找不到 #{bar}')
            self.assertIn(listing, parent, f'模板里找不到 #{listing}')
            self.assertEqual(
                parent[bar], page,
                f'#{bar} 的父节点是 #{parent[bar]}，期望 #{page}')
            self.assertEqual(
                parent[listing], page,
                f'#{listing} 的父节点是 #{parent[listing]}，期望 #{page}')
            self.assertNotEqual(
                parent[bar], listing,
                f'#{bar} 被放进了 #{listing} 里面 —— 列表一重建筛选条就会消失')

        # 打卡历史必须是独立区块，否则重渲染会把表单里填好的内容清空。
        self.assertEqual(parent.get('checkinHistorySection'), 'page-checkin')
        self.assertEqual(parent.get('checkinContent'), 'page-checkin')

    def test_adopter_date_bar_mount_ids_exist(self):
        """`ensureDateBar` 的挂载点 id 必须真实存在。

        写错 id 的表现是「这一页根本没有筛选条」—— `getElementById` 返回
        null，函数直接 return，不报错。
        """
        html = read(PORTALS['adopter'])
        source = strip_js_comments(html)
        pairs = ENSURE_DATE_BAR.findall(source)
        self.assertGreaterEqual(len(pairs), 4,
                                '领养人端挂载的起止时间筛选条少于 4 个')
        self.assertIn('function dateBarHtml(key)', source,
                      'dateBarHtml 不见了，筛选条渲染不出控件')
        for key, mount_id in pairs:
            self.assertIn(f'id="{mount_id}"', html,
                          f'ensureDateBar({key!r}, {mount_id!r}) 的挂载点不存在')
            # 状态表里必须有对应条目，否则 dateFilterOf 会现场新建一个
            # —— 值读得回来，但**区间不会在页面间保留**，且失败时无从排查。
            self.assertIn(f"{key}: {{ start: '', end: '' }}", source,
                          f'dateFilters 里没有 {key} 的条目')

    def test_institution_has_created_at(self):
        """`Institution` 必须有建档时间。

        政府端「机构管理」列表要按起止时间筛选，而 `Institution` 曾经是
        **唯一没有任何时间字段**的业务列表实体。缺字段不会报错：
        `inDateRange` 对「设了区间、记录没有时间」返回 false →
        **一填日期就整列空表**，看起来就像「这个区间真的没有机构」。
        """
        from core.models import Institution
        field_names = {f.name for f in Institution._meta.concrete_fields}
        self.assertIn('created_at', field_names,
                      'Institution 缺少 created_at —— 政府端机构列表的'
                      '起止时间会把所有机构筛掉（空表且不报错）')


# ---------------------------------------------------------------------------
# 可空时间字段必须带「创建时间」兜底
# ---------------------------------------------------------------------------
#
# `inDateRange` 的语义是「设了区间、记录没有时间 → 排除」。这条语义只有在
# **每行都拿得到时间**时才是对的。但很多业务时间是**可空**的，因为事件还没
# 发生：`Adoption.adopted_at`（待领出为空）、`Transfer.received_at`（待签收为空）、
# `Euthanasia.euthanized_at`、`Release.released_at` 都是这样。
#
# 直接拿它们当唯一判据的后果**不是报错**，而是：用户圈一个「全部」区间
# （2000~2099）反而把这些行**藏起来**。GUI 实测抓到过 ——
# `Adoption` 5 行，圈 2000~2099 后只剩 3 行，界面没有任何提示。
# ---------------------------------------------------------------------------

# 事件未发生时即为空的业务时间字段（驼峰与蛇形两种别名都列上）。
#
# 判定依据是**模型的 `null=True`**，不是「名字里带 at」：
#   Adoption.adopted_at / Release.released_at / Euthanasia.euthanized_at /
#   Transfer.received_at / OwnerReturn.return_time / AdoptionHallListing.published_at
# 都是 `null=True`（`business/models.py` 可查）。
#
# ⚠ 特意**不**收录 `last_at`：它不是模型字段，而是公告列表聚合出来的
# `Max('created_at')`（`supervision/views.py` 的 notices 接口）。分组里
# 至少有一条 Message，`Max` 必然有值 —— 把它当可空字段会逼着调用点写一个
# 永远用不上的兜底，属于「按名字猜」而不是「按事实判」。
NULLABLE_TIME_FIELDS = (
    'adopted_at', 'adoptedAt',
    'released_at', 'releasedAt',
    'euthanized_at', 'euthanizedAt',
    'received_at', 'receivedAt',
    'return_time', 'returnTime',
    'published_at', 'publishedAt',
)

# 把时间值当参数传进去的判据助手（调用点上不出现字段名，单独用行级规则兜）。
DATE_FILTER_HELPERS = (
    'inDateRange', 'inRange(', 'inR(', 'hit(', 'matInR', '_inRange', '_dashInRange',
)


class NullableTimeFieldFallbackTest(SimpleTestCase):
    """可空业务时间字段一律要退到 `created_at`。"""

    CALL = re.compile(r'inDateRange\s*\(')

    def _nullable_in(self, text):
        return [f for f in NULLABLE_TIME_FIELDS
                if re.search(r'\.' + re.escape(f) + r'\b', text)]

    def test_in_date_range_args_fall_back_to_created_at(self):
        """`inDateRange(<可空字段>, ...)` 的第一个实参必须含 `created`。"""
        problems = []
        checked = 0
        for name, rel in PORTALS.items():
            source = strip_js_comments(read(rel))
            for m in self.CALL.finditer(source):
                arg = call_first_arg(source, m.end())
                fields = self._nullable_in(arg)
                if not fields:
                    continue
                checked += 1
                if 'created' not in arg.lower():
                    line_no = source.count('\n', 0, m.start()) + 1
                    problems.append(
                        f'{name}:{line_no} 用 {"/".join(fields)} 做唯一判据、'
                        f'没有 created_at 兜底 → {arg.strip()[:80]}')
        self.assertGreater(checked, 0, '没有解析到任何可空时间字段的调用点，用例可能已失效')
        self.assertFalse(
            problems,
            '可空时间字段缺 created_at 兜底（圈「全部」区间会把这些行藏起来）：\n  '
            + '\n  '.join(problems))

    def test_helper_passed_dates_fall_back_to_created_at(self):
        """把时间值当参数传给判据助手时，同一行也必须含 `created`。

        `hit(r, r.releasedAt || r.released_at)` 这种写法里，`inDateRange`
        收到的是一个变量，上一条用例看不见它 —— 但它同样是缺陷。
        """
        problems = []
        checked = 0
        for name, rel in PORTALS.items():
            source = strip_js_comments(read(rel))
            for line_no, line in enumerate(source.splitlines(), start=1):
                if not any(h in line for h in DATE_FILTER_HELPERS):
                    continue
                fields = self._nullable_in(line)
                if not fields:
                    continue
                checked += 1
                if 'created' not in line.lower():
                    problems.append(f'{name}:{line_no} {line.strip()[:90]}')
        self.assertGreater(checked, 0, '没有解析到任何「传参给判据助手」的行，用例可能已失效')
        self.assertFalse(
            problems,
            '传参给判据助手时缺 created_at 兜底：\n  ' + '\n  '.join(problems))

    def test_ledger_mapping_tables_list_created_at_fallback(self):
        """两个「时间字段映射表」必须给可空字段配好兜底列表。

        映射表把「哪个标签页比哪个字段」收口成一处（这是对的），但值是
        **字段列表**而不是单个字段名 —— 只写 `adopted_at` 的话，待领出的单子
        在任何区间下都筛不出来，包括「全部」。
        """
        problems = []
        for name, rel, var in (
            ('shelter', PORTALS['shelter'], 'LEDGER_DATE_KEYS'),
            ('gov', PORTALS['gov'], 'SUP_DATE_KEYS'),
        ):
            body = find_object_literal(read(rel), var)
            self.assertIsNotNone(body, f'{name}: 找不到 {var}')
            entries = re.findall(r"(\w+)\s*:\s*\[([^\]]*)\]", body)
            self.assertGreaterEqual(len(entries), 5, f'{name}: {var} 条目太少')
            for tab, inner in entries:
                fields = re.findall(r"'([^']+)'", inner)
                self.assertGreaterEqual(
                    len(fields), 1, f'{name}: {var}.{tab} 是空列表')
                if any(f in NULLABLE_TIME_FIELDS for f in fields):
                    if not any('created' in f.lower() for f in fields):
                        problems.append(f'{name}: {var}.{tab} = {fields}')
        self.assertFalse(
            problems,
            '映射表里的可空时间字段没有配 created_at 兜底：\n  ' + '\n  '.join(problems))


class CaptureCreateInteractionContractTest(SimpleTestCase):
    """新增捕捉的默认一只 / 加号增行 / 三类必传材料契约。"""

    PORTAL = PORTALS['shelter']

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = strip_js_comments(read(cls.PORTAL))

    def _method(self, name):
        marker = f'async {name}() {{'
        start = self.source.find(marker)
        self.assertGreaterEqual(start, 0, f'找不到 Shelter.{name}()')
        end = self.source.find('\n  },', start)
        self.assertGreater(end, start, f'Shelter.{name}() 未闭合')
        return self.source[start:end]

    def test_form_defaults_to_one_and_initializes_it_without_manual_generate(self):
        body = self._method('render_capture_add')
        self.assertRegex(body, r'id="ca_petCount"[^>]*value="1"')
        self.assertNotIn('btnGenCodes', body,
                         '新增捕捉不应再要求用户点击「自动生成」')
        self.assertRegex(
            body,
            r'bindQtyStepper\(countInput,[\s\S]*?onChange:[\s\S]*?this\._generatePetEntries\(\)[\s\S]*?\);',
            '数量右侧 + 必须触发重新生成编号和单只信息')
        self.assertIn('await this._generatePetEntries();', body,
                      '打开表单时必须自动生成默认的第 1 只')

    def test_submit_checks_all_three_required_materials_before_formdata(self):
        body = self._method('render_capture_add')
        submit = body[body.find('id="btnSubmitCapture"'):]
        # 用行为顺序锁定：三类必传守卫都要出现在 FormData 之前，
        # 否则会出现「界面提示了，但已经开始提交」的半成品路径。
        fd_pos = submit.find('new FormData()')
        self.assertGreater(fd_pos, 0, '找不到新增捕捉 FormData 构造')
        checks = (
            "!this._groupPhoto || !this._groupPhoto.file",
            "missingPetPhoto",
            'sigPad.isEmpty()',
        )
        for check in checks:
            with self.subTest(check=check):
                self.assertGreater(submit.find(check), -1)
                self.assertLess(submit.find(check), fd_pos,
                                f'{check} 必须在 FormData/写请求前校验')
        self.assertIn("fd.append('group_photo', this._groupPhoto.file)", submit)
        self.assertIn("fd.append('signature', sigPad.getDataURL())", submit)
        self.assertIn("fd.append('pet_photo_' + code, rec.file)", submit)

    def test_reset_restores_the_default_first_animal(self):
        """重置后必须重新生成第 1 只。

        「自动生成」按钮移除后，数量右侧的 + 是唯一增行入口；若重置只把数量
        设回 1 却不生成条目，用户就**再也没有办法录入任何一只**了 —— 而这条
        路径在页面上看起来完全正常（空列表 + 数量 1，点 + 也只能从 1 变 2，
        编号与条目依旧对不上）。
        """
        body = self._method('render_capture_add')
        # ⚠ 只搜 `btnResetCapture` 会先命中按钮**标签**本身（`id="btnResetCapture">重置表单`），
        #   截取到的片段里根本没有回调，断言就成了空转。必须定位到事件绑定处。
        start = body.find("getElementById('btnResetCapture')")
        self.assertGreater(start, 0, '找不到「重置」按钮的事件绑定')
        end = body.find('\n    });', start)
        self.assertGreater(end, start, '重置回调未闭合')
        reset = body[start:end]
        self.assertIn("addEventListener('click', async () => {", reset,
                      '重置回调必须是 async，才能等待编号生成')
        self.assertIn('await this._generatePetEntries();', reset,
                      '重置后必须重新生成默认的第 1 只')


# ============================================================
# 第四十三轮：门户首页（`/portal/`）与登录页的「入口 + 账号提示」必须完整
# ============================================================
#: 演示账号清单文档 —— 「系统里有哪些演示账号」的权威来源。
DEMO_ACCOUNTS_DOC = 'DEMO_ACCOUNTS.md'
#: 账号提示的两个落点（门户首页 / 登录页）。改一处必须改另一处。
DEMO_HINT_TARGETS = (
    ('portal', '/portal/', 'portal-demo-hint'),
    ('login', '/login/', 'login-demo-hint'),
)


class PortalLandingPageTest(TestCase):
    """门户首页的入口卡片与账号提示必须与系统实际状态一致。

    **真实案例（第四十二轮 → 第四十三轮）**：新增平台管理端 `/platform/` 后，
    门户首页仍是「四端门户」：

      * 平台管理端**在首页上完全不存在** —— 只有知道 URL 的人能手输进去；
      * 政府监管端卡片却还在宣传「机构管理 / 账号权限」—— 那两块已经搬去平台端，
        用户按文案去政府端找，**找不到任何入口，而系统不会有任何报错**；
      * 登录页的演示账号提示同样漏了 `platform`，照着提示是进不去管理后台的。

    这三条都不抛异常、不影响任何接口返回 200，属于典型的「文案承诺了不存在的
    功能 / 新功能没有入口」。只能靠判据盯住。

    **判据来源刻意不写死列表**：
      * 「有几个端」→ `accounts.views.ROLE_PORTAL_MAP`（登录后往哪跳的权威定义）；
      * 「有哪些演示账号」→ `DEMO_ACCOUNTS.md` 的表格。
    写死的话，将来加一个端而忘了加卡片，闸门照样是绿的 —— 那正是本轮要防的。

    **为什么是 `TestCase` 而不是本文件惯用的 `SimpleTestCase`**：这几条要
    **渲染真实页面**（走完整请求栈），而 `request_started` 上挂着
    `business.apps` 的启动补偿，它要查库；在 `SimpleTestCase` 里会被禁库拦下
    并记一条 ERROR 堆栈 —— 那只是测试噪音，但会盖住真正的失败信号。
    同 `core/tests.py::ApiAwareCsrfFailureEndToEndTest`。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        doc = read(DEMO_ACCOUNTS_DOC)
        cls.doc_accounts = set(
            re.findall(r'^\|\s*`([a-z][a-z0-9_]*)`\s*\|', doc, re.M))
        cls.portal_urls = sorted(set(ROLE_PORTAL_MAP.values()))

    # -- 辅助 ---------------------------------------------------------------

    def _html(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, f'{url} 返回 {resp.status_code}')
        # ⚠ 先剥 HTML 注释再断言。本文件已经**两次**栽在「判据是源码里有没有
        #   出现 X，而修复时顺手写的说明注释正好包含 X」上（见
        #   `strip_html_comments` 的注释）。剥掉之后断言的是**用户真正看得见的
        #   文字**，语义也更准确：HTML 注释不会渲染给用户。
        return strip_html_comments(resp.content.decode('utf-8'))

    @staticmethod
    def _card(html, accent):
        """按 `data-accent` 截出**卡片**（`<a>`）那一块。

        ⚠ 必须锚在 `<a class="portal-card` 上：同一段渲染结果里 `<style>`
          也含 `data-accent="gold"` 这类选择器，只按 `data-accent=` 找会先命中 CSS。
        """
        m = re.search(
            rf'<a class="portal-card[^>]*data-accent="{re.escape(accent)}".*?</a>',
            html, re.S)
        return m.group(0) if m else None

    @staticmethod
    def _hint_usernames(html, cls):
        """取出提示块里出现的**用户名**（`<b>小写标识符</b>`）。

        中文文案（如「演示账号」）与小写字母数字以外的内容都不会被匹配到，
        所以不需要维护「哪些 `<b>` 是用户名」的排除清单。
        """
        m = re.search(rf'class="{re.escape(cls)}"[^>]*>(.*?)</div>', html, re.S)
        return set(re.findall(r'<b>([a-z][a-z0-9_]*)</b>', m.group(1))) if m else set()

    # -- 入口卡片 -----------------------------------------------------------

    def test_doc_and_urlconf_are_parsed(self):
        """守卫的守卫：解析逻辑失效时下面几条会**静默变绿**。"""
        self.assertGreaterEqual(
            len(self.doc_accounts), 10,
            f'没能从 {DEMO_ACCOUNTS_DOC} 解析出演示账号，解析逻辑可能已失效')
        self.assertGreaterEqual(
            len(self.portal_urls), 5,
            '没能从 ROLE_PORTAL_MAP 解析出端，解析逻辑可能已失效')

    def test_every_portal_has_a_landing_card(self):
        html = self._html('/portal/')
        missing = [u for u in self.portal_urls if f'href="{u}"' not in html]
        self.assertEqual(
            missing, [],
            '门户首页缺少这些端的入口卡片（端存在但用户在首页看不到）：'
            + '、'.join(missing))

    def test_card_count_matches_portal_count(self):
        """卡片数必须**恰好**等于端数 —— 多一张是「入口指向不存在的端」。"""
        html = self._html('/portal/')
        # ⚠ 用 `[\s"]` 收口：`class="portal-card-current-badge"` 也以
        #   `class="portal-card` 开头，直接 `count('class="portal-card')`
        #   会把 5 张卡片数成 10。
        cards = re.findall(r'class="portal-card[\s"]', html)
        self.assertEqual(
            len(cards), len(self.portal_urls),
            f'门户首页有 {len(cards)} 张入口卡片，但系统里有 '
            f'{len(self.portal_urls)} 个端：{self.portal_urls}')

    def test_gov_card_no_longer_advertises_platform_only_pages(self):
        """政府端卡片不得再宣传已迁走的全局设置功能（含正向对照）。"""
        html = self._html('/portal/')
        gov = self._card(html, 'gold')
        self.assertIsNotNone(gov, '门户首页找不到政府监管端卡片（data-accent="gold"）')
        for word in ('机构管理', '账号权限', '系统配置'):
            self.assertNotIn(
                word, gov,
                f'政府监管端卡片仍在宣传「{word}」—— 该功能第四十二轮已迁到'
                f'平台管理端，政府端没有任何入口，用户按文案去找必然扑空')

        # 正向对照：这些词必须**真的**出现在平台端卡片上。
        # 没有这条，把两处文案一起删掉也能让上面的断言通过。
        platform = self._card(html, 'ochre')
        self.assertIsNotNone(
            platform, '门户首页找不到平台管理端卡片（data-accent="ochre"）')
        self.assertIn('机构管理', platform,
                      '平台管理端卡片没有说明它负责机构管理')

    # -- 账号提示 -----------------------------------------------------------

    def test_demo_hints_only_list_real_accounts(self):
        """两处提示里出现的用户名必须真实存在（防手滑写出 `platform1`）。"""
        problems = []
        for name, url, cls in DEMO_HINT_TARGETS:
            shown = self._hint_usernames(self._html(url), cls)
            self.assertTrue(shown, f'{url} 的 .{cls} 提示块里一个用户名都没解析到')
            for user in sorted(shown - self.doc_accounts):
                problems.append(
                    f'{name}（{url}）提示里的 `{user}` 不在 {DEMO_ACCOUNTS_DOC} 中')
        self.assertEqual(problems, [], '提示里出现了不存在的演示账号：\n  '
                         + '\n  '.join(problems))

    def test_portal_hint_covers_every_demo_account(self):
        """门户首页提示必须列全 `DEMO_ACCOUNTS.md` 里的每一个演示账号。

        「只列一部分」的后果：用户拿到的是一个**看起来完整**的清单，
        照着重置/试账号时才发现还有几个根本不知道存在。
        """
        shown = self._hint_usernames(self._html('/portal/'), 'portal-demo-hint')
        missing = sorted(self.doc_accounts - shown)
        self.assertEqual(
            missing, [],
            f'门户首页演示账号提示漏了：{missing}（以 {DEMO_ACCOUNTS_DOC} 为准）')

    def test_demo_hints_show_the_current_initial_password(self):
        """两处公开提示的口令必须与 `DEMO_INITIAL_PASSWORD` 同源。

        口令改了而提示没改时：用户照着页面提示登录必然失败，而系统本身
        完全正常 —— 排查会被完全带偏。反过来，提示里还留着旧口令也同样
        会让看旧文档的人登不进来。
        """
        for name, url, cls in DEMO_HINT_TARGETS:
            with self.subTest(target=name):
                html = self._html(url)
                m = re.search(
                    rf'class="{re.escape(cls)}"[^>]*>(.*?)</div>', html, re.S)
                self.assertIsNotNone(m, f'{url} 找不到 .{cls} 提示块')
                hint = m.group(1)
                self.assertIn(
                    DEMO_INITIAL_PASSWORD, hint,
                    f'{url} 的演示提示没有写当前初始口令 {DEMO_INITIAL_PASSWORD}')
                self.assertNotIn(
                    '123456', hint,
                    f'{url} 的演示提示还在写旧口令 123456 —— 照着提示登录会失败')

    def test_both_hints_mention_the_platform_account(self):
        """两个提示都必须给出平台管理端账号。

        第四十二轮新增 `/platform/` 后，`platform` 一度在两个提示里都缺席 ——
        照着提示走**没有任何办法**进管理后台，而提示本身看不出「少了什么」。
        """
        for name, url, cls in DEMO_HINT_TARGETS:
            with self.subTest(target=name):
                shown = self._hint_usernames(self._html(url), cls)
                self.assertIn(
                    'platform', shown,
                    f'{url} 的账号提示没有列出平台管理端账号 `platform`')


# ---------------------------------------------------------------------------
# 第五十二轮 · 重构 A：四端门户外壳（portal/_portal_shell.html）
# ---------------------------------------------------------------------------
#
# 合并前，`shelter_base.html` / `hospital_base.html` / `gov_base.html` /
# `platform_base.html` 各抄了一份 156 行 CSS + 25 行侧栏脚本（剥掉注释、
# 归一印章配色后四份 md5 完全相同）。合并的**唯一**风险不是「页面坏了」——
# 坏了会 500、会看见 —— 而是两类静默漂移：
#
#   1. 侧栏高亮机制换了继承层级（`nav_xxx` 从「嵌在 `body` 里」变成
#      「嵌在 `nav_items` 里，而 `nav_items` 在 `body` 里」）。Django 的
#      `get_blocks()` 会递归收集嵌套 block，所以理论上照旧生效；但「理论上」
#      不等于验证过：一旦某处不再递归收集，高亮**全部消失**，页面照常 200，
#      用户只是分不清自己在哪一页。
#   2. 印章底色改走 `--portal-seal`（外壳给兜底值、角色 base 覆盖）。写死
#      映射表时我把平台端误填成朱砂 —— 前后 DOM/CSS 逐字比对才抓到。
#      也就是说这类错误**只有比对渲染结果才会发现**，静态读源码发现不了。
#
# 所以这里的判据全部打在**渲染产物**上（不是读源码），且每条正向判据都配
# 一条反向对照：`nav_xxx` 写错名字时必须「一个都不高亮」，否则「高亮存在」
# 这条断言可能只是恒真。
#
# 静态判据（第 5 条）防止退化：角色 base 里不许再出现 `<style>` 或侧栏脚本 ——
# 一旦有人把 CSS 抄回角色文件，合并就白做了，而漂移又回来了。
# ---------------------------------------------------------------------------

#: 角色 base → (用于渲染的 User.role, 印章底色, 高亮试点的 block 名, 该 block 的侧栏标签)
PORTAL_SHELL_CASES = {
    'gov': ('gov_city', 'var(--grad-gold)', 'nav_dashboard', '数据总览大屏'),
    'shelter': ('shelter', 'var(--grad-cinnabar)', 'nav_blacklist', '黑名单管理'),
    'hospital': ('hospital', 'var(--grad-cinnabar)', 'nav_ledger', '单据台账'),
    'platform': ('platform_admin', 'var(--grad-gold)', 'nav_config', '系统配置'),
}

#: 渲染出的侧栏锚点：class 与标签文本
SHELL_ANCHOR = re.compile(
    r'<a class="nav-item([^"]*)"[^>]*>(.*?)</a>', re.S)
SHELL_LABEL = re.compile(r'nav-item-label">([^<]+)</span>')


class PortalShellTest(SimpleTestCase):
    """四端门户共用外壳的渲染契约（重构 A 的回归锁）。"""

    MARKER = 'SHELL-ACTIVE-MARKER'

    def _render(self, role, override_block=None):
        """渲染 `{role}_base.html`，可附带一个 block 覆写（模拟各端 portal.html）。"""
        src = "{% extends 'portal/" + role + "_base.html' %}"
        if override_block:
            src += ('{% block ' + override_block + ' %}'
                    + self.MARKER + '{% endblock %}')
        template = Engine.get_default().from_string(src)
        request = RequestFactory().get('/')
        request.user = User(username='测试员', role=PORTAL_SHELL_CASES[role][0])
        return template.render(Context({'user': request.user}))

    def _nav_classes(self, html):
        """返回 {侧栏标签: 该锚点的 class 后缀}。"""
        out = {}
        for cls, inner in SHELL_ANCHOR.findall(html):
            m = SHELL_LABEL.search(inner)
            if m:
                out[m.group(1).strip()] = cls.strip()
        return out

    def test_shell_assets_render_exactly_once(self):
        """每页只有一份外壳的 CSS 与侧栏脚本 —— 既没丢，也没被抄第二遍。"""
        for role in PORTAL_SHELL_CASES:
            with self.subTest(role=role):
                html = self._render(role)
                # 选一条在外壳 CSS 里**只出现一次**的规则：`.portal-sidebar {`
                # 本身出现两次（基础态 + @media 收起态），拿它计数会把正常结果
                # 判成「被抄了两遍」。
                self.assertEqual(
                    html.count('.portal-sidebar-overlay.show'), 1,
                    f'{role}: 侧栏遮罩规则出现 '
                    f'{html.count(".portal-sidebar-overlay.show")} 次（应为 1 次：'
                    '0 = 外壳没接上，≥2 = 有人又把 CSS 抄回角色文件）')
                self.assertEqual(
                    html.count('portalSidebarToggle'), 2,   # 按钮 + 脚本各一次
                    f'{role}: 侧栏开关脚本/按钮数量不对，实得 '
                    f'{html.count("portalSidebarToggle")}')
                self.assertIn('transform: translateX(-100%)', html,
                              f'{role}: 移动端收起侧栏的规则丢了')

    def test_nav_highlight_block_still_reaches_the_shell(self):
        """portal.html 覆写 `nav_xxx` 仍然只点亮那一条导航。

        这正是重构 A 换掉继承层级的地方：block 名躲在 `nav_items` 里，
        `nav_items` 又躲在 `body` 里。断言打在**渲染出的 class** 上。
        """
        for role, (_kind, _seal, block, label) in PORTAL_SHELL_CASES.items():
            with self.subTest(role=role):
                classes = self._nav_classes(self._render(role, block))
                self.assertTrue(classes, f'{role}: 一条导航都没解析到，判据失效')
                self.assertEqual(
                    classes.get(label), self.MARKER,
                    f'{role}: 「{label}」没有被 {{% block {block} %}} 点亮'
                    f'（实得 {classes.get(label)!r}）—— 嵌套 block 的继承断了')
                lit = [t for t, c in classes.items() if self.MARKER in c]
                self.assertEqual(lit, [label],
                                 f'{role}: 点亮了 {lit}，应只有「{label}」')

    def test_unknown_block_name_highlights_nothing(self):
        """反向对照：block 名写错时必须**一个都不亮**。

        没有这条，「高亮存在」可能只是恒真（比如 MARKER 被写进了正文）。
        """
        for role in PORTAL_SHELL_CASES:
            with self.subTest(role=role):
                html = self._render(role, 'nav_this_block_does_not_exist')
                self.assertNotIn(
                    self.MARKER, html,
                    f'{role}: 不存在的 block 名居然把 MARKER 渲染出来了 —— '
                    '上一条用例的正向断言不可信')

    def test_seal_color_per_role(self):
        """印章底色：政府端 / 平台端=鎏金，捕捉点 / 医院端=朱砂。

        合并时这一处最容易改错（底色原来硬编码在各自的 CSS 里，
        现在走 `--portal-seal`）。我第一版就把平台端写成了朱砂。
        """
        for role, (_kind, seal, _block, _label) in PORTAL_SHELL_CASES.items():
            with self.subTest(role=role):
                html = self._render(role)
                self.assertIn(
                    '--portal-seal: %s' % seal, html,
                    f'{role}: 印章底色不是 {seal}')

    def test_role_base_holds_only_role_differences(self):
        """角色 base 里不许再出现整段外壳（CSS / 侧栏脚本）。

        这条防的是**退化**：有人图省事把 CSS 抄回角色文件，改一处漏三处
        的老问题就跟着回来了 —— 而页面依旧 200，静态走查看不出来。
        """
        for role in PORTAL_SHELL_CASES:
            rel = f'templates/portal/{role}_base.html'
            src = read(rel)
            with self.subTest(role=role):
                self.assertIn("{% extends 'portal/_portal_shell.html' %}", src,
                              f'{rel}: 没有接共用外壳')
                self.assertNotIn('<style>', src,
                                 f'{rel}: 又出现了 <style> —— 外壳 CSS 被抄回角色文件')
                self.assertNotIn('portalSidebarToggle', src,
                                 f'{rel}: 侧栏开关脚本被抄回角色文件')
                self.assertLess(
                    len(src.splitlines()), 80,
                    f'{rel}: {len(src.splitlines())} 行 —— 角色 base 应当只放本端差异')

    #: 五端门户模板（真实文件，不是模拟的 from_string 子模板）→ 渲染时用的账号角色
    PORTAL_RENDER_ROLES = {
        'shelter': 'shelter', 'hospital': 'hospital', 'gov': 'gov_city',
        'platform': 'platform_admin', 'adopter': 'adopter',
    }

    def _render_real(self, role, user_role):
        request = RequestFactory().get('/portal/')
        request.user = User(username='测试员', role=user_role)
        return render_to_string(f'portal/{role}/portal.html', request=request)

    def test_real_portals_keep_the_mobile_sidebar_toggle(self):
        """五端真实门户页：侧栏的开关按钮与开关脚本必须**同生同灭**。

        重构 B 实测到的缺陷正是这一条：shelter / hospital 的 `portal.html` 覆写了
        `{% block extra_js %}` 却没写 `{{ block.super }}`，把外壳那段开关脚本整段
        顶掉 —— 页面照样 200、电脑端照样能用，只有手机上侧栏再也展不开。
        所以断言必须打在**渲染结果**上：`portalSidebarToggle` 出现两次
        （`<button id=…>` + `getElementById('…')`），少一次就是半边丢了。

        领养端刻意不用侧栏（自有 `mobile-frame` 布局），所以它两侧都不该有；
        这条同时把「有侧栏却没有开关」和「没侧栏却留着开关」两种错法卡住。
        """
        for role, user_role in self.PORTAL_RENDER_ROLES.items():
            with self.subTest(role=role):
                html = self._render_real(role, user_role)
                has_sidebar = 'id="portalSidebar"' in html
                got = html.count('portalSidebarToggle')
                if has_sidebar:
                    self.assertEqual(
                        got, 2,
                        f'{role}: 页面有侧栏，但 portalSidebarToggle 出现 {got} 次（应为 2）'
                        '—— 1 = 开关脚本被 `{% block extra_js %}` 顶掉了（补 '
                        '`{{ block.super }}`）；0 = 连按钮也丢了')
                else:
                    self.assertEqual(
                        got, 0,
                        f'{role}: 本端不渲染侧栏，却留着 {got} 处侧栏开关 —— '
                        '外壳与本页布局已经对不上')

    def test_extra_js_override_without_block_super_reproduces_the_defect(self):
        """反向对照：上一条的「== 2」不是恒真 —— 摘掉 `{{ block.super }}` 立刻变 1。

        这正是 shelter / hospital 当时的写法。没有这条对照，「按钮和脚本各一份」
        有可能只是恰好恒成立（比如判据根本没渲染到那段），改坏了也不报。
        """
        role, user_role = 'shelter', 'shelter'
        stripped = "{% extends 'portal/shelter_base.html' %}" \
                   '{% block extra_js %}<!-- 顶掉了外壳的侧栏开关 -->{% endblock %}'
        kept = "{% extends 'portal/shelter_base.html' %}" \
               '{% block extra_js %}{{ block.super }}{% endblock %}'
        request = RequestFactory().get('/portal/')
        request.user = User(username='测试员', role=user_role)
        html = Engine.get_default().from_string(stripped).render(
            Context({'user': request.user}))
        self.assertEqual(
            html.count('portalSidebarToggle'), 1,
            '反向对照失效：不写 block.super 也拿得到开关脚本，判据不可信')
        self.assertEqual(
            Engine.get_default().from_string(kept).render(
                Context({'user': request.user})).count('portalSidebarToggle'),
            2,
            f'{role}: 写了 block.super 仍然只有开关按钮 —— 外壳那段脚本没接上')


class DischargeAdviceKeyCoverageTest(SimpleTestCase):
    """出院弹窗的「医生已建议」映射必须覆盖后端全部去向 key（第五十二轮）。

    `mark_done()` 把去向**建议**写进 `Treatment.disposal_advised`，医院端待出院
    弹窗 `openDischargeChoice()` 再拿一张前端手写的 `ADVISED_KIND` 表把它翻译成
    四类出院单据之一（领养 / 死亡含无害化 / 领回 / 放归）。

    那张表是后端 `DISPOSAL_CHOICES` 的**第二份清单**：加一个 key 时后端不会报错，
    前端只会走进 `suggested = ''` 分支，弹一句「诊疗完成时没记去向建议」——
    而医生在「诊疗完成」那一刻明明选过。界面把已发生的事说成没发生，
    与第五十二轮开头那条「打得出办结去向而动物还在院」是同一族缺陷，
    所以这里按**源码**比对两份清单，不靠人记得同步。
    """

    def test_advised_kind_covers_every_backend_disposal_key(self):
        js = read_js('hospital')
        literal = find_object_literal(js, 'ADVISED_KIND')
        self.assertIsNotNone(
            literal,
            '医院端找不到 `const ADVISED_KIND = {` —— 出院弹窗不再翻译去向建议了？'
            '若确实改名，本判据要跟着改；悄悄删掉等于建议功能没人管了。')
        mapped = object_keys(literal)
        backend = {k for k, _ in Treatment.DISPOSAL_CHOICES}
        self.assertEqual(
            backend - mapped, set(),
            f'后端新增去向 {sorted(backend - mapped)} 没进 ADVISED_KIND —— '
            '勾了这些建议的单子弹窗会说「没记去向建议」，等于把医生的判断丢掉。')
        self.assertEqual(
            mapped - backend, set(),
            f'ADVISED_KIND 里有后端不存在的 key {sorted(mapped - backend)} —— '
            '`mark_done()` 的校验不会放行它们，这张表里的分支是死代码。')

    def test_advised_kind_targets_a_real_discharge_option(self):
        """映射的**值**必须是弹窗 OPTS 里真的四类单据之一。

        写错成一个不存在的 kind（`'dead'` 打成 `'died'`）时，
        `suggested` 永远匹配不上任何按钮，高亮与「医生已建议」标签静默失效。
        """
        js = read_js('hospital')
        # 第六十一轮起弹窗要能把操作员送到「修改建议去向」，所以又带回 `tid`
        # （不传的话 `openTreatmentFlow` 会去新建/取「当前单」，可能不是这一张）。
        start = js.find('openDischargeChoice(petId, advised, tid)')
        self.assertGreater(start, 0, '医院端找不到出院去向选择弹窗')
        opts_end = js.find('];', start)
        self.assertGreater(opts_end, start, '出院弹窗的 OPTS 数组找不到结尾')
        opts = set(re.findall(r"\{\s*k:\s*'([a-z]+)'", js[start:opts_end]))
        self.assertTrue(opts, '出院弹窗的 OPTS 里没解析出任何单据类型')
        literal = find_object_literal(js, 'ADVISED_KIND')
        targets = set(re.findall(r":\s*'([a-z]+)'", literal))
        self.assertFalse(
            targets - opts,
            f'ADVISED_KIND 映射到不存在的单据类型 {sorted(targets - opts)}，'
            f'弹窗里只有 {sorted(opts)} —— 建议高亮会静默不生效。')

    def test_board_row_exposes_the_advised_key(self):
        """看板行必须同时给出建议的 key 与中文，前端才既预选又能显示。

        只发 `disposal_advised`（key）时，界面要么显示英文码、要么在
        后端加 key 时静默漏一项；只发 label 时弹窗又拿不到 key 去映射。
        """
        src = _read_raw('business/views_treatment.py')
        start = src.find('def _board_row(treatment):')
        self.assertGreater(start, 0, '找不到诊疗看板行构造函数')
        body = src[start:src.find('\ndef ', start + 1)]
        self.assertIn("'disposal_advised': treatment.disposal_advised", body,
                      '看板行没给出建议的 key —— 出院弹窗没法预选单据')
        self.assertIn("'disposal_advised_label': _disposal_label(treatment.disposal_advised)", body,
                      '看板行没给出建议的中文 —— 界面会漏出英文枚举码')


class HospitalDischargeTodoTest(SimpleTestCase):
    """首页待办必须把「待诊疗」与「待出院」分开（第五十二轮电脑端巡检发现）。

    实测数据：动物 `TNR261018002` 的诊疗单已办结（`disposal='released'`），
    但放归分两段，`dispose(defer_pet_status=True)` 会**故意**把 `Pet.status`
    留在 `in_treatment` 等转运与放归单据。首页当时按
    `pets.filter(p => p.status === 'in_treatment')` 生成待办，于是它挂成
    「待诊疗：TNR261018002 · 狗」，点进去诊疗看板却**任何一列都没有它**
    （看板按诊疗单阶段分，它已在「已出院」）。
    界面承诺了一件已经做完的事，又把真正没做完的那件藏起来 ——
    与本轮开头「印得出办结去向而动物还在院」是同一族，方向相反罢了。

    判据按源码取证的三条：待办由「最新一张诊疗单是否已办结」分流、
    两条待办各自跳对的标签页、概览卡片与待办**同源**（不能一个数 3 一个列 5）。
    """

    def _pending_treatment_loop_source(self, js):
        """返回推送「待诊疗：」那条待办的 `.forEach` 是被哪个数组驱动的。"""
        idx = js.find("title: '待诊疗：'")
        self.assertGreater(idx, 0, '首页没有「待诊疗」待办 —— 分类逻辑被删了？')
        head = js.rfind('.forEach(', 0, idx)
        self.assertGreater(head, 0, '「待诊疗」待办不在任何 forEach 里，判据取不到来源')
        start = js.rfind('\n', 0, head)
        return js[start:head].strip()

    def test_pending_treatment_is_split_by_latest_record(self):
        js = read_js('hospital')
        loop = self._pending_treatment_loop_source(js)
        self.assertIn('pendTreatPets', loop,
                      '「待诊疗」待办不是由 `pendTreatPets` 驱动的 —— '
                      '退回按 `Pet.status` 一刀切的话，放归两段的动物会被说成待诊疗，'
                      '而诊疗看板里没有它。')
        # 分流判据只看一个事实：最新一张诊疗单办结了没有
        self.assertIn('latestTxByPet', js, '首页没有「每只动物最新一张诊疗单」的归并')
        self.assertIn('t.is_disposed', js, '分流没用到「已办结」判据')

    def test_pending_discharge_todo_navigates_to_discharge(self):
        js = read_js('hospital')
        idx = js.find("title: '待出院：'")
        self.assertGreater(idx, 0,
                           '首页没有「待出院」待办 —— 已办结但还没出院的动物就没有任何入口了')
        tail = js[idx:idx + 260]
        self.assertIn("nav: 'discharge'", tail,
                      '「待出院」待办没跳到出院管理，点了等于没点')

    def test_stat_card_shares_the_todo_source(self):
        """概览卡片的数字与待办列表必须同源，否则「待诊疗任务 5 / 列表 3 条」。"""
        js = read_js('hospital')
        self.assertIn("const pendTreat = pendTreatPets.length;", js,
                      '「待诊疗任务」卡片不再用分流后的数量')
        self.assertRegex(
            js, r"pendDischarge = pendDischargePets\.length",
            '「待出院动物」卡片没有数量来源')
        # 禁止式：老的一刀切写法不能残留在首页待办里
        self.assertNotIn(
            "pets.filter(p => p.status === 'in_treatment').forEach", js,
            '首页仍按 `Pet.status` 一刀切生成待办 —— 本条判据要防的正是这个写法')


class HospitalDischargePetStageCopyTest(SimpleTestCase):
    """出院办理弹窗的动物阶段文案要走派生真源（第五十九轮界面录入巡检）。

    实测：TNR261026014/016 的诊疗单已经「诊疗完成」（`done_at` 有、
    `disposed_at` 空），后端 `services_lifecycle.pet_stage()` 派生为
    `pending_discharge`「待出院」；医院端「办理领回」「登记安乐死 / 死亡」
    两个弹窗的动物下拉却仍读 `Pet.status` 的原始文案，显示成「（待诊疗）」——
    第五十六轮起诊疗完成**不再改** `Pet.status`，读原始状态必然说错。

    同一个「阶段」在列表列、一宠一档、弹窗选项三处三个说法：任务 #23/#24
    已收口前两处，这里是漏网的第三处。表现是操作员以为动物还没治完，
    转去诊疗页白跑一趟，回来又在领回列表里看到同一只。
    """

    def test_pet_dropdowns_use_the_derived_stage(self):
        js = strip_js_comments(read_js('hospital'))
        for sel in ('dspOrPet', 'dspEuthPet'):
            start = js.find('id="' + sel + '"')
            self.assertGreater(start, 0, f'出院弹窗找不到动物下拉 {sel}')
            body = js[start:start + 500]
            self.assertIn('this.dspPetStageText(p)', body,
                          f'{sel} 的动物下拉还在读原始 `Pet.status` —— '
                          '诊疗完成未办结的动物会被说成「待诊疗」')
        self.assertNotIn(
            "TNR_UI.petStatusText(p.status) + '）</option>'", js,
            '出院弹窗的动物下拉里又出现了直接读原始状态的写法')

    def test_stage_helper_follows_the_board_bucket(self):
        """派生判据要和 `pet_stage()` 同源：看板 `pending_discharge` 桶里有它 = 待出院。"""
        js = strip_js_comments(read_js('hospital'))
        start = js.find('dspPetStageText(pet) {')
        self.assertGreater(start, 0, '派生阶段取值函数被删了 —— 弹窗会退回读原始状态')
        end = _close_brace(js, js.find('{', start))
        self.assertGreater(end, start, '派生阶段函数没有闭合，判据取不到')
        body = js[start:end + 1]
        self.assertIn('this.dspAdviceOf(pet.id)', body,
                      '判据没读「待出院」桶的建议去向索引 —— 等于没走派生源')
        self.assertIn('!== undefined', body,
                      '判据没区分「不在桶里」（undefined，按原始状态显示）'
                      '与「在桶里但没补录」（空串）')
        self.assertIn("TNR_UI.boardStageText('pending_discharge')", body,
                      '「待出院」文案没走 `BOARD_STAGE` 真源')
        self.assertIn('TNR_UI.petStatusText(pet.status)', body,
                      '桶外动物（历史存量）的兜底分支没了，它们会被统一说成待出院')


class HospitalAdoptInfoTodoTest(SimpleTestCase):
    """「待领养资料待维护」要按资料完成度筛（第五十九轮界面录入巡检）。

    实测：TNR261026003 花卷 的简介与照片都已在「维护资料」里补齐、也已上架，
    首页待办仍对它写死「请完善照片和简介」，卡片也照样计数。待办的意义是
    「还有事要做」，谎报的待办会把真缺口淹没 —— 操作员学会忽略整张卡片之后，
    真正缺照片的那只也一样被忽略。
    """

    def test_gap_check_is_one_shared_source(self):
        js = strip_js_comments(read_js('hospital'))
        start = js.find('function adoptInfoGaps(pet, listing) {')
        self.assertGreater(start, 0, '领养资料缺口判据被删了')
        end = _close_brace(js, js.find('{', start))
        body = js[start:end + 1]
        self.assertIn('listing.intro', body,
                      '简介没优先读大厅清单 —— 只填进大厅、没回写 Pet.description 的资料会被判成缺')
        self.assertIn('pet.description', body, '简介缺了 `Pet.description` 兜底')
        self.assertIn('visible', body,
                      '照片只数张数不看显隐 —— 把全部照片都隐藏掉的资料会被判成「齐了」')

    def test_dashboard_filters_and_counts_by_gaps(self):
        js = strip_js_comments(read_js('hospital'))
        self.assertIn('.filter(it => it.gaps.length)', js,
                      '待办没有按缺口过滤：资料已齐的动物仍挂在待办里')
        self.assertRegex(js, r'const pendAdopt = adoptTodo\.length',
                         '卡片数字与待办列表不同源 —— 会出现「卡片 5 / 列表 2 条」')
        self.assertIn("'请完善' + it.gaps.join('和')", js,
                      '待办文案不随缺口变化，又写死成一句')
        self.assertNotIn("'请完善照片和简介'", js,
                         '谎报缺口的固定文案回来了')

    def test_dashboard_reads_hall_listings_strictly(self):
        js = strip_js_comments(read_js('hospital'))
        start = js.find('async render_dashboard() {')
        self.assertGreater(start, 0, '找不到首页看板')
        body = js[start:start + 1400]
        self.assertIn('getHallListingsStrict()', body,
                      '首页读大厅清单用了静默读：接口拒绝时 `[]` 会被当成「所有动物都缺资料」，'
                      '恰好把这条待办的意义反过来')
        self.assertNotIn('TNR_API.getHallListings()', body,
                          '首页用了静默版 `getHallListings()`')

    def test_system_photo_fields_have_one_frontend_source(self):
        js = strip_js_comments(read_js('hospital'))
        self.assertEqual(js.count('const PET_SYSTEM_PHOTOS = ['), 1,
                         '系统照片清单不止一处定义')
        self.assertNotIn('const SYSTEM_PHOTOS = [', js,
                         '「维护资料」弹窗又另写了一份系统照片清单 —— '
                         '加一个照片环节时两处必有一处把新照片当「没有照片」')
        self.assertIn('PET_SYSTEM_PHOTOS\n      .map', js.replace('\r\n', '\n'),
                      '弹窗没复用共享清单')


class ShelterAdoptionCandidateTest(SimpleTestCase):
    """捕捉点两个领养下拉的候选口径要和后端守卫一致（第五十九轮界面录入巡检）。

    - 「领养登记」`#ad_pet` 列出了 5 只 `status=adopted` 的动物（老流程
      「先把状态改成已领养、再补一张单」留下的过滤条件）。现在
      `adoption_register` 只认 `pending_adopt` / `in_treatment`，选中已领出的
      必报「宠物当前状态(已领养)不可领养」。
    - 「违规收回」`#fu_adoption` 直接用未筛选的 `getAdoptions()`，把「已取消」
      也灌进下拉；选中后被 `adoption_reclaim` 以「该领养记录已撤销，请勿重复操作」
      挡回。

    界面摆出必然报错的选项，等于把后端判据的活推给操作员逐只试。
    """

    def test_register_dropdown_excludes_adopted_and_claimed(self):
        js = strip_js_comments(read_js('shelter'))
        start = js.find('async render_adoption() {')
        self.assertGreater(start, 0, '找不到领养审核页')
        body = js[start:js.find('const adFilters', start)]
        self.assertIn("p.status === 'pending_adopt'", body,
                      '领养登记候选里没有「待领养」判据')
        self.assertNotIn("=== 'adopted'", body,
                         '领养登记下拉又列出已领出的动物 —— 后端必拒，选中只会报错')
        self.assertIn("a.status === 'pending_claim'", body,
                      '没有排除已挂着未完结领养单的动物（后端 `pet_has_active_adoption` 同样拒）')

    def test_reclaim_dropdown_only_lists_established_adoptions(self):
        js = strip_js_comments(read_js('shelter'))
        start = js.find('async render_followup() {')
        self.assertGreater(start, 0, '找不到领养回访页')
        head = js[start:js.find('const fuFilters', start)]
        self.assertIn("adoptions.filter(a => a.status === 'completed')", head,
                      '违规收回候选没有限定「领养关系已成立」的单')
        sel = js.find('id="fu_adoption"')
        self.assertGreater(sel, 0, '找不到违规收回的领养记录下拉')
        body = js[sel:sel + 400]
        self.assertIn('reclaimable.map(a =>', body,
                      '下拉又直接用整份 `adoptions`（含已取消、待领出）')


class ShelterDispatchTargetKeepTest(SimpleTestCase):
    """物料下发换物料后必须保住已选的接收方（第五十九轮界面录入巡检）。

    `loadTargets()` 每次重建 `#ob_target` 的 innerHTML 而不回填选中值：
    操作员先选「爱心医院」、再把物料从麻醉剂换成芯片，接收方静默落到第一项，
    提交时货发去了另一家 —— 界面上没有任何一句话说过这件事，
    而下拉框看起来「有值」，不会引起怀疑。
    """

    def test_selected_target_is_restored_after_reload(self):
        js = strip_js_comments(read_js('shelter'))
        start = js.find('const loadTargets = async () => {')
        self.assertGreater(start, 0, '找不到下发接收方的加载函数')
        end = js.find('\n      };', start)
        self.assertGreater(end, start, '接收方加载函数没有闭合，判据取不到')
        body = js[start:end]
        self.assertIn('const keep = targetSel.value;', body,
                      '重建下拉前没有记下操作员已选的接收方')
        self.assertIn('list.some(t => String(t.id) === String(keep))', body,
                      '回填前没校验新清单里还有没有那一家 —— 直接赋值会静默落回空')
        self.assertIn('targetSel.value = keep;', body, '已选接收方没有回填')


class HospitalMaterialLoadTest(SimpleTestCase):
    """物料页的读取失败必须说出来（第五十三轮电脑端巡检）。

    物料主数据与流水是这一整页的唯一数据源。静默读（`getMaterials` /
    `getMaterialTransactions`）在服务端拒绝时返回 `[]`，页面会渲染成
    「六个页签全是 0、台账暂无记录」—— 与「本院真的没有物料」在界面上
    完全一样，操作员会以为库存被清空了。

    另外「库存异动」「出库报备」原先各自在**每次筛选**时重拉一遍物料并
    空 catch 吞掉失败：搜索框每敲一个字就多一次请求。物料主数据现在只在
    进入页面时拉一次，页签与筛选只做本地过滤。
    """

    def test_material_page_uses_strict_reads_only(self):
        js = strip_js_comments(read_js('hospital'))
        self.assertIn('getMaterialTransactionsStrict()', js,
                      '物料流水不再严格读 —— 403/500 会被渲染成「暂无台账」')
        self.assertIn('getMaterialsStrict()', js,
                      '物料主数据不再严格读 —— 库存页会渲染成「暂无物料」')
        for silent in ('getMaterialTransactions()', 'getMaterials()'):
            self.assertNotIn(
                silent, js,
                f'医院端又用静默读 `{silent}` —— 失败会被伪装成「没有数据」；'
                '整页数据源请用 Strict 版本并在失败时显示错误')

    def test_material_load_failure_has_visible_error_and_retry(self):
        js = strip_js_comments(read_js('hospital'))
        start = js.find('async render_material()')
        self.assertGreater(start, 0, '医院端物料页入口不在了')
        end = js.find('\n  async renderMaterialTab', start)
        self.assertGreater(end, start, '找不到 render_material 的结尾')
        body = js[start:end]
        self.assertIn('物料数据加载失败', body,
                      '物料页读取失败时没有专用错误态 —— 会静默显示成空数据')
        self.assertIn('materialReloadBtn', body,
                      '物料页错误态没有「重新加载」按钮，用户只能切页重进')
        self.assertIn('this._matMaterials = materials', body,
                      '物料主数据没有缓存 —— 页签会各自重拉（每次筛选一次请求）')

    def test_material_tab_switch_passes_the_cached_materials(self):
        """页签点击与筛选回调都必须把缓存传给 tab 渲染器。

        只给 `txns` 不给 `materials` 时，「实时库存」会显示「暂无物料」、
        异动/出库的下拉只剩「请选择」—— 页面不报错，看起来像真的没数据。
        """
        js = strip_js_comments(read_js('hospital'))
        for call in ('\n        this.renderMaterialTab(txns, materials);',
                     'this.renderMaterialTab(this._matCache || [], this._matMaterials || []), page)'):
            self.assertIn(call, js,
                          f'物料页少了带缓存参数的渲染调用：{call!r}')


class HospitalDischargeSecondaryMenuTest(SimpleTestCase):
    """出院四种去向收进页内二级菜单（第五十四轮）。

    领养 / 死亡出库 / 领回 / 放归是出院的四个**子状态**，不是四个独立
    业务模块，因此侧栏不再保留「领养信息维护」「安乐死处置」两个一级页。
    判据同时看模板与渲染函数：只删导航不删页面会留下不可达的死页面；
    只删页面不删导航则点进去是空白。
    """

    def test_sidebar_and_page_shell_drop_the_two_standalone_pages(self):
        nav = _read_raw('templates/portal/hospital_base.html')
        for gone in ('data-nav="adoption-info"', 'data-nav="euthanasia"'):
            self.assertNotIn(gone, nav,
                             f'医院侧栏仍有独立入口 {gone} —— 四个去向应只在出院页内')
        portal = _read_raw('templates/portal/hospital/portal.html')
        for gone in ('id="page-adoption-info"', 'id="page-euthanasia"'):
            self.assertNotIn(gone, portal,
                             f'医院端仍保留独立页面容器 {gone}，与二级菜单重复')
        self.assertIn('id="page-discharge"', portal, '出院页容器被误删')

    def test_discharge_shell_renders_four_secondary_tabs(self):
        js = strip_js_comments(read_js('hospital'))
        start = js.find('renderDischargeShell()')
        self.assertGreater(start, 0, '医院端找不到出院页二级菜单渲染函数')
        end = js.find('\n  renderDspTab()', start)
        self.assertGreater(end, start, '找不到 renderDischargeShell 的结尾')
        body = js[start:end]
        for key in ("k: 'adopt'", "k: 'dead'", "k: 'owner'", "k: 'release'"):
            self.assertIn(key, body,
                          f'出院二级菜单缺少去向 {key} —— 四个去向必须同屏可选')
        self.assertIn("'data-dspmod'", body,
                      '二级菜单没有事件委托 —— 重渲染后按钮会点不动')
        self.assertIn("'discharge'", body,
                      '二级菜单委托没有用稳定 tag，多次渲染会重复绑定')

    def test_adoption_three_states_all_bind_their_filters(self):
        """待领养 / 已预约 / 已出库 三个子页签都必须挂筛选。

        任何一条分支提前 return 或忘了 `bindFilter`，搜索框都看得见却没反应
        （第五十三轮领养维护页踩过同一个坑）。
        """
        js = strip_js_comments(read_js('hospital'))
        bounds = {
            'renderDspAdoptReady(body) {': '\n  renderDspAdoptReserved(body) {',
            'renderDspAdoptReserved(body) {': '\n  renderDspAdoptOutbound(body) {',
            'renderDspAdoptOutbound(body) {': '\n  renderDspDeath(',
        }
        for fn, next_marker in bounds.items():
            start = js.find(fn)
            self.assertGreater(start, 0, f'找不到领养子页签 {fn}')
            end = js.find(next_marker, start + 1)
            self.assertGreater(end, start, f'找不到 {fn} 的结尾')
            self.assertIn('bindFilter(mount, document.getElementById(', js[start:end],
                          f'{fn} 没有绑定筛选 —— 搜索框会没反应')

    def test_adoption_ready_excludes_reserved_animals(self):
        """捕捉点批准线上申请后不改 `Pet.status`，待领养列表必须按申请单排除。

        不排除的话，同一只动物会同时出现在「待领养」和「已预约」，
        数量、操作入口和「已预约」的业务含义全部失真。
        """
        js = strip_js_comments(read_js('hospital'))
        start = js.find('dspReservedPetIds()')
        self.assertGreater(start, 0, '找不到「已被领养流程预留」的共用判据')
        end = js.find('\n  },', start)
        helper = js[start:end]
        self.assertIn("a.status === 'pending_claim'", helper,
                      '预留集合没有包含已有领养单的动物')
        self.assertIn("a.status === 'approved'", helper,
                      '预留集合没有包含已批准线上申请的动物')

        start = js.find('dspReadyPets()')
        self.assertGreater(start, 0, '找不到待领养动物筛选函数')
        end = js.find('\n  },', start)
        body = js[start:end]
        self.assertIn('dspReservedPetIds()', body,
                      '待领养没有排除已被领养流程预留的动物')

    def test_other_discharge_candidates_exclude_reserved_animals(self):
        """运回 / 放归 / 领回 / 死亡登记的可选动物同样要排除已预留的动物。

        这些列表都按 `Pet.status` 筛选，而「已批准申请」不写状态；
        不共用预留集合，动物会在「已预约」之外再出现在其他去向里。
        """
        js = strip_js_comments(read_js('hospital'))
        for fn in ('dspOwnerPets', 'dspReleaseCandidates', 'dspOpenEuthCreate'):
            start = js.find(f'\n  {fn}() {{')
            self.assertGreater(start, 0, f'找不到 {fn}')
            end = js.find('\n  },', start)
            self.assertIn('dspReservedPetIds()', js[start:end],
                          f'{fn} 没有排除已被领养流程预留的动物')

    def test_adoption_outbound_carries_handover_evidence(self):
        js = strip_js_comments(read_js('hospital'))
        start = js.find('dspOpenAdoptionOutbound(source, id)')
        self.assertGreater(start, 0, '找不到领养出库单入口')
        end = js.find('\n  dspViewAdoption(', start)
        self.assertGreater(end, start, '找不到领养出库单的结尾')
        body = js[start:end]
        for field in ('outbound_at', 'handover_name', 'signature', 'handover_photo'):
            self.assertIn(field, body,
                          f'领养出库单缺少交接字段 {field} —— 出库必须有可追溯凭证')
        self.assertIn('initSignatureField', body, '领养出库单没有领养人签字')

    def test_death_outbound_carries_handover_evidence(self):
        js = strip_js_comments(read_js('hospital'))
        start = js.find('dspOpenEuthOutbound(id)')
        self.assertGreater(start, 0, '找不到死亡出库单入口')
        end = js.find('\n  dspViewEuth(', start)
        self.assertGreater(end, start, '找不到死亡出库单的结尾')
        body = js[start:end]
        for field in ('receiving_org', 'receiver_name',
                      'signature', 'photo', 'outbound_at'):
            self.assertIn(field, body,
                          f'死亡出库单缺少交接字段 {field} —— 无害化交接必须可追溯')
        # 「接收机构」与「接收单位」是同一件事，出库单上只留前者；
        # 两个输入框并排会让交接员不知道该把厂名填在哪一个。
        self.assertNotIn('接收单位', body, '死亡出库单仍保留重复的「接收单位」字段')
        self.assertNotIn("fd.append('receiver_unit'", body,
                         '死亡出库单仍在提交重复的 receiver_unit')
        self.assertIn('initSignatureField', body, '死亡出库单没有接收人签字')

    def test_outbound_photos_offer_camera_and_library_on_mobile(self):
        """出库照片在手机上要能直接拍照，也能从相册 / 本地文件选。

        单个 `<input type=file>` 在部分手机浏览器里只弹文件管理器，现场拍的
        照片反而找不到；两个入口选完统一本地压缩再上传。
        """
        js = strip_js_comments(read_js('hospital'))
        self.assertIn('data-photo-cam="', js)
        self.assertIn('data-photo-lib="', js)
        self.assertIn('capture="environment"', js)
        self.assertIn('TNR_UI.compressImage(raw)', js)
        for key, label in (('out', '领养出库'), ('eo', '死亡出库')):
            self.assertIn(f"dspPhotoPickHtml('{key}'", js,
                          f'{label}单没接手机照片选择器（拍照 / 相册）')
            self.assertIn(f"dspBindPhotoPick(bodyEl, '{key}'", js,
                          f'{label}单的照片选择器没有绑定')
        for gone in ('id="dspOutPhoto"', 'id="dspEoPhoto"'):
            self.assertNotIn(gone, js,
                             f'仍是单个 file input（{gone}），手机端无法区分拍照/相册')

    def test_discharge_modal_styles_are_not_scoped_to_the_page(self):
        """弹层挂在 body 下，`#page-discharge .dsp-*` 在弹层里不生效。

        照片格 / 摘要 / 复选框都只在弹层里使用；带页面前缀时它们会退化成
        默认块级布局（照片竖排大图、两列摘要变一列），界面上看不出报错。
        """
        html = read_html('hospital')
        for cls in ('.dsp-summary', '.dsp-photo-grid', '.dsp-photo-box',
                    '.dsp-photo-label', '.dsp-check', '.dsp-return-pets',
                    '.dsp-note', '.dsp-photo-pick', '.dsp-photo-picked'):
            self.assertNotIn('#page-discharge ' + cls, html,
                             f'{cls} 只在弹层里使用，不能加 #page-discharge 前缀')

    def test_return_tab_exposes_every_order_status_and_withdraw(self):
        js = strip_js_comments(read_js('hospital'))
        start = js.find('renderDspRelease(c) {')
        self.assertGreater(start, 0, '找不到放归页签')
        end = js.find('\n  onDspAction(', start)
        self.assertGreater(end, start, '找不到放归页签的结尾')
        body = js[start:end]
        for status in ('pending', 'received', 'rejected', 'withdrawn'):
            self.assertIn("value: '" + status + "'", body,
                          f'运回单筛选缺少状态 {status} —— 捕捉点驳回/撤回后查不到')
        self.assertIn('return-withdraw', body, '待确认运回单没有撤回入口')


class ShelterReturnOrderReceiveTest(SimpleTestCase):
    """捕捉点必须能签收医院开出的运回单（放归第一段的反向物流）。

    没有接收单，「医院运回 → 捕捉点放归」在捕捉点侧就断了：
    动物状态停在 `return_pending`，医院放不出去、捕捉点也收不到。
    """

    def test_release_tab_lists_pending_orders_and_signs_receipt(self):
        js = strip_js_comments(read_js('shelter'))
        for token in ('data-return-receive', 'data-return-reject',
                      'receiveReturnOrder', 'rejectReturnOrder',
                      'showReturnReceiveModal', 'showReturnRejectModal'):
            self.assertIn(token, js,
                          f'捕捉点运回单接收缺少 {token}')
        start = js.find('showReturnReceiveModal(id)')
        self.assertGreater(start, 0, '找不到捕捉点接收单弹窗')
        end = js.find('\n  showReturnRejectModal(', start)
        self.assertGreater(end, start, '找不到接收单弹窗的结尾')
        body = js[start:end]
        self.assertIn('initSignatureField', body, '接收单没有接收人签字')
        self.assertIn('receiver_name', body, '接收单没有接收人')

    def test_pending_release_animals_are_releasable(self):
        """捕捉点签收后动物是 `pending_release`，必须能继续发起放归。

        只认 `in_treatment/pending_adopt` 时，刚签收的动物会从下拉里消失，
        放归第二段永远办不了。
        """
        js = strip_js_comments(read_js('shelter'))
        self.assertIn("p.status === 'pending_release'", js,
                      '捕捉点放归页签没有纳入 pending_release 动物')


# ============================================================
# 表格列直绑 DateTimeField（第五十二轮电脑端巡检）
# ============================================================

#: 这些字段在库里是 `DateTimeField`，`serialize_instance` 用 `isoformat()`
#: 输出 `2026-10-16T16:33:51.504093+00:00`。列上不写 `render` 就会把这一串
#: **原样打到界面上**（实测：领养信息维护「登记时间」列）。
def _datetime_attnames():
    from django.apps import apps as django_apps
    from django.db.models import DateTimeField
    names = set()
    for model in django_apps.get_models():
        if model._meta.app_label not in ('business', 'core', 'accounts'):
            continue
        for field in model._meta.get_fields():
            if isinstance(field, DateTimeField):
                names.add(field.attname)
    return names


def _enclosing_object_literal(source, idx):
    """返回 `idx` 所在**最内层** `{...}` 对象字面量的文本（跳过字符串）。"""
    depth = 0
    start = None
    for i in range(idx, -1, -1):
        c = source[i]
        if c in '"\'`':
            continue
        if c == '}':
            depth += 1
        elif c == '{':
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return None
    return brace_body(source, start)


class TableColumnDatetimeBindingTest(SimpleTestCase):
    """凡是列 `key` 直绑 DateTimeField，必须配 `render` 做本地化格式化。

    `TNR_UI.mountTable` 的取值是 `row[col.key] ?? ''`，没有任何兜底格式化，
    所以「忘了写 render」不会报错、不会 500，只是把 UTC ISO 串摊给操作员看。
    同一条列上 `key` 仍要保留 —— 表头排序按 `row[col.key]` 比，ISO 串
    字典序 = 时间序，删掉 `key` 会让该列失去排序（`gov.js` 的写法就是两者都留）。
    """

    def test_no_column_binds_a_datetime_field_raw(self):
        names = _datetime_attnames()
        self.assertTrue(names, '取不到 DateTimeField 清单，判据会静默恒真')
        offenders = []
        for role, rel in PORTAL_JS.items():
            src = strip_js_comments(_read_raw(rel))
            for m in re.finditer(r"key:\s*['\"](\w+)['\"]", src):
                if m.group(1) not in names:
                    continue
                literal = _enclosing_object_literal(src, m.start())
                if literal is None or 'render' in literal:
                    continue
                offenders.append(
                    f'{role}:{src[:m.start()].count(chr(10)) + 1} '
                    f'key={m.group(1)}')
        self.assertFalse(
            offenders,
            '这些表格列把 DateTimeField 原样渲染成 UTC ISO 串，'
            f'需要 `render: r => TNR_UI.formatDateTime(r.<字段>)`：{offenders}')

    def test_formatter_used_by_those_columns_is_the_shared_one(self):
        """格式化只能走 `TNR_UI.formatDateTime` —— 各端自己 `slice(0,10)` 会退一天。"""
        for role in ('hospital', 'gov'):
            js = strip_js_comments(read_js(role))
            for m in re.finditer(r"key:\s*'created_at'[^{}]*?render:[^{}]*?\}", js):
                self.assertIn(
                    'formatDateTime', m.group(0),
                    f'{role}: created_at 列没有走共享格式化函数')

    def test_euthanasia_date_column_shows_dash_when_null(self):
        """`euthanized_at` 是可空 DateField，空值要显示 `—` 而不是空白格。"""
        js = strip_js_comments(read_js('hospital'))
        self.assertEqual(
            len(re.findall(r"title:\s*'处置日期'[^{}]*render:[^{}]*formatDate", js),),
            2,
            '安乐死列表与台账的「处置日期」列应各有 1 处 formatDate 兜底')

# ============================================================
# 平台端核心配置页的读取与建号（第五十四轮电脑端巡检）
# ============================================================

class PlatformAdminReadTest(SimpleTestCase):
    """平台端是「建机构 / 建账号 / 改配置」的入口，读失败不能伪装成空列表。

    另外两条与安全直接相关：
    - 编辑弹窗在缓存/读取失败时**必须中止**，不能把 `id` 当成新增；
    - 新账号不得带统一默认口令，密码框必须是 password 类型。
    """

    def _src(self):
        return strip_js_comments(read_js('platform'))

    def test_platform_lists_use_strict_reads_only(self):
        js = self._src()
        for strict in ('getDistrictsStrict()', 'getInstitutionsStrict(', 'getUsersStrict()'):
            self.assertIn(strict, js, f'平台端缺少严格读 {strict}')
        for silent in ('getDistricts()', 'getInstitutions(', 'getUsers('):
            self.assertNotIn(silent, js,
                             f'平台端又用静默读 `{silent}` —— 失败会被渲染成「暂无数据」')
        api = strip_js_comments(_read_raw('static/js/tnr-api.js'))
        self.assertIn('async getUsersStrict(role)', api,
                      '`getUsersStrict` 没有对应的严格读方法')

    def test_list_failures_are_visible_and_retryable(self):
        js = self._src()
        for title in ('区县数据加载失败', '机构数据加载失败', '账号数据加载失败'):
            self.assertIn(title, js)
        self.assertIn('showLoadError(', js)
        self.assertIn('data-reload', js)

    def test_settings_failures_are_visible_and_retryable(self):
        """编号规则 / 操作日志 / 公告读失败也必须带「重新加载」。

        这三处原先各写一份红色的「加载失败…」空态，没有重试入口，
        用户只能切页签碰运气；弱网下会被误认为功能坏了。
        """
        js = self._src()
        for title in ('编号规则加载失败', '操作日志加载失败', '公告数据加载失败'):
            self.assertIn(f"this.showLoadError(el, '{title}'", js,
                          f'「{title}」没有走统一的失败态（含重新加载按钮）')

    def test_edit_modals_abort_instead_of_falling_back_to_create(self):
        js = self._src()
        for msg in ('区县不存在或已被删除，请刷新列表', '机构不存在或已被删除，请刷新列表'):
            self.assertIn(msg, js, '编辑弹窗在记录找不到时会静默退化成新增')

    def test_account_creation_has_no_default_password(self):
        """建号口令必须由管理员显式填写，不得预填或回退任何演示口令。

        `111111` 只是 seed / 自动建号直接写库的演示口令，按强度口径它
        连 8 位门槛都过不了 —— 一旦前端把它当默认值，建出来的账号
        要么绕过了校验，要么点提交就被拒。
        """
        js = self._src()
        for default in ('123456', DEMO_INITIAL_PASSWORD):
            self.assertNotIn(f'value="{default}"', js,
                             f'账号创建表单又预填了默认口令 {default}')
            self.assertNotIn(f"password || '{default}'", js,
                             f'前端又把空口令回退成 {default}')
        self.assertIn('type="password" class="form-input" id="acct-password"', js,
                      '初始密码框必须是 password 类型')
        self.assertIn('请设置初始密码', js)

    def test_account_password_reset_entry(self):
        """管理员重置密码：路由、API 封装、行内按钮与模态框必须连得上。

        平台端原先只有「新增 / 编辑 / 启停」——本人忘记口令后，除了改库
        没有别的办法；这条同时钉住重置表单不得预填默认口令。
        """
        urls = read('supervision/urls.py')
        self.assertRegex(
            urls, r"users/<int:pk>/reset-password/'[^)]*views\.user_reset_password",
            '`users/<pk>/reset-password/` 路由没了 —— 管理员无法重置忘记口令的账号。')
        api = strip_js_comments(_read_raw('static/js/tnr-api.js'))
        self.assertRegex(
            api, r'resetUserPassword\(id,\s*password\)[\s\S]{0,160}users/\$\{id\}/reset-password/',
            '`TNR_API.resetUserPassword` 没打到 `users/${id}/reset-password/` —— 按钮是空操作。')
        js = self._src()
        self.assertIn('data-reset-password', js, '账号列表没有「重置密码」入口')
        self.assertIn('TNR_API.resetUserPassword(', js, '重置密码模态框没调接口')
        self.assertIn('id="acctReset-password"', js)
        self.assertIn('id="acctReset-confirm"', js)
        self.assertIn('password !== confirm', js, '两次输入没有比对')
        for default in ('123456', DEMO_INITIAL_PASSWORD):
            self.assertNotIn(f'value="{default}"', js,
                             f'重置表单又预填了默认口令 {default}')

    def test_reset_entry_is_not_shown_for_self(self):
        """当前账号不显示重置按钮：本人必须走验原口令的自助改密。"""
        js = self._src()
        start = js.find('const rowActions = r => r.id === selfId')
        self.assertGreaterEqual(start, 0, '账号列表的行操作结构变了，需要同步断言')
        seg = js[start:start + 800]
        self.assertRegex(
            seg, r'当前账号[\s\S]*?:\s*`[\s\S]*?data-reset-password',
            '「当前账号」也渲染了重置密码按钮 —— 本人应走验原口令的自助改密')

    def test_tables_show_the_date_they_filter_on(self):
        js = self._src()
        for title in ('创建时间', '开户时间'):
            self.assertIn(title, js, f'列表按日期筛选却没有「{title}」列')

    def test_filter_after_load_error_does_not_overwrite_error_state(self):
        """读失败后筛选框仍绑着 `renderAccountsTable()`，不能被空列表覆盖成「暂无账号」。

        失败时必须把缓存置空、渲染函数在未加载状态下直接返回 ——
        否则用户一敲搜索框，错误提示就消失了，看到的是一份空表。
        """
        js = self._src()
        self.assertIn('this._users = null;', js,
                      '账号读取失败后仍保留旧缓存，筛选会把错误态换成过期名单')
        self.assertIn('if (!this._users) return;', js,
                      '未加载状态仍允许渲染账号表，错误态会被空表覆盖')


class ChipTracePetArchiveLinkTest(SimpleTestCase):
    """芯片追溯里「绑定宠物」必须能跳到一宠一档（第五十轮）。

    原先追溯抽屉只把宠物编号当纯文本显示。现场看到一颗已使用的芯片后，
    最自然的追问是「它打到哪只动物身上、之后去过哪」，而档案入口在另一处，
    用户只能退出、再翻动物列表按编号找。三段链路各钉一条。
    """

    def test_backend_exposes_pet_id(self):
        src = read('business/views_chip.py')
        self.assertRegex(
            src, r"'pet_id':\s*chip\.pet_id",
            '追溯接口没下发 pet_id —— 前端拿到编号也无法打开档案（编号不是主键）')
        self.assertRegex(
            src, r"'pet_code':\s*chip\.pet\.code",
            '追溯接口不再下发绑定动物编号')

    def test_trace_drawer_links_to_archive(self):
        js = strip_js_comments(read('static/js/tnr-common.js'))
        self.assertIn('data-pet-archive=', js,
                      '芯片追溯的绑定宠物没有做成可点击的档案入口')
        self.assertIn('this.showPetArchive(petLink.dataset.petArchive)', js,
                      '档案入口没绑到 TNR_UI.showPetArchive —— 点了没反应')

    def test_archive_impl_is_shared(self):
        """实现必须在公共层：捕捉点/医院两端的追溯抽屉都要跳同一份档案。"""
        js = strip_js_comments(read('static/js/tnr-common.js'))
        self.assertIn('async showPetArchive(petId)', js,
                      '一宠一档实现不在 TNR_UI —— 两端会各写一份并漂移')
        self.assertIn('getPetLifecycleStrict(petId)', js,
                      '一宠一档没有走严格取数接口（失败会被静默当成空档案）')
        shelter = strip_js_comments(read('static/js/portals/shelter.js'))
        self.assertRegex(
            shelter, r'async showPetArchive\(petId\)\s*\{\s*return TNR_UI\.showPetArchive\(petId\);',
            '捕捉点端没有委派到公共实现 —— 两处实现迟早口径不一致')


class DischargeClosesTreatmentContractTest(SimpleTestCase):
    """第五十六轮：诊疗页只选「建议去向」，出院状态只由出院单据终态决定。

    此前「诊疗完成 → 选去向 → 完成办结」会在诊疗页直接把 `disposed_at`
    落掉：动物还没交到领养人 / 无害化厂 / 捕捉点手里，看板已经算「已出院」。
    现在 `done_at` 只代表「待出院」，五条出院终态路径各自在单据完成时
    调 `dispose_for_discharge()`。
    """

    def _hospital_js(self):
        return strip_js_comments(read_js('hospital'))

    def test_treatment_page_no_longer_disposes_directly(self):
        js = self._hospital_js()
        for gone in ('treatmentDispose', 'tx-finish', 'finishTreatment'):
            self.assertNotIn(gone, js,
                             f'诊疗页仍在调用 `{gone}` —— 会在出院单据办完前办结')
        api = strip_js_comments(_read_raw('static/js/tnr-api.js'))
        self.assertNotIn('treatmentDispose', api,
                         '已无调用方的 `treatmentDispose` 仍留在 API 层')

    def test_advice_wording_and_discharge_entry(self):
        js = self._hospital_js()
        self.assertIn('建议去向', js)
        self.assertIn('data-act="tx-discharge"', js, '诊疗完成后没有去出院管理的入口')
        self.assertIn('去出院管理', js)
        self.assertIn('诊疗完成，动物进入待出院', js)
        self.assertIn('诊疗完成，动物进入待出院',
                      read('business/views_treatment.py'),
                      '后端完成文案仍写着「请选择办结去向」')

    def test_terminal_actions_close_treatment(self):
        for path in ('business/views_adoption.py', 'business/views_euthanasia.py',
                     'business/views_return.py', 'business/views_release.py',
                     'business/views_capture.py'):
            self.assertIn('dispose_for_discharge', read(path),
                          f'{path} 的出院终态没有办结诊疗单')
        services = read('business/services_treatment.py')
        self.assertIn('def dispose_for_discharge(', services)
        self.assertIn('promote_hall=False', services,
                      '出院办结不得按建议去向重新上架领养大厅')


class DischargeDocMaintenanceContractTest(SimpleTestCase):
    """平台端「出院单据维护」页与服务端白名单的契约（第六十一轮）。

    这个页面直接改动监管台账里的**终态**记录，所以两处最容易出事：

      1. 单据类型表 —— 页签少一类，那类单据在维护入口里永远找不到；
      2. 状态机 —— 前端自己判「这张单能不能退回」会与服务端白名单漂移，
         结果是按钮亮着、提交 400，或反过来该能退的没给入口。
    """

    def _docs_js(self):
        return strip_js_comments(_read_raw('static/js/portals/platform.js'))

    def test_doc_type_labels_match_backend_doc_labels(self):
        js = self._docs_js()
        # 它是 Platform 对象上的属性（`DOC_TYPE_LABELS: {`），不是 `const X = {`；
        # 用错取法会「匹配不到 → 断言静默全绿」。
        literal = find_property_literal(js, 'DOC_TYPE_LABELS')
        self.assertIsNotNone(
            literal,
            '平台端找不到 `DOC_TYPE_LABELS` —— 维护页的类型表没了？'
            '改名要同步本判据，悄悄删掉等于页签再不同步后端。')
        self.assertIn("'': '全部单据'", literal,
                      '空 key 的「全部单据」页签不在了（默认要一眼看到四类单据）')
        fe = dict(re.findall(r'([a-z_]+)\s*:\s*\'([^\']*)\'', literal))
        self.assertEqual(fe, dict(DOC_LABELS),
                          '页签类型与文案必须逐项等于服务端 DOC_LABELS')

    def test_row_actions_read_server_provided_targets(self):
        """可退回状态只能来自响应里的 `targets` —— 前端不得再写一份状态机。"""
        js = self._docs_js()
        self.assertNotIn('TRANSITIONS', js,
                         '平台端又复制了一份状态迁移白名单（服务端改一处就漂移）')
        self.assertIn('r.targets.map', js,
                      '行按钮不再读 targets，退回入口会与白名单脱节')
        self.assertIn('target_labels', js, '按钮文案要取服务端的中文状态名')

    def test_reason_cap_matches_backend(self):
        """原因长度上限两边同一个数：前端放过、后端拒 = 白填一次表单。"""
        js = self._docs_js()
        start = js.find('id="docRollback-reason"')
        self.assertGreater(start, 0, '维护弹窗里没有原因输入框（原因必填是硬要求）')
        window = js[start:start + 200]
        cap = re.search(r'maxlength="(\d+)"', window)
        self.assertIsNotNone(cap, '原因输入框没有 maxlength')
        self.assertEqual(int(cap.group(1)), REASON_MAX,
                         '原因上限与后端 REASON_MAX 不一致')

    def test_api_paths_reach_the_supervision_endpoints(self):
        api = strip_js_comments(_read_raw('static/js/tnr-api.js'))
        self.assertIn("'/api/supervision/discharge-docs/'", api,
                      '列表接口路径不在了 —— 维护页会拿不到单据')
        self.assertIn("'/api/supervision/discharge-docs/status/'", api,
                      '退回接口路径不在了 —— 按钮点了没反应')


class DischargeAdviceCatchUpEntryTest(SimpleTestCase):
    """「没记建议去向 → 四个去向都选不到」必须配一条走得通的补救路（第六十一轮）。

    后端把候选收紧之后，操作员只剩两种结局：自己找到改建议的入口，或者把
    出院页当成系统坏了。所以入口本身要成立：出院页的横幅、横幅点进去的
    诊疗第三步、以及「看板异步加载不能把流程视图覆盖掉」这条竞态守卫。
    """

    def _js(self):
        return strip_js_comments(_read_raw('static/js/portals/hospital.js'))

    def test_discharge_shell_renders_the_notice_and_binds_it(self):
        js = self._js()
        self.assertIn('this.dspAdviceNotice()', js,
                      '出院页不再插「待补录建议去向」横幅 —— 收紧后的空建议动物会静默消失')
        self.assertIn("'data-dspadvice'", js,
                      '横幅里的动物芯片没有事件委托，点了没反应')
        self.assertIn('没有记录建议去向', js, '横幅要说清为什么选不到')

    def test_notice_jumps_into_step_three_of_the_flow(self):
        js = self._js()
        start = js.find('dspGotoAdvice(petId, tid)')
        self.assertGreater(start, 0, '找不到横幅跳转诊疗流程的函数')
        body = js[start:js.find('\n  },', start)]
        nav = body.find("navigateTo('treatment')")
        flow = body.find('openTreatmentFlow(')
        self.assertGreater(nav, -1, '流程视图挂在 #treatmentContent（只在诊疗页里）')
        self.assertGreater(flow, -1, '跳转没有打开诊疗流程')
        self.assertLess(nav, flow,
                        '必须先切到诊疗页再开流程：容器不存在时 openTreatmentFlow 渲染不出东西')

    def test_flow_render_keeps_the_opened_flow(self):
        """看板数据在途时打开的流程，不能被异步回来的 `renderBoard()` 覆盖。"""
        js = self._js()
        start = js.find('async renderTreatmentFlow()')
        self.assertGreater(start, 0, '找不到诊疗流程渲染函数')
        body = js[start:js.find('\n  },', start)]
        guard = "if (this.state.treatmentFlow) { this.renderFlowView(); return; }"
        self.assertEqual(
            body.count(guard), 2,
            '函数里要**两处**守卫：入口一次、await 之后一次。'
            '少了 await 之后那次 = 出院页横幅点进来会被看板覆盖。')
        self.assertLess(body.rindex(guard), body.find('this.renderBoard();'),
                        '守卫必须在 renderBoard() 之前，否则覆盖照样发生')

    def test_discharge_popup_offers_the_edit_entry(self):
        """「与建议去向不符」的弹窗要现场给出改建议入口，而且**接得上线**。

        早先这判据是三条全文件 `assertIn` —— 空转：`openTreatmentFlow(petId,
        tid, 3)` 出院页横幅的跳转也在用，把弹窗按钮的接线整段删掉照样全绿
        （M194 就是这样暴露的）。改成只在弹窗函数体内取体判序。
        """
        js = self._js()
        start = js.find('openDischargeChoice(petId, advised, tid)')
        self.assertGreater(start, 0, '找不到出院去向选择弹窗')
        body = js[start:js.find('\n  },', start)]
        self.assertGreater(len(body), 200, '弹窗函数体被截断，判据会静默失真')
        self.assertNotIn('openDischargeForm(kind, petId)', body,
                         '函数体取到了下一个函数，断言会误命中别处的接线')
        self.assertIn('data-dspedit', body,
                      '出院弹窗不再给「现在就去修改」—— 换去向成了死胡同')
        self.assertIn("querySelector('[data-dspedit]')", body,
                      '按钮没有绑定点击：点了没反应，改建议仍是死胡同')
        bind = body.find("querySelector('[data-dspedit]')")
        self.assertGreater(
            body.find('this.openTreatmentFlow(petId, tid, 3)', bind), -1,
            '修改入口没有落到第三步（建议去向就在第三步）；'
            '注意要的是**接线之后**那次调用，函数开头的「没记建议 → 直接送去补录」不算')


class NoNativeBlockingDialogTest(SimpleTestCase):
    """四端门户 JS 不得用原生 `alert` / `confirm` / `prompt` 收集输入。

    第六十四轮实测踩到的：捕捉点拒绝领养申请时走 `window.prompt` 取原因，
    原生对话框**阻塞 JS 线程** —— 在自动化与内嵌 webview 里点了「确认拒绝」
    之后既不报错也不发请求，操作员以为审核完成，申请其实还挂在待审列表里。
    同一页面上的通过走 `TNR_UI.confirm`，两条按钮一套自研弹层、一套原生框，
    风格也不一致。

    只查**函数调用**形态（`window.prompt(` / 裸 `prompt(`），注释与字符串
    字面量里的字样不算 —— 本文件大量注释就在引用这些 API。
    """

    #: 允许出现在提示文案里的词（如「请输入拒绝原因」），只拦真正的调用。
    CALL = re.compile(r'(?<![\w.$])(?:window\s*\.\s*)?(alert|confirm|prompt)\s*\(')

    def test_portal_js_has_no_native_dialog_calls(self):
        offenders = []
        for role, rel in sorted(PORTAL_JS.items()):
            src = strip_js_comments(_read_raw(rel))
            for no, line in enumerate(src.split('\n'), 1):
                m = self.CALL.search(line)
                if not m:
                    continue
                # `TNR_UI.confirm(` / `this.confirm(` 是自研弹层，合法。
                pre = line[:m.start()]
                if pre.rstrip().endswith('.'):
                    continue
                offenders.append(f'{rel}:{no}  {line.strip()[:70]}')
        self.assertFalse(
            offenders,
            '这些门户代码在用原生对话框收输入（阻塞线程、webview 里会静默失效，'
            '请改用 TNR_UI.modal / TNR_UI.confirm）：\n  ' + '\n  '.join(offenders))

    def test_shelter_adoption_reject_collects_reason_in_modal(self):
        """拒绝领养申请的原因必须来自弹窗，且**必填**。

        审核意见会原样下发到领养人端（`adopter.js` 的「审核意见」）和站内信，
        空着等于把人拒了却不给理由。
        """
        src = strip_js_comments(_read_raw(PORTAL_JS['shelter']))
        start = src.find("title: '拒绝领养申请'")
        self.assertGreater(start, 0,
                           '捕捉点端拒绝领养申请不再有专门的弹窗（退回原生 prompt 了）')
        body = src[start:src.find('\n      };', start)]
        self.assertGreater(len(body), 200, '拒绝弹窗函数体被截断，断言会静默失真')
        self.assertIn('adRejectNote', body, '弹窗里没有拒绝原因输入框')
        self.assertIn('请填写拒绝原因', body,
                      '拒绝原因不再必填：领养人会收到一条没有说明的拒绝')
        bind = body.find("getElementById('btnAdoptReject')")
        self.assertGreater(bind, -1, '「确认拒绝」按钮没有接线')
        self.assertGreater(body.find("submit(id, 'reject'", bind), -1,
                           '按钮没有把原因提交出去（只关了弹窗）')
