"""第二十二轮：**接口 × 角色矩阵的横向一致性** —— 同族接口里只有一个"多放行"。

第十九轮扫的是「读接口用了写权限」（角色**不够**，页面拿 403 后静默渲染成空值）。
这一轮是**反方向**：角色**多放了** —— 某个接口比同族接口多放行了几个角色，
而那几个角色**界面里根本没有这个功能**。

实测案例：黑名单一族。

| 接口 | 角色集合 |
|---|---|
| `/blacklist/`（列表） | shelter, gov_city, gov_district |
| `/blacklist/create/` | shelter, gov_city, gov_district |
| `/blacklist/<pk>/update/` | shelter, gov_city, gov_district |
| `/blacklist/<pk>/delete/` | shelter, gov_city, gov_district |
| **`/blacklist/check/`** | **+ hospital, adopter** ← 唯一异类 |

危害不是"多给了个接口"，而是**这个接口的返回体含个人信息**
（`name` 姓名 / `reason` 拉黑原因），且 `check_blacklist()` **刻意不做区县收敛**
（黑名单是全市概念）。于是一个**站外领养人账号**就能拿任意手机号问
「这人在黑名单里吗、叫什么、为什么被拉黑」。实测 `adopter1`（襄城区）
读到了**东津新区**某条记录的姓名与原因「弃养」—— 既跨机构也**跨区县**。

本文件锁三件事：

1. **通用规则**（`AuxiliaryEndpointRoleSubsetTest`）：同一资源前缀下，
   「只读辅助接口」（名字含 check/validate/query/preview/probe）
   的角色集合**不得超出**该资源其他接口的角色并集 ——
   这正是"同类接口漂移"的机械判据，能挡住下一个。
2. **行为**（`BlacklistCheckRoleTest`）：多出来的两个角色必须被拒（403），
   而捕捉点/政府端**照常放行**（守卫不能做成一律禁止）。
3. **反方向**（第三十轮的 `PortalCallRoleMatchTest`，见文件末尾）：
   接口没问题、**调用方**有问题 —— 门户前端不得调用自己角色进不去的接口。
"""
import os
import re
import sys
from collections import defaultdict

from django.urls import get_resolver

from business.models import Blacklist
from business.tests.base import BusinessTestBase, make_district, make_user

ROLES = {'gov_city', 'gov_district', 'shelter', 'hospital', 'adopter'}
# `api_routes()` 用这个更宽集合：`platform_admin` 是平台端专用角色，
# 第三十轮的「门户调用点」检查要判 `platform.js`，漏了它就整片扫不到。
ALL_ROLES = ROLES | {'platform_admin'}

# 只读辅助接口的命名特征。**必须带词边界** ——
# 资源名本身含 "check" 时（如 `checkin_list`）会误报，
# 第一版没加边界，把 `checkins` 整组三条都报了假警。
AUX_RE = re.compile(r'(?:^|_)(check|validate|query|preview|probe)(?:_|$)')

# 显式豁免：(资源前缀, 视图函数名) → 理由。
# 豁免必须写理由 —— 没有理由的豁免就是下一个洞。
EXEMPT = {}


def roles_of(callback, valid=None):
    """从 `role_required` 的闭包里取允许角色集合。

    `role_required(...)` 把角色元组塞在装饰器闭包的 cell 里，
    顺着 `__closure__` → `cell_contents` 找「全部元素都是合法角色名」的那个元组。
    """
    valid = ROLES if valid is None else valid
    f = callback
    for _ in range(6):
        for cell in getattr(f, '__closure__', None) or []:
            try:
                v = cell.cell_contents
            except Exception:
                continue
            if isinstance(v, (tuple, list)) and v and all(
                    isinstance(x, str) and x in valid for x in v):
                return set(v)
        f = getattr(f, '__wrapped__', None)
        if f is None:
            break
    return None


def api_routes():
    """全部 `/api/` 路由 → (路径, 视图函数, 角色集合)。"""
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
        rs = roles_of(cb, ALL_ROLES)
        if rs is None:
            continue
        out.append((pat, getattr(cb, '__name__', ''), rs, cb))
    return out


class AuxiliaryEndpointRoleSubsetTest(BusinessTestBase):
    """通用规则：只读辅助接口不得比同资源其他接口放行更多角色。

    **为什么是这条规则**：一个"多放行"的接口几乎总是漂移出来的
    （有人写新接口时顺手抄了另一族更宽的角色列表），而它的危害
    取决于返回体里有没有敏感字段 —— 靠人工 review 逐个接口比对必然再漏。
    把判据机械化，就变成"每次新增接口自动被检查"。

    **局限（要诚实说）**：它只能发现"同资源内**互不一致**"的漂移。
    如果一个资源**整族**都多放了角色，这条规则不会响 ——
    那种情况要靠「每个放行角色都必须有前端调用点」来抓（见本文件末尾说明）。
    """

    def test_auxiliary_endpoints_do_not_exceed_siblings(self):
        groups = defaultdict(list)
        for pat, name, rs, _cb in api_routes():
            if '/api/business/' not in pat:
                continue
            seg = [s for s in pat.split('/') if s and not s.startswith('<')]
            if len(seg) < 3:
                continue
            groups[seg[2]].append((pat, name, rs))

        checked = []
        offenders = []
        for res, items in sorted(groups.items()):
            others = [rs for _p, n, rs in items if not AUX_RE.search(n)]
            if not others:
                # 没有对照接口就无从比较 —— 不报，但要记下来（避免"扫了 0 条还说通过"）
                continue
            union = set().union(*others)
            for pat, name, rs in items:
                if not AUX_RE.search(name):
                    continue
                checked.append(f'{res}.{name}')
                if (res, name) in EXEMPT:
                    continue
                extra = rs - union
                if extra:
                    offenders.append(
                        f'{name} ({pat}) 多放行 {sorted(extra)}；'
                        f'该资源其他接口的并集 = {sorted(union)}')

        self.assertTrue(checked,
                        '没扫到任何只读辅助接口 —— 扫描逻辑失效了，不是"全部通过"')
        self.assertEqual(offenders, [],
                         '只读辅助接口比同族放行了更多角色（同类接口漂移）：\n  '
                         + '\n  '.join(offenders))


class BlacklistCheckRoleTest(BusinessTestBase):
    """行为层：黑名单查询接口的角色边界。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.district_c = make_district(name='测试丙区')
        # 受害记录归属**另一个区县**，用来证明"跨区县也能读到"
        cls.victim = Blacklist.objects.create(
            name='受害黑名单人', phone='13700001234', id_card='420602198905124321',
            reason='弃养', district=cls.district_c, is_deleted=False)
        cls.adopter = make_user('bl_adopter', role='adopter', district=cls.district_a)
        cls.hospital = make_user('bl_hosp', role='hospital', district=cls.district_a,
                                 institution=cls.hospital_a)
        cls.shelter = make_user('bl_shelter', role='shelter', district=cls.district_a,
                                institution=cls.shelter_a)
        cls.gov = make_user('bl_gov', role='gov_district', district=cls.district_a)

    def _check(self, user, phone=None):
        """返回**响应对象**（`get_json` 返回 resp，body 要自己 `.json()`）。"""
        self.login_as(user)
        return self.get_json(
            f'/api/business/blacklist/check/?phone={phone or self.victim.phone}')

    # ---------- 必须被拒（第二十二轮收口）----------

    def test_adopter_cannot_probe_blacklist(self):
        """站外领养人**不能**拿手机号来查黑名单 —— 这是本轮修的那条口子。"""
        resp = self._check(self.adopter)
        self.assertEqual(resp.status_code, 403,
                         '领养人不应能查询黑名单（返回体含姓名与拉黑原因）')

    def test_hospital_cannot_probe_blacklist(self):
        """医院端也没有黑名单功能，不应放行。"""
        resp = self._check(self.hospital)
        self.assertEqual(resp.status_code, 403)

    def test_adopter_cannot_learn_name_or_reason(self):
        """不只看状态码 —— 确认**响应体里没有**姓名与拉黑原因。

        （403 的响应体理论上不该带业务数据，但这条断言防的是
        「以后有人把它改成 json_fail 200 + success:false」这类改法。）
        """
        resp = self._check(self.adopter)
        body = resp.content.decode('utf-8', 'replace')
        self.assertNotIn('受害黑名单人', body)
        self.assertNotIn('弃养', body)

    # ---------- 正向对照：照常放行 ----------

    def test_shelter_can_still_check(self):
        """捕捉点端是**唯一**真正用它的角色（前端三处调用点），必须照常放行。"""
        body = self.ok(self._check(self.shelter))
        self.assertTrue(body['data']['in_blacklist'])
        self.assertEqual(body['data']['name'], '受害黑名单人')

    def test_gov_can_still_check(self):
        """政府端与同族接口口径一致（列表/建/改/删都放行政府端），保留。"""
        body = self.ok(self._check(self.gov))
        self.assertTrue(body['data']['in_blacklist'])

    def test_shelter_can_still_see_cross_district_hit(self):
        """**跨区县命中是设计如此**，本轮没改：黑名单是全市概念。

        一个人在襄城区被拉黑，去樊城区照样不该能领养 —— 所以
        `check_blacklist()` 不做区县收敛。这条断言把它**钉住**，
        防止以后有人"顺手"把它也收敛掉、导致跨区县绕过。
        """
        body = self.ok(self._check(self.shelter, phone=self.victim.phone))
        self.assertEqual(self.victim.district_id, self.district_c.id,
                         '夹具失效：受害记录应属于另一个区县')
        self.assertNotEqual(self.shelter.district_id, self.victim.district_id)
        self.assertTrue(body['data']['in_blacklist'],
                        '跨区县命中被收窄了 —— 这会让黑名单形同虚设')


class BlacklistRoleSetCharacterizationTest(BusinessTestBase):
    """把黑名单一族五个接口的角色集合**钉死**，任何改动都必须是有意的。

    这条不是"规则"，是"快照" —— 它挡不住同类问题在别处复发（那是上面
    `AuxiliaryEndpointRoleSubsetTest` 的职责），但能挡住**这里**被静默改回去。
    """

    # 第四十九轮：政府端转只读。列表与 `check` 是**读**（`check` 虽是 POST，
    # 只查询不改库），政府端照旧放行；`create/update/delete` 三个写接口收掉。
    EXPECTED = {
        '/api/business/blacklist/': {'shelter', 'gov_city', 'gov_district'},
        '/api/business/blacklist/create/': {'shelter'},
        '/api/business/blacklist/check/': {'shelter', 'gov_city', 'gov_district'},
        '/api/business/blacklist/<int:pk>/update/': {'shelter'},
        '/api/business/blacklist/<int:pk>/delete/': {'shelter'},
    }

    def test_role_sets_are_pinned(self):
        actual = {pat: rs for pat, _n, rs, _cb in api_routes()
                  if pat in self.EXPECTED}
        self.assertEqual(
            set(actual), set(self.EXPECTED),
            '黑名单路由有增减 —— 请同步更新本快照与说明')
        for pat, expected in self.EXPECTED.items():
            self.assertEqual(
                actual[pat], expected,
                f'{pat} 的角色集合变了：{sorted(actual[pat])} != {sorted(expected)}。'
                '若是刻意放宽，请同时确认该角色**前端有调用点**且返回体不含多余个人信息；'
                '若是把政府端放回去，先回答「监管代录」这个问题（见 `gov_portal` 的说明）')


# ---------------------------------------------------------------- 第三十轮：
# 同一张矩阵的**第三个方向**：接口没多放行、也没少放行，是**前端多调用**了。
#
# 实测案例（本轮修的）：医院端「办理领回」在提交前调 `TNR_API.checkBlacklist()`
# 做黑名单预检。接口本身没问题 —— 第二十二轮刚把它的放行集合收回到
# `shelter / gov_*`；问题是**医院端根本不该调它**。结果每次点「确认领回」
# 都吃一个 403，界面报「黑名单校验失败，请重试」，领回流程整个走不下去
# （实测卡在 TNR261026 系列第 3 条）。而服务端 `owner_return_create()`
# 本来就会 `check_blacklist()` 并返回「该主人已在黑名单中：…」，预检纯属多余。
#
# 上面 `AuxiliaryEndpointRoleSubsetTest` 抓不到这一类：它比的是**接口之间**的
# 角色并集，这里接口是对的、调用方是错的。本类把判据反过来 ——
# **每个门户 JS 调用的接口，都必须放行该门户自己的角色**。
# 顺带把 `views_checkin.blacklist_check()` 文档里那句「前端零调用点」
# 从**口头断言**变成**机器检查**：以前它只写在注释里、没人验证，
# 所以医院端加一行调用时它照样"成立"。
PORTAL_JS_ROLES = {
    'shelter.js': {'shelter'},
    'hospital.js': {'hospital'},
    'gov.js': {'gov_city', 'gov_district'},
    'adopter.js': {'adopter'},
    'platform.js': {'platform_admin'},
}

# (门户文件, 接口路径) → 理由。同样**没有理由的豁免就是下一个洞**。
PORTAL_EXEMPT = {}

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# 判据扫的是「JS 里有没有出现这个路径」，所以**必须先把注释抹平**：
# 说明注释里写一句「`/api/business/adoptions/` 的 `pet` 是嵌套对象」，
# 不剥注释就会被当成一个真实调用点，报出「领养人端调用了不放行 adopter 的
# 接口（点即 403）」的假警 —— 而全量测试会因为这条假警红掉。
# 用 `scripts/jslex.blank_comments`（只抹注释、保留字符串字面量、**等长**），
# 不用 `strip_js`：那个连字符串一起抹，路径会被抹没，判据反过来恒假。
_SCRIPTS = os.path.join(ROOT, 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from jslex import blank_comments  # noqa: E402

WILD = '{*}'
_LIT = re.compile(r"['\"`]([^'\"`]*)['\"`]")
# 拼接续接：先试「+ 字面量」，再试「+ 变量 + 字面量」（变量段还原成 WILD）。
_NEXT_LIT = re.compile(r"^\s*\+\s*['\"`]([^'\"`]*)['\"`]")
_NEXT_VAR = re.compile(r"^\s*\+\s*[^+;,\n)]{1,80}?\s*\+\s*['\"`]([^'\"`]*)['\"`]")
# 相对路径只认「紧跟在 get/post 之后」这一种写法（`TNR_API.post('a/b/', …)`），
# 否则会把 `'Content-Type'`、`'application/json'` 之类当成路径。
_RELATIVE_HEAD = re.compile(r"(?:this|TNR_API)\._?(?:get|post)\(\s*$")
_METHOD_DEF = re.compile(r'(?m)^  (?:async )?([A-Za-z_]\w*)\s*\([^)]*\)\s*\{')
_METHOD_CALL = re.compile(r'TNR_API\.([A-Za-z_]\w*)\(')


def read_js(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding='utf-8') as fobj:
        return fobj.read()


def _js_src(rel_path):
    """读 JS、抹掉注释，并把 `BASE` / `KEYS.x` 常量展开成完整路径字面量。"""
    src = blank_comments(read_js(rel_path))
    keys = {}
    block = re.search(r'KEYS:\s*\{(.*?)\n  \}', src, re.S)
    if block:
        keys = dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))
    src = re.sub(r'this\.KEYS\.(\w+)',
                 lambda m: "'/api/business/%s'" % keys.get(m.group(1), 'x'), src)
    return (src.replace('this.BASE', "'/api/business/'")
               .replace('TNR_API.BASE', "'/api/business/'"))


def js_api_paths(src):
    """JS 源码里出现过的 `/api/...` 路径。

    模板串（`` `/x/${id}/` ``）和字符串拼接（`'/x/' + id + '/'`）都还原成一条
    路径，变量段统一成 `WILD`；`this.BASE` / `TNR_API.BASE` 先展开成
    `'/api/business/'`，所以 `BASE + 'a/b/'` 也能拼成完整路径。
    """
    out = set()
    for m in _LIT.finditer(src):
        lit = re.sub(r'\$\{[^}]*\}', WILD, m.group(1))
        tail = src[m.end():]
        while True:
            nxt = _NEXT_LIT.match(tail)
            if nxt:
                lit += nxt.group(1)
                tail = tail[nxt.end():]
                continue
            nxt = _NEXT_VAR.match(tail)
            if nxt:
                lit += WILD + nxt.group(1)
                tail = tail[nxt.end():]
                continue
            break
        # `BASE + KEYS.x` 展开后会出现两遍前缀
        lit = lit.replace('/api/business//api/business/', '/api/business/')
        if not lit.startswith('/api/'):
            if not (lit and '/' in lit
                    and _RELATIVE_HEAD.search(src[:m.start()])):
                continue
            lit = '/api/business/' + lit
        out.add(lit.split('?')[0])
    return out


def api_method_paths():
    """`tnr-api.js` 的方法名 → 它实际请求的路径集合。

    门户 JS 基本不写裸路径，走的全是 `TNR_API.xxx()`；判据要落在门户文件上，
    就得先把方法名翻译回路径 —— 这一步就是这个用例的全部价值所在。
    """
    src = _js_src('static/js/tnr-api.js')
    defs = list(_METHOD_DEF.finditer(src))
    bodies = {}
    for i, m in enumerate(defs):
        end = defs[i + 1].start() if i + 1 < len(defs) else len(src)
        bodies[m.group(1)] = src[m.end():end]
    out = {name: js_api_paths(body) for name, body in bodies.items()}
    # 方法套方法（`async getX() { return this.getData('/api/…') }`）再展开一层
    for name, body in bodies.items():
        for callee in (_METHOD_CALL.findall(body)
                       + re.findall(r'\bthis\.(\w+)\(', body)):
            out[name] = out[name] | out.get(callee, set())
    return out


def portal_api_paths(fname):
    """某门户 JS 实际会打到的接口路径（直接字面量 + 经 `TNR_API` 方法间接调用）。"""
    src = _js_src(f'static/js/portals/{fname}')
    paths = js_api_paths(src)
    methods = api_method_paths()
    for name in _METHOD_CALL.findall(src):
        paths |= methods.get(name, set())
    return paths



def segs(path):
    return tuple(s for s in path.split('/') if s)


def _seg_match(js_seg, pat_seg):
    """一个路径段的比对。`WILD`（JS 变量段）能顶任何路由段。"""
    if js_seg == WILD:
        return True
    if pat_seg.startswith('<'):
        # ⚠ 必须校验转换器类型，否则 `/adoptions/hall/{*}` 会**同时**命中
        # `/adoptions/<int:pk>/edit-info/`（`hall` 顶上了 `<int:pk>`），
        # 于是拿了一堆不相干接口的角色并集，误报成"领养人越权"。
        inner = pat_seg[1:-1]
        converter = inner.split(':')[0]
        if converter == 'int':
            return js_seg.isdigit()
        if converter == 'slug':
            return bool(re.fullmatch(r'[-\w]+', js_seg))
        return True
    return js_seg == pat_seg


def match_routes(shape, routes):
    """JS 路径形状 → 命中的路由（段数相同，`<pk>`/`WILD` 按类型通配）。"""
    hits = []
    for pat, rs in routes:
        psegs = segs(pat)
        if len(psegs) != len(shape):
            continue
        if all(_seg_match(a, b) for a, b in zip(shape, psegs)):
            hits.append((pat, rs))
    return hits


class PortalCallRoleMatchTest(BusinessTestBase):
    """通用规则：门户前端调用的接口必须放行该门户自己的角色。"""

    def test_portal_calls_match_own_role(self):
        routes = [(pat, rs) for pat, _n, rs, _cb in api_routes()]
        checked = []
        offenders = []
        for fname, portal_roles in sorted(PORTAL_JS_ROLES.items()):
            for path in sorted(portal_api_paths(fname)):
                hits = match_routes(segs(path), routes)
                if not hits:
                    # 路由表里没有这条（或该视图不带 `role_required`）—— 无从判定，
                    # 但不计入 checked，避免"扫了 0 条还说通过"。
                    continue
                checked.append(f'{fname} -> {path}')
                if (fname, path) in PORTAL_EXEMPT:
                    continue
                allowed = set().union(*[rs for _p, rs in hits])
                missing = portal_roles - allowed
                if missing:
                    offenders.append(
                        f'{fname} 调用 {path}（命中 '
                        f'{sorted(p for p, _ in hits)}）放行 {sorted(allowed)}，'
                        f'缺 {sorted(missing)}')

        self.assertGreater(len(checked), 100,
                           f'只扫到 {len(checked)} 个调用点 —— 解析逻辑失效了，不是"全部通过"')
        self.assertEqual(offenders, [],
                         '门户前端调用了自己角色被挡在门外的接口（点即 403）：\n  '
                         + '\n  '.join(offenders))

    def test_comment_paths_are_not_counted_as_calls(self):
        """注释里出现的接口名**不是调用点** —— 判据只能看见真代码。

        实测成因：`adopter.js` 的说明注释里写了 `/api/business/adoptions/`
        （解释列表接口的 `pet` 是嵌套对象、打卡记录里是数字 id）。不剥注释时
        这条路径被当成领养人端的真实调用点，于是报「放行 shelter/hospital/gov_*、
        缺 ['adopter']，点即 403」——**假指控**，接口根本没人调，全量测试却红掉。
        反方向更危险：注释里写「这里曾经调用过 X」也会让「X 已被移除」的判据恒假。
        """
        raw = read_js('static/js/portals/adopter.js')
        self.assertIn('/api/business/adoptions/', raw,
                      '锚点已失效：注释里那条路径不见了，本用例不再能证明剥离器在工作')
        self.assertNotIn(
            '/api/business/adoptions/', portal_api_paths('adopter.js'),
            '注释里的路径被当成调用点 —— `_js_src` 必须先用 `jslex.blank_comments` '
            '抹注释；否则说明文字会造出假 403 指控，也会把「已删掉的调用」算还在')

    def test_hospital_does_not_call_blacklist_check(self):
        """行为锚：本轮那条具体缺陷 —— 医院端不该再碰黑名单查询。

        （通用规则已能拦住，这条是**变异锚**：把 `js_api_paths` 的解析改坏时，
        通用用例可能整片扫不到而"通过"，这条点名到接口，删一行调用就红。）
        """
        called = portal_api_paths('hospital.js')
        self.assertNotIn('/api/business/blacklist/check/', called,
                         '医院端不应调用 `/blacklist/check/`：放行集合是 shelter/gov_*'
                         '（`BlacklistRoleSetCharacterizationTest` 钉死），服务端已做校验')
