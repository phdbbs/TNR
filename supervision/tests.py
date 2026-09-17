"""supervision 应用测试：政府监管端接口与权限边界。"""
from accounts.models import User
from business.models import (
    Adoption, Capture, Euthanasia, Material, MaterialTransaction, Pet,
    Release, Treatment,
)
from business.tests.base import (
    ApiMixin, make_district, make_institution, make_material, make_pet,
    make_user,
)
from core.models import District
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
        from django.contrib.admin.models import ADDITION, LogEntry
        from django.contrib.contenttypes.models import ContentType
        LogEntry.objects.log_action(
            user_id=self.gov_city.id, content_type_id=ContentType.objects.get_for_model(Pet).id,
            object_id=1, object_repr='测试', action_flag=ADDITION, change_message='测试日志')
        self.client.force_login(self.gov_city)
        data = self.ok(self.client.get(f'{API}/logs/'))['data']
        self.assertTrue(any(log['change_message'] == '测试日志' for log in data))

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

    def test_system_config_gov_district_403(self):
        self.client.force_login(self.gov_a)
        self.expect_fail(self.client.get(f'{API}/config/'), status=403)
        self.expect_fail(self.post_json(f'{API}/config/', {'x': 'y'}), status=403)
