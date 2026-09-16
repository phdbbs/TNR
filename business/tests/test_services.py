"""服务层单元测试：编号生成、芯片管理、库存、黑名单、区县隔离、序列化。"""
import json as json_lib
import re
from datetime import date
from unittest import mock

from django.test import TestCase

from business.models import Material, MaterialTransaction, Pet
from business.services import (
    check_blacklist, generate_ledger_no, generate_pet_codes,
    get_district_filtered_queryset, get_hospital_stock, serialize_instance,
    adjust_stock, use_chip, amap_ip_location,
    capture_transfer_state, capture_states_bulk,
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
