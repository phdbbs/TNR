"""B5 物料批次子表回归测试。

覆盖面（与方案一一对应）：
- FEFO 顺序：近效期先出、无效期垫底；
- 跨批次拆行：同 ledger_no 多笔流水、每笔 batch_no = 所扣批次；
- 过期跳过：consume 跳过已过期批次；dispatch/adjustment 不跳；
- 全过期拦截：consume 报错引导异动报废，adjustment 放行（正规出口）；
- 同批号再入库：累加 + 效期更新；
- 对账等式：Σ批次 == MaterialStock.quantity（业务后恒等）；
- 存量迁移：init_batch_rows（0029 口径）与 materialize_initial_batches
  （运行时结转）两种物化；
- 芯片不拆行：号码管理依赖单行流水（chip_batch_receive 的强绑定校验）；
- 视图层：跨批次下发 + 整单签收（接收方批次带效期）、批次列表/预览接口、
  跨区县权限。

成对反向对照（项目约定）：每个「拦」的用例都配一个「放」的对照 ——
只测拦截不测放行，拦过头了测试照样全绿。
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from business.models import Material, MaterialBatch, MaterialStock, MaterialTransaction
from business.services import (
    adjust_stock, expired_material_error, get_institution_stock,
    get_material_batches, plan_fefo_deduction,
)
from business.stock_init import init_batch_rows
from business.tests.base import (
    BusinessTestBase, make_hospital_txn, make_institution, make_material, make_user,
)


def _purchase(material, institution, qty, batch_no, expiry=None, operator=None):
    """测试助手：走生产同一条入库路径（adjust_stock）建批次。"""
    txns = adjust_stock(
        material=material, hospital=None, quantity=qty, txn_type='purchase',
        institution=institution, operator=operator,
        batch_no=batch_no, expiry_date=expiry, ledger_no=f'PUR-{batch_no}')
    return txns[0]


class BatchServiceTests(BusinessTestBase):
    """服务层：FEFO / 拆行 / 过期跳过 / 对账等式 / 存量物化。"""

    def test_fefo_order_near_expiry_first(self):
        """近效期先出 —— FEFO 的核心顺序。"""
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 50, 'B1', timezone.localdate() + timedelta(days=10))
        _purchase(m, self.shelter_a, 30, 'B2', timezone.localdate() + timedelta(days=200))

        plan = plan_fefo_deduction(m, self.shelter_a, 20)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][0].batch_no, 'B1')
        self.assertEqual(plan[0][1], 20)

    def test_fefo_order_no_expiry_lasts(self):
        """无效期批次垫底 —— 猫粮等耗材不能挡在有过期风险的药品前面。"""
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 50, '无期批', None)
        _purchase(m, self.shelter_a, 30, '有期批', timezone.localdate() + timedelta(days=30))

        plan = plan_fefo_deduction(m, self.shelter_a, 20)
        self.assertEqual(plan[0][0].batch_no, '有期批')

    def test_cross_batch_deduction_splits_ledger_rows(self):
        """跨批次出库拆多笔流水：同 ledger_no、每笔 batch_no = 所扣批次。"""
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 10, 'B1', timezone.localdate() + timedelta(days=10))
        _purchase(m, self.shelter_a, 30, 'B2', timezone.localdate() + timedelta(days=200))

        txns = adjust_stock(
            m, self.hospital_a, 25, 'dispatch', institution=self.shelter_a,
            operator=self.shelter_user_a, ledger_no='DIS-1')
        self.assertEqual(len(txns), 2)
        # FEFO：B1 全部 10 + B2 扣 15
        self.assertEqual([(t.batch_no, t.quantity) for t in txns],
                         [('B1', 10), ('B2', 15)])
        self.assertEqual({t.ledger_no for t in txns}, {'DIS-1'})
        # 批次余量同步扣减
        qtys = {b.batch_no: b.quantity for b in MaterialBatch.objects.filter(material=m)}
        self.assertEqual(qtys, {'B1': 0, 'B2': 15})

    def test_consume_skips_expired_batch(self):
        """消耗跳过已过期批次（不拿过期货诊疗），扣的是有效批次。"""
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 50, '过期批', timezone.localdate() - timedelta(days=1))
        _purchase(m, self.shelter_a, 30, '有效批', timezone.localdate() + timedelta(days=100))

        txns = adjust_stock(
            m, None, 30, 'consume', institution=self.shelter_a,
            operator=self.shelter_user_a, ledger_no='CON-1')
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].batch_no, '有效批')
        qtys = {b.batch_no: b.quantity for b in MaterialBatch.objects.filter(material=m)}
        self.assertEqual(qtys, {'过期批': 50, '有效批': 0})

    def test_consume_all_expired_blocked_but_adjustment_allowed(self):
        """成对反向对照：全部过期时 consume 拦 / adjustment 放（正规出口）。"""
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 40, 'B1', timezone.localdate() - timedelta(days=30))

        with self.assertRaises(ValueError) as ctx:
            plan_fefo_deduction(m, self.shelter_a, 10, skip_expired=True)
        self.assertIn('全部过期', str(ctx.exception))
        self.assertIn('异动', str(ctx.exception))

        # 对照：异动（报废）不跳过过期批次 —— 出口必须通
        txns = adjust_stock(
            m, None, 40, 'adjustment', institution=self.shelter_a,
            operator=self.shelter_user_a, ledger_no='ADJ-1', note='过期报废')
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].batch_no, 'B1')
        self.assertEqual(get_institution_stock(m, self.shelter_a), 0)

    def test_same_batch_reentry_accumulates_and_updates_expiry(self):
        """同批号再次入库 = 同一批货：累加 + 效期随之更新。"""
        m = make_material(district=self.district_a, shelter_stock=0)
        d1 = timezone.localdate() + timedelta(days=100)
        d2 = timezone.localdate() + timedelta(days=150)
        _purchase(m, self.shelter_a, 10, 'B1', d1)
        _purchase(m, self.shelter_a, 20, 'B1', d2)

        batches = MaterialBatch.objects.filter(material=m, institution=self.shelter_a)
        self.assertEqual(batches.count(), 1)
        self.assertEqual(batches[0].quantity, 30)
        self.assertEqual(batches[0].expiry_date, d2)
        self.assertEqual(get_institution_stock(m, self.shelter_a), 30)

    def test_batch_equation_after_mixed_business(self):
        """对账等式：混合业务后 Σ批次 == MaterialStock（B5 的硬判据）。"""
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 10, 'B1', timezone.localdate() + timedelta(days=5))
        _purchase(m, self.shelter_a, 30, 'B2', timezone.localdate() + timedelta(days=200))
        # 出库（跨批拆行）+ 再入库
        adjust_stock(m, self.hospital_a, 25, 'dispatch', institution=self.shelter_a,
                     ledger_no='DIS-1')
        _purchase(m, self.shelter_a, 8, 'B3', timezone.localdate() + timedelta(days=50))

        batch_total = sum(b.quantity for b in MaterialBatch.objects.filter(
            material=m, institution=self.shelter_a))
        stock = MaterialStock.objects.get(material=m, institution=self.shelter_a)
        self.assertEqual(batch_total, stock.quantity)
        self.assertEqual(batch_total, 23)  # 10+30-25+8

    def test_materialize_initial_batches_from_direct_stock(self):
        """存量物化（运行时）：shelter_stock 直写、无批次 → 第一笔业务一起物化。"""
        m = make_material(district=self.district_a, shelter_stock=100)
        self.assertFalse(MaterialBatch.objects.filter(material=m).exists())

        # 第一笔业务（出库 10）触发物化：初始批次 100 - 10 = 90
        adjust_stock(m, None, 10, 'dispatch', institution=self.shelter_a,
                     ledger_no='DIS-1')
        batches = MaterialBatch.objects.filter(material=m, institution=self.shelter_a)
        self.assertEqual(batches.count(), 1)
        self.assertEqual(batches[0].quantity, 90)
        self.assertEqual(batches[0].batch_no, '初始批次')

    def test_init_batch_rows_migration_shape(self):
        """存量物化（0029 迁移口径）：每个库存行拆一条初始批次，不造假流水。"""
        m = make_material(district=self.district_a, shelter_stock=0,
                          batch_no='S-2024', expiry_date=timezone.localdate() + timedelta(days=300))
        adjust_stock(m, None, 60, 'purchase', institution=self.shelter_a,
                     batch_no='S-2024', ledger_no='PUR-1')
        # 乙区捕捉点也有一行（模拟下发签收后的第二机构）
        adjust_stock(m, self.shelter_b, 20, 'receive', institution=self.shelter_b,
                     batch_no='S-2024', ledger_no='RCV-1')

        created = init_batch_rows(Material, MaterialStock, MaterialBatch)
        # 已有批次行的不重复建（幂等），没有的物化
        self.assertEqual(MaterialBatch.objects.filter(
            material=m, institution=self.shelter_a).count(), 1)
        self.assertEqual(created, 0)  # 两个机构都已有批次行

        # 制造「有库存行、无批次行」的存量形态再物化
        MaterialBatch.objects.filter(material=m).delete()
        created = init_batch_rows(Material, MaterialStock, MaterialBatch)
        self.assertEqual(created, 2)
        for b in MaterialBatch.objects.filter(material=m):
            self.assertEqual(b.batch_no, 'S-2024')  # 沿用主表快照批号
            self.assertEqual(b.expiry_date, m.expiry_date)

    def test_chip_dispatch_keeps_single_row(self):
        """芯片出库不拆行：号码管理挂在单条 dispatch 流水上（第四十六轮强绑定）。"""
        from business.services_chip import chip_batch_create, parse_chip_numbers
        m = make_material(category='chip', district=self.district_a, shelter_stock=0)
        first = '900000000000001'
        count = 5
        parse_chip_numbers(first, count)
        _purchase(m, self.shelter_a, count, 'CHIP-1')
        # 同批号再加 5（跨批次场景：批次两行，但流水必须一行）
        _purchase(m, self.shelter_a, count, 'CHIP-2')
        chip_batch_create(material=m, institution=self.shelter_a,
                          first_number=first, count=count * 2, batch_no='CHIP-ALL')

        txns = adjust_stock(
            m, self.hospital_b, 7, 'dispatch', institution=self.shelter_a,
            operator=self.shelter_user_a, ledger_no='DIS-CHIP')
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].quantity, 7)
        # 批次行照常按 FEFO 扣（Σ批次 == 库存等式不破）
        batch_total = sum(b.quantity for b in MaterialBatch.objects.filter(
            material=m, institution=self.shelter_a))
        self.assertEqual(batch_total, 10 - 7)

    def test_legacy_path_without_institution_unchanged(self):
        """兼容路径（无机构归属）：行为与 B5 之前一致 —— 单行流水、不动批次。"""
        m = make_material(district=self.district_a, shelter_stock=100)
        txns = adjust_stock(m, None, 30, 'dispatch')
        self.assertEqual(len(txns), 1)
        self.assertFalse(MaterialBatch.objects.filter(material=m).exists())
        m.refresh_from_db()
        self.assertEqual(m.shelter_stock, 70)

    def test_expired_material_error_batch_level(self):
        """批次级过期判据：老批过期、新批有效 → 放行（旧主表判据会假拦截）。"""
        m = make_material(district=self.district_a, shelter_stock=0)
        _purchase(m, self.shelter_a, 50, '老批', timezone.localdate() - timedelta(days=1))
        _purchase(m, self.shelter_a, 30, '新批', timezone.localdate() + timedelta(days=100))
        # 主表快照已被「最近一次采购」顶新 —— 旧判据放行，但那只是碰巧
        self.assertIsNone(expired_material_error(m, '用于诊疗', institution=self.shelter_a))

        # 反向对照：把新批耗尽，只剩过期批次 → 必须拦
        adjust_stock(m, None, 30, 'consume', institution=self.shelter_a, ledger_no='C1')
        err = expired_material_error(m, '用于诊疗', institution=self.shelter_a)
        self.assertIsNotNone(err)
        self.assertIn('全部过期', err)

    def test_expired_material_error_legacy_fallback(self):
        """主表快照判据兜底：无批次行时行为与 B5 之前完全一致。"""
        m = make_material(district=self.district_a, shelter_stock=10,
                          expiry_date=timezone.localdate() - timedelta(days=2))
        err = expired_material_error(m, '用于诊疗')
        self.assertIsNotNone(err)
        self.assertIn('过期', err)
        # 传了 institution 但没有批次行 → 仍走快照判据
        err2 = expired_material_error(m, '用于诊疗', institution=self.shelter_a)
        self.assertIsNotNone(err2)


class BatchViewTests(BusinessTestBase):
    """视图层：整单签收 / 批次列表 / FEFO 预览 / 权限。"""

    def setUp(self):
        super().setUp()
        self.m = make_material(district=self.district_a, shelter_stock=0)
        self.expiry_near = timezone.localdate() + timedelta(days=10)
        self.expiry_far = timezone.localdate() + timedelta(days=200)
        _purchase(self.m, self.shelter_a, 10, 'B1', self.expiry_near,
                  operator=self.shelter_user_a)
        _purchase(self.m, self.shelter_a, 30, 'B2', self.expiry_far,
                  operator=self.shelter_user_a)

    def test_cross_batch_dispatch_then_receive_whole_order(self):
        """跨批次下发 → 整单签收：2 笔 receive 同单号，接收方批次带效期。"""
        self.login_as(self.shelter_user_a)
        resp = self.post_json('/api/business/materials/dispatch/', {
            'material_id': self.m.id, 'hospital_id': self.hospital_a.id,
            'quantity': 25, 'note': '下发',
        })
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        # 拆成 2 笔 dispatch（B1 全 10 + B2 扣 15）
        dis = MaterialTransaction.objects.filter(type='dispatch', ledger_no=resp.json()['data']['ledger_no'])
        self.assertEqual(dis.count(), 2)

        # 医院签收 → 2 笔 receive 同 RCV 单号；接收方批次 = 发起方批次 + 效期
        self.login_as(self.hospital_user_a)
        resp2 = self.post_json('/api/business/materials/%d/receive/' % dis.first().id, {})
        self.assertEqual(resp2.status_code, 200, resp2.content[:400])
        rcv_no = resp2.json()['data']['ledger_no']
        rcv = MaterialTransaction.objects.filter(type='receive', ledger_no=rcv_no)
        self.assertEqual(rcv.count(), 2)
        self.assertEqual({t.batch_no for t in rcv}, {'B1', 'B2'})
        # 接收方批次行带效期（货到哪、效期跟到哪）
        rx_batches = {b.batch_no: b for b in MaterialBatch.objects.filter(
            material=self.m, institution=self.hospital_a)}
        self.assertEqual(rx_batches['B1'].expiry_date, self.expiry_near)
        self.assertEqual(rx_batches['B2'].expiry_date, self.expiry_far)
        self.assertEqual(get_institution_stock(self.m, self.hospital_a), 25)

        # 重复签收（点另一笔兄弟行）→ 仍被整单判据拦住
        resp3 = self.post_json('/api/business/materials/%d/receive/' % dis.last().id, {})
        self.expect_fail(resp3, 400, message='已签收')

    def test_batches_endpoint_own_institution_fefo_order(self):
        """批次列表：本机构、FEFO 顺序、含过期/临期标记。"""
        self.login_as(self.shelter_user_a)
        resp = self.get_json('/api/business/materials/%d/batches/' % self.m.id)
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        data = resp.json()['data']
        self.assertEqual(data['institution_name'], '甲区捕捉点')
        self.assertEqual([b['batch_no'] for b in data['batches']], ['B1', 'B2'])
        self.assertEqual(data['batches'][0]['near_expiry'], True)
        self.assertEqual(data['batches'][0]['expired'], False)

    def test_batches_endpoint_gov_grouped(self):
        """政府端批次列表：按机构分组（监管视角：货在谁手上）。"""
        self.login_as(self.gov_a)
        resp = self.get_json('/api/business/materials/%d/batches/' % self.m.id)
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        data = resp.json()['data']
        groups = {g['institution_name']: g for g in data['groups']}
        self.assertIn('甲区捕捉点', groups)
        self.assertEqual(len(groups['甲区捕捉点']['batches']), 2)

    def test_batches_endpoint_cross_district_denied(self):
        """跨区县物料不给看批次（与签收/异动同一把权限尺子）。"""
        # 乙区捕捉点用户看甲区物料
        self.login_as(self.shelter_user_b)
        resp = self.get_json('/api/business/materials/%d/batches/' % self.m.id)
        self.expect_fail(resp, 404)

    def test_batch_preview_endpoint(self):
        """FEFO 预览：plan/enough/expired_skipped，consume 与 dispatch 判据不同。"""
        # 造一个过期批次（consume 应跳过）
        _purchase(self.m, self.shelter_a, 40, 'B0',
                  timezone.localdate() - timedelta(days=1), operator=self.shelter_user_a)

        self.login_as(self.shelter_user_a)
        resp = self.post_json('/api/business/materials/%d/batch-preview/' % self.m.id,
                              {'quantity': 20, 'txn_type': 'consume'})
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        data = resp.json()['data']
        self.assertTrue(data['enough'])
        self.assertEqual(data['expired_skipped'], 40)
        # B0 过期被跳过 → 从 B1 开始扣（B1 全 10 + B2 扣 10）
        self.assertEqual([(p['batch_no'], p['take']) for p in data['plan']],
                         [('B1', 10), ('B2', 10)])

        # dispatch 不跳过：从 B0 开始扣
        resp2 = self.post_json('/api/business/materials/%d/batch-preview/' % self.m.id,
                               {'quantity': 20, 'txn_type': 'dispatch'})
        data2 = resp2.json()['data']
        self.assertEqual([(p['batch_no'], p['take']) for p in data2['plan']],
                         [('B0', 20)])

        # 不足：预览与落库同判据（plan 为空 + 文案）
        resp3 = self.post_json('/api/business/materials/%d/batch-preview/' % self.m.id,
                               {'quantity': 999, 'txn_type': 'consume'})
        data3 = resp3.json()['data']
        self.assertFalse(data3['enough'])
        self.assertIn('不足', data3['message'])

    def test_batch_preview_requires_institution_account(self):
        """预览仅捕捉点/医院可用：政府端没有「本机构库存」概念。"""
        self.login_as(self.gov_a)
        resp = self.post_json('/api/business/materials/%d/batch-preview/' % self.m.id,
                              {'quantity': 5, 'txn_type': 'consume'})
        self.expect_fail(resp, 403)

    def test_purchase_creates_batch_with_expiry(self):
        """采购接口建批次：批号 + 效期从表单进入批次台账。"""
        self.login_as(self.shelter_user_a)
        expiry = (timezone.localdate() + timedelta(days=365)).isoformat()
        resp = self.post_json('/api/business/materials/purchase/', {
            'category': 'vaccine', 'name': '批次采购疫苗', 'quantity': 15,
            'unit': '支', 'batch_no': 'PB-1', 'expiry_date': expiry,
        })
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        m = Material.objects.get(name='批次采购疫苗')
        b = MaterialBatch.objects.get(material=m, institution=self.shelter_a)
        self.assertEqual(b.batch_no, 'PB-1')
        self.assertEqual(b.quantity, 15)
        self.assertEqual(b.expiry_date.isoformat(), expiry)
