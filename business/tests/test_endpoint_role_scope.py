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

本文件锁两件事：

1. **通用规则**（`AuxiliaryEndpointRoleSubsetTest`）：同一资源前缀下，
   「只读辅助接口」（名字含 check/validate/query/preview/probe）
   的角色集合**不得超出**该资源其他接口的角色并集 ——
   这正是"同类接口漂移"的机械判据，能挡住下一个。
2. **行为**（`BlacklistCheckRoleTest`）：多出来的两个角色必须被拒（403），
   而捕捉点/政府端**照常放行**（守卫不能做成一律禁止）。
"""
import re
from collections import defaultdict

from django.urls import get_resolver

from business.models import Blacklist
from business.tests.base import BusinessTestBase, make_district, make_user

ROLES = {'gov_city', 'gov_district', 'shelter', 'hospital', 'adopter'}

# 只读辅助接口的命名特征。**必须带词边界** ——
# 资源名本身含 "check" 时（如 `checkin_list`）会误报，
# 第一版没加边界，把 `checkins` 整组三条都报了假警。
AUX_RE = re.compile(r'(?:^|_)(check|validate|query|preview|probe)(?:_|$)')

# 显式豁免：(资源前缀, 视图函数名) → 理由。
# 豁免必须写理由 —— 没有理由的豁免就是下一个洞。
EXEMPT = {}


def roles_of(callback):
    """从 `role_required` 的闭包里取允许角色集合。

    `role_required(...)` 把角色元组塞在装饰器闭包的 cell 里，
    顺着 `__closure__` → `cell_contents` 找「全部元素都是合法角色名」的那个元组。
    """
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
        rs = roles_of(cb)
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

    EXPECTED = {
        '/api/business/blacklist/': {'shelter', 'gov_city', 'gov_district'},
        '/api/business/blacklist/create/': {'shelter', 'gov_city', 'gov_district'},
        '/api/business/blacklist/check/': {'shelter', 'gov_city', 'gov_district'},
        '/api/business/blacklist/<int:pk>/update/': {'shelter', 'gov_city', 'gov_district'},
        '/api/business/blacklist/<int:pk>/delete/': {'shelter', 'gov_city', 'gov_district'},
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
                '若是刻意放宽，请同时确认该角色**前端有调用点**且返回体不含多余个人信息')
