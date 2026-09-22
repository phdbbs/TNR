"""supervision 应用测试：政府监管端接口与权限边界。"""
import json

from accounts.models import User
from business.models import (
    Adoption, Capture, Euthanasia, Material, MaterialTransaction, Pet,
    Release, Treatment,
)
from business.tests.base import (
    ApiMixin, make_district, make_institution, make_material, make_pet,
    make_user,
)
from core.models import AuditLog, District, Institution
from django.test import TestCase
from supervision.models import SystemConfig

API = '/api/supervision'


class SupervisionBase(ApiMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.city = make_district(name="全市", code="SCITY", is_city=True)
        cls.district_a = make_district(name="甲区", code="SA")
        cls.district_b = make_district(name="乙区", code="SB")
        cls.shelter_a = make_institution(type="shelter", district=cls.district_a)
        cls.hospital_a = make_institution(type="hospital", district=cls.district_a)
        cls.hospital_b = make_institution(type="hospital", district=cls.district_b)
        cls.gov_city = make_user("sv_gov_city", role="gov_city", district=cls.city)
        cls.gov_a = make_user("sv_gov_a", role="gov_district", district=cls.district_a)
        cls.gov_b = make_user("sv_gov_b", role="gov_district", district=cls.district_b)
        cls.shelter_user = make_user("sv_shelter", role="shelter",
                                     district=cls.district_a, institution=cls.shelter_a)
        cls.hospital_user = make_user("sv_hospital", role="hospital",
                                      district=cls.district_a, institution=cls.hospital_a)
        cls.adopter = make_user("sv_adopter", role="adopter")


class SupervisionPermissionTest(SupervisionBase):
    """政府端接口的角色访问控制。"""

    def test_anonymous_401(self):
        for url in (f'{API}/dashboard/', f'{API}/users/', f'{API}/config/'):
            self.expect_fail(self.client.get(url), status=401, message='请先登录')

    def test_business_roles_403(self):
        for user in (self.adopter, self.shelter_user, self.hospital_user):
            self.client.force_login(user)
            for url in (f'{API}/dashboard/', f'{API}/users/create/', f'{API}/business/'):
                self.expect_fail(self.client.get(url), status=403)

    def test_district_list_open_to_all_logged_in(self):
        """区县/机构列表是基础数据，所有登录用户可查。"""
        for user in (self.adopter, self.shelter_user, self.gov_city):
            self.client.force_login(user)
            self.assertEqual(self.client.get(f'{API}/districts/').status_code, 200)
            self.assertEqual(self.client.get(f'{API}/institutions/').status_code, 200)


class DashboardStatsTest(SupervisionBase):
    def test_keys_and_counts(self):
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a, status='in_treatment')
        Treatment.objects.create(pet=pet, pet_code=pet.code,
                                 hospital=self.hospital_a, district=self.district_a)
        self.client.force_login(self.gov_city)
        body = self.ok(self.client.get(f'{API}/dashboard/'))
        data = body['data']
        for key in ('capture_total', 'treatment_total', 'release_total',
                    'adoption_total', 'euthanasia_total', 'pet_total',
                    'institution_counts', 'material_stats', 'pet_status_distribution'):
            self.assertIn(key, data)
        self.assertEqual(data['pet_total'], 1)
        self.assertEqual(data['treatment_total'], 1)

    def test_district_scope_filtering(self):
        make_pet(district=self.district_a)
        make_pet(district=self.district_b)
        self.client.force_login(self.gov_b)
        data = self.ok(self.client.get(f'{API}/dashboard/'))['data']
        self.assertEqual(data['pet_total'], 1)
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(f'{API}/dashboard/'))['data']
        self.assertEqual(data['pet_total'], 2)


class InstitutionApiTest(SupervisionBase):
    def test_list_scoped_by_district(self):
        make_institution(type='hospital', district=self.district_a, name='甲区医院2')
        make_institution(type='hospital', district=self.district_b, name='乙区医院2')
        self.client.force_login(self.gov_a)
        data = self.ok(self.client.get(f'{API}/institutions/'))['data']
        self.assertEqual({i['district_id'] for i in data}, {self.district_a.id})

    def test_list_type_filter(self):
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(f'{API}/institutions/?type=hospital'))['data']
        self.assertTrue(data)
        self.assertTrue(all(i['type'] == 'hospital' for i in data))

    def test_create_success(self):
        self.client.force_login(self.gov_a)
        body = self.ok(self.post_json(f'{API}/institutions/create/', {
            'name': '新医院', 'type': 'hospital',
            'district_id': self.district_a.id,
            'address': '某路1号', 'contact': '联系人', 'phone': '010-88889999',
        }))
        self.assertEqual(body['data']['type'], 'hospital')

    def test_create_assigns_institution_code(self):
        """新建机构必须拿到 `I###` / `C###` 编号。

        机构编号是**稳定业务键**（去重、引用、种子脚本幂等匹配都靠它）；
        创建接口此前不写 `code`，界面上「机构编号」列永远是空的，也让界面
        建的机构与 seed 建的那批不在同一套编号体系里。
        """
        self.client.force_login(self.gov_a)
        hospital = self.ok(self.post_json(f'{API}/institutions/create/', {
            'name': '编号测试医院', 'type': 'hospital',
            'district_id': self.district_a.id,
        }))['data']
        self.assertRegex(hospital['code'], r'^I\d{3}$')

        community = self.ok(self.post_json(f'{API}/institutions/create/', {
            'name': '编号测试小区', 'type': 'community',
            'district_id': self.district_a.id,
        }))['data']
        self.assertRegex(community['code'], r'^C\d{3}$')
        self.assertNotEqual(hospital['code'], community['code'])

    def test_create_hospital_on_city_district_rejected(self):
        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(f'{API}/institutions/create/', {
            'name': '市医院', 'type': 'hospital', 'district_id': self.city.id,
        }), message='医院必须挂具体区县')

    def test_create_missing_name(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/institutions/create/',
                                        {'type': 'hospital', 'district_id': self.district_a.id}),
                         message='机构名称不能为空')

    def test_create_invalid_type(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/institutions/create/',
                                        {'name': 'X', 'type': 'factory',
                                         'district_id': self.district_a.id}),
                         message='机构类型无效')

    def test_create_bad_phone(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/institutions/create/', {
            'name': 'X', 'type': 'hospital',
            'district_id': self.district_a.id, 'phone': 'abc123',
        }), message='联系电话格式不正确')

    def test_edit_and_toggle(self):
        institution = make_institution(type='hospital', district=self.district_a)
        self.client.force_login(self.gov_a)
        self.ok(self.post_json(f'{API}/institutions/{institution.id}/edit/',
                               {'name': '改名医院'}))
        institution.refresh_from_db()
        self.assertEqual(institution.name, '改名医院')

        body = self.ok(self.post_json(f'{API}/institutions/{institution.id}/toggle/'))
        self.assertEqual(body['data']['status'], 'inactive')

    def test_edit_unknown_404(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/institutions/999999/edit/', {}),
                         status=404)

    def test_edit_other_district_404(self):
        """区级管理员不得编辑其他区县的机构（与 institution_list 范围一致）。"""
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/institutions/{self.hospital_b.id}/edit/',
                                        {'name': '越权改名'}),
                         status=404, message='无权访问')
        self.hospital_b.refresh_from_db()
        self.assertNotEqual(self.hospital_b.name, '越权改名')

    def test_toggle_other_district_404(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/institutions/{self.hospital_b.id}/toggle/'),
                         status=404, message='无权访问')
        self.hospital_b.refresh_from_db()
        self.assertEqual(self.hospital_b.status, 'active')

    def test_edit_cannot_move_institution_to_other_district(self):
        """区级管理员不得把机构调整到其他区县。"""
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(
            f'{API}/institutions/{self.hospital_a.id}/edit/',
            {'district_id': self.district_b.id}), message='无权将机构调整到其他区县')
        self.hospital_a.refresh_from_db()
        self.assertEqual(self.hospital_a.district_id, self.district_a.id)

    # ---------- 新建机构的区县范围（与编辑同一条判据） ----------
    #
    # 这一组是补漏：`institution_edit` 早就有「不得调整到他区」的校验，
    # 而 `institution_create` 没有 —— 区级管理员 POST 一个他区 `district_id`
    # 就能把机构种到别的区县。两处各写一份校验必然会漂移，故改为共用
    # `_district_out_of_scope()`，下面正反两组用例把这条判据钉死。

    def test_create_district_admin_cannot_create_in_other_district(self):
        self.client.force_login(self.gov_a)
        before = Institution.objects.filter(district=self.district_b).count()
        self.expect_fail(self.post_json(f'{API}/institutions/create/', {
            'name': '越权机构', 'type': 'shelter',
            'district_id': self.district_b.id,
        }), message='无权在其他区县创建机构')
        self.assertEqual(Institution.objects.filter(district=self.district_b).count(),
                         before, '被拒后不得落库')

    def test_create_district_admin_can_create_in_own_district(self):
        """正向对照：只加拦截不测放行，过紧的校验发现不了。"""
        self.client.force_login(self.gov_a)
        body = self.ok(self.post_json(f'{API}/institutions/create/', {
            'name': '本区新捕捉点', 'type': 'shelter',
            'district_id': self.district_a.id,
        }))
        inst = Institution.objects.get(id=body['data']['id'])
        self.assertEqual(inst.district_id, self.district_a.id)

    def test_create_district_admin_without_district_id_falls_back_to_own(self):
        """不传 district_id 时按操作员区县兜底，仍不得落到他区。"""
        self.client.force_login(self.gov_a)
        body = self.ok(self.post_json(f'{API}/institutions/create/', {
            'name': '兜底机构', 'type': 'shelter',
        }))
        inst = Institution.objects.get(id=body['data']['id'])
        self.assertEqual(inst.district_id, self.district_a.id)

    def test_create_city_admin_can_create_in_any_district(self):
        """市级管理员不受区县限制。"""
        self.client.force_login(self.gov_city)
        body = self.ok(self.post_json(f'{API}/institutions/create/', {
            'name': '市级建的乙区机构', 'type': 'shelter',
            'district_id': self.district_b.id,
        }))
        inst = Institution.objects.get(id=body['data']['id'])
        self.assertEqual(inst.district_id, self.district_b.id)
        # 市级仍然可以建挂市级的捕捉点（编辑侧同样允许，见 keeps_city_level 用例）
        body2 = self.ok(self.post_json(f'{API}/institutions/create/', {
            'name': '挂市级的捕捉点', 'type': 'shelter',
            'district_id': self.city.id,
        }))
        self.assertEqual(Institution.objects.get(id=body2['data']['id']).district_id,
                         self.city.id)

    def test_edit_moving_institution_cascades_operators(self):
        """市级管理员把机构换到别的区县时，挂在它下面的操作员必须一起搬。

        不搬的后果与「账号区县 ≠ 机构区县」同源：操作员会看到**旧**区县的档案，
        却以**新**区县的机构身份操作 —— 正是 `validate_operator_district`
        要拦的那种账号，而且政府端用户管理没有编辑入口，事后改不回来。
        """
        hospital = make_institution(type='hospital', district=self.district_a)
        op = make_user('cascade_api_op', role='hospital',
                       district=self.district_a, institution=hospital)

        self.client.force_login(self.gov_city)
        body = self.ok(self.post_json(f'{API}/institutions/{hospital.id}/edit/',
                                      {'district_id': self.district_b.id}))

        op.refresh_from_db()
        self.assertEqual(op.district_id, self.district_b.id)
        # 搬了几个人必须让操作者看见，否则这次编辑看起来「只改了机构」
        self.assertIn('1 个操作员', body['message'])

    def test_edit_keeps_city_level_shelter_operator(self):
        """挂「市级」的捕捉点操作员是现场约定，机构换区县时不能顺手把它搬走。"""
        shelter = make_institution(type='shelter', district=self.district_a)
        op = make_user('cascade_api_city', role='shelter',
                       district=self.city, institution=shelter)

        self.client.force_login(self.gov_city)
        body = self.ok(self.post_json(f'{API}/institutions/{shelter.id}/edit/',
                                      {'district_id': self.district_b.id}))

        op.refresh_from_db()
        self.assertEqual(op.district_id, self.city.id)
        self.assertEqual(body['message'], '机构更新成功')

    # ---------- 编辑侧的「医院不能挂市级」（补 create/edit 漂移） ----------
    #
    # `institution_create` 一直拒绝「医院挂市级」，编辑没有这条校验。实测提权链：
    # 把医院改挂市级 → `cascade_operator_district()` 把它的操作员一起搬过去 →
    # 该账号 `district.is_city` 变 True → `get_district_scope()` 返回 None →
    # **全市所有区县的数据都看得到**。界面上点不出来（下拉过滤了市级），
    # 只有直接打接口才会暴露。

    def test_edit_cannot_move_hospital_to_city_district(self):
        hospital = make_institution(type='hospital', district=self.district_a)
        op = make_user('esc_hosp_op', role='hospital',
                       district=self.district_a, institution=hospital)

        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(
            f'{API}/institutions/{hospital.id}/edit/', {'district_id': self.city.id}),
            message='医院必须挂具体区县，不能挂市级')

        hospital.refresh_from_db()
        op.refresh_from_db()
        self.assertEqual(hospital.district_id, self.district_a.id)
        # 关键：机构没改成，操作员也**不能**被连带搬走 ——
        # 否则「账号挂市级」这一步照样完成，提权链只断了一半。
        self.assertEqual(op.district_id, self.district_a.id)

    def test_edit_cannot_turn_institution_into_city_hospital_via_type_change(self):
        """组合改动也要拦：同时把 type 改成 hospital、区县改成市级。

        只看单个字段会漏掉这种组合 —— 两个字段各自「看起来没问题」。
        """
        shelter = make_institution(type='shelter', district=self.district_a)
        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(f'{API}/institutions/{shelter.id}/edit/', {
            'type': 'hospital', 'district_id': self.city.id,
        }), message='医院必须挂具体区县，不能挂市级')
        shelter.refresh_from_db()
        self.assertEqual(shelter.type, 'shelter')

    def test_edit_still_allows_city_level_shelter(self):
        """正向对照：捕捉点挂市级是现场约定，必须仍然放行。"""
        shelter = make_institution(type='shelter', district=self.district_a)
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(f'{API}/institutions/{shelter.id}/edit/',
                               {'district_id': self.city.id}))
        shelter.refresh_from_db()
        self.assertEqual(shelter.district_id, self.city.id)

    def test_edit_rejects_bad_phone_like_create_does(self):
        """电话格式：create 有校验、edit 原来没有（前端拦着，接口没拦）。"""
        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(
            f'{API}/institutions/{self.hospital_a.id}/edit/', {'phone': 'abc123'}),
            message='联系电话格式不正确')
        self.hospital_a.refresh_from_db()
        self.assertNotEqual(self.hospital_a.phone, 'abc123')


class DistrictApiTest(SupervisionBase):
    def test_create_only_gov_city(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/districts/create/',
                                        {'name': 'X区', 'code': 'XZ1'}), status=403)
        self.client.force_login(self.gov_city)
        body = self.ok(self.post_json(f'{API}/districts/create/',
                                      {'name': '新城区', 'code': 'XC1'}))
        self.assertTrue(District.objects.filter(code='XC1').exists())

    def test_create_duplicate_code(self):
        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(f'{API}/districts/create/',
                                        {'name': '重复区', 'code': 'SA'}),
                         message='区县代码已存在')

    def test_delete_unreferenced(self):
        self.client.force_login(self.gov_city)
        district = make_district()
        body = self.ok(self.post_json(f'{API}/districts/{district.id}/delete/'))
        self.assertEqual(body['data']['id'], district.id)
        self.assertFalse(District.objects.filter(id=district.id).exists())

    def test_delete_referenced_blocked(self):
        make_pet(district=self.district_a)
        self.client.force_login(self.gov_city)
        body = self.expect_fail(self.post_json(f'{API}/districts/{self.district_a.id}/delete/'),
                                message='不可删除')
        self.assertIn('宠物档案', body['message'])
        self.assertTrue(District.objects.filter(id=self.district_a.id).exists())

    def test_edit(self):
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(f'{API}/districts/{self.district_a.id}/edit/',
                               {'name': '甲区改名'}))
        self.district_a.refresh_from_db()
        self.assertEqual(self.district_a.name, '甲区改名')


class UserApiTest(SupervisionBase):
    def test_list_scoped(self):
        make_user(role='shelter', district=self.district_b)
        self.client.force_login(self.gov_a)
        data = self.ok(self.client.get(f'{API}/users/'))['data']
        self.assertEqual({u['district_id'] for u in data}, {self.district_a.id})

    def test_list_role_filter(self):
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(f'{API}/users/?role=hospital'))['data']
        self.assertTrue(data)
        self.assertTrue(all(u['role'] == 'hospital' for u in data))

    def _create_payload(self, **kw):
        payload = {'username': 'new_user_1', 'name': '新用户',
                   'role': 'shelter', 'district_id': self.district_a.id,
                   'institution_id': self.shelter_a.id}
        payload.update(kw)
        return payload

    def test_create_shelter_operator(self):
        self.client.force_login(self.gov_a)
        body = self.ok(self.post_json(f'{API}/users/create/', self._create_payload()))
        user = User.objects.get(id=body['data']['id'])
        self.assertEqual(user.role, 'shelter')
        self.assertEqual(user.institution, self.shelter_a)
        self.assertTrue(user.check_password('123456'), '默认密码 123456')
        self.assertEqual(user.status, 'active')

    def test_create_duplicate_username(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(username='sv_gov_a')),
                         message='用户名已存在')

    def test_create_adopter_rejected(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(role='adopter')),
                         message='领养人不在政府端创建')

    def test_create_invalid_role(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(role='boss')),
                         message='角色无效')

    def test_create_short_password(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(password='123')),
                         message='密码长度不能少于6位')

    def test_create_gov_city_requires_city_district(self):
        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(role='gov_city',
                                                             institution_id=None)),
                         message='必须为市级')
        self.ok(self.post_json(f'{API}/users/create/',
                               self._create_payload(role='gov_city',
                                                    district_id=self.city.id,
                                                    institution_id=None)))

    def test_create_gov_district_rejects_city_district(self):
        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(role='gov_district',
                                                             district_id=self.city.id,
                                                             institution_id=None)),
                         message='不能选市级')

    def test_create_hospital_requires_hospital_institution(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(role='hospital',
                                                             institution_id=None)),
                         message='医院操作员必须关联一个医院机构')
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(role='hospital',
                                                             institution_id=self.shelter_a.id)),
                         message='所选机构不是医院类型')
        self.ok(self.post_json(f'{API}/users/create/',
                               self._create_payload(role='hospital',
                                                    institution_id=self.hospital_a.id)))

    def test_create_shelter_requires_shelter_institution(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(institution_id=None)),
                         message='捕捉点操作员必须关联一个捕捉点机构')
        self.expect_fail(self.post_json(f'{API}/users/create/',
                                        self._create_payload(institution_id=self.hospital_a.id)),
                         message='所选机构不是捕捉点类型')

    def test_create_hospital_operator_district_must_match_institution(self):
        """账号区县决定**读取**范围，机构决定它代表谁，两者不一致就拦下。

        实测库里存在过一个这样的账号（区县=南漳县、机构=东津新区的宠安宠物诊所）：
        它会看到南漳县的档案，却以一家东津新区的医院身份写数据。
        """
        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(f'{API}/users/create/', self._create_payload(
            username='mismatch_hosp', role='hospital',
            district_id=self.district_b.id, institution_id=self.hospital_a.id)),
            message='不一致')
        self.ok(self.post_json(f'{API}/users/create/', self._create_payload(
            username='match_hosp', role='hospital',
            district_id=self.district_a.id, institution_id=self.hospital_a.id)))

    def test_create_shelter_operator_district_rule(self):
        """捕捉点操作员：挂「市级」放行（现场约定），挂具体区县则必须与机构一致。"""
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(f'{API}/users/create/', self._create_payload(
            username='shelter_city_ok', district_id=self.city.id,
            institution_id=self.shelter_a.id)))
        self.expect_fail(self.post_json(f'{API}/users/create/', self._create_payload(
            username='shelter_bad', district_id=self.district_b.id,
            institution_id=self.shelter_a.id)), message='不一致')

    def test_district_admin_cannot_create_government_roles(self):
        """区级管理员不得建市级/区级管理员账号（服务端必须自己拦）。

        前端 `canCreateRole()` 只是把角色下拉的选项过滤掉 —— 接口不做同一套校验
        就等于没校验：直接 POST `role=gov_city` + 市级 `district_id` 会建出一个
        **市级管理员**账号（口令还是攻击者自己填的），登录后
        `get_district_scope()` 返回 `None`，**全部区县的数据都看得到**。
        界面上点不出这个选项，所以怎么点都发现不了 —— 只有打接口才会暴露。
        """
        self.client.force_login(self.gov_a)
        for role, district_id in (('gov_city', self.city.id),
                                  ('gov_district', self.district_a.id)):
            with self.subTest(role=role):
                self.expect_fail(self.post_json(f'{API}/users/create/',
                                                self._create_payload(
                                                    username=f'esc_{role}',
                                                    role=role,
                                                    district_id=district_id,
                                                    institution_id=None)),
                                 message='无权创建或修改该角色的账号')
        self.assertFalse(User.objects.filter(username__startswith='esc_').exists())

    def test_district_admin_cannot_create_user_in_other_district(self):
        """区级管理员只能管本区县 —— 否则等于给自己开一个跨区县的后门账号。"""
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/create/', self._create_payload(
            username='cross_hosp', role='hospital',
            district_id=self.district_b.id, institution_id=self.hospital_b.id)),
            message='无权管理其他区县的账号')
        self.assertFalse(User.objects.filter(username='cross_hosp').exists())

    def test_district_admin_can_create_own_district_operators(self):
        """正向对照：只加拦截不测放行，过紧的校验发现不了。

        捕捉点操作员按现场约定挂「市级」，判据必须看**机构**而不是账号区县 ——
        判错就会把区级管理员唯一能建的那类账号全挡掉。
        """
        self.client.force_login(self.gov_a)
        self.ok(self.post_json(f'{API}/users/create/', self._create_payload(
            username='own_hosp', role='hospital',
            district_id=self.district_a.id, institution_id=self.hospital_a.id)))
        self.ok(self.post_json(f'{API}/users/create/', self._create_payload(
            username='own_shelter', role='shelter',
            district_id=self.city.id, institution_id=self.shelter_a.id)))

    def test_city_admin_can_still_create_government_roles(self):
        """正向对照：市级管理员不受这条限制（否则整个系统建不出第二个管理员）。"""
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(f'{API}/users/create/', self._create_payload(
            username='city_new_dist', role='gov_district',
            district_id=self.district_a.id, institution_id=None)))
        self.ok(self.post_json(f'{API}/users/create/', self._create_payload(
            username='city_new_city', role='gov_city',
            district_id=self.city.id, institution_id=None)))

    def test_toggle_self_rejected(self):
        """管理员不能停用自己（防止自锁）"""
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/{self.gov_a.id}/toggle/'),
                         message='不能停用当前登录账号自己')
        self.gov_a.refresh_from_db()
        self.assertTrue(self.gov_a.is_active)

    def test_toggle_superuser_rejected(self):
        target = make_user(role='shelter', district=self.district_a, is_superuser=True)
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/{target.id}/toggle/'),
                         message='超级管理员账号不可停用')

    def test_district_admin_cannot_toggle_city_admin(self):
        """区级管理员不得操作市级管理员账号（越权提权）。

        与 user_list 的区县过滤保持一致：列表里看不到的账号，写接口也不能操作。
        """
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/{self.gov_city.id}/toggle/'),
                         status=404, message='无权访问')
        self.gov_city.refresh_from_db()
        self.assertTrue(self.gov_city.is_active)

    def test_district_admin_cannot_toggle_other_district_user(self):
        """区级管理员不得操作其他区县的账号。"""
        target = make_user(role='shelter', district=self.district_b)
        self.client.force_login(self.gov_a)
        self.expect_fail(self.post_json(f'{API}/users/{target.id}/toggle/'),
                         status=404, message='无权访问')
        target.refresh_from_db()
        self.assertTrue(target.is_active)

    def test_district_admin_can_toggle_own_district_user(self):
        target = make_user(role='shelter', district=self.district_a)
        self.client.force_login(self.gov_a)
        self.ok(self.post_json(f'{API}/users/{target.id}/toggle/'))
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_city_admin_can_toggle_other_city_admin(self):
        """市级管理员之间仍可互相停用（兜底逻辑不过度拦截）。

        说明：「至少保留一个启用中的市级管理员」兜底在收紧区县范围后，
        已无法从区级管理员路径触达（他们看不到也操作不了市级账号），
        现仅作为市级管理员互相操作时的最后一道防线保留。
        """
        target = make_user(role='gov_city', district=self.city)
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(f'{API}/users/{target.id}/toggle/'))
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_district_rename_propagates(self):
        """区县改名后所有引用处联动展示新名称（外键实时读取，无名称快照）"""
        new_name = '联动新区'
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(f'{API}/districts/{self.district_a.id}/edit/',
                               {'name': new_name}))
        # 用户列表中的区县名
        data = self.ok(self.client.get(f'{API}/users/?role=gov_district'))['data']
        target = next(u for u in data if u['id'] == self.gov_a.id)
        self.assertEqual(target['district_name'], new_name)
        # 机构列表中的区县名
        insts = self.ok(self.client.get(f'{API}/institutions/'))['data']
        inst = next(i for i in insts if i['id'] == self.shelter_a.id)
        self.assertEqual(inst['district_name'], new_name)
        # 区县列表本身
        dists = self.ok(self.client.get(f'{API}/districts/'))['data']
        self.assertEqual(next(d['name'] for d in dists if d['id'] == self.district_a.id),
                         new_name)

    def test_toggle_status(self):
        target = make_user(role='shelter', district=self.district_a)
        self.client.force_login(self.gov_a)
        body = self.ok(self.post_json(f'{API}/users/{target.id}/toggle/'))
        self.assertEqual(body['data']['is_active'], False)
        target.refresh_from_db()
        self.assertEqual(target.status, 'inactive')
        body = self.ok(self.post_json(f'{API}/users/{target.id}/toggle/'))
        self.assertEqual(body['data']['is_active'], True)


class SupervisionDataTest(SupervisionBase):
    def test_business_supervision_all_types(self):
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a, status='in_treatment')
        Capture.objects.create(district=self.district_a, shelter=self.shelter_a,
                               pet_codes=pet.code, pet_count=1)
        Treatment.objects.create(pet=pet, pet_code=pet.code,
                                 hospital=self.hospital_a, district=self.district_a)
        self.client.force_login(self.gov_city)
        body = self.ok(self.client.get(f'{API}/business/'))
        data = body['data']
        self.assertTrue(data['captures'])
        self.assertTrue(data['treatments'])

    def test_business_supervision_type_filter(self):
        self.client.force_login(self.gov_city)
        body = self.ok(self.client.get(f'{API}/business/?business_type=capture'))
        self.assertNotIn('treatments', body['data'])

    def test_material_supervision_low_stock_alert(self):
        material = make_material(district=self.district_a, shelter_stock=2, safety_stock=10)
        self.client.force_login(self.gov_city)
        body = self.ok(self.client.get(f'{API}/materials/'))
        alerts = body['data']['alerts']
        self.assertTrue(any(a.get('id') == material.id for a in alerts),
                        '低于安全库存应产生预警')
        alert = next(a for a in alerts if a['id'] == material.id)
        self.assertEqual(alert['shortage'], 8)

    def test_ledger_center(self):
        pet = make_pet(district=self.district_a)
        Capture.objects.create(district=self.district_a, shelter=self.shelter_a,
                               pet_codes=pet.code, pet_count=1, ledger_no='CAP-SV-1')
        self.client.force_login(self.gov_city)
        body = self.ok(self.client.get(f'{API}/ledger/'))
        self.assertGreaterEqual(body['data']['total'], 1)
        records = body['data']['records']
        self.assertEqual(records[0]['ledger_no'], 'CAP-SV-1')

    def test_ledger_pet_archive(self):
        """一宠一档：按宠物聚合档案字段（分类/芯片/绝育/驱虫/免疫/出库）"""
        from accounts.models import User as UserModel
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a, status='adopted',
                       species='狗', breed='中华田园犬', gender='公',
                       age='约2岁', chip_no='CHIP-ARC-0001')
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            items_sterilization=True, items_vaccine=True, items_deworming=True,
            sterilization_surgeon='测试医师', sterilization_surgery_date=__import__('datetime').date.today(),
            vaccine_type='狂犬疫苗', deworming_type='体内外驱虫',
            status='completed', district=self.district_a)
        adopter = UserModel.objects.create_user(username='arc_adopter', password='x',
                                                role='adopter')
        Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter=adopter,
            adopter_name='档案领养人', adopter_phone='13800001234',
            hospital=self.hospital_a, status='completed',
            adopted_at=__import__('datetime').date.today(),
            ledger_no='ADP-ARC-001', district=self.district_a)
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(f'{API}/ledger/?business_type=pet'))['data']
        self.assertGreaterEqual(data['total'], 1)
        rec = next(r for r in data['records'] if r['ledger_no'] == pet.code)
        self.assertEqual(rec['species'], '狗')
        self.assertEqual(rec['breed'], '中华田园犬')
        self.assertEqual(rec['gender'], '公')
        self.assertEqual(rec['chip_no'], 'CHIP-ARC-0001')
        self.assertEqual(rec['status_display'], '已领养')
        self.assertTrue(rec['sterilized'], '绝育情况应为是')
        self.assertEqual(rec['outbound_reason'], '领养')
        self.assertIn('档案领养人', rec['delivery_unit'])
        self.assertTrue(any(v['drug'] == '狂犬疫苗' for v in rec['vaccine_records']))
        self.assertTrue(any(v['drug'] == '体内外驱虫' for v in rec['deworm_records']))
        # 诊疗记录须包含医院与医师
        self.assertEqual(len(rec['treatment_records']), 1)
        trt = rec['treatment_records'][0]
        self.assertEqual(trt['hospital'], self.hospital_a.name)
        self.assertEqual(trt['doctor'], '测试医师')
        self.assertIn('绝育', trt['items'])
        self.assertEqual(trt['status'], '已完成')

    def test_ledger_date_filter_includes_end_date(self):
        """回归：结束日期应包含当天全部记录（此前 __lte 当天零点排除当天记录）"""
        from datetime import date
        pet = make_pet(district=self.district_a)
        Capture.objects.create(district=self.district_a, shelter=self.shelter_a,
                               pet_codes=pet.code, pet_count=1,
                               ledger_no='CAP-DATEFIX-001')
        today = date.today().isoformat()
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(
            f'{API}/ledger/?start_date={today}&end_date={today}'))['data']
        self.assertGreaterEqual(data['total'], 1, '结束日期当天的记录不应被排除')
        self.assertTrue(any(r['ledger_no'] == 'CAP-DATEFIX-001' for r in data['records']),
                        '结束日期当天的捕捉记录应在结果中')

    def test_ledger_center_business_type_filter(self):
        pet = make_pet(district=self.district_a)
        Capture.objects.create(district=self.district_a, shelter=self.shelter_a,
                               pet_codes=pet.code, pet_count=1)
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(f'{API}/ledger/?business_type=capture'))['data']
        self.assertTrue(all(r['business_type'] == 'capture' for r in data['records']))

    def test_operation_logs(self):
        """操作日志读的是 core.AuditLog。

        这里曾经断言 admin `LogEntry`：业务接口**从不经过 admin**，
        `LogEntry` 永远是 0 行，测试用 `log_action()` 手工塞一条才能过 ——
        等于把「日志页永远为空」这个缺陷固化成了守卫。
        """
        AuditLog.objects.create(
            user=self.gov_city, user_name='sv_gov_city', role='gov_city',
            district=self.district_a, district_name=self.district_a.name,
            module='机构管理', action_flag=AuditLog.ACTION_ADD,
            object_repr='I999', summary='机构管理·新增 I999')
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(f'{API}/logs/'))['data']
        self.assertTrue(any(log['summary'] == '机构管理·新增 I999' for log in data))

    def test_operation_logs_records_real_business_write(self):
        """端到端：捕捉点写业务 → 政府端日志里能看到，且归属区县按业务记录算。"""
        self.client.force_login(self.shelter_user)
        self.ok(self.client.post(
            '/api/business/captures/create/',
            data=json.dumps({
                'shelter_id': self.shelter_a.id,
                'pet_count': 1,
                'property_name': '甲区物业',
                'community_name': '甲区小区',
                'contact_person': '张三',
                'contact_phone': '13800000000',
            }), content_type='application/json'))

        self.client.force_login(self.gov_a)
        data = self.ok(self.client.get(f'{API}/logs/'))['data']
        row = next((r for r in data if r['module'] == '捕捉登记'), None)
        self.assertIsNotNone(row, '本区县政府的日志页里看不到本区捕捉点刚登记的操作')
        self.assertEqual(row['districtId'], self.district_a.id)
        self.assertEqual(row['actionLabel'], '新增')
        self.assertEqual(row['userName'], 'sv_shelter')

    def test_operation_logs_hidden_from_other_district(self):
        self.client.force_login(self.shelter_user)
        self.ok(self.client.post(
            '/api/business/captures/create/',
            data=json.dumps({
                'shelter_id': self.shelter_a.id,
                'pet_count': 1,
                'property_name': '甲区物业',
                'community_name': '甲区小区',
                'contact_person': '张三',
                'contact_phone': '13800000000',
            }), content_type='application/json'))
        self.client.force_login(self.gov_b)
        data = self.ok(self.client.get(f'{API}/logs/'))['data']
        self.assertFalse([r for r in data if r['module'] == '捕捉登记'],
                         '乙区政府不应看到甲区的操作日志')

    def test_system_config_get_defaults(self):
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(f'{API}/config/'))['data']
        self.assertEqual(data['pet_code_prefix'], 'TNR')
        self.assertEqual(data['capture_prefix'], 'CAP')

    def test_system_config_post_updates(self):
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(f'{API}/config/', {'capture_prefix': 'ZB'}))
        self.assertEqual(SystemConfig.objects.get(key='capture_prefix').value, 'ZB')
        data = self.ok(self.client.get(f'{API}/config/'))['data']
        self.assertEqual(data['capture_prefix'], 'ZB')

    def test_system_config_gov_district_can_read(self):
        """区级管理员必须能读到**真实**前缀，而不是空串。

        第十九轮：此前 GET 也限死 `gov_city`，区级拿 403 后
        `TNR_API._get()` 静默返回 `[]`，政府端「系统配置」页把
        CAP/TRF/… 渲染成**空白输入框** —— 看上去像「编号规则没配置」。
        这与「兜底谎报业务状态」同族：无权限时不能拿空值冒充真实值。
        """
        self.client.force_login(self.gov_a)
        data = self.ok(self.client.get(f'{API}/config/'))['data']
        self.assertEqual(data['pet_code_prefix'], 'TNR')
        self.assertEqual(data['capture_prefix'], 'CAP')
        self.assertEqual(data['transfer_prefix'], 'TRF')

    def test_system_config_gov_district_cannot_write(self):
        """区级只读：POST 必须 403，且**库里的值不能被改**。"""
        SystemConfig.objects.update_or_create(
            key='capture_prefix', defaults={'value': 'CAP'})
        self.client.force_login(self.gov_a)
        self.expect_fail(
            self.post_json(f'{API}/config/', {'capture_prefix': 'ZZ'}), status=403)
        self.assertEqual(SystemConfig.objects.get(key='capture_prefix').value, 'CAP')

    def test_system_config_other_roles_403(self):
        """捕捉点 / 医院 / 领养人：读写一律 403。"""
        for user in (self.shelter_user, self.hospital_user, self.adopter):
            self.client.force_login(user)
            self.expect_fail(self.client.get(f'{API}/config/'), status=403)
            self.expect_fail(self.post_json(f'{API}/config/', {'x': 'y'}), status=403)


class SystemConfigPageContractTest(SupervisionBase):
    """源码级契约：编号规则的读/写权限必须**两端同时**收口（第十九轮）。

    这一类的典型形态是「同一条规则只写了一半」——后端 GET 限市级、
    前端却按「可读」渲染，界面上就是一片空白前缀。所以断言必须两侧都查，
    并且**反向断言**「静默封装不得再被用于此处」：`TNR_API._get()` 在非 2xx
    时返回 `[]`，用它读配置就等于把 403 渲染成「没有配置」。
    """

    def _read(self, rel):
        import pathlib
        from django.conf import settings
        return (pathlib.Path(settings.BASE_DIR) / rel).read_text(encoding='utf-8')

    def test_backend_splits_read_and_write(self):
        import inspect
        import re
        from supervision import views
        src = inspect.getsource(views.system_config)
        self.assertRegex(
            src, r"@role_required\('gov_city',\s*'gov_district'\)",
            'GET 必须对区级管理员开放')
        self.assertRegex(
            src, r"if request\.user\.role != 'gov_city':\s*\n\s*return json_fail\([^)]*status=403\)",
            'POST 必须保留市级守卫')
        self.assertNotRegex(
            src, r"@role_required\('gov_city'\)\s*\n@login_required\ndef system_config",
            '整函数限市级会让区级读到空值')

    def test_frontend_gates_write_and_surfaces_read_failure(self):
        src = self._read('templates/portal/gov/portal.html')
        self.assertIn("const canEdit = this.isCityLevel();", src)
        # 输入框只读门禁
        self.assertIn("canEdit ? '' : ' readonly'", src)
        # 保存按钮受同一门禁
        self.assertIn('${canEdit ? `<div class="card-header"', src)
        # 读失败必须显式报错，不得静默渲染空值
        self.assertIn("await TNR_API.get('/api/supervision/config/')", src)
        self.assertIn('加载配置失败', src)   # 失败要显式渲染，不能只是吞掉
        # 反向断言：整个政府端都不得再用静默封装读配置。
        # 注意不能写成 `assertNotIn('config = await TNR_API.getSystemConfig();')`
        # —— 改写成 `const json = await ...` 就绕过去了，是空转断言。
        self.assertNotIn(
            'TNR_API.getSystemConfig()', src,
            '静默封装会把 403 渲染成「编号规则没配置」')

    def test_silent_config_wrapper_stays_removed(self):
        """`getSystemConfig` 不得被加回来。

        它就是本轮的成因：经 `_get()` 在非 2xx 时静默返回 `[]`。
        删掉之后「读配置」只剩 `TNR_API.get(...)` 一条路，失败必抛错。
        """
        src = self._read('static/js/tnr-api.js')
        self.assertNotIn('async getSystemConfig', src)
        # 但 `_get` 本身的危害必须在源码里写明，否则下一个调用方还会踩
        self.assertIn('静默封装', src)


class DistrictIsCityGuardTest(SupervisionBase):
    """区县 `is_city` 翻转的权限后果（第十七轮）。

    `get_district_scope()` / `get_district_filtered_queryset()` 对
    「所属区县 is_city=True」的用户返回**全部数据**。因此把**已有账号挂靠**的
    区县改成市级，等于给该区县下所有账号发放全市读权限 —— 而且不需要碰账号本身。
    前端区县表单有这个下拉（`#dist-is-city`），后端原本零守卫。
    """

    def test_flip_to_city_with_accounts_rejected(self):
        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(
            f'{API}/districts/{self.district_a.id}/edit/', {'is_city': True}))
        self.district_a.refresh_from_db()
        self.assertFalse(self.district_a.is_city)

    def test_flip_to_city_does_not_widen_district_scope(self):
        """主断言：翻完之后区级账号仍只看得到本区数据。"""
        make_pet(district=self.district_a, shelter=self.shelter_a)
        make_pet(district=self.district_b, shelter=self.hospital_b)
        self.client.force_login(self.gov_city)
        self.post_json(f'{API}/districts/{self.district_a.id}/edit/', {'is_city': True})
        self.login_as(self.gov_a)
        data = self.ok(self.client.get(f'{API}/dashboard/'))['data']
        self.assertEqual(data['pet_total'], 1)

    def test_demote_city_district_with_accounts_rejected(self):
        """反向：把市级区县降级会让市级账号失去全部可见性。"""
        self.client.force_login(self.gov_city)
        self.expect_fail(self.post_json(
            f'{API}/districts/{self.city.id}/edit/', {'is_city': False}))
        self.city.refresh_from_db()
        self.assertTrue(self.city.is_city)

    def test_flip_empty_district_allowed(self):
        """正向对照：没有账号/数据挂靠的区县仍可改为市级。"""
        empty = make_district(name='空区', code='SEMPTY')
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(f'{API}/districts/{empty.id}/edit/', {'is_city': True}))
        empty.refresh_from_db()
        self.assertTrue(empty.is_city)

    def test_plain_edit_without_is_city_unaffected(self):
        """正向对照：不动 is_city 的普通改名不受影响。"""
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(
            f'{API}/districts/{self.district_a.id}/edit/', {'name': '甲区改名'}))
        self.district_a.refresh_from_db()
        self.assertEqual(self.district_a.name, '甲区改名')

    def test_same_value_is_city_is_noop(self):
        """正向对照：提交与当前值相同的 is_city 不算「改动」。"""
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(
            f'{API}/districts/{self.district_a.id}/edit/', {'is_city': False}))


class InactiveDistrictAssignmentTest(SupervisionBase):
    """停用区县不得再作为归属（第十七轮）。

    前端机构表单的区县下拉写的是 `d.status === 'active' && !d.is_city`，
    后端上一轮补了 `!is_city`、**漏了 `status`** —— 而同一个模块的
    `user_create` 两条都校验了（`所选区县已停用`）。同一模块内两条创建路径
    各写一份，必然漂移。
    """

    def setUp(self):
        self.district_b.status = 'inactive'
        self.district_b.save(update_fields=['status'])
        self.client.force_login(self.gov_city)

    def test_create_institution_in_inactive_district_rejected(self):
        self.expect_fail(self.post_json(f'{API}/institutions/create/', {
            'name': '停用区医院', 'type': 'hospital',
            'district_id': self.district_b.id}))
        self.assertFalse(Institution.objects.filter(name='停用区医院').exists())

    def test_edit_institution_into_inactive_district_rejected(self):
        inst = make_institution(type='hospital', district=self.district_a,
                                name='待迁医院')
        self.expect_fail(self.post_json(
            f'{API}/institutions/{inst.id}/edit/', {'district_id': self.district_b.id}))
        inst.refresh_from_db()
        self.assertEqual(inst.district_id, self.district_a.id)

    def test_create_institution_in_active_district_allowed(self):
        """正向对照。"""
        self.ok(self.post_json(f'{API}/institutions/create/', {
            'name': '正常医院', 'type': 'hospital',
            'district_id': self.district_a.id}))

    def test_edit_institution_without_district_change_allowed(self):
        """正向对照：原地改名不受影响（区县没换，即使本区停用也允许改名）。"""
        inst = make_institution(type='hospital', district=self.district_a,
                                name='原名医院')
        self.ok(self.post_json(
            f'{API}/institutions/{inst.id}/edit/', {'name': '新名医院'}))
        inst.refresh_from_db()
        self.assertEqual(inst.name, '新名医院')

    def test_user_create_in_inactive_district_already_rejected(self):
        """锚点：同模块 `user_create` 早有这条校验，机构侧必须对齐。"""
        self.expect_fail(self.post_json(f'{API}/users/create/', {
            'username': 'inactive_probe', 'name': '探针', 'role': 'hospital',
            'district_id': self.district_b.id,
            'institution_id': self.hospital_b.id}), message='所选区县已停用')


# ============================================
# 系统配置的写入端契约（第三十五轮）
# ============================================
class SystemConfigWriteWhitelistTest(SupervisionBase):
    """系统配置的**写入端**必须有键白名单。

    此前 POST 是

        for key, value in data.items():
            SystemConfig.objects.update_or_create(key=key, defaults={'value': str(value)})

    ——**请求体的每个顶层 key 都会变成一行配置**。枚举实测：用 23 种畸形
    请求体打一遍 78 条 POST 路由，`SystemConfig` 被写入 **16 行**，全是
    `district_id` / `count` / `old_password` / `status` / `action` 这类垃圾键。
    这些垃圾随后会被 GET **原样读出来**返回给前端，且无法与真实配置区分 ——
    有写权限的人手滑一次（或前端 payload 构造出错）就永久污染配置表。

    ⚠ 值必须是字符串：`str(value)` **对任何类型都成功**，
    `str(None)` = `'None'`、`str({'$ne': None})` = `"{'$ne': None}"` ——
    畸形值不会报错，只会静默变成垃圾前缀，被拼进之后新生成的所有单据号里。
    """

    def test_unknown_key_is_rejected_and_not_written(self):
        self.client.force_login(self.gov_city)
        before = SystemConfig.objects.count()
        for key in ('district_id', 'count', 'old_password', 'status', 'action',
                    'id', 'anything_at_all'):
            with self.subTest(key=key):
                resp = self.post_json(f'{API}/config/', {key: 'x'})
                self.assertEqual(resp.status_code, 400,
                                 '未知配置项 %r 必须被拒绝' % key)
                self.assertIn('不支持的配置项', resp.json().get('message', ''))
        self.assertEqual(SystemConfig.objects.count(), before,
                         '被拒绝的请求不得写库')

    def test_whitelisted_key_still_works(self):
        """正向对照：白名单内的键必须照常写入。

        守卫不能做成「一律禁止」—— 那样测试会全绿、功能却废了。
        这是本项目的固定套路：**每个守卫都要有正向对照**。
        """
        self.client.force_login(self.gov_city)
        self.ok(self.post_json(f'{API}/config/', {'capture_prefix': 'ZB'}))
        self.assertEqual(SystemConfig.objects.get(key='capture_prefix').value, 'ZB')

    def test_whitelist_matches_read_defaults(self):
        """白名单与读接口的默认值必须**同源**。

        分成两份必然漂移，漂移的表现恰好是最难查的那种：新加的配置项
        「读得到、写不进去」，用户点了保存界面还提示成功。
        这里顺带断言「库里没有白名单外的键」—— 那正是上一版留下的污染形态。
        """
        from supervision.views import SYSTEM_CONFIG_DEFAULTS
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(f'{API}/config/'))['data']
        self.assertEqual(
            set(data), set(SYSTEM_CONFIG_DEFAULTS),
            '读接口返回的键集合与写白名单不一致（或库里存在白名单外的残留键）')

    def test_non_string_values_are_rejected(self):
        self.client.force_login(self.gov_city)
        before = SystemConfig.objects.count()
        for payload in ({'capture_prefix': None},
                        {'capture_prefix': [1, 2, 3]},
                        {'capture_prefix': {'$ne': None}},
                        {'capture_prefix': 123},
                        {'capture_prefix': '   '},
                        {'capture_prefix': 'X' * 50}):
            with self.subTest(payload=payload):
                resp = self.post_json(f'{API}/config/', payload)
                self.assertEqual(resp.status_code, 400,
                                 '%r 应被拒绝，实际 %s：%r'
                                 % (payload, resp.status_code, resp.content[:200]))
        self.assertEqual(SystemConfig.objects.count(), before)

    def test_district_admin_cannot_write(self):
        """锚点：写权限仍然只有市级 —— 白名单不能顺手放宽了角色。"""
        self.client.force_login(self.gov_a)
        self.expect_fail(
            self.post_json(f'{API}/config/', {'capture_prefix': 'ZZ'}), status=403)


class DistrictEditStatusEnumTest(SupervisionBase):
    """区县编辑接口的 `status` 必须与切换接口**同值域**（第三十五轮）。

    `District.status` 是 `CharField` **没有 choices**，而停用判据是
    `business.services.inactive_district_error()` 里的 `status != 'active'`
    —— 于是写入**任何**非 `'active'` 的字符串都等价于「停用该区县」，
    且界面会把那个垃圾值当状态显示出来（无 choices 时
    `get_status_display()` 返回原值）。

    孪生入口 `district_toggle_status` 只会写 `'active'` / `'inactive'`；
    编辑接口必须与它同值域 —— 否则前者只是「界面上不容易踩到」，不是约束，
    直接调接口就能塞进任意状态。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.gov_city)

    def test_malformed_status_is_rejected_and_not_written(self):
        for value in ('zzz', '', '   ', 'ACTIVE', 'active ', None, [1, 2, 3]):
            with self.subTest(value=value):
                resp = self.post_json(
                    f'{API}/districts/{self.district_a.id}/edit/',
                    {'status': value})
                self.assertEqual(resp.status_code, 400,
                                 'status=%r → %s：%r'
                                 % (value, resp.status_code, resp.content[:200]))
                self.assertIn('状态', resp.json().get('message', ''))
        self.district_a.refresh_from_db()
        self.assertEqual(self.district_a.status, 'active',
                         '被拒绝的请求不得改库')

    def test_valid_status_values_still_work(self):
        """正向对照：两个合法值必须照常写入。"""
        for value in ('inactive', 'active'):
            with self.subTest(value=value):
                self.ok(self.post_json(
                    f'{API}/districts/{self.district_a.id}/edit/',
                    {'status': value}))
                self.district_a.refresh_from_db()
                self.assertEqual(self.district_a.status, value)

    def test_non_active_status_really_disables(self):
        """锚点：解释为什么 `status` 必须收窄。

        这条断言不是在测 `inactive_district_error`，而是把「为什么这里
        必须校验」写进测试里 —— 判据一旦变化，这里会先红。
        """
        from business.services import inactive_district_error
        self.district_a.status = 'zzz'
        self.assertIsNotNone(
            inactive_district_error(self.district_a),
            '`status != "active"` 就是停用判据 —— 所以写入任意非 active 值'
            '都等价于停用该区县')
