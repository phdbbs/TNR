"""物料供应链双台账视图测试：采购/下发/签收/异动/流水/芯片。"""
from datetime import date

from django.utils import timezone

from business.models import Chip, Material, MaterialTransaction
from business.services import get_hospital_stock
from business.services_chip import CHIP_BATCH_MAX
from business.tests.base import (
    assert_doc_no,
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
        assert_doc_no(self, txn.ledger_no, 'PUR')

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

    def test_purchase_chip_batch_creates_chips(self):
        """芯片采购按「首号 + 数量」**逐号**建行（第四十六轮）。

        原实现传的是 `chip_range_start` / `chip_range_end` 两个号段字符串，
        而且非数字号段会**静默 `return`** —— 库存加了、芯片一颗没建，
        接口照样回「采购入库成功，增加库存 10」。
        现在号码个数必须等于数量，且每一颗都带持有机构与所属物料。
        """
        material = make_material(category='chip', district=self.district_a)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 5,
            'chip_start': '9000000001',
        }))
        nums = sorted(Chip.objects.filter(material=material)
                      .values_list('number', flat=True))
        self.assertEqual(nums, ['9000000001', '9000000002', '9000000003',
                                '9000000004', '9000000005'])
        chip = Chip.objects.get(number='9000000001')
        self.assertEqual(chip.status, 'available')
        self.assertEqual(chip.institution, self.shelter_user_a.institution,
                         '新入库的芯片必须归属入库机构')
        self.assertTrue(chip.batch_no, '应记下入库批次号（采购单号）')

    def test_purchase_chip_non_numeric_start_rejected(self):
        """非数字首号必须 400 —— **不能静默成功**。

        这是原实现最隐蔽的缺陷：`_create_chip_range()` 解析失败时直接
        `return`，调用方浑然不觉。这条是**反向对照** —— 没有它的话，
        把判据写成「一律拒绝」也能让上一条通过。
        """
        material = make_material(category='chip', district=self.district_a)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 5,
            'chip_start': 'ABC-001',
        }), message='必须是纯数字')
        self.assertFalse(Chip.objects.filter(material=material).exists(),
                         '校验失败时不该建出任何芯片')

    def test_purchase_chip_batch_over_limit_rejected(self):
        """单批数量有上限 —— 防「把数量误打成十万」建出十万行。"""
        material = make_material(category='chip', district=self.district_a)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': CHIP_BATCH_MAX + 1,
            'chip_start': '9000000001',
        }), message='单批最多')
        self.assertFalse(Chip.objects.filter(material=material).exists())

    def test_purchase_chip_duplicate_number_rejected_no_orphan(self):
        """芯片重号 → 400 + 具体号码；且失败时不留下孤儿物料行。

        原实现：采购事务没有 `try`（下发/异动都有），`chip_batch_create`
        的重号 `ValueError` 直接打成 500；且「按名称新建物料」在事务外、
        autocommit 已提交 —— 失败后留下一个从未入过库的物料行。
        """
        first = make_material(category='chip', district=self.district_a)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': first.id, 'quantity': 2, 'chip_start': '9100000001',
        }))

        resp = self.post_json(f'{URL}purchase/', {
            'name': '重号芯片测试', 'category': 'chip', 'quantity': 2,
            'chip_start': '9100000001',
        })
        self.expect_fail(resp, message='芯片号已存在')
        self.assertFalse(
            Material.objects.filter(name='重号芯片测试').exists(),
            '采购失败的物料行不应留下（孤儿物料）')

    def test_hospital_cannot_purchase(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}purchase/', {'quantity': 1}), status=403)


class DispatchTest(BusinessTestBase):
    def _material(self, stock=50):
        return make_material(district=self.district_a, shelter_stock=stock)

    def _chip_material(self, stock=50):
        """芯片物料 —— 第四十六轮的「首号 + 数量」下发只对 `category='chip'` 生效。"""
        return make_material(category='chip', district=self.district_a,
                             shelter_stock=stock)

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
        assert_doc_no(self, txn.ledger_no, 'DIS')

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
        """已使用的芯片不能下发。"""
        material = self._chip_material()
        make_chip(number='9000000001', status='used', material=material,
                  institution=self.shelter_user_a.institution)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 1, 'chip_start': '9000000001',
        }), message='不可用')

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

    def test_dispatch_chip_marks_in_transit(self):
        """下发成功后这批芯片必须变「在途」并挂到下发单上（第四十六轮）。

        原实现只往备注里拼了一句「芯片号段 …」，芯片归属**压根没动** ——
        下发了 1 颗，发起方库存扣了 1，而那颗芯片仍挂在他名下、状态还是
        「可用」，于是同一个人可以把它**再下发一次**。
        """
        material = self._chip_material()
        chip = make_chip(number='9000000001', material=material,
                         institution=self.shelter_user_a.institution)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 1, 'chip_start': '9000000001',
        }))
        chip.refresh_from_db()
        self.assertEqual(chip.status, 'in_transit')
        self.assertEqual(chip.dispatch_txn_id, body['data']['id'],
                         '在途芯片必须能反查回下发单（签收时靠它找号码）')

    def test_dispatch_chip_of_other_institution_rejected(self):
        """别的机构手上的芯片不能下发（归属校验）。

        这条也是**反向对照**：上一条证明「本院的能发出去」，
        这一条证明「别院的不行」—— 只测一边的话，把判据写成
        「一律放行」同样能通过。
        """
        material = self._chip_material()
        make_chip(number='9000000001', material=material,
                  institution=self.hospital_a)  # 归属医院，不是本捕捉点
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 1, 'chip_start': '9000000001',
        }), message='不属于')

    def test_dispatch_chip_of_other_material_rejected(self):
        """同一机构、但不是该物料的芯片不能下发。

        不校验的话，「从 A 批的号码下发 B 物料」会扣掉 B 物料的库存，
        而 A 批的芯片被标成在途 —— 两条物料的账当场错开。
        """
        material = self._chip_material()
        other = make_material(category='chip', district=self.district_a)
        make_chip(number='9000000001', material=other,
                  institution=self.shelter_user_a.institution)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 1, 'chip_start': '9000000001',
        }), message='不属于物料')

    def test_dispatch_missing_chip_number_rejected(self):
        """号码不存在必须 400，且**扣库存前**就拦下（不能扣了库存再报错）。"""
        material = self._chip_material()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 1, 'chip_start': '9000000009',
        }), message='不存在')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 50, '被拒的下发不该动库存')


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

    def test_receive_rejects_chip_count_mismatch(self):
        """在途芯片数与下发数量不一致时**拒绝签收**。

        构造：下发 2 颗，然后人为把其中 1 颗的 `dispatch_txn` 清掉
        （模拟「有人绕过接口直接改了库」）。此时在途只剩 1 颗，
        与下发单的 2 不符 —— 照单全收会让「芯片数 vs 库存数」的错开
        被**合法化**，事后无从追。所以必须拒，且拒得干净（库存不加）。
        """
        material = make_material(category='chip', district=self.district_a,
                                 shelter_stock=10)
        for n in ('9000000001', '9000000002'):
            make_chip(number=n, material=material,
                      institution=self.shelter_user_a.institution)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 2, 'chip_start': '9000000001',
        }))
        # 人为破坏：把其中一颗从下发单上摘掉
        Chip.objects.filter(number='9000000002').update(dispatch_txn=None)
        txn = MaterialTransaction.objects.get(material=material, type='dispatch')

        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}{txn.id}/receive/'),
                         message='不一致')
        self.assertFalse(
            MaterialTransaction.objects.filter(material=material,
                                               type='receive').exists(),
            '被拒的签收不该留下接收单')
        self.assertEqual(
            Chip.objects.filter(dispatch_txn=txn).count(), 1,
            '被拒的签收不该改动在途芯片')


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

    def test_gov_cannot_adjust_stock(self):
        """政府端只读（第四十九轮）：监管不再异动库存。

        减量、余额闸门、跨区县不暴露存在性这三条判据，原来是用政府角色跑的
        （见 `test_gov_adjustment_*`），现在由 `test_shelter_adjustment_*` 用
        合法角色逐条钉住。这里只补一条：**两个**政府角色都被挡在角色闸门外，
        且断言库存与流水**一格没动** —— 角色闸门必须跑在建流水之前，
        只断 403 抓不到「先落库再拒绝」这类半截写。
        """
        material = make_material(district=self.district_a, shelter_stock=10)
        for user in (self.gov_city, self.gov_a):
            self.login_as(user)
            self.expect_fail(self.post_json(f'{URL}adjustment/', {
                'material_id': material.id, 'quantity': 4, 'reason': '损耗',
            }), status=403)
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 10)
        self.assertFalse(MaterialTransaction.objects.filter(
            material=material, type='adjustment').exists())

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

    # ------------------------------------------------------------------
    # 芯片异动（第四十六轮）：库存减了，**号码也必须跟着作废**
    # ------------------------------------------------------------------
    def _chip_material(self, stock=10):
        return make_material(category='chip', district=self.district_a,
                             shelter_stock=stock)

    def test_chip_adjustment_voids_numbers(self):
        """芯片报废必须把对应号码标为 `void`。

        这是第四十六轮补上的洞：库存异动会让 `MaterialStock` 减掉 N，
        而芯片行原来**不动** —— 「可用芯片数」于是比库存多出 N 且不报错。
        """
        material = self._chip_material()
        for n in ('9000000001', '9000000002', '9000000003'):
            make_chip(number=n, material=material,
                      institution=self.shelter_user_a.institution)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 2, 'reason': '芯片损坏报废',
            'chip_start': '9000000001',
        }))
        self.assertIn('已作废', body['message'])
        self.assertEqual(
            list(Chip.objects.filter(status='void').order_by('number')
                 .values_list('number', flat=True)),
            ['9000000001', '9000000002'])
        self.assertEqual(Chip.objects.get(number='9000000003').status,
                         'available')

    def test_chip_adjustment_other_institution_rejected(self):
        """别家机构的芯片不能作废（**反向对照**：证明上面的放行不是「一律放行」）。"""
        material = self._chip_material()
        make_chip(number='9000000001', material=material,
                  institution=self.hospital_a)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 1,
            'chip_start': '9000000001',
        }), message='不属于')
        self.assertEqual(Chip.objects.get(number='9000000001').status, 'available')

    def test_chip_adjustment_rejects_non_available(self):
        """已使用的芯片不能再作废一次（否则库存被重复扣）。"""
        material = self._chip_material()
        make_chip(number='9000000001', status='used', material=material,
                  institution=self.shelter_user_a.institution)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 1,
            'chip_start': '9000000001',
        }), message='不可作废')

    def test_chip_adjustment_rolls_back_stock_on_bad_number(self):
        """号码对不上时**库存也不能扣**（半截状态必须整块回滚）。

        ⚠ 这是最要紧的一条：接口原来「先扣库存、再校验号码」的话，
        用户看到 400，而库存已经实实在在少了 —— 且没有任何提示。
        """
        material = self._chip_material(stock=10)
        make_chip(number='9000000001', material=material,
                  institution=self.shelter_user_a.institution)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}adjustment/', {
            'material_id': material.id, 'quantity': 2,
            'chip_start': '9000000001',  # 只有 1 颗，数量 2 不够
        }), message='不存在')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 10, '被拒的异动不该动库存')
        self.assertFalse(
            MaterialTransaction.objects.filter(material=material,
                                               type='adjustment').exists(),
            '被拒的异动不该留下流水')


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
