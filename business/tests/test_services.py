"""服务层单元测试：编号生成、芯片管理、库存、黑名单、区县隔离、序列化。"""
import json as json_lib
import os
import re
from datetime import date
from unittest import mock

from django.test import SimpleTestCase, TestCase

from business.models import Material, MaterialTransaction, Pet
from business.services import (
    check_blacklist, generate_ledger_no, generate_pet_codes,
    get_district_filtered_queryset, get_hospital_stock, serialize_instance,
    adjust_stock, use_chip, amap_ip_location,
    capture_transfer_state, capture_states_bulk,
    validate_operator_district, cascade_operator_district,
    resolve_operator_district, validate_user_manage_scope,
)
from business.tests.base import (
    BusinessTestBase, make_chip, make_district, make_hospital_txn,
    make_institution, make_material, make_pet, make_user,
)


class GeneratePetCodesTest(TestCase):
    def test_format_and_count(self):
        codes = generate_pet_codes(5)
        self.assertEqual(len(codes), 5)
        for code in codes:
            self.assertRegex(code, r'^TNR\d{6}\d{3}$')

    def test_sequential_and_unique(self):
        codes = generate_pet_codes(3)
        self.assertEqual(len(set(codes)), 3)
        seqs = [int(c[-3:]) for c in codes]
        self.assertEqual(seqs, [seqs[0] + i for i in range(3)])

    def test_continues_after_existing_codes(self):
        today_prefix = generate_pet_codes(1)[0][:-3]
        Pet.objects.create(code=f'{today_prefix}007', district=make_district())
        codes = generate_pet_codes(2)
        self.assertEqual([int(c[-3:]) for c in codes], [8, 9])

    def test_zero_count_returns_empty(self):
        self.assertEqual(generate_pet_codes(0), [])


class GenerateLedgerNoTest(TestCase):
    def test_format(self):
        no = generate_ledger_no('CAP')
        self.assertRegex(no, r'^CAP-\d{6}-\d{4}$')

    def test_prefix_preserved(self):
        for prefix in ('TRF', 'TRE', 'REL', 'ADP', 'EUT'):
            self.assertTrue(generate_ledger_no(prefix).startswith(prefix + '-'))


class UseChipTest(TestCase):
    def test_success_marks_used_and_binds_pet(self):
        chip = make_chip()
        pet = make_pet()
        self.assertTrue(use_chip(chip.number, pet))
        chip.refresh_from_db()
        pet.refresh_from_db()
        self.assertEqual(chip.status, 'used')
        self.assertEqual(chip.pet, pet)
        self.assertEqual(chip.used_at, date.today())
        self.assertEqual(pet.chip_no, chip.number)

    def test_missing_chip_raises(self):
        with self.assertRaises(ValueError) as ctx:
            use_chip('NOPE-404', make_pet())
        self.assertIn('不存在', str(ctx.exception))

    def test_reuse_used_chip_raises(self):
        pet = make_pet()
        chip = make_chip(status='used')
        with self.assertRaises(ValueError) as ctx:
            use_chip(chip.number, pet)
        self.assertIn('已被使用', str(ctx.exception))


class AdjustStockTest(TestCase):
    def test_purchase_increases_shelter_stock(self):
        material = make_material(shelter_stock=10)
        adjust_stock(material, None, 40, 'purchase')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 50)

    def test_dispatch_decreases_shelter_stock(self):
        material = make_material(shelter_stock=10)
        adjust_stock(material, None, 4, 'dispatch')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 6)

    def test_shelter_stock_cannot_go_negative(self):
        material = make_material(shelter_stock=2)
        with self.assertRaises(ValueError) as ctx:
            adjust_stock(material, None, 5, 'adjustment')
        self.assertIn('库存不足', str(ctx.exception))
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 2)

    def test_hospital_side_does_not_change_fields(self):
        material = make_material(shelter_stock=10)
        hospital = make_institution(type='hospital')
        adjust_stock(material, hospital, 7, 'consume')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 10)
        self.assertEqual(get_hospital_stock(material, hospital), -7)


class GetHospitalStockTest(TestCase):
    def test_purchase_plus_receive_minus_consume_minus_adjustment(self):
        material = make_material()
        hospital = make_institution(type='hospital')
        other = make_institution(type='hospital')
        make_hospital_txn(material, hospital, 'purchase', 10)
        make_hospital_txn(material, hospital, 'receive', 30)
        make_hospital_txn(material, hospital, 'consume', 5)
        make_hospital_txn(material, hospital, 'adjustment', 3)
        # 其他医院的流水不应计入
        make_hospital_txn(material, other, 'receive', 100)
        # 无医院的流水（下发待签收）不应计入
        MaterialTransaction.objects.create(
            material=material, quantity=99, type='dispatch',
            date=date.today(), district=material.district)
        self.assertEqual(get_hospital_stock(material, hospital), 10 + 30 - 5 - 3)

    def test_none_hospital_returns_zero(self):
        self.assertEqual(get_hospital_stock(make_material(), None), 0)


class CheckBlacklistTest(TestCase):
    def _add(self, **kw):
        from business.models import Blacklist
        return Blacklist.objects.create(
            name=kw.pop('name', '黑名单用户'),
            reason=kw.pop('reason', '弃养'),
            district=kw.pop('district', make_district()),
            **kw,
        )

    def test_phone_exact_match(self):
        self._add(phone='13900001111')
        self.assertIsNotNone(check_blacklist(None, '13900001111'))

    def test_id_card_head_tail_match(self):
        # 黑名单掩码身份证：前6位+后4位可定位
        self._add(id_card='110102****5678')
        self.assertIsNotNone(check_blacklist('11010219900101567X'.replace('X', '8'), None))
        self.assertIsNotNone(check_blacklist('110102********5678', None))

    def test_same_prefix_different_person_not_matched(self):
        """同区县（前6位相同）但尾号不同的人不应被误伤。"""
        self._add(id_card='110102****5678')
        self.assertIsNone(check_blacklist('110102199001019999', None))

    def test_no_input_returns_none(self):
        self.assertIsNone(check_blacklist('', ''))

    def test_no_records_returns_none(self):
        self.assertIsNone(check_blacklist('13900001111', '13900001111'))


class DistrictFilterTest(BusinessTestBase):
    def test_gov_city_sees_all(self):
        make_pet(district=self.district_a)
        make_pet(district=self.district_b)
        qs = get_district_filtered_queryset(Pet, self.gov_city)
        self.assertEqual(qs.count(), 2)

    def test_district_gov_sees_only_own(self):
        pet_a = make_pet(district=self.district_a)
        make_pet(district=self.district_b)
        qs = get_district_filtered_queryset(Pet, self.gov_a)
        self.assertEqual(list(qs), [pet_a])

    def test_city_district_user_sees_all(self):
        """挂在市级区县的捕捉点操作员（如种子数据的 cy_shelter）可见全部。"""
        user = make_user(role='shelter', district=self.city)
        make_pet(district=self.district_a)
        make_pet(district=self.district_b)
        self.assertEqual(get_district_filtered_queryset(Pet, user).count(), 2)

    def test_user_without_district_sees_none(self):
        qs = get_district_filtered_queryset(Pet, make_user(role='adopter'))
        self.assertEqual(qs.count(), 0)


class SerializeInstanceTest(TestCase):
    def test_snake_and_camel_keys(self):
        pet = make_pet()
        data = serialize_instance(pet)
        self.assertEqual(data['chip_no'], data['chipNo'])
        self.assertIn('district_id', data)
        self.assertEqual(data['district_id'], pet.district_id)
        self.assertEqual(data['code'], pet.code)

    def test_pet_codes_string_to_array(self):
        from business.models import Capture
        from business.tests.base import make_capture
        capture = make_capture(pet_codes=['A1', 'A2'])
        data = serialize_instance(capture)
        self.assertEqual(data['petCodes'], ['A1', 'A2'])

    def test_date_isoformat(self):
        from business.models import MaterialTransaction
        material = make_material()
        txn = MaterialTransaction.objects.create(
            material=material, quantity=1, type='purchase', date=date(2026, 1, 2),
            district=material.district)
        self.assertEqual(serialize_instance(txn)['date'], '2026-01-02')

    def test_fields_filter(self):
        pet = make_pet()
        data = serialize_instance(pet, fields=['code', 'status'])
        self.assertEqual(set(data.keys()), {'code', 'status'})

    def test_none_instance(self):
        self.assertIsNone(serialize_instance(None))


class AmapIpLocationTest(TestCase):
    """amap_ip_location：高德 IP 定位（rectangle 取中心点）+ 中文错误信息。"""

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': 'test-key'})
    @mock.patch('business.services.urllib.request.urlopen')
    def test_rectangle_center_success(self, m_open):
        resp = mock.Mock()
        resp.read.return_value = json_lib.dumps({
            'status': '1', 'info': 'OK', 'province': '浙江省', 'city': '杭州市',
            'adcode': '330100', 'rectangle': '119.5,30.0;120.0,30.5',  # 高德实际用分号分隔
        }).encode('utf-8')
        m_open.return_value.__enter__.return_value = resp
        out = amap_ip_location()
        self.assertEqual(out['latitude'], 30.25)
        self.assertEqual(out['longitude'], 119.75)
        self.assertEqual(out['province'], '浙江省')
        self.assertEqual(out['city'], '杭州市')

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': 'test-key'})
    @mock.patch('business.services.urllib.request.urlopen')
    def test_no_rectangle_raises(self, m_open):
        resp = mock.Mock()
        resp.read.return_value = json_lib.dumps({
            'status': '1', 'info': 'OK', 'province': [], 'city': [], 'rectangle': '',
        }).encode('utf-8')
        m_open.return_value.__enter__.return_value = resp
        with self.assertRaises(ValueError) as ctx:
            amap_ip_location()
        self.assertIn('未获取到有效位置范围', str(ctx.exception))

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': ''})
    def test_missing_key_raises(self):
        with self.assertRaises(ValueError) as ctx:
            amap_ip_location()
        self.assertIn('未配置地图服务Key', str(ctx.exception))

    @mock.patch.dict('os.environ', {'TNR_AMAP_KEY': 'test-key'})
    @mock.patch('business.services.urllib.request.urlopen')
    def test_amap_error_raises_chinese_message(self, m_open):
        resp = mock.Mock()
        resp.read.return_value = json_lib.dumps({
            'status': '0', 'info': 'DAILY_QUERY_OVER_LIMIT', 'infocode': '10021',
        }).encode('utf-8')
        m_open.return_value.__enter__.return_value = resp
        with self.assertRaises(ValueError) as ctx:
            amap_ip_location()
        self.assertIn('DAILY_QUERY_OVER_LIMIT', str(ctx.exception))


class CaptureTransferStateTest(BusinessTestBase):
    """捕捉单转运状态统计：必须能区分「转运中 / 部分完成 / 完成」。

    合并列「转运状态」的四档取值完全依赖本函数返回的 received / settled：
    缺了它们，前端永远只能显示「未转运」或「转运中」。
    """

    def _capture_with_pets(self, count=2):
        from business.tests.base import make_capture
        capture = make_capture(district=self.district_a, shelter=self.shelter_a)
        pets = [make_pet(district=self.district_a, shelter=self.shelter_a,
                         capture=capture, status='in_transit')
                for _ in range(count)]
        return capture, pets

    def _transfer(self, capture, pets, status='pending'):
        from business.models import Transfer
        return Transfer.objects.create(
            from_shelter=self.shelter_a, to_hospital=self.hospital_a,
            pet_codes=','.join(p.code for p in pets), pet_count=len(pets),
            status=status, district=self.district_a, capture=capture)

    def test_untouched_capture(self):
        capture, _ = self._capture_with_pets(2)
        st = capture_transfer_state(capture)
        self.assertEqual(st['total'], 2)
        self.assertEqual((st['transferred'], st['received'], st['settled']), (0, 0, 0))
        self.assertTrue(st['can_edit'])

    def test_pending_only_is_in_transit(self):
        """1 只提交、医院未签收 → transferred=1 / received=0。"""
        capture, pets = self._capture_with_pets(2)
        self._transfer(capture, pets[:1])
        st = capture_transfer_state(capture)
        self.assertEqual((st['total'], st['transferred'], st['received'], st['settled']),
                         (2, 1, 0, 0))
        self.assertFalse(st['can_edit'], '有在途转运单时不可编辑')

    def test_received_counts_as_partial(self):
        """1 只医院已签收 → received=1、settled=1，另一只未提交。"""
        capture, pets = self._capture_with_pets(2)
        self._transfer(capture, pets[:1], status='received')
        pets[0].status = 'in_treatment'
        pets[0].hospital = self.hospital_a
        pets[0].save(update_fields=['status', 'hospital'])
        st = capture_transfer_state(capture)
        self.assertEqual((st['total'], st['transferred'], st['received'], st['settled']),
                         (2, 1, 1, 1))

    def test_all_received_is_settled(self):
        """两只都签收 → settled == total（前端判定「完成」）。"""
        capture, pets = self._capture_with_pets(2)
        self._transfer(capture, pets, status='received')
        Pet.objects.filter(id__in=[p.id for p in pets]).update(
            status='in_treatment', hospital=self.hospital_a)
        st = capture_transfer_state(capture)
        self.assertEqual(st['settled'], st['total'])
        self.assertEqual(st['received'], 2)

    def test_owner_returned_counts_as_settled(self):
        """主人领回（回收）也算「已离开本单流程」，计入完成。"""
        capture, pets = self._capture_with_pets(2)
        self._transfer(capture, pets[:1], status='received')
        Pet.objects.filter(id=pets[0].id).update(status='in_treatment')
        Pet.objects.filter(id=pets[1].id).update(status='owner_returned')
        st = capture_transfer_state(capture)
        self.assertEqual(st['settled'], 2)
        self.assertEqual(st['received'], 1)

    def test_void_transfer_does_not_count(self):
        """撤回（void）的转运单不得计入 transferred / received。"""
        capture, pets = self._capture_with_pets(2)
        self._transfer(capture, pets, status='void')
        st = capture_transfer_state(capture)
        self.assertEqual((st['transferred'], st['received']), (0, 0))
        self.assertTrue(st['can_edit'], '撤回后捕捉单应解锁')

    def test_rejected_then_pending_counts_pending(self):
        """被驳回后重新下发：应算「转运中」，不能停留在旧的 rejected。"""
        capture, pets = self._capture_with_pets(1)
        self._transfer(capture, pets, status='rejected')
        self._transfer(capture, pets, status='pending')
        st = capture_transfer_state(capture)
        self.assertEqual(st['transferred'], 1)
        self.assertEqual(st['rejected'], 0, '重新下发后不应再算退回')

    def test_bulk_matches_single(self):
        """列表接口用的批量版必须与单张版结果一致，否则列表与详情会打架。"""
        capture, pets = self._capture_with_pets(2)
        self._transfer(capture, pets[:1], status='received')
        Pet.objects.filter(id=pets[0].id).update(status='in_treatment')
        self._transfer(capture, pets[1:], status='pending')

        single = capture_transfer_state(capture)
        bulk = capture_states_bulk([capture])[capture.id]
        for key in ('total', 'transferred', 'received', 'settled', 'rejected',
                    'untouched', 'can_edit', 'can_delete'):
            self.assertEqual(single[key], bulk[key], f'{key} 不一致：{single} vs {bulk}')

    def test_bulk_handles_multiple_captures(self):
        from business.tests.base import make_capture
        cap_a, pets_a = self._capture_with_pets(2)
        cap_b = make_capture(district=self.district_a, shelter=self.shelter_a)
        make_pet(district=self.district_a, shelter=self.shelter_a,
                 capture=cap_b, status='in_transit')
        self._transfer(cap_a, pets_a, status='received')
        Pet.objects.filter(id__in=[p.id for p in pets_a]).update(status='in_treatment')

        bulk = capture_states_bulk([cap_a, cap_b])
        self.assertEqual(bulk[cap_a.id]['received'], 2)
        self.assertEqual(bulk[cap_a.id]['settled'], 2)
        self.assertEqual(bulk[cap_b.id]['received'], 0)
        self.assertEqual(bulk[cap_b.id]['settled'], 0)
        self.assertEqual(bulk[cap_b.id]['total'], 1)


# ---------------------------------------------------------------------------
# 本地日期守卫
# ---------------------------------------------------------------------------

ROOT_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

# 生产代码（刻意不含 tests/ 与 migrations/ —— 测试里可以任意构造时间）
LOCAL_DATE_FILES = (
    'business/services.py',
    'business/tasks.py',
    'business/views_checkin.py',
    'business/views_capture.py',
    'business/views_portal.py',
    'business/views_transfer.py',
    'business/views_material.py',
    'business/views_adoption.py',
    'business/views_release.py',
    'business/views_euthanasia.py',
    'supervision/views.py',
)


class ValidateOperatorDistrictTest(BusinessTestBase):
    """账号区县必须与所属机构所在区县自洽。

    不一致的账号会「看到 A 区县的档案、以 B 区县的机构身份操作」——
    实测库里就有一个（区县=南漳县、机构=东津新区的宠安宠物诊所），
    而政府端用户管理只有新增与启停、没有编辑，事后在界面上改不回来。
    """

    def test_hospital_operator_must_match_institution(self):
        err = validate_operator_district('hospital', self.district_b, self.hospital_a)
        self.assertIn('不一致', err)
        self.assertIn(self.district_b.name, err)
        self.assertIn(self.district_a.name, err)

    def test_hospital_operator_matching_passes(self):
        self.assertIsNone(
            validate_operator_district('hospital', self.district_a, self.hospital_a))

    def test_hospital_operator_cannot_sit_in_city_district(self):
        err = validate_operator_district('hospital', self.city, self.hospital_a)
        self.assertIn('市级', err)

    def test_shelter_operator_may_sit_in_city_district(self):
        """现场约定：两个捕捉点操作员都挂在「全市（市级）」下。"""
        self.assertIsNone(
            validate_operator_district('shelter', self.city, self.shelter_a))

    def test_shelter_operator_with_specific_district_must_match(self):
        self.assertIsNone(
            validate_operator_district('shelter', self.district_a, self.shelter_a))
        self.assertIsNotNone(
            validate_operator_district('shelter', self.district_b, self.shelter_a))

    def test_missing_district_is_rejected(self):
        self.assertIsNotNone(
            validate_operator_district('hospital', None, self.hospital_a))

    def test_roles_without_institution_are_not_checked(self):
        self.assertIsNone(validate_operator_district('gov_city', self.city, None))
        self.assertIsNone(validate_operator_district('gov_district', self.district_a, None))


class ResolveOperatorDistrictTest(BusinessTestBase):
    """`resolve_operator_district()` 与 `validate_operator_district()` 必须互补。

    这两个函数是一对：校验负责**拦**（建号时），解析负责**修**（历史账号）。
    一旦它们对同一个输入给出矛盾结论，就会出现「巡检报错、修复脚本却说不用改」
    的死循环 —— 而且两边各自的用例都会通过，因为没人测过它们的一致性。
    所以这里用一张表把两个结论并排断言。
    """

    def test_resolver_is_the_exact_complement_of_validator(self):
        cases = [
            # (角色, 账号区县, 机构, 期望：是否报错, 期望解析结果)
            ('hospital', 'district_a', 'hospital_a', False, None),
            ('hospital', 'district_b', 'hospital_a', True, 'district_a'),
            ('hospital', 'city', 'hospital_a', True, 'district_a'),
            ('hospital', None, 'hospital_a', True, 'district_a'),
            ('hospital', 'district_b', None, False, None),
            ('shelter', 'city', 'shelter_a', False, None),      # 现场约定：放行
            ('shelter', 'district_a', 'shelter_a', False, None),
            ('shelter', 'district_b', 'shelter_a', True, 'district_a'),
            ('gov_city', 'city', None, False, None),
            ('gov_district', 'district_a', None, False, None),
        ]
        for role, district_key, institution_key, should_error, expect in cases:
            with self.subTest(role=role, district=district_key, institution=institution_key):
                district = getattr(self, district_key) if district_key else None
                institution = getattr(self, institution_key) if institution_key else None

                err = validate_operator_district(role, district, institution)
                resolved = resolve_operator_district(role, district, institution)

                self.assertEqual(bool(err), should_error, err)
                # 互补的核心：校验通过 ⟺ 无需修复
                self.assertEqual(resolved is None, err is None, (err, resolved))
                if expect:
                    self.assertEqual(resolved, getattr(self, expect))


class ValidateUserManageScopeTest(BusinessTestBase):
    """操作员自己的管辖范围：角色白名单 + 只能管本区县。

    为什么必须做在服务端：前端 `canCreateRole()` 只是把角色下拉的选项过滤掉，
    接口没有同一套校验就等于没校验 —— 实测区级管理员直接 POST `role=gov_city`
    **能建出一个市级管理员账号**（口令还是他自己填的），登录后看到全部区县数据。
    这类「界面上点不出来、只有打接口才会暴露」的漏洞，正是最该用用例锁住的一类。
    """

    def test_city_admin_can_manage_all_roles(self):
        for role in ('gov_city', 'gov_district', 'shelter', 'hospital'):
            with self.subTest(role=role):
                self.assertIsNone(
                    validate_user_manage_scope(self.gov_city, role, self.district_a, None))

    def test_district_admin_cannot_create_government_roles(self):
        """核心越权：区级管理员不得建市级/区级管理员账号。"""
        for role in ('gov_city', 'gov_district'):
            with self.subTest(role=role):
                err = validate_user_manage_scope(self.gov_a, role, self.district_a, None)
                self.assertIn('无权', err)

    def test_district_admin_can_create_own_district_hospital(self):
        """正向对照：只加拦截不测放行，过紧的校验发现不了。"""
        self.assertIsNone(validate_user_manage_scope(
            self.gov_a, 'hospital', self.district_a, self.hospital_a))

    def test_district_admin_can_create_city_level_shelter_of_own_institution(self):
        """捕捉点操作员按现场约定挂「市级」，判据要看**机构**而不是账号区县。

        若误判成账号区县，区级管理员唯一能建的那类账号会被全部挡掉。
        """
        self.assertIsNone(validate_user_manage_scope(
            self.gov_a, 'shelter', self.city, self.shelter_a))

    def test_district_admin_cannot_use_other_district_institution(self):
        """跨区县：机构在乙区，甲区管理员不得拿它建号。"""
        err = validate_user_manage_scope(
            self.gov_a, 'hospital', self.district_b, self.hospital_b)
        self.assertIn('其他区县', err)
        err = validate_user_manage_scope(
            self.gov_a, 'shelter', self.city, self.shelter_b)
        self.assertIn('其他区县', err)

    def test_district_admin_cannot_create_account_in_other_district(self):
        """没有机构时退化成判账号区县。"""
        err = validate_user_manage_scope(self.gov_a, 'hospital', self.district_b, None)
        self.assertIn('其他区县', err)

    def test_non_government_roles_have_no_manageable_roles(self):
        self.assertIsNotNone(
            validate_user_manage_scope(self.shelter_user_a, 'hospital',
                                       self.district_a, self.hospital_a))


class CascadeOperatorDistrictTest(BusinessTestBase):
    """机构换区县时，区县随机构走的操作员必须一起搬。"""

    def test_moves_matching_operators(self):
        hospital = make_institution(type='hospital', district=self.district_a)
        op = make_user('cascade_op_t', role='hospital',
                       district=self.district_a, institution=hospital)
        hospital.district = self.district_b
        hospital.save(update_fields=['district'])

        moved = cascade_operator_district(hospital, self.district_a)

        op.refresh_from_db()
        self.assertEqual(moved, 1)
        self.assertEqual(op.district, self.district_b)

    def test_keeps_city_level_operators(self):
        """挂「市级」的捕捉点操作员是现场约定，搬动会破坏它。"""
        shelter = make_institution(type='shelter', district=self.district_a)
        op = make_user('cascade_city_t', role='shelter',
                       district=self.city, institution=shelter)
        shelter.district = self.district_b
        shelter.save(update_fields=['district'])

        moved = cascade_operator_district(shelter, self.district_a)

        op.refresh_from_db()
        self.assertEqual(moved, 0)
        self.assertEqual(op.district, self.city)

    def test_noop_when_district_unchanged(self):
        self.assertEqual(
            cascade_operator_district(self.hospital_a, self.district_a.id), 0)


def _read_source(rel):
    with open(os.path.join(ROOT_DIR, rel), encoding='utf-8') as f:
        return f.read()


def _function_source(source, name):
    """按缩进切出顶层函数体（含 def 行与内部注释）。"""
    m = re.search(r'^def\s+' + re.escape(name) + r'\s*\(', source, re.M)
    if m is None:
        raise AssertionError(f'未找到函数 {name}()，测试本身需要同步更新')
    lines = source[m.start():].split('\n')
    body = [lines[0]]
    for line in lines[1:]:
        if line.strip() and not line.startswith((' ', '\t')):
            break
        body.append(line)
    return '\n'.join(body)


class LocalDateUsageTest(SimpleTestCase):
    """后端不得把 `timezone.now()`（UTC）当作日期来用。

    真实案例：9/18 凌晨生成捕捉单，编号仍是 `TNR260917xxx`、单号仍是
    `RET-260917-xxxx`。根因是 `generate_pet_codes()` / `generate_ledger_no()`
    用 `timezone.now()` 取日期——`timezone.now()` 返回 **UTC**，
    项目 `TIME_ZONE='Asia/Shanghai'`，北京时间 00:00–08:00 这段 UTC 还停在前一天。

    `business/views_checkin.py` 的默认月份同理：月初凌晨会把月份记成上一个月。

    这与前端 `toISOString()` 截日期是**同一类坑**（见 `core/tests_frontend_consistency.py`
    的 `LocalDateDefaultTest`）。判据：
    - 取日期字符串 / 生成编号 / 判定今天或本月 → `timezone.localdate()`
    - 存时间戳、算时间差、比 `created_at` → `timezone.now()`
    """

    # 形态一：timezone.now() 直接接日期方法
    UTC_AS_DATE = re.compile(
        r'timezone\.now\(\)\s*\.\s*(?:strftime|date|isoformat)\s*\(')
    # 形态二（历史缺陷的真实形态）：先赋值给变量，再用该变量取日期
    NOW_ASSIGN = re.compile(r'\b(\w+)\s*=\s*timezone\.now\(\)')

    def _scan(self, rel):
        source = _read_source(rel)
        problems = []
        for m in self.UTC_AS_DATE.finditer(source):
            problems.append(
                f'{rel}:{source[:m.start()].count(chr(10)) + 1} '
                'timezone.now().strftime/date 直接取日期')
        for m in self.NOW_ASSIGN.finditer(source):
            var = m.group(1)
            use = re.search(
                r'\b' + re.escape(var) + r'\s*\.\s*(?:strftime|date|isoformat)\s*\(',
                source[m.end():])
            if use:
                problems.append(
                    f'{rel}:{source[:m.start()].count(chr(10)) + 1} '
                    f'{var} = timezone.now() 之后又用 {var}.strftime/date 取日期')
        return problems

    def test_no_utc_now_used_as_date(self):
        problems = []
        for rel in LOCAL_DATE_FILES:
            problems.extend(self._scan(rel))
        self.assertFalse(
            problems,
            '以下位置把 UTC 当本地日期用 —— 北京时间 00:00–08:00 会得到前一天：\n  '
            + '\n  '.join(problems)
            + '\n请改用 timezone.localdate()。')

    def test_number_generators_use_local_date(self):
        """编号/单号生成器必须用 localdate()，这是最容易被改回去的地方。"""
        source = _read_source('business/services.py')
        for fn in ('generate_pet_codes', 'generate_ledger_no'):
            body = _function_source(source, fn)
            self.assertNotIn(
                'timezone.now()', body,
                f'{fn}() 不应使用 timezone.now()（UTC）——凌晨生成的编号会退到前一天')
            self.assertIn(
                'timezone.localdate()', body,
                f'{fn}() 应使用 timezone.localdate() 取本地日期')

    def test_checkin_default_month_uses_local_date(self):
        """报修默认月份同理：月初凌晨用 UTC 会记成上一个月。"""
        body = _function_source(_read_source('business/views_checkin.py'),
                                _month_function_name())
        self.assertNotIn('timezone.now()', body,
                         '默认月份不应使用 timezone.now()（UTC）')
        self.assertIn('timezone.localdate()', body,
                      '默认月份应使用 timezone.localdate()')


def _month_function_name():
    """定位 `views_checkin.py` 里使用 localdate() 的那个函数名。"""
    source = _read_source('business/views_checkin.py')
    for m in re.finditer(r'^def\s+(\w+)\s*\(', source, re.M):
        body = _function_source(source, m.group(1))
        if 'localdate()' in body and 'strftime' in body:
            return m.group(1)
    raise AssertionError('未找到取默认月份的函数，测试本身需要同步更新')
