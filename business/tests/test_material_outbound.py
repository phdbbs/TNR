"""物料出库（报废 / 退回 / 调拨 / 盘点）回归测试。

需求原文（磊哥）：
    「物料正常使用消耗，还有出库功能，选择出库数量、批号、出库原因
      （报废、退回、调拨、盘点）。出库数量可以为负，表示实际入库或者盘盈」

覆盖面（与实现一一对应）：
- 服务层：`stock_delta` 对 outbound 的方向；`validate_outbound_reason` 三态；
  定向批号**只扣那一批**、余量不足报错且**不改扣别的批次**；负数量加库存
  并记回指定批号；余额判据只在正数时生效；
- 对账等式：出库后 Σ批次 == MaterialStock.quantity == 流水合计（改一个字段
  会改变一批账号可见范围，这类字段必须有对账判据盯着）；
- 存量漂移判据：`stock_init.ledger_total` 的类型清单必须与
  `INBOUND_TXN_TYPES + OUTBOUND_TXN_TYPES` 逐项一致 —— 那是手抄副本，
  新增类型漏抄会让期初算错，而错误**不报任何警**。

成对反向对照（项目约定）：每个「拦」的用例都配一个「放」的对照 ——
只测拦截不测放行，拦过头了测试照样全绿。
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from business.models import Material, MaterialBatch, MaterialStock, MaterialTransaction
from business.services import (
    INBOUND_TXN_TYPES, OUTBOUND_TXN_TYPES, adjust_stock, get_institution_stock,
    parse_signed_int_param,
)
from business.services_stock import (
    validate_outbound_reason,
)
from business.tests.base import (
    assert_doc_no, BusinessTestBase, make_institution, make_material,
)


def _purchase(material, institution, qty, batch_no, expiry=None):
    """测试助手：走生产同一条入库路径（adjust_stock）建批次。"""
    txns = adjust_stock(
        material=material, hospital=None, quantity=qty, txn_type='purchase',
        institution=institution, batch_no=batch_no, expiry_date=expiry,
        ledger_no=f'PUR-{batch_no}')
    return txns[0]


# ============================================================
# 1. 方向与参数解析
# ============================================================
class OutboundDirectionTest(TestCase):
    """`stock_delta` / `parse_signed_int_param` 的纯函数判据。"""

    def test_outbound_is_registered_in_both_direction_tables(self):
        """新类型必须**同时**登记进 INBOUND/OUTBOUND 常量表。

        漏登记的后果不是报错，而是 `stock_delta()` 抛 ValueError ——
        在生产路径上就是「点出库按钮服务器 500」，而且只在真正用到时才发现。
        """
        self.assertIn('outbound', OUTBOUND_TXN_TYPES)
        self.assertNotIn('outbound', INBOUND_TXN_TYPES)

    def test_positive_outbound_reduces_stock(self):
        from business.services import stock_delta
        self.assertEqual(stock_delta('outbound', 5), -5)

    def test_negative_outbound_increases_stock(self):
        """负数量 = 盘盈 / 退回入库，方向上必须反过来。

        不需要在实现里为负数特判：`-quantity` 对负数天然给出正 delta。
        这条用例钉住「没有特判」这件事本身 —— 一旦有人给 outbound 加了
        `abs()`，盘盈就会变成扣库存，而库存数字照样「看起来合理」。
        """
        from business.services import stock_delta
        self.assertEqual(stock_delta('outbound', -3), 3)


class ParseSignedIntParamTest(TestCase):
    """带符号数量解析：与 `parse_int_param` 逐条对齐，只多一个前导 '-'。"""

    def test_negative_accepted(self):
        value, err = parse_signed_int_param('-5', '出库数量')
        self.assertIsNone(err)
        self.assertEqual(value, -5)

    def test_positive_accepted(self):
        value, err = parse_signed_int_param('5', '出库数量')
        self.assertIsNone(err)
        self.assertEqual(value, 5)

    def test_bare_minus_is_not_a_number(self):
        value, err = parse_signed_int_param('-', '出库数量')
        self.assertIsNone(value)
        self.assertIsNotNone(err)

    def test_double_minus_rejected(self):
        value, err = parse_signed_int_param('--5', '出库数量')
        self.assertIsNone(value)
        self.assertIsNotNone(err)

    def test_non_numeric_rejected(self):
        for raw in ('abc', '5.5', '1e3', ''):
            value, err = parse_signed_int_param(raw, '出库数量')
            self.assertIsNone(value, f'{raw!r} 不该被解析成数字')

    def test_zero_is_a_valid_number(self):
        """0 是合法数字，「业务上必须非 0」由调用方判。

        解析器不应替业务把 0 也拒了 —— 那样错误文案会说「不是数字」，
        而真正想说的是「不能为 0」，两件事不能混。
        """
        value, err = parse_signed_int_param('0', '出库数量')
        self.assertIsNone(err)
        self.assertEqual(value, 0)

    def test_absent_is_none_state(self):
        value, err = parse_signed_int_param(None, '出库数量')
        self.assertIsNone(value)
        self.assertIsNone(err)

    def test_oversized_rejected(self):
        value, err = parse_signed_int_param('9' * 25, '出库数量')
        self.assertIsNone(value)
        self.assertIn('范围', err)

    def test_direction_is_preserved_for_oversized_negative(self):
        """反向对照：负数方向不能被 `len` 判断吞掉。"""
        value, err = parse_signed_int_param('-999', '出库数量')
        self.assertIsNone(err)
        self.assertEqual(value, -999)


class ValidateOutboundReasonTest(TestCase):
    """出库原因三态：合法 / 非法 / 未提供。"""

    def test_all_four_reasons_accepted(self):
        for reason in ('scrap', 'return', 'transfer', 'inventory'):
            value, err = validate_outbound_reason(reason)
            self.assertIsNone(err, f'{reason} 应当合法')
            self.assertEqual(value, reason)

    def test_unknown_reason_rejected(self):
        value, err = validate_outbound_reason('lost')
        self.assertIsNone(value)
        self.assertIsNotNone(err)

    def test_free_text_rejected(self):
        """自由文本**必须**被拒 —— 四选一枚举就是为了让台账能按原因汇总。"""
        value, err = validate_outbound_reason('外包装破损')
        self.assertIsNone(value)
        self.assertIsNotNone(err)

    def test_empty_reason_rejected(self):
        """空串 / None / 纯空格都算「没填」，一律拒。"""
        for raw in (None, '', '   '):
            value, err = validate_outbound_reason(raw)
            self.assertIsNone(value, f'{raw!r} 不该被当成合法原因')
            self.assertIsNotNone(err)

    def test_reason_table_matches_model_choices(self):
        """服务层白名单与模型 choices 必须同源（孪生漂移判据）。"""
        model_reasons = {r for r, _ in MaterialTransaction.OUTBOUND_REASON_CHOICES}
        service_reasons = set()
        for reason in ('scrap', 'return', 'transfer', 'inventory'):
            value, err = validate_outbound_reason(reason)
            self.assertIsNone(err, f'{reason} 应当合法')
            service_reasons.add(value)
        self.assertEqual(model_reasons, service_reasons)


# ============================================================
# 2. 批号定向扣减
# ============================================================
class DirectedBatchDeductionTest(BusinessTestBase):
    """`adjust_stock(txn_type='outbound', batch_no=...)` 的批次判据。

    核心约束：**指定了哪一批就只扣哪一批**。静默改扣别的批次 =
    台账批号与实际出库的货对不上，而这个错不会报任何警。
    """

    def _material_with_two_batches(self):
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 10, '近效期',
                  timezone.localdate() + timedelta(days=5))
        _purchase(m, self.shelter_a, 30, '远效期',
                  timezone.localdate() + timedelta(days=200))
        return m

    def test_directed_batch_deducts_only_that_batch(self):
        """正向：指定「远效期」→ 扣它，哪怕「近效期」在 FEFO 更前排。"""
        m = self._material_with_two_batches()
        txns = adjust_stock(
            m, None, 20, 'outbound', institution=self.shelter_a,
            batch_no='远效期', reason='transfer', ledger_no='OUT-1')
        self.assertEqual(len(txns), 1, '定向批号不应拆行')
        self.assertEqual(txns[0].batch_no, '远效期')
        self.assertEqual(txns[0].quantity, 20)
        qtys = {b.batch_no: b.quantity
                for b in MaterialBatch.objects.filter(material=m)}
        self.assertEqual(qtys, {'近效期': 10, '远效期': 10})

    def test_directed_batch_insufficient_does_not_fall_back(self):
        """反向对照：指定批次余量不足 → 报错，**不**改扣别的批次。"""
        m = self._material_with_two_batches()
        with self.assertRaises(ValueError) as ctx:
            adjust_stock(
                m, None, 25, 'outbound', institution=self.shelter_a,
                batch_no='近效期', reason='scrap', ledger_no='OUT-2')
        self.assertIn('近效期', str(ctx.exception))
        # 关键：两个批次行**一个都没动**
        qtys = {b.batch_no: b.quantity
                for b in MaterialBatch.objects.filter(material=m)}
        self.assertEqual(qtys, {'近效期': 10, '远效期': 30})
        self.assertEqual(
            MaterialTransaction.objects.filter(type='outbound').count(), 0,
            '批次不足时不能留下孤儿流水')

    def test_directed_batch_unknown_batch_no_rejected(self):
        """批号不存在 → 报错，不能「按主表快照写一行流水、批次一个没动」。"""
        m = self._material_with_two_batches()
        with self.assertRaises(ValueError) as ctx:
            adjust_stock(
                m, None, 5, 'outbound', institution=self.shelter_a,
                batch_no='不存在的批', reason='scrap', ledger_no='OUT-3')
        self.assertIn('不存在的批', str(ctx.exception))
        self.assertEqual(
            MaterialTransaction.objects.filter(type='outbound').count(), 0)

    def test_no_batch_no_falls_back_to_fefo(self):
        """不给批号 → 走 FEFO（与 dispatch 同口径，近效期先出）。"""
        m = self._material_with_two_batches()
        txns = adjust_stock(
            m, None, 15, 'outbound', institution=self.shelter_a,
            reason='inventory', ledger_no='OUT-4')
        self.assertEqual([(t.batch_no, t.quantity) for t in txns],
                         [('近效期', 10), ('远效期', 5)])
        qtys = {b.batch_no: b.quantity
                for b in MaterialBatch.objects.filter(material=m)}
        self.assertEqual(qtys, {'近效期': 0, '远效期': 25})

    def test_outbound_deducts_expired_batch(self):
        """出库（报废）**不跳过**过期批次 —— 过期物料的正规出口之一。

        反向对照：consume 会跳过。少了这条对照，把 outbound 也改成
        「跳过过期」照样全绿，而死库存被焊死在账上。
        """
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 40, '过期批',
                  timezone.localdate() - timedelta(days=30))
        txns = adjust_stock(
            m, None, 40, 'outbound', institution=self.shelter_a,
            batch_no='过期批', reason='scrap', ledger_no='OUT-5')
        self.assertEqual(txns[0].batch_no, '过期批')
        self.assertEqual(get_institution_stock(m, self.shelter_a), 0)

    def test_directed_outbound_preview_matches_actual(self):
        """预览与落库**同一份判据**：预览说扣「远效期 20」，落库就扣这些。"""
        from business.services import plan_fefo_deduction
        m = self._material_with_two_batches()
        plan = plan_fefo_deduction(
            m, self.shelter_a, 20, skip_expired=False, batch_no='远效期')
        self.assertEqual([(b.batch_no, take) for b, take in plan],
                         [('远效期', 20)])


# ============================================================
# 3. 负数量（盘盈 / 退回入库）
# ============================================================
class NegativeOutboundTest(BusinessTestBase):
    """负数量 = 入库方向。三条判据：加库存、记回指定批号、不被余额判据拦。"""

    def _material_with_batch(self):
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 10, 'B1',
                  timezone.localdate() + timedelta(days=100))
        return m

    def test_negative_increases_institution_stock(self):
        m = self._material_with_batch()
        adjust_stock(m, None, -3, 'outbound', institution=self.shelter_a,
                     reason='inventory', ledger_no='OUT-N1')
        self.assertEqual(get_institution_stock(m, self.shelter_a), 13)
        self.assertEqual(Material.objects.get(pk=m.pk).shelter_stock, 13)

    def test_negative_credits_the_named_batch(self):
        """申报的是哪一批，就记回哪一批 —— 记到别的批次上等于账实不符。"""
        m = self._material_with_batch()
        _purchase(m, self.shelter_a, 5, 'B2',
                  timezone.localdate() + timedelta(days=50))
        adjust_stock(m, None, -4, 'outbound', institution=self.shelter_a,
                     batch_no='B2', reason='return', ledger_no='OUT-N2')
        qtys = {b.batch_no: b.quantity
                for b in MaterialBatch.objects.filter(material=m)}
        self.assertEqual(qtys, {'B1': 10, 'B2': 9})

    def test_negative_without_batch_credits_fefo_first(self):
        """没有指定批号 → 退 FEFO 最前排（近似还原）。"""
        m = self._material_with_batch()
        adjust_stock(m, None, -4, 'outbound', institution=self.shelter_a,
                     reason='inventory', ledger_no='OUT-N3')
        qtys = {b.batch_no: b.quantity
                for b in MaterialBatch.objects.filter(material=m)}
        self.assertEqual(qtys, {'B1': 14})

    def test_negative_not_blocked_by_balance_check(self):
        """负数量**不能**被余额判据拦 —— 它是加库存，方向整个是反的。

        少了 `quantity > 0` 这个限定时，「盘盈必填负数」会被自己的下限拦住，
        而错误文案会说「库存不足」。
        """
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 2, 'B1',
                  timezone.localdate() + timedelta(days=100))
        txns = adjust_stock(
            m, None, -8, 'outbound', institution=self.shelter_a,
            reason='inventory', ledger_no='OUT-N4')
        self.assertEqual(txns[0].quantity, -8)
        self.assertEqual(get_institution_stock(m, self.shelter_a), 10)

    def test_positive_still_blocked_when_insufficient(self):
        """成对反向对照：正数出库余额不足时**必须**拦。

        ⚠ 断言**不能只停在 assertRaises**：余额判据和批次判据都会抛
        ValueError，只写 `assertRaises(ValueError)` 的话，把余额判据
        整段删掉测试照样绿（实测过 —— 批次判据的文案「批次库存不足」
        里也含「库存不足」这个子串）。
        这里断言机构名：只有 `adjust_stock` 的余额判据会把它写进文案
        （`f'{inst.name}库存不足（当前 …）'`），批次判据写的是物料名。
        """
        m = self._material_with_batch()
        with self.assertRaises(ValueError) as ctx:
            adjust_stock(m, None, 50, 'outbound', institution=self.shelter_a,
                         reason='scrap', ledger_no='OUT-N5')
        self.assertIn(self.shelter_a.name, str(ctx.exception))
        self.assertIn('当前 10', str(ctx.exception))
        self.assertEqual(get_institution_stock(m, self.shelter_a), 10)


# ============================================================
# 4. 对账等式与存量漂移
# ============================================================
class OutboundLedgerParityTest(BusinessTestBase):
    """出库之后三个数必须仍然相等：Σ批次 / 机构库存 / 流水合计。"""

    def test_three_way_equality_after_outbound(self):
        from business.services import get_institution_ledger_total
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 20, 'B1',
                  timezone.localdate() + timedelta(days=5))
        _purchase(m, self.shelter_a, 20, 'B2',
                  timezone.localdate() + timedelta(days=200))
        adjust_stock(m, None, 10, 'outbound', institution=self.shelter_a,
                     batch_no='B1', reason='scrap', ledger_no='OUT-P1')
        adjust_stock(m, None, -5, 'outbound', institution=self.shelter_a,
                     reason='inventory', ledger_no='OUT-P2')
        stock = MaterialStock.objects.get(material=m, institution=self.shelter_a)
        batch_total = sum(b.quantity for b in
                          MaterialBatch.objects.filter(material=m))
        ledger = get_institution_ledger_total(m, self.shelter_a)
        # 20 + 20 采购 = 40；出库 -10；盘盈 +5 → 35
        self.assertEqual(batch_total, 35)
        self.assertEqual(stock.quantity, 35)
        self.assertEqual(ledger, 35)

    def test_ledger_total_counts_outbound(self):
        """`get_institution_ledger_total` 必须把 outbound 算进去。

        它是 `check_data_integrity` 的对账侧；漏算就等于对账基准本身错了，
        账面与台账不一致被它判成「一致」。
        """
        from business.services import get_institution_ledger_total
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 10, 'B1',
                  timezone.localdate() + timedelta(days=100))
        adjust_stock(m, None, 4, 'outbound', institution=self.shelter_a,
                     reason='transfer', ledger_no='OUT-P3')
        self.assertEqual(get_institution_ledger_total(m, self.shelter_a), 6)

    def test_stock_init_ledger_total_covers_every_type(self):
        """**存量漂移判据**：`stock_init.ledger_total` 是常量表的手抄副本。

        新增流水类型时漏抄它，期初会算错，而 `quantity == opening_quantity +
        流水合计` 这条等式**不再成立**，`check_data_integrity` 只报一条
        「机构库存与流水合计不一致」—— 看起来像别的问题。
        这条判据把「抄全了」本身钉住：任一类型不在副本里就红。
        """
        from business import stock_init
        import inspect

        src = inspect.getsource(stock_init.ledger_total)
        missing = [t for t in INBOUND_TXN_TYPES + OUTBOUND_TXN_TYPES
                   if f"'{t}'" not in src]
        self.assertEqual(missing, [],
                         f'stock_init.ledger_total 漏了这些流水类型：{missing}')

    def test_stock_init_handles_outbound(self):
        """正向对照：`init_stock_rows` 见到 outbound 流水要正常建行。"""
        from business import stock_init
        from core.models import Institution as CoreInstitution
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 10, 'B1',
                  timezone.localdate() + timedelta(days=100))
        adjust_stock(m, None, 4, 'outbound', institution=self.shelter_a,
                     reason='scrap', ledger_no='OUT-P4')
        # 清掉库存行再重跑初始化，量应等于流水合计 6
        MaterialStock.objects.filter(material=m).delete()
        MaterialBatch.objects.filter(material=m).delete()
        shelter_rows, hospital_rows = stock_init.init_stock_rows(
            Material, MaterialStock, MaterialTransaction, CoreInstitution)
        self.assertEqual(shelter_rows, 1)
        self.assertEqual(hospital_rows, 0)
        self.assertEqual(get_institution_stock(m, self.shelter_a), 6)


# ============================================================
# 5. 视图层：POST /api/business/materials/outbound/
# ============================================================
OUTBOUND_URL = '/api/business/materials/outbound/'


class OutboundViewTest(BusinessTestBase):
    """出库接口的**业务结果**判据（不停在状态码）。"""

    def _material_with_batches(self):
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 10, '近效期',
                  timezone.localdate() + timedelta(days=5))
        _purchase(m, self.shelter_a, 30, '远效期',
                  timezone.localdate() + timedelta(days=200))
        return m

    # ---------- 正向 ----------

    def test_shelter_outbound_reduces_stock(self):
        m = self._material_with_batches()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 8, 'batch_no': '近效期',
            'reason': 'scrap', 'note': '外包装破损',
        }))
        self.assertEqual(body['data']['type'], 'outbound')
        self.assertEqual(body['data']['reason'], 'scrap')
        self.assertEqual(body['data']['quantity'], 8)
        self.assertEqual(body['data']['batch_no'], '近效期')
        self.assertEqual(get_institution_stock(m, self.shelter_a), 32)
        assert_doc_no(self, body['data']['ledger_no'], 'OUT')

    def test_hospital_outbound_reduces_hospital_stock(self):
        """医院侧出库走 `get_hospital_stock` 现算口径 —— 这是医院的余额真源。"""
        from business.tests.base import make_hospital_txn
        m = make_material(district=self.district_a)
        make_hospital_txn(m, self.hospital_a, 'receive', 12)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 5, 'reason': 'transfer',
        }))
        from business.services import get_hospital_stock
        self.assertEqual(get_hospital_stock(m, self.hospital_a), 7)

    def test_negative_quantity_is_inventory_gain(self):
        """负数量 = 盘盈 / 退回入库，医院库存要**增加**。"""
        from business.tests.base import make_hospital_txn
        from business.services import get_hospital_stock
        m = make_material(district=self.district_a)
        make_hospital_txn(m, self.hospital_a, 'receive', 12)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': -4, 'reason': 'inventory',
        }))
        self.assertEqual(body['data']['quantity'], -4)
        self.assertEqual(get_hospital_stock(m, self.hospital_a), 16)

    def test_negative_not_blocked_when_stock_is_zero(self):
        """仓库空着也能盘盈 —— 这正是负数出库存在的理由。"""
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 1, 'B1',
                  timezone.localdate() + timedelta(days=100))
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': -3, 'reason': 'return',
        }))
        self.assertEqual(get_institution_stock(m, self.shelter_a), 4)

    def test_gov_cannot_outbound(self):
        """政府端只读（第四十九轮）：监管不出库。

        出库的四条正向能力（捕捉点侧、医院侧、负数盘盈、零库存盘盈）都由
        `shelter` / `hospital` 角色各自钉住；这里钉政府角色被挡在门外，
        并且**物料一格没少** —— 只断 403 的话，「先扣库存再拒绝」看不出来。
        """
        m = make_material(district=self.district_a, shelter_stock=10)
        for user in (self.gov_city, self.gov_a):
            self.login_as(user)
            self.expect_fail(self.post_json(OUTBOUND_URL, {
                'material_id': m.id, 'quantity': 4, 'reason': 'transfer',
            }), status=403)
        m.refresh_from_db()
        self.assertEqual(m.shelter_stock, 10)
        self.assertFalse(MaterialTransaction.objects.filter(
            material=m, type='outbound').exists())

    def test_outbound_shows_in_hospital_ledger(self):
        """出库单必须出现在医院台账里 —— 否则它就是一条看得见摸不着的流水。"""
        from business.tests.base import make_hospital_txn
        m = make_material(district=self.district_a)
        make_hospital_txn(m, self.hospital_a, 'receive', 12)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 5, 'reason': 'scrap',
        }))
        rows = self.ok(self.get_json('/api/business/materials/hospital-ledger/'))['data']
        hit = [r for r in rows if r['type'] == 'outbound']
        self.assertEqual(len(hit), 1, '出库单没进医院台账')
        self.assertEqual(hit[0]['reason'], 'scrap')

    # ---------- 校验（每个「拦」都配一个「放」） ----------

    def test_missing_reason_rejected(self):
        """原因四选一**必填** —— 自由文本一份都不收。"""
        m = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 2,
        }), message='出库原因')
        # 放行的对照见 test_shelter_outbound_reduces_stock
        self.assertEqual(
            MaterialTransaction.objects.filter(type='outbound').count(), 0)

    def test_invalid_reason_rejected(self):
        m = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 2, 'reason': '丢失',
        }), message='出库原因')

    def test_zero_quantity_rejected(self):
        """出 0 件既不改变库存也不改变批次，却会生成一条流水和一张单号。"""
        m = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 0, 'reason': 'scrap',
        }), message='不能为0')
        self.assertEqual(
            MaterialTransaction.objects.filter(type='outbound').count(), 0)

    def test_missing_quantity_rejected(self):
        m = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'reason': 'scrap',
        }), message='缺少出库数量')

    def test_non_numeric_quantity_rejected(self):
        m = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 'abc', 'reason': 'scrap',
        }), message='必须是数字')

    def test_oversized_quantity_rejected(self):
        """超大数不能走到 ORM 里才炸（`OverflowError` 500）。"""
        m = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': '9' * 25, 'reason': 'scrap',
        }), message='范围')

    def test_insufficient_stock_rejected_no_orphan(self):
        """余额不足 → 400 且**不留孤儿流水**（判据必须前置到写库前）。

        ⚠ 这里有两个「只测一半就会假绿」的坑，逐条钉住：

        ① **文案断言不能停在子串**：批次判据的文案「批次库存不足」
           也含「库存不足」。只写 `message='库存不足'` 的话，把
           `adjust_stock` 的余额判据整段删掉，这条照样绿 ——
           拦截从「余额层」掉到「批次层」，看着没事，
           实则 `materialize_initial_batches` 这个下游副作用已经跑了
           （见 ②）。所以断言 **`当前 3`**：只有余额判据输出它。

        ② **下游连带写入必须也没发生**：`materialize_initial_batches`
           跑在建流水**之前**，余额判据拦得住时它一行都不该建。
           少了这条断言，拦截点从「余额」漂到「批次」时，
           失败的请求也会在批次表里留下一行「初始批次」。
        """
        m = make_material(district=self.district_a, shelter_stock=3)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 5, 'reason': 'scrap',
        }), message='当前 3')
        m.refresh_from_db()
        self.assertEqual(m.shelter_stock, 3)
        self.assertEqual(
            MaterialTransaction.objects.filter(type='outbound').count(), 0,
            '库存不足时不能留下孤儿流水')
        self.assertEqual(
            MaterialBatch.objects.filter(material=m).count(), 0,
            '余额判据拦住时，批次物化这个下游写入也不该发生')

    def test_hospital_insufficient_stock_rejected(self):
        """医院侧的余额闸门在视图里自己把关（`adjust_stock` 不判医院侧）。"""
        from business.tests.base import make_hospital_txn
        m = make_material(district=self.district_a)
        make_hospital_txn(m, self.hospital_a, 'receive', 2)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 9, 'reason': 'scrap',
        }), message='医院库存不足')
        self.assertEqual(
            MaterialTransaction.objects.filter(type='outbound').count(), 0)

    def test_directed_batch_insufficient_rejected(self):
        """指定批次余量不足 → 400，且其它批次**一个都没动**。"""
        m = self._material_with_batches()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 15, 'batch_no': '近效期',
            'reason': 'scrap',
        }), message='余量不足')
        qtys = {b.batch_no: b.quantity
                for b in MaterialBatch.objects.filter(material=m)}
        self.assertEqual(qtys, {'近效期': 10, '远效期': 30})

    def test_cross_district_material_404(self):
        """甲区捕捉点不能出库乙区物料 —— 角色放开不等于**范围**放开。"""
        m = make_material(district=self.district_b, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 1, 'reason': 'scrap',
        }), status=404, message='无权访问')
        m.refresh_from_db()
        self.assertEqual(m.shelter_stock, 10)

    def test_missing_material_id_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'quantity': 1, 'reason': 'scrap',
        }), message='缺少物料ID')

    def test_malformed_material_id_rejected(self):
        """畸形外键不能进 ORM（`ValueError` → 500）。"""
        m = make_material(district=self.district_a, shelter_stock=10)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(OUTBOUND_URL, {
            'material_id': 'abc', 'quantity': 1, 'reason': 'scrap',
        }), message='必须是数字')

    def test_only_post_allowed(self):
        """GET 不能触发写操作。"""
        self.login_as(self.shelter_user_a)
        resp = self.get_json(OUTBOUND_URL)
        self.assertIn(resp.status_code, (403, 405))

    # ---------- 芯片 ----------

    def test_chip_outbound_voids_numbers(self):
        """芯片出库必须同步作废号码 —— 库存减了、号码不动 = 对不上。"""
        from business.services_chip import chip_batch_create
        # ⚠ `chip_batch_create` 只建 Chip 行、**不建** `MaterialStock`，
        # 所以库存要靠 `shelter_stock` 这个存量字段给（与
        # `test_material_views` 的芯片用例同一套设定）。
        m = make_material(category='chip', district=self.district_a,
                          shelter_stock=5)
        chip_batch_create(material=m, institution=self.shelter_a,
                          first_number='042060107000101', count=5,
                          batch_no='CHIP-OUT')
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 2, 'reason': 'scrap',
            'chip_start': '042060107000101',
        }))
        self.assertEqual(body['data']['type'], 'outbound')
        from business.models import Chip
        voided = Chip.objects.filter(
            material=m, institution=self.shelter_a, status='void').count()
        self.assertEqual(voided, 2)
        self.assertEqual(get_institution_stock(m, self.shelter_a), 3)

    def test_chip_outbound_ignores_batch_no(self):
        """芯片不认批号定向：号码才是真源，扣批次行会让等式失效。"""
        from business.services_chip import chip_batch_create
        m = make_material(category='chip', district=self.district_a,
                          shelter_stock=5)
        chip_batch_create(material=m, institution=self.shelter_a,
                          first_number='042060107000201', count=5,
                          batch_no='CHIP-OUT2')
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(OUTBOUND_URL, {
            'material_id': m.id, 'quantity': 1, 'reason': 'scrap',
            'chip_start': '042060107000201', 'batch_no': '瞎编的批号',
        }))
        # 没有因为「批号不存在」被拒，也没有按那个假批号扣批次
        self.assertEqual(body['data']['quantity'], 1)
        self.assertEqual(get_institution_stock(m, self.shelter_a), 4)

    # ---------- 批次预览 ----------

    def test_batch_preview_supports_outbound_and_directed_batch(self):
        """预览必须支持 outbound + 定向批号，且与落库同判据。"""
        m = self._material_with_batches()
        self.login_as(self.shelter_user_a)
        url = f'/api/business/materials/{m.id}/batch-preview/'
        resp = self.ok(self.post_json(url, {
            'quantity': 20, 'txn_type': 'outbound', 'batch_no': '远效期'}))
        self.assertEqual([(p['batch_no'], p['take']) for p in resp['data']['plan']],
                         [('远效期', 20)])

    def test_batch_preview_outbound_insufficient(self):
        m = self._material_with_batches()
        self.login_as(self.shelter_user_a)
        url = f'/api/business/materials/{m.id}/batch-preview/'
        resp = self.ok(self.post_json(url, {
            'quantity': 15, 'txn_type': 'outbound', 'batch_no': '近效期'}))
        self.assertFalse(resp['data']['enough'])
        self.assertIn('余量不足', resp['data']['message'])

    def test_batch_preview_unknown_txn_type_rejected(self):
        m = self._material_with_batches()
        self.login_as(self.shelter_user_a)
        url = f'/api/business/materials/{m.id}/batch-preview/'
        self.expect_fail(self.post_json(url, {
            'quantity': 5, 'txn_type': 'nope'}), message='txn_type')
