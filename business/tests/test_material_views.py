"""物料供应链双台账视图测试：采购/下发/签收/异动/流水/芯片号段。"""
from datetime import date

from django.utils import timezone

from business.models import Chip, MaterialTransaction
from business.services import get_hospital_stock
from business.tests.base import (
    BusinessTestBase, make_chip, make_hospital_txn, make_material,
)

URL = '/api/business/materials/'


class MaterialListTest(BusinessTestBase):
    def test_list_with_category_filter(self):
        vaccine = make_material(category='vaccine', district=self.district_a)
        make_material(category='chip', district=self.district_a)
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(URL + '?category=vaccine'))['data']
        self.assertEqual([m['id'] for m in data], [vaccine.id])

    def test_hospital_gets_own_stock(self):
        material = make_material(category='vaccine', district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 7)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json(URL))['data']
        target = next(m for m in data if m['id'] == material.id)
        self.assertEqual(target['hospital_stock'], 7)

    def test_gov_gets_all_hospital_stocks(self):
        material = make_material(category='vaccine', district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 3)
        self.login_as(self.gov_city)
        data = self.ok(self.get_json(URL))['data']
        target = next(m for m in data if m['id'] == material.id)
        self.assertIn(str(self.hospital_a.id), target['hospital_stocks'])
        self.assertEqual(target['hospital_stocks'][str(self.hospital_a.id)]['stock'], 3)


class PurchaseTest(BusinessTestBase):
    def test_purchase_existing_material(self):
        material = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 40,
            'supplier': '国药', 'batch_no': 'B2026',
        }))
        self.assertIn('增加库存 40', body['message'])
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 50)
        self.assertEqual(material.supplier, '国药')
        txn = MaterialTransaction.objects.get(id=body['data']['id'])
        self.assertEqual(txn.type, 'purchase')
        self.assertTrue(txn.ledger_no.startswith('PUR-'))

    def test_purchase_new_material(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}purchase/', {
            'name': '新疫苗', 'category': 'vaccine', 'quantity': 20,
        }))
        material = MaterialTransaction.objects.get(id=body['data']['id']).material
        self.assertEqual(material.name, '新疫苗')
        self.assertEqual(material.shelter_stock, 20)
        self.assertEqual(material.district, self.district_a)

    def test_purchase_new_material_duplicate_name_reused(self):
        existing = make_material(name='同名物料', category='vaccine',
                                 district=self.district_a)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}purchase/', {
            'name': '同名物料', 'category': 'vaccine', 'quantity': 5,
        }))
        existing.refresh_from_db()
        self.assertEqual(existing.shelter_stock, 105)

    def test_purchase_missing_material_info(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}purchase/', {'quantity': 5}),
                  message='缺少物料ID或物料名称/类别')

    def test_purchase_invalid_category(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}purchase/',
                                 {'name': 'X', 'category': 'food', 'quantity': 5}),
                  message='缺少物料ID或物料名称/类别')

    def test_purchase_non_positive_quantity(self):
        material = make_material(district=self.district_a)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}purchase/',
                                 {'material_id': material.id, 'quantity': 0}),
                  message='采购数量必须大于0')

    def test_purchase_chip_range_creates_chips(self):
        material = make_material(category='chip', district=self.district_a)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 10,
            'chip_range_start': '9000000001', 'chip_range_end': '9000000005',
        }))
        self.assertEqual(Chip.objects.filter(
            number__between=None) .count() if False else
            Chip.objects.filter(number__gte='9000000001', number__lte='9000000005').count(), 5)
        material.refresh_from_db()
        self.assertEqual(material.chip_range_start, '9000000001')

    def test_hospital_cannot_purchase(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}purchase/', {'quantity': 1}), status=403)


class DispatchTest(BusinessTestBase):
    def _material(self, stock=50):
        return make_material(district=self.district_a, shelter_stock=stock)

    def test_dispatch_success(self):
        material = self._material()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 20,
        }))
        self.assertIn('待医院签收', body['message'])
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 30)
        txn = MaterialTransaction.objects.get(id=body['data']['id'])
        self.assertEqual(txn.type, 'dispatch')
        self.assertEqual(txn.hospital, self.hospital_a)
        self.assertTrue(txn.ledger_no.startswith('DIS-'))

    def test_dispatch_insufficient_stock(self):
        material = self._material(stock=5)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 10,
        }), message='捕捉点库存不足')

    def test_dispatch_unknown_hospital(self):
        material = self._material()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': 999999, 'quantity': 1,
        }), message='医院不存在')

    def test_dispatch_used_chip_rejected(self):
        material = self._material()
        material.category = 'chip'
        material.save()
        used = make_chip(status='used')
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 1, 'chip_numbers': [used.number],
        }), message='已被使用')

    def test_dispatch_does_not_increase_hospital_stock(self):
        material = self._material()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 10,
        }))
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 0,
                         '下发未签收前医院库存不应增加')

    def test_dispatch_cross_district_hospital_rejected(self):
        """只能下发到本区县医院。"""
        material = self._material()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_b.id,
            'quantity': 1,
        }), message='只能下发到本区县医院')

    def test_dispatch_cross_district_material_404(self):
        material = make_material(district=self.district_b, shelter_stock=50)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 1,
        }), status=404, message='无权访问')

    def test_dispatch_chip_numbers_as_csv_string(self):
        """chip_numbers 传逗号分隔字符串时不应被逐字符迭代。"""
        material = self._material()
        material.category = 'chip'
        material.save()
        used = make_chip(status='used')
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 1, 'chip_numbers': f'{used.number},',
        }), message='已被使用')


class MaterialReceiveTest(BusinessTestBase):
    def _dispatch(self, quantity=10):
        material = make_material(district=self.district_a, shelter_stock=50)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': quantity,
        }))
        return material, body['data']['id']

    def test_receive_success_increases_stock(self):
        material, txn_id = self._dispatch()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{URL}{txn_id}/receive/'))
        self.assertEqual(body['data']['type'], 'receive')
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 10)

    def test_receive_twice_rejected(self):
        _, txn_id = self._dispatch()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{URL}{txn_id}/receive/'))
        self.expect_fail(self.post_json(f'{URL}{txn_id}/receive/'), message='此物料已签收')

    def test_receive_wrong_hospital(self):
        """他院签收 → 404，且与**不存在的 id** 响应完全一致。

        第二十三轮前这里是 `400「无权签收此物料」`，而「不存在」是
        `404「下发记录不存在」` —— 两者可区分 = 存在性预言机。现已合并。
        """
        _, txn_id = self._dispatch()
        self.login_as(self.hospital_user_b)
        other = self.post_json(f'{URL}{txn_id}/receive/')
        ghost = self.post_json(f'{URL}99999999/receive/')
        self.assertEqual(other.status_code, 404, other.content)
        self.assertFalse(other.json()['success'])
        self.assertEqual(other.status_code, ghost.status_code,
                         '「他院的」与「不存在的」状态码不同 = 可枚举')
        self.assertEqual(other.json()['message'], ghost.json()['message'],
                         '「他院的」与「不存在的」文案不同 = 可枚举')

    def test_receive_unknown_txn(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}999999/receive/'), status=404,
                  message='下发记录不存在')

    def test_receive_non_dispatch_txn_rejected(self):
        """非 `dispatch` 类型的流水不能签收。

        ⚠ 夹具必须把 `hospital` 设成**本院**（第二十三轮变异 M20 才发现原先漏了）：
        不设 `hospital` 时，这条单据是被**机构过滤**挡掉的，与 `type` 过滤无关 ——
        于是把 `type='dispatch'` 从视图里删掉，这条用例**照样通过**（空转）。
        设成本院后，只剩 `type` 过滤能拒它，判别力才落在被测的那行代码上。
        """
        material = make_material(district=self.district_a)
        txn = MaterialTransaction.objects.create(
            material=material, quantity=1, type='purchase',
            hospital=self.hospital_a,
            district=self.district_a, date=timezone.localdate())
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}{txn.id}/receive/'), status=404)


class StockAdjustmentTest(BusinessTestBase):
    def test_hospital_adjustment_reduces_hospital_stock(self):
        material = make_material(district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 10)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 3, 'reason': '过期报废',
        }))
        self.assertEqual(body['data']['type'], 'adjustment')
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 7)

    def test_gov_adjustment_reduces_shelter_stock(self):
        material = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.gov_city)
        self.ok(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 4, 'reason': '损耗',
        }))
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 6)

    def test_gov_adjustment_insufficient(self):
        material = make_material(district=self.district_a, shelter_stock=2)
        self.login_as(self.gov_city)
        self.expect_fail(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 5, 'reason': '损耗',
        }), message='库存不足')

    def test_adjustment_quantity_zero(self):
        material = make_material(district=self.district_a)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}adjustment/',
                                 {'material_id': material.id, 'quantity': 0}),
                  message='异动数量必须大于0')

    def test_hospital_adjustment_cannot_go_negative(self):
        """医院侧库存由流水累加，异动必须自行校验余额，不能扣成负数。"""
        material = make_material(district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 3)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 5, 'reason': '过期报废',
        }), message='医院库存不足')
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 3)
        self.assertFalse(MaterialTransaction.objects.filter(
            material=material, type='adjustment').exists())

    def test_adjustment_cross_district_material_404(self):
        material = make_material(district=self.district_b, shelter_stock=10)
        # 异动接口限医院/监管部门/捕捉点；用甲区监管操作乙区物料应被拒
        self.login_as(self.gov_a)
        self.expect_fail(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 1, 'reason': '损耗',
        }), status=404, message='无权访问')

    # ---------- 捕捉点库存异动（第三十七轮补的权限） ----------
    #
    # 背景：`expiry_date` 判据上线后，捕捉点库存里的过期物料**既不能用于
    # 诊疗、也不能下发**（下发时判据会拦）。而 `stock_adjustment` 原先的
    # 角色白名单里**没有 shelter** —— 于是那些过期物料成了「既不能报废、
    # 也不能用」的死库存。本轮把 shelter 补进白名单，这几条用例钉住
    # 「补上了」且「没顺手放宽范围」。

    def test_shelter_adjustment_reduces_shelter_stock(self):
        """捕捉点现在能自己报废过期物料 —— 这是它唯一的正规出口。"""
        material = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 4, 'reason': '过期报废',
        }))
        self.assertEqual(body['data']['type'], 'adjustment')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 6)

    def test_shelter_adjustment_cannot_go_negative(self):
        """捕捉点侧余额闸门（`adjust_stock` 内的**前置**判据）必须真的生效。

        ⚠ 放开角色**不等于**放开余额 —— 两道闸门缺一不可。且判据必须在
        建流水**之前**，否则会留下「库存没扣、流水已写」的孤儿记录。
        """
        material = make_material(district=self.district_a, shelter_stock=2)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 5, 'reason': '过期报废',
        }), message='捕捉点库存不足')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 2)
        self.assertFalse(
            MaterialTransaction.objects.filter(
                material=material, type='adjustment').exists(),
            '库存不足时不能留下孤儿流水')

    def test_shelter_adjustment_cross_district_404(self):
        """甲区捕捉点不能异动乙区物料 —— 角色放开不等于**范围**放开。"""
        material = make_material(district=self.district_b, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 1, 'reason': '损耗',
        }), status=404, message='无权访问')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 10)

    def test_shelter_adjustment_writes_traceable_transaction(self):
        """异动必须留下可追溯的流水（谁、扣了多少、为什么）。"""
        material = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 3, 'reason': '过期报废',
        }))
        txn = MaterialTransaction.objects.get(material=material,
                                              type='adjustment')
        self.assertEqual(txn.quantity, 3)
        self.assertEqual(txn.operator_id, self.shelter_user_a.id)
        self.assertIn('过期报废', txn.note)
        self.assertIsNone(txn.hospital_id, '捕捉点侧异动不该挂到医院名下')


class LedgerViewsTest(BusinessTestBase):
    def test_transactions_filter(self):
        material = make_material(district=self.district_a)
        MaterialTransaction.objects.create(material=material, quantity=1, type='purchase',
                                           district=self.district_a,
                                           date=date.today())
        MaterialTransaction.objects.create(material=material, quantity=2, type='dispatch',
                                           district=self.district_a,
                                           date=date.today())
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(URL + 'transactions/?type=dispatch'))['data']
        self.assertEqual({t['type'] for t in data}, {'dispatch'})

    def test_hospital_transactions_scoped(self):
        material = make_material(district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 1)
        make_hospital_txn(material, self.hospital_b, 'receive', 2)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json(URL + 'transactions/'))['data']
        self.assertEqual({t['hospital_id'] for t in data}, {self.hospital_a.id})

    def test_shelter_ledger_contains_purchase_and_dispatch(self):
        material = make_material(district=self.district_a)
        purchase = MaterialTransaction.objects.create(
            material=material, quantity=1, type='purchase', district=self.district_a,
            date=date.today(), ledger_no='PUR-T1')
        dispatch = MaterialTransaction.objects.create(
            material=material, quantity=2, type='dispatch', district=self.district_a,
            hospital=self.hospital_a, date=date.today(),
            ledger_no='DIS-T1')
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(URL + 'shelter-ledger/'))['data']
        ids = {t['id'] for t in data}
        self.assertIn(purchase.id, ids)
        self.assertIn(dispatch.id, ids)

    def test_shelter_ledger_district_isolation(self):
        """BUG-FIX 期望：捕捉点台账不应泄露其他区县数据（原实现 | 合并破坏过滤）。"""
        mat_a = make_material(district=self.district_a)
        mat_b = make_material(district=self.district_b)
        MaterialTransaction.objects.create(material=mat_b, quantity=9, type='purchase',
                                           district=self.district_b,
                                           date=date.today())
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(URL + 'shelter-ledger/'))['data']
        self.assertEqual([t['district_id'] for t in data], [self.district_a.id] * len(data))

    def test_hospital_ledger_scoped(self):
        material = make_material(district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 1)
        make_hospital_txn(material, self.hospital_b, 'receive', 2)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json(URL + 'hospital-ledger/'))['data']
        self.assertEqual({t['hospital_id'] for t in data}, {self.hospital_a.id})
