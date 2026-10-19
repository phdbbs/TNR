"""政府端只读（第四十九轮）的机械化闸门。

设计口径来自 `accounts/models.py` 的角色表注释 —— 政府端
（`gov_city` / `gov_district`）「只保留查看」。这条约束在项目里**写了很久，
但从来没有判据**：直到本轮之前，25 个会改库的接口的 `@role_required`
里都还挂着政府角色，而且 `shelter_portal` / `hospital_portal` /
`adopter_portal` 三个门户页也对政府账号放行（进去就是一整套写 UI）。

「靠人记得」在这种规模的接口面上必然漂移，所以这里用四条判据钉住：

1. `GovReachableRoutePinTest` —— 政府角色**实际打得到哪些接口**是一份显式清单。
   新接口想放行政府端，必须先把路径加进清单，加的人就得回答「这是读还是写」。
2. `GovReadOnlySweepTest` —— 政府会话把这些路径 GET + POST 各打一遍，
   断言**业务数据指纹一比特都没变**。这是「只读」最直接的定义，而且它是
   **方法无关**的：本项目多数写接口根本不判 `request.method`
   （`checkin_review` 用 GET 也能审核通过），所以「禁止 POST」这种闸门
   本身就是漏的 —— 必须按角色封。
3. `GovWriteRouteClosedTest` —— 反过来钉本轮**关闭**的写路径：
   两个政府角色打它们都必须是 403（而不是 200/400/404 —— 那说明还在往里走）。
4. `FingerprintCanSeeARealWriteTest` —— 反向对照。指纹判据写成
   「永远相等」也能通过，所以必须证明它抓得到一次真实的写。

页面入口（`/shelter/`、`/hospital/`、`/adopter/`）另由
`GovPortalEntryTest` 覆盖：那三个是 HTML 页面，被拒时走的是
`role_required` 的**非 API 分支**（`messages.error` + 302 回 `/`），
不是 403 JSON —— 两种形态不能混为一谈。

⚠ 判据 1 为什么按**行为**枚举而不是按装饰器内省：
`business/tests/test_endpoint_role_scope.py` 的 `roles_of()` 只认
`ROLES` 里那五个业务角色，`@role_required('platform_admin')` 的闭包它读不出来
（会返回 None，整条路由从枚举里消失）。拿它当「政府可达全集」等于把
`/api/supervision/**` 那批平台端接口当成不存在 —— 万一哪天有人忘了加装饰器，
闸门反而看不见。所以这里直接枚举 URLconf，用真实响应码定性。
"""
import hashlib
import json
import re

from django.apps import apps
from django.urls import get_resolver

from business.models import Capture, Treatment
from business.tests.base import (
    BusinessTestBase, make_capture, make_material, make_pet,
)
from business.tests.test_endpoint_role_scope import api_routes

GOV_ROLES = {'gov_city', 'gov_district'}

#: 政府端**实际可达**的 `/api/` 路径（本轮收口后的快照，全部是读）。
#: 每一项都得是「查询」；加新条目前先问：这个 view 会 `.save()` 吗？
GOV_API_ALLOWLIST = {
    '/api/business/adoptions/',
    '/api/business/adoptions/applications/',
    '/api/business/adoptions/hall/',
    '/api/business/adoptions/hall/<int:pk>/',
    '/api/business/adoptions/my-applications/',
    '/api/business/blacklist/',
    '/api/business/blacklist/check/',
    '/api/business/captures/',
    '/api/business/captures/<int:pk>/',
    '/api/business/captures/codes-preview/',
    '/api/business/checkins/',
    '/api/business/chips/',
    '/api/business/chips/preview/',
    '/api/business/chips/trace/',
    '/api/business/euthanasia/',
    '/api/business/geocode/ip/',
    '/api/business/geocode/reverse/',
    '/api/business/hall-listings/',
    '/api/business/materials/',
    '/api/business/materials/<int:pk>/batches/',
    '/api/business/materials/dispatch-targets/',
    '/api/business/materials/hospital-ledger/',
    '/api/business/materials/shelter-ledger/',
    '/api/business/materials/transactions/',
    '/api/business/owner-returns/',
    '/api/business/pets/',
    '/api/business/pets/<int:pet_id>/lifecycle/',
    '/api/business/pets/archive/',
    '/api/business/portal/adoptions/',
    '/api/business/portal/messages/',
    '/api/business/releases/',
    '/api/business/transfers/',
    '/api/business/treatments/',
    '/api/business/treatments/<int:pk>/',
    '/api/business/treatments/board/',
    '/api/me/',
    '/api/supervision/business/',
    '/api/supervision/dashboard/',
    '/api/supervision/districts/',
    '/api/supervision/institutions/',
    '/api/supervision/ledger/',
    '/api/supervision/materials/',
}

#: 可达但**不是业务写**的例外：改自己的登录口令。
#: 它确实 `.save()`，所以不能塞进「全部是读」的清单里装作没看见；
#: 但把它封掉等于把监管账号永久锁死在初始口令上 —— 账号自助不属于
#: 「监管代录业务数据」，第四十九轮的口径只收业务写。
GOV_SELF_SERVICE_WRITES = {'/api/me/password/'}

#: 对政府角色关闭的写接口（视图函数名 → 路径模板）。
#: 名字而不是路径做键：路径里带 `<pk>`，而**每一个**都必须在两个政府角色下 403。
#: 第四十九轮收口时关掉的是下面这一批；**此后新增的写接口也要登记在这里** ——
#: 判据 1（可达清单）只会说「政府端多了一条」，而这份名单指名道姓，
#: 漏登记一个新写入口时它才答得出「是哪一行的角色被改回去了」。
#: 例：`treatment_sterilization`（第五十一轮，绝育手术病历的独立写入口）。
GOV_CLOSED_WRITE_ROUTES = {
    'adoption_info_edit': '/api/business/adoptions/%s/edit-info/',
    'adoption_register': '/api/business/adoptions/register/',
    'adoption_reclaim': '/api/business/adoptions/%s/reclaim/',
    'adoption_apply': '/api/business/adoptions/apply/',
    'adoption_application_review': '/api/business/adoptions/applications/%s/review/',
    'capture_create': '/api/business/captures/create/',
    'capture_update': '/api/business/captures/%s/update/',
    'capture_delete': '/api/business/captures/%s/delete/',
    'owner_return_create': '/api/business/owner-returns/create/',
    'checkin_review': '/api/business/checkins/%s/review/',
    'blacklist_create': '/api/business/blacklist/create/',
    'blacklist_update': '/api/business/blacklist/%s/update/',
    'blacklist_delete': '/api/business/blacklist/%s/delete/',
    'euthanasia_create': '/api/business/euthanasia/create/',
    'body_receive': '/api/business/euthanasia/%s/body-receive/',
    'purchase_create': '/api/business/materials/purchase/',
    'dispatch_create': '/api/business/materials/dispatch/',
    'stock_adjustment': '/api/business/materials/adjustment/',
    'material_outbound': '/api/business/materials/outbound/',
    'mark_message_read': '/api/business/portal/messages/%s/read/',
    'release_create': '/api/business/releases/create/',
    'release_transfer': '/api/business/releases/%s/transfer/',
    'release_confirm': '/api/business/releases/%s/confirm/',
    'transfer_create': '/api/business/transfers/create/',
    'transfer_withdraw': '/api/business/transfers/%s/withdraw/',
    'treatment_sterilization': '/api/business/treatments/%s/sterilization/',
}

#: 政府角色不再能进入的操作门户（HTML 页面，非 `/api/`）
GOV_BLOCKED_PAGES = ('/shelter/', '/hospital/', '/adopter/')

#: 出网的外部调用（高德逆地理 / IP 定位），与「读还是写」无关，
#: 已由 `business/tests` 里 mock 过的用例单独覆盖。视图名见 `business/urls.py:24-25`。
OUTBOUND_HTTP_VIEWS = {'geocode_reverse', 'geocode_ip'}

DENIED_STATUSES = (401, 403)


def all_api_routes():
    """URLconf 里全部 `/api/` 路由 → (路径模式, url name)。

    不走 `api_routes()` 的角色内省，理由见模块 docstring 末尾。
    """
    out = []

    def walk(resolver, prefix=''):
        for p in resolver.url_patterns:
            if hasattr(p, 'url_patterns'):
                walk(p, prefix + str(p.pattern))
            else:
                path = '/' + (prefix + str(p.pattern)).lstrip('/')
                if '/api/' in path:
                    out.append((path, p.name or ''))

    walk(get_resolver())
    return sorted(set(out))


def fingerprint():
    """业务库指纹：每个模型的「行数 + 全部行内容」哈希。

    ⚠ 不能只比行数（`core.tests.model_counts()` 那种）。政府端只读最典型的
    破坏形态恰恰是**原地改状态**：把一张转运单撤回、把一条领养申请改成
    approved、把一段诊疗办结 —— 行数一格不变，而数据已经被改了。
    所以这里连内容一起算。

    ⚠ `core.AuditLog` **必须排除**，但原因不是「它不重要」：
    `AuditLogMiddleware` 对**任意**已登录用户打向 `/api/` 的 POST 都落一行
    （它只看方法，不看视图是否真的写了库，见 `core/middleware.py:_should_log`）。
    本判据要故意把 POST 打到只读接口上，审计表必然增长；把它算进指纹
    等于让判据恒红。业务表才是「只读」的判据对象。
    """
    out = {}
    for model in apps.get_models():
        label = model._meta.label
        if label.startswith('django_q.') or label in ('sessions.Session', 'core.AuditLog'):
            continue
        try:
            rows = list(model.objects.order_by('pk').values())
        except Exception:                        # noqa: BLE001
            continue                             # 第三方表在测试库里可能不存在
        blob = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
        out[label] = (len(rows), hashlib.md5(blob.encode()).hexdigest())
    return out


def diff_keys(before, after):
    return sorted({k for k in set(before) | set(after)
                   if before.get(k) != after.get(k)})


class GovReadOnlySweepTest(BusinessTestBase):
    """判据 2：政府会话打遍它能访问的每条 `/api/` 路径 → 业务数据零变化。"""

    def setUp(self):
        # 夹具放 setUp 而不是 setUpTestData：Django 不允许在 setUpTestData 里
        # 改模型实例属性，而这里几条用例共享同一份业务数据、
        # 每条都要从「干净的表」开始比指纹。
        self.pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                            status='in_treatment')
        self.capture = make_capture(district=self.district_a,
                                     shelter=self.shelter_a,
                                     pet_codes=[self.pet.code])
        self.material = make_material(district=self.district_a, shelter_stock=10)
        self.treatment = Treatment.objects.create(
            pet=self.pet, hospital=self.hospital_a, district=self.district_a,
            status='in_progress')
        # url name → 该路径 `<pk>` 该用哪个真实主键。给真实 id 才能把视图
        # 一路打到查询/写库的那一行，给 99999999 只会早早 404（那样
        # 「没有变化」是假绿）。
        self.pk_by_view = {
            'capture_detail': self.capture.id,
            'pet_lifecycle': self.pet.id,
            'material_batches': self.material.id,
            'treatment_detail': self.treatment.id,
        }
        self.routes = self._concretize()
        self.assertGreater(
            len(self.routes), 20,
            '只枚举出 %d 条政府端可达路由 —— 枚举逻辑坏了，判据会假绿'
            % len(self.routes))

    def _concretize(self):
        """把模式串换成带真实主键的具体路径，并剔掉出网的那几条。"""
        out, skipped = [], set()
        for pattern, name in all_api_routes():
            if name in OUTBOUND_HTTP_VIEWS:
                skipped.add(pattern)
                continue
            pk = self.pk_by_view.get(name, 99999999)
            out.append((pattern, re.sub(r'<[^>]+>', str(pk), pattern)))
        self.skipped_patterns = skipped
        return sorted(set(out))

    def _sweep(self, method):
        """政府两个角色 × 每条可达路由 × GET/POST，断言业务数据零变化。

        指纹是**滚动基线**：被 403/401 拒绝的请求不重算 —— `role_required`
        在视图之前就返回，请求根本没执行，不可能改数据；对它们逐条算整库
        指纹会让这条用例慢到没人愿意跑，而「慢到没人跑」本身就是一种失效。
        """
        problems = []
        reachable = {}
        for user in (self.gov_city, self.gov_a):
            self.login_as(user)
            baseline = fingerprint()
            for pattern, path in self.routes:
                if method == 'GET':
                    resp = self.client.get(path)
                else:
                    resp = self.client.post(path, data='{}',
                                            content_type='application/json')
                if resp.status_code >= 500:
                    problems.append('%s %s %s → %s（政府请求不该打出 5xx，'
                                    '5xx 说明代码真被执行了）'
                                    % (user.role, method, pattern, resp.status_code))
                if resp.status_code not in DENIED_STATUSES:
                    changed = diff_keys(baseline, fingerprint())
                    if changed:
                        problems.append('%s %s %s → 改动了 %s'
                                        % (user.role, method, pattern, changed))
                    reachable.setdefault(pattern, set()).add(resp.status_code)
                    baseline = fingerprint()
        return problems, reachable

    def test_gov_get_changes_nothing(self):
        problems, _ = self._sweep('GET')
        self.assertEqual([], problems, '政府端的 GET 改变了业务数据')

    def test_gov_post_changes_nothing(self):
        """绝大多数写接口**不判 request.method**，所以「禁止 POST」也不是判据。

        这条抓的是「角色闸门放行 + 视图自己会写库」的组合，
        与项目里那些 `@csrf_exempt` 且无方法校验的 view 一一对应。
        """
        problems, _ = self._sweep('POST')
        self.assertEqual([], problems, '政府端的 POST 改变了业务数据')

    def test_gov_reachable_set_by_behavior(self):
        """判据 1 的行为版本：政府**打得通**（非 403）的路径必须正好是清单。

        与 `test_gov_reachable_set_matches_allowlist`（读装饰器）互为补充：
        装饰器内省看不见漏加 `role_required` 的路由，这条看得见 ——
        那正是最危险的一种漂移。
        """
        _, reachable = self._sweep('GET')
        actual = set(reachable) | self.skipped_patterns
        expected = set(GOV_API_ALLOWLIST) | GOV_SELF_SERVICE_WRITES
        self.assertEqual(
            expected, actual,
            '政府端可达接口清单与实际响应不一致。\n'
            '多出来的（新放行）：%s\n'
            '少掉的（已收紧或视图报错）：%s\n'
            '改清单前先回答：这个 view 会改库吗？会的话不属于「监管查看」，'
            '要单独论证"监管代录"（第四十九轮已经否掉过一次）。'
            % (sorted(actual - expected), sorted(expected - actual)))


class GovReachableRoutePinTest(BusinessTestBase):
    """判据 1 的源码版本：装饰器里被点名的政府角色也得和清单对齐。"""

    def test_gov_reachable_set_matches_allowlist(self):
        actual = {pat for pat, _n, roles, _cb in api_routes() if roles & GOV_ROLES}
        # 清单里那几条「无装饰器/平台端」的公开读路径由行为判据覆盖，
        # 这里只比装饰器能看见的部分。
        decorated = {p for p in GOV_API_ALLOWLIST
                     if p in {pat for pat, _n, _r, _c in api_routes()}}
        added = sorted(actual - decorated)
        removed = sorted(decorated - actual)
        self.assertEqual(
            [], added,
            '政府端新拿到了这些接口：%s —— 加进 GOV_API_ALLOWLIST 之前，'
            '先确认这个 view 不会 .save()/.create()/.delete()。'
            '（第四十九轮的口径是「监管只查看」，要写就得当作「监管代录」'
            '单独论证，不能顺手加角色。）' % added)
        self.assertEqual(
            [], removed,
            '这些路径不再对政府角色放行：%s —— 若是刻意收紧，请同步清单；'
            '若是误伤，监管端会直接看不到数据。' % removed)

    def test_no_write_view_grants_gov_role(self):
        """源码级复核：`GOV_CLOSED_WRITE_ROUTES` 里每个视图的角色集合都不含政府。

        行为闸门依赖「请求真能打到视图」；这条只看装饰器本身，
        两者互为补充 —— 装饰器被改回去时它会立刻指名道姓。
        """
        offenders = []
        for _pat, name, roles, _cb in api_routes():
            if name in GOV_CLOSED_WRITE_ROUTES and roles & GOV_ROLES:
                offenders.append('%s 仍然放行 %s' % (name, sorted(roles & GOV_ROLES)))
        self.assertEqual([], offenders,
                         '这些写接口又对政府角色放开了：' + ', '.join(offenders))


class GovWriteRouteClosedTest(BusinessTestBase):
    """判据 3：已关闭的写路径对两个政府角色统一 403。"""

    def setUp(self):
        self.pet = make_pet(district=self.district_a, shelter=self.shelter_a)
        self.capture = make_capture(district=self.district_a,
                                    shelter=self.shelter_a, pet_codes=[self.pet.code])

    def _path(self, template):
        return template % self.capture.id if '%s' in template else template

    def test_closed_write_routes_return_403_for_gov(self):
        misses = []
        for user in (self.gov_city, self.gov_a):
            self.login_as(user)
            for _name, template in sorted(GOV_CLOSED_WRITE_ROUTES.items()):
                path = self._path(template)
                resp = self.client.post(path, data='{}',
                                        content_type='application/json')
                if resp.status_code != 403:
                    misses.append('%s POST %s → %s（期望 403）'
                                  % (user.role, path, resp.status_code))
        self.assertEqual([], misses,
                         '\n'.join(misses) + '\n\n403 是「角色闸门在视图之前」的形态；'
                         '拿到别的状态码说明请求已经进去了。')

    def test_legitimate_role_can_still_write(self):
        """反向对照：关掉政府端**不能**顺手把合法角色一起关掉。

        本轮收口前逐个核对过每个写接口都还有非政府角色能用；这条把它钉住。
        挑三个不同归属的：捕捉登记（shelter）、领养申请（adopter）、
        领养信息编辑（hospital）。
        """
        self.login_as(self.shelter_user_a)
        resp = self.client.post('/api/business/captures/create/', data='{}',
                                content_type='application/json')
        self.assertNotEqual(
            resp.status_code, 403,
            '捕捉登记对捕捉点角色变成 403 —— 政府端收口把 shelter 一起带走了')

        self.login_as(self.adopter)
        resp = self.client.post('/api/business/adoptions/apply/', data='{}',
                                content_type='application/json')
        self.assertNotEqual(resp.status_code, 403, '领养申请不再放行 adopter')

        self.login_as(self.hospital_user_a)
        resp = self.client.post('/api/business/adoptions/%s/edit-info/' % self.capture.id,
                                data='{}', content_type='application/json')
        self.assertNotEqual(resp.status_code, 403, 'edit-info 不再放行 hospital')


class GovPortalEntryTest(BusinessTestBase):
    """页面侧判据：政府账号进不了另外三个操作门户。

    页面不是 `/api/`，`role_required` 走的是另一条分支：
    `messages.error('无权访问该页面')` + **302 回 `/`**。
    所以这里断的是重定向，不是 403 —— 拿 403 去断页面会永远失败，
    拿 302 去断接口会永远失败，两个形态都不能省。
    """

    def test_gov_cannot_open_operator_portals(self):
        for user in (self.gov_city, self.gov_a):
            self.login_as(user)
            for page in GOV_BLOCKED_PAGES:
                resp = self.client.get(page)
                self.assertIn(resp.status_code, (302, 403),
                              '%s 访问 %s 得到 %s，期望被拒'
                              % (user.role, page, resp.status_code))
                if resp.status_code == 302:
                    self.assertEqual(resp['Location'], '/',
                                     '被拒的页面应该回到端口选择页，实际 %s'
                                     % resp['Location'])

    def test_gov_can_still_open_own_portal(self):
        for user in (self.gov_city, self.gov_a):
            self.login_as(user)
            self.assertEqual(self.client.get('/gov/').status_code, 200)

    def test_shelter_and_hospital_still_open_their_portals(self):
        """反向对照：入口收口不能把真正的操作角色挡在门外。"""
        for user, page in ((self.shelter_user_a, '/shelter/'),
                           (self.hospital_user_a, '/hospital/'),
                           (self.adopter, '/adopter/')):
            self.login_as(user)
            self.assertEqual(self.client.get(page).status_code, 200,
                             '%s 打不开 %s —— 只读收口误伤了操作端'
                             % (user.username, page))

    def test_gov_gets_public_hall_not_a_redirect_loop(self):
        """公开领养大厅对政府账号仍然是公开页。

        第四十九轮 `adoption_hall_public` 里那句「gov 也直接进完整门户」
        如果留着，`adopter_portal` 现在会把政府账号 302 回 `/`，
        而 `/`（`dashboard_redirect`）又把 gov 送回 `/gov/` ——
        想看一眼大厅长什么样，结果被自己的门户页弹走。
        """
        for user in (self.gov_city, self.gov_a):
            self.login_as(user)
            resp = self.client.get('/adopter/hall/')
            self.assertEqual(resp.status_code, 200,
                             '%s 访问公开领养大厅得到 %s' % (user.role, resp.status_code))
            # `public_mode` 是视图 context 里的开关，不保证出现在渲染结果里
            # （大厅模板用它控制"是否显示登录入口"），断 context 才稳。
            self.assertIs(True, resp.context['public_mode'],
                          '政府账号看公开大厅却拿到了门户态 —— 说明那条 '
                          '「gov 直接进完整门户」的分支又被加回去了')


class FingerprintCanSeeARealWriteTest(BusinessTestBase):
    """反向对照：`fingerprint()` 必须真的抓得到一次写。

    判据写成「前后相等」也能全绿 —— 如果它其实什么都没算（比如所有模型都被
    排除表吃掉、或 `values()` 抛异常被 `except` 咽掉），那上面整套闸门就是
    空转。所以这里用一次**真实**的业务写来校准它。
    """

    def test_detector_notices_a_legitimate_capture_create(self):
        self.login_as(self.shelter_user_a)
        before = fingerprint()
        resp = self.post_json('/api/business/captures/create/', {
            'pet_count': 1, 'property_name': '阳光物业',
            'community_name': '幸福小区', 'address': '幸福路1号',
            'contact_person': '物业张三', 'contact_phone': '13800001234',
        })
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        changed = diff_keys(before, fingerprint())
        self.assertIn('business.Capture', changed,
                      '创建了一张捕捉单而指纹没变 —— 判据失效，'
                      '政府端只读的那几条断言全是假绿')
        self.assertIn('business.Pet', changed)

    def test_detector_notices_an_in_place_update(self):
        """原地 UPDATE（行数不变）也必须被抓到 —— 那才是只读收口最怕漏的形态。"""
        capture = make_capture(district=self.district_a, shelter=self.shelter_a,
                               pet_codes=['FP1'])
        before = fingerprint()
        self.assertEqual(before['business.Capture'][0],
                         fingerprint()['business.Capture'][0])
        Capture.objects.filter(id=capture.id).update(property_name='只是改了物业名')
        self.assertIn('business.Capture', diff_keys(before, fingerprint()),
                      '一次不改变行数的 UPDATE 没被指纹抓到 → 判据只看计数，'
                      '「撤回/办结/改状态」这类破坏会全部漏掉')
