"""操作日志（审计）回归测试。

覆盖三件容易悄悄退化的事：
1. 写操作必须留下记录，且**归属区县按业务记录算**，不按操作人算
   （现场捕捉点操作员挂在「全市（市级）」下，按操作人算会让本区县政府
   在自己的日志页里看不到本区的操作）；
2. 只读请求（GET / 只读 POST）不能污染日志；
3. 审计是旁路，写日志失败**不得**影响业务响应。
"""
import re
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from business.models import (
    Adoption, AdoptionApplication, AdoptionHallListing, CheckIn, Message,
)
from business.tests.base import (
    BusinessTestBase, make_capture, make_district, make_pet, make_user,
)
from core import audit
from core.models import AuditLog, District

GOV_PORTAL = 'templates/portal/gov/portal.html'


def _log_render_fields():
    """从政府端门户里取出日志渲染/搜索实际读取的响应字段名。

    日志渲染是模板里的两段 JS（`renderLogList` / `logSearchFields`），
    这里从源码提取 `log.<name>`，用来和接口真实返回的键做交叉校验。
    """
    source = Path(GOV_PORTAL).read_text(encoding='utf-8')
    fields = set()
    for method in ('renderLogList', 'logSearchFields'):
        m = re.search(rf'{method}\((?:\w+)\)\s*\{{(.*?)\n  \}}', source, re.S)
        if m is None:
            raise AssertionError(f'{GOV_PORTAL} 里找不到 {method}，测试本身可能已失效')
        fields |= set(re.findall(r'\blog\.([A-Za-z_$][\w$]*)', m.group(1)))
    return fields


class AuditWriteTest(BusinessTestBase):
    """写操作落审计。"""

    def _create_capture(self, user):
        return self.post_json('/api/business/captures/create/', {
            'shelter_id': self.shelter_a.id,
            'pet_count': 1,
            'property_name': '甲区物业',
            'community_name': '甲区小区',
            'contact_person': '张三',
            'contact_phone': '13800000000',
        })

    def test_capture_create_writes_audit_log(self):
        self.login_as(self.shelter_user_a)
        self.ok(self._create_capture(self.shelter_user_a))

        log = AuditLog.objects.order_by('-id').first()
        self.assertIsNotNone(log, '写操作没有留下审计记录')
        self.assertEqual(log.module, '捕捉登记')
        self.assertEqual(log.object_type, '捕捉单')
        self.assertEqual(log.action_flag, AuditLog.ACTION_ADD)
        self.assertEqual(log.district, self.district_a)
        self.assertEqual(log.district_name, self.district_a.name)
        self.assertEqual(log.user, self.shelter_user_a)
        self.assertEqual(log.user_name, 'shelter_a_t')
        self.assertTrue(log.success)
        self.assertIn('capture', log.path)

    def test_failed_write_is_recorded_as_failure(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json('/api/business/captures/create/', {
            'shelter_id': self.shelter_a.id,
            'pet_count': 1,
            # 缺 property_name → 服务端拒绝
            'community_name': '甲区小区',
            'contact_person': '张三',
            'contact_phone': '13800000000',
        }))

        log = AuditLog.objects.order_by('-id').first()
        self.assertIsNotNone(log, '失败的写操作也应留痕')
        self.assertFalse(log.success)
        self.assertIn('失败', log.summary)

    def test_get_request_is_not_logged(self):
        self.login_as(self.gov_city)
        before = AuditLog.objects.count()
        self.ok(self.get_json('/api/business/captures/'))
        self.assertEqual(AuditLog.objects.count(), before, 'GET 请求不应产生审计记录')

    def test_read_only_post_is_not_logged(self):
        """黑名单校验是 POST 但只读，不能污染日志。

        **必须用 `shelter` 身份**（第二十二轮）：该接口第二十二轮把角色集合
        收敛到 `('shelter', 'gov_city', 'gov_district')`，原先这里用的
        `hospital_user_a` 会拿到 403 —— 而 403 同样不写审计，
        **测试会绿得毫无意义**（空转）。所以下面同时断言 200，
        确保请求真的进了视图。
        """
        self.login_as(self.shelter_user_a)
        before = AuditLog.objects.count()
        resp = self.post_json('/api/business/blacklist/check/', {
            'id_card': '420602199001011234', 'phone': '13800000000'})
        self.assertEqual(resp.status_code, 200,
                         '请求没进视图（403？）—— 这条断言会因此空转')
        self.assertEqual(AuditLog.objects.count(), before,
                         '只读 POST 接口不应产生审计记录')

    def test_anonymous_request_is_not_logged(self):
        before = AuditLog.objects.count()
        self.client.post('/api/business/captures/create/', data='{}',
                         content_type='application/json')
        self.assertEqual(AuditLog.objects.count(), before)


class AuditDistrictAttributionTest(BusinessTestBase):
    """归属区县必须按业务记录算 —— 这是本功能最容易写错的一处。"""

    def test_city_level_operator_log_is_attributed_to_business_district(self):
        city_shelter_user = make_user(
            'city_shelter_t', role='shelter', district=self.city,
            institution=self.shelter_a)
        self.login_as(city_shelter_user)
        self.ok(self.post_json('/api/business/captures/create/', {
            'shelter_id': self.shelter_a.id,
            'pet_count': 1,
            'property_name': '甲区物业',
            'community_name': '甲区小区',
            'contact_person': '张三',
            'contact_phone': '13800000000',
        }))

        log = AuditLog.objects.order_by('-id').first()
        self.assertEqual(
            log.district, self.district_a,
            '操作员挂在「全市（市级）」下时，日志必须归属到业务记录所在区县，'
            '否则本区县政府在自己的日志页里看不到这条操作')


class AuditLogViewScopeTest(BusinessTestBase):
    """政府端日志接口的区县隔离与筛选。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        AuditLog.objects.create(
            user=cls.shelter_user_a, user_name='shelter_a_t', role='shelter',
            district=cls.district_a, district_name=cls.district_a.name,
            module='捕捉登记', action_flag=AuditLog.ACTION_ADD,
            object_repr='CAP-T0001', summary='捕捉登记·新增 CAP-T0001')
        AuditLog.objects.create(
            user=cls.shelter_user_b, user_name='shelter_b_t', role='shelter',
            district=cls.district_b, district_name=cls.district_b.name,
            module='转运下发', action_flag=AuditLog.ACTION_CHANGE,
            object_repr='TRF-T0002', summary='转运下发·签收 TRF-T0002')
        AuditLog.objects.create(
            user=cls.gov_city, user_name='gov_city_t', role='gov_city',
            module='系统配置', action_flag=AuditLog.ACTION_CHANGE,
            object_repr='', summary='系统配置·修改')

    def _logs(self, url='/api/supervision/logs/'):
        return self.ok(self.get_json(url))['data']

    def test_city_admin_sees_all(self):
        self.login_as(self.gov_city)
        self.assertEqual(len(self._logs()), 3)

    def test_district_admin_sees_only_own_district(self):
        self.login_as(self.gov_a)
        rows = self._logs()
        self.assertEqual([r['objectRepr'] for r in rows], ['CAP-T0001'],
                         '区县政府只应看到归属本区县的日志')
        self.assertTrue(all(r['districtId'] == self.district_a.id for r in rows))

    def test_unattributed_log_is_hidden_from_district_admin(self):
        """没有归属区县的日志不对区县政府展示，否则等于绕开区县隔离。"""
        self.login_as(self.gov_a)
        self.assertNotIn('系统配置', [r['module'] for r in self._logs()])

    def test_module_filter(self):
        self.login_as(self.gov_city)
        rows = self._logs('/api/supervision/logs/?module=转运下发')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['objectRepr'], 'TRF-T0002')

    def test_action_flag_filter(self):
        self.login_as(self.gov_city)
        rows = self._logs('/api/supervision/logs/?action_flag=1')
        self.assertEqual([r['objectRepr'] for r in rows], ['CAP-T0001'])

    def test_keyword_search_covers_summary_and_operator(self):
        self.login_as(self.gov_city)
        self.assertEqual(len(self._logs('/api/supervision/logs/?q=签收')), 1)
        self.assertEqual(len(self._logs('/api/supervision/logs/?q=shelter_b_t')), 1)

    def test_keyword_search_matches_account_name_not_only_display_name(self):
        """日志里存的是显示名快照，按**账号名**也必须搜得到。

        实际形态：`user_name` 是 `get_full_name()`（如「襄城捕捉点操作员」），
        审计人员按账号 `cy_shelter` 搜却什么都搜不到 —— 搜索框就在眼前，
        界面不会报错，只会静静地给出空结果。
        """
        log = AuditLog.objects.create(
            user=self.gov_city, user_name='市级政府管理员（显示名）', role='gov_city',
            district=self.district_a, district_name=self.district_a.name,
            module='用户管理', action_flag=AuditLog.ACTION_CHANGE,
            object_repr='账号 #12', summary='用户管理·启停')
        self.login_as(self.gov_city)
        rows = self._logs(f'/api/supervision/logs/?q={self.gov_city.username}')
        self.assertIn(log.id, [r['id'] for r in rows])

    def test_payload_has_camel_and_snake_keys(self):
        """手工聚合的接口必须自己补驼峰，否则前端读 `r.actionTime` 得 undefined。"""
        self.login_as(self.gov_city)
        row = self._logs()[0]
        for key in ('action_time', 'actionTime', 'action_flag', 'actionFlag',
                    'object_repr', 'objectRepr', 'user_name', 'userName',
                    'district_name', 'districtName', 'action_label', 'actionLabel'):
            self.assertIn(key, row)

    def test_adopter_cannot_read_logs(self):
        self.login_as(self.adopter)
        resp = self.get_json('/api/supervision/logs/')
        self.assertIn(resp.status_code, (302, 403, 404))


class AuditLogSearchFieldContractTest(BusinessTestBase):
    """前端日志搜索读的字段必须由接口真实返回。

    回归对象：前端曾按 `l.action` 过滤，而后端**从未返回过 `action`** ——
    搜索框提示写着「操作/详情/操作人」，但按「操作」搜永远是空结果且不报错。
    """

    def test_frontend_log_fields_exist_in_api_response(self):
        AuditLog.objects.create(
            user=self.gov_city, user_name='gov_city_t', role='gov_city',
            district=self.district_a, district_name=self.district_a.name,
            module='捕捉登记', action_flag=AuditLog.ACTION_ADD,
            object_repr='CAP-T0001', summary='捕捉登记·新增 CAP-T0001')
        self.login_as(self.gov_city)
        row = self.ok(self.get_json('/api/supervision/logs/'))['data'][0]

        missing = sorted(f for f in _log_render_fields() if f not in row)
        self.assertEqual(
            missing, [],
            '政府端日志渲染/搜索引用了接口未返回的字段（界面会静默失效）：'
            + ', '.join(missing))


class AuditResilienceTest(BusinessTestBase):
    """审计是旁路：写日志失败绝不能影响业务响应。"""

    def test_audit_failure_does_not_break_business_request(self):
        self.login_as(self.shelter_user_a)
        with mock.patch.object(AuditLog.objects, 'create',
                               side_effect=RuntimeError('audit boom')):
            resp = self.post_json('/api/business/captures/create/', {
                'shelter_id': self.shelter_a.id,
                'pet_count': 1,
                'property_name': '甲区物业',
                'community_name': '甲区小区',
                'contact_person': '张三',
                'contact_phone': '13800000000',
            })
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json().get('success'))


class AuditUnitTest(SimpleTestCase):
    """解析逻辑的单元测试。"""

    def test_sniff_district_id_walks_nested_structures(self):
        self.assertEqual(audit.sniff_district_id({'a': [{'district_id': 7}]}), 7)
        self.assertEqual(audit.sniff_district_id({'a': [{'districtId': 3}]}), 3)
        self.assertIsNone(audit.sniff_district_id({'district_id': 0}))
        self.assertIsNone(audit.sniff_district_id({'district_id': '7'}))
        self.assertIsNone(audit.sniff_district_id(None, {}, []))

    def test_sniff_object_repr_prefers_business_codes(self):
        self.assertEqual(audit.sniff_object_repr({'ledger_no': 'CAP-1'}), 'CAP-1')
        self.assertEqual(audit.sniff_object_repr([{'name': '甲区小区'}]), '甲区小区')
        self.assertEqual(audit.sniff_object_repr({'pet_code': ['TNR2501001']}),
                         'TNR2501001')
        self.assertEqual(audit.sniff_object_repr({}), '')

    def test_describe_derives_module_and_action_from_path(self):
        class _Req:
            path = '/api/business/transfers/12/receive/'
            method = 'POST'

        info = audit.describe(_Req())
        self.assertEqual(info['module'], '转运下发')
        self.assertEqual(info['object_type'], '转运单')
        self.assertEqual(info['action_flag'], AuditLog.ACTION_CHANGE)
        self.assertEqual(info['verb'], '签收')
        self.assertEqual(info['object_id'], '12')

    def test_describe_keeps_unknown_action_verb_visible(self):
        """未登记的动作词不能静默归成「修改」——保留原段名便于发现漏登记。"""
        class _Req:
            path = '/api/business/captures/9/frobnicate/'
            method = 'POST'

        info = audit.describe(_Req())
        self.assertEqual(info['action_flag'], AuditLog.ACTION_CHANGE)
        self.assertEqual(info['verb'], 'frobnicate')


class ClientIpTest(SimpleTestCase):
    """审计来源 IP 必须取「代理看到的对端」，不能取客户端可写的值。"""

    class _Req:
        def __init__(self, meta):
            self.META = meta

    def test_uses_remote_addr_by_default(self):
        req = self._Req({'REMOTE_ADDR': '203.0.113.9',
                         'HTTP_X_FORWARDED_FOR': '1.2.3.4'})
        self.assertEqual(audit.client_ip(req), '203.0.113.9')

    def test_uses_x_real_ip_behind_local_proxy(self):
        """nginx 用 `$remote_addr` **覆盖**写 `X-Real-IP`，客户端改不动它。"""
        req = self._Req({'REMOTE_ADDR': '127.0.0.1',
                         'HTTP_X_REAL_IP': '114.247.50.2',
                         'HTTP_X_FORWARDED_FOR': '1.2.3.4'})
        self.assertEqual(audit.client_ip(req), '114.247.50.2')

    def test_forged_forwarded_for_does_not_win(self):
        """⚠ 回归：伪造的 `X-Forwarded-For` **不得**进审计台账。

        nginx 的 `$proxy_add_x_forwarded_for` 把**客户端原值放在最前**、
        真实对端追加在最后；而 nginx 默认还会**透传**客户端发来的未知头。
        所以「取第一段」等于让任何人往台账里写假 IP（生产已实测），
        「取最后一段」也只是依赖另一处配置不出错 —— 都不够。
        **一律不看 XFF**，只认 nginx 覆盖写入的 `X-Real-IP`。
        """
        req = self._Req({'REMOTE_ADDR': '127.0.0.1',
                         'HTTP_X_FORWARDED_FOR': '203.0.113.7, 39.181.5.30'})
        self.assertEqual(audit.client_ip(req), '127.0.0.1')

    def test_invalid_ip_returns_none(self):
        req = self._Req({'REMOTE_ADDR': 'not-an-ip'})
        self.assertIsNone(audit.client_ip(req))


class AuditWriteCoverageTest(BusinessTestBase):
    """每个写接口都必须留下**带归属区县**的审计记录。

    这是本功能的核心保证：「日志页上没有这条」必须等价于「这个操作没发生」。
    最容易漏的形态不是「没写记录」，而是**写了但 district 为空** —— 市级管理员
    看得到、区县政府看不到，该区县的监管等于瞎的。
    """

    def setUp(self):
        AuditLog.objects.all().delete()

    def _last(self):
        return AuditLog.objects.order_by('-id').first()

    def _assert_attributed(self, district, module):
        log = self._last()
        self.assertIsNotNone(log, f'{module} 的写操作没有留下审计记录')
        self.assertEqual(log.module, module)
        self.assertEqual(
            log.district, district,
            f'{module} 的日志没有归属区县（或归属错了），区县政府将看不到这条操作')
        self.assertTrue(log.success)
        return log

    def test_capture_delete(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'/api/business/captures/{capture.id}/delete/'))
        log = self._assert_attributed(self.district_a, '捕捉登记')
        self.assertEqual(log.action_flag, AuditLog.ACTION_DELETE)

    def test_checkin_create(self):
        pet = make_pet(district=self.district_a, status='adopted')
        Adoption.objects.create(pet=pet, pet_code=pet.code, adopter=self.adopter,
                                district=self.district_a, status='completed')
        self.login_as(self.adopter)
        self.ok(self.post_json('/api/business/checkins/create/', {
            'pet_id': pet.id, 'month': '2026-01', 'note': '适应良好'}))
        self._assert_attributed(self.district_a, '回访打卡')

    def test_checkin_review(self):
        pet = make_pet(district=self.district_a)
        checkin = CheckIn.objects.create(pet=pet, pet_code=pet.code,
                                         adopter=self.adopter, month='2026-01')
        self.login_as(self.gov_a)
        self.ok(self.post_json(f'/api/business/checkins/{checkin.id}/review/',
                               {'status': 'approved'}))
        self._assert_attributed(self.district_a, '回访打卡')

    def test_adoption_apply(self):
        pet = make_pet(district=self.district_a, status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet, hospital=self.hospital_a)
        self.login_as(self.adopter)
        self.ok(self.post_json('/api/business/adoptions/apply/', {
            'pet_id': pet.id, 'applicant_name': '王领养',
            'applicant_phone': '13800000009'}))
        self._assert_attributed(self.district_a, '领养业务')

    def test_adoption_application_review(self):
        pet = make_pet(district=self.district_a, status='pending_adopt')
        application = AdoptionApplication.objects.create(
            pet=pet, pet_code=pet.code, applicant=self.adopter,
            applicant_name='王领养', applicant_phone='13800000009',
            hospital=self.hospital_a, status='pending')
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'reject', 'review_note': '资质不符'}))
        self._assert_attributed(self.district_a, '领养业务')

    def test_adoption_info_edit(self):
        pet = make_pet(district=self.district_a, status='pending_adopt',
                       hospital=self.hospital_a)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'/api/business/adoptions/{pet.id}/edit-info/',
                               {'intro': '性格亲人'}))
        self._assert_attributed(self.district_a, '领养业务')

    def test_institution_toggle(self):
        self.login_as(self.gov_a)
        self.ok(self.post_json(
            f'/api/supervision/institutions/{self.hospital_a.id}/toggle/'))
        self._assert_attributed(self.district_a, '机构管理')

    def test_district_create(self):
        self.login_as(self.gov_city)
        self.ok(self.post_json('/api/supervision/districts/create/',
                               {'name': '新城区', 'code': 'TAUDIT'}))
        created = District.objects.get(code='TAUDIT')
        self._assert_attributed(created, '区县管理')

    def test_district_delete_keeps_log_without_fk(self):
        """区县删除后日志必须还在。

        踩过的坑：把日志归属到「正在被删除的那个区县」，外键约束会在写入时
        报错，整条日志被吞掉 —— 删除区县这种最该留痕的操作反而没有记录。
        """
        temp = make_district(name='待删区', code='TDEL')
        self.login_as(self.gov_city)
        self.ok(self.post_json(f'/api/supervision/districts/{temp.id}/delete/'))
        log = self._last()
        self.assertIsNotNone(log, '区县删除的日志丢失了')
        self.assertIsNone(log.district)
        self.assertIn('待删区', log.summary)

    def test_user_create(self):
        self.login_as(self.gov_city)
        self.ok(self.post_json('/api/supervision/users/create/', {
            'username': 'audit_new_user', 'name': '新用户',
            'role': 'gov_district', 'district_id': self.district_a.id,
            'password': '123456'}))
        self._assert_attributed(self.district_a, '用户管理')

    def test_user_toggle(self):
        target = make_user('audit_toggle_t', role='hospital',
                           district=self.district_a, institution=self.hospital_a)
        self.login_as(self.gov_city)
        self.ok(self.post_json(f'/api/supervision/users/{target.id}/toggle/'))
        log = self._assert_attributed(self.district_a, '用户管理')
        self.assertEqual(log.object_repr, 'audit_toggle_t')

    def test_message_read_is_not_audited(self):
        """读回执不写审计：否则每次打开消息都产生一条，把业务操作挤出日志页。"""
        msg = Message.objects.create(user=self.adopter, type='system',
                                     title='通知', content='内容')
        self.login_as(self.adopter)
        self.ok(self.post_json(f'/api/business/portal/messages/{msg.id}/read/'))
        self.assertIsNone(self._last())


class AuditCityOperatorAttributionTest(BusinessTestBase):
    """现场真实配置下的归属区县：操作员挂在「全市（市级）」。

    这类用例才真正锁得住「按业务记录归属」：操作员区县是市级，
    **任何「按操作人归属」的实现都会解析出 None**，断言必然失败。
    用区县操作员写同样的用例是测不出来的 —— 实测把区县嗅探函数改成恒返回
    None，区县操作员的用例全绿，只有这里会红。
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.city_shelter = make_user('city_shelter_cov', role='shelter',
                                     district=cls.city, institution=cls.shelter_a)
        cls.city_hospital = make_user('city_hospital_cov', role='hospital',
                                      district=cls.city, institution=cls.hospital_a)

    def setUp(self):
        AuditLog.objects.all().delete()

    def _assert_district(self, expected):
        log = AuditLog.objects.order_by('-id').first()
        self.assertIsNotNone(log, '写操作没有留下审计记录')
        self.assertEqual(
            log.district, expected,
            f'{log.module} 的日志没有按业务记录归属区县'
            '（操作员挂在市级，按操作人归属的实现会解析出空）')

    def test_capture_create(self):
        self.login_as(self.city_shelter)
        self.ok(self.post_json('/api/business/captures/create/', {
            'shelter_id': self.shelter_a.id, 'pet_count': 1,
            'property_name': '甲区物业', 'community_name': '甲区小区',
            'contact_person': '张三', 'contact_phone': '13800000000'}))
        self._assert_district(self.district_a)

    def test_capture_delete(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a)
        self.login_as(self.city_shelter)
        self.ok(self.post_json(f'/api/business/captures/{capture.id}/delete/'))
        self._assert_district(self.district_a)

    def test_transfer_create(self):
        pets = [make_pet(district=self.district_a, shelter=self.shelter_a)
                for _ in range(2)]
        self.login_as(self.city_shelter)
        self.ok(self.post_json('/api/business/transfers/create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [p.code for p in pets]}))
        self._assert_district(self.district_a)

    def test_treatment_create(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        self.login_as(self.city_hospital)
        self.ok(self.post_json('/api/business/treatments/create/',
                               {'pet_id': pet.id, 'items': {}}))
        self._assert_district(self.district_a)

    def test_blacklist_create(self):
        self.login_as(self.city_shelter)
        self.ok(self.post_json('/api/business/blacklist/create/', {
            'name': '李某某', 'phone': '13900000001', 'reason': '弃养领养宠物'}))
        self._assert_district(self.district_a)


class AuditModelTest(BusinessTestBase):
    """模型层约束。"""

    def test_district_deletion_keeps_audit_trail(self):
        """区县被删掉时日志必须保留（SET_NULL），审计不能被级联清空。"""
        district = make_district(name='临时区', code='TTMP')
        AuditLog.objects.create(district=district, district_name='临时区',
                                module='机构管理', summary='机构管理·新增')
        district.delete()
        log = AuditLog.objects.order_by('-id').first()
        self.assertIsNotNone(log)
        self.assertIsNone(log.district)
        self.assertEqual(log.district_name, '临时区', '名称快照应保留')

    def test_default_ordering_is_newest_first(self):
        for i in range(3):
            AuditLog.objects.create(module='捕捉登记', summary=f'第{i}条')
        self.assertEqual(
            [log.summary for log in AuditLog.objects.all()],
            ['第2条', '第1条', '第0条'])
