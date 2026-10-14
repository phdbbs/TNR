"""诊疗流程（v5.1 三步流程）判据 —— 第四十六轮。

覆盖：开单 / 检查单 / 医嘱处方 / 诊断 / 执行扣库存 / 撤销退回 /
诊疗完成 / 办结改状态 / 看板分组 / 阶段过滤 / 权限收口。

⚠ 判据一律落在**业务结果**上（库存真变了、芯片真绑上了、状态真改了），
不停在状态码 —— 本地全绿 + 生产 200 也可能功能 100% 不可用。

⚠ 反向对照成对：只测「被拒」的话，把判据写成「一律拒绝」也能通过。
凡是有「拦住」的用例，都配一条「正常路径能过」的对照。
"""
from django.test import TestCase

from business import services_treatment as st
from business.models import Chip, MaterialStock, Pet, Treatment, TreatmentItem
from business.services import get_hospital_stock
from business.tests.base import (
    BusinessTestBase, make_chip, make_hospital_txn, make_institution,
    make_material, make_pet, make_user,
)

URL = '/api/business/treatments/'


class FlowBase(BusinessTestBase):
    """诊疗流程用例基类：甲区医院 + 一只在院猫 + 一批药品。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.med_a = make_material(category='medicine', name='头孢曲松钠',
                                  district=cls.district_a)
        cls.med_b = make_material(category='medicine', name='美洛昔康注射液',
                                  district=cls.district_a)
        cls.vaccine = make_material(category='vaccine', name='猫三联',
                                    district=cls.district_a)
        make_hospital_txn(cls.med_a, cls.hospital_a, 'receive', 20)
        make_hospital_txn(cls.med_b, cls.hospital_a, 'receive', 20)
        make_hospital_txn(cls.vaccine, cls.hospital_a, 'receive', 10)

    def setUp(self):
        self.login_as(self.hospital_user_a)
        self.pet = make_pet(status='in_treatment', district=self.district_a,
                            hospital=self.hospital_a, species='猫')

    # ---- 助手 ----
    def _open(self, pet=None):
        resp = self.post_json(URL + 'open/', {'pet_id': (pet or self.pet).id})
        return self.ok(resp)['data']

    def _check(self, tid, submit=True, **kw):
        body = {'temperature': 38.5, 'temperature_flag': 'normal'}
        body.update(kw)
        if not submit:
            body['draft'] = True
        return self.post_json(f'{URL}{tid}/check/', body)

    def _plan(self, tid, items, advice='术后观察精神与食欲'):
        return self.post_json(f'{URL}{tid}/plan/',
                              {'advice': advice, 'items': items})

    def _diagnosed(self, tid, items=None):
        """把单子推到「已诊断」，返回 detail。"""
        self.ok(self._check(tid))
        items = items or [{'key': 'other', 'planned_date': '2026-03-01',
                           'rx': [self.med_a.id]}]
        self.ok(self._plan(tid, items))
        return self.ok(self.post_json(f'{URL}{tid}/diagnose/', {}))['data']

    def _first_item_id(self, detail):
        return detail['items'][0]['id']

    def _stock(self, material):
        return get_hospital_stock(material, self.hospital_a)


# ============================================================
# 选项表：真源只有一份
# ============================================================
class ItemMetaTest(TestCase):
    def test_item_meta_covers_every_choice(self):
        """`ITEM_META` 的 key 集合必须与 `TreatmentItem.ITEM_CHOICES` 完全一致。

        少一个 key = 那个处置项目在界面上**永远没有入口**
        （「枚举值存在、产生点缺失」—— 列表永远空，像「暂时没有数据」）。
        多一个 key = 界面上出现一个后端不认的项目，提交必 400。
        """
        meta_keys = {m['key'] for m in st.ITEM_META}
        choice_keys = {k for k, _ in TreatmentItem.ITEM_CHOICES}
        self.assertEqual(meta_keys, choice_keys)

    def test_every_disposal_maps_to_a_pet_status(self):
        """办结去向的 key 必须都是 `Pet.STATUS_CHOICES` 里真实存在的状态。

        `dispose()` 是把去向 key **直接赋值**给 `pet.status` 的，
        多一个不存在于 choices 的 key，就会写进一个「界面渲染不出来」的状态。
        """
        pet_status = {k for k, _ in Pet.STATUS_CHOICES}
        for key, _ in Treatment.DISPOSAL_CHOICES:
            self.assertIn(key, pet_status)

    def test_test_options_cover_both_species(self):
        for species in ('猫', '狗'):
            options = st.check_options(species)
            self.assertEqual(len(options['infectious_tests']), 3, species)


# ============================================================
# 开单
# ============================================================
class OpenTreatmentTest(FlowBase):
    def test_open_creates_treatment(self):
        detail = self._open()
        self.assertEqual(detail['stage'], 'checking')
        self.assertEqual(detail['petCode'], self.pet.code)
        self.assertFalse(detail['isDisposed'])
        self.assertTrue(detail['ledgerNo'].startswith('TRE'))

    def test_open_reuses_in_progress_treatment(self):
        """重复进入不能开第二张单 —— 否则台账里同一只动物多张空单。"""
        first = self._open()
        second = self._open()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(Treatment.objects.filter(pet=self.pet).count(), 1)

    def test_open_rejects_disallowed_status(self):
        pet = make_pet(status='adopted', district=self.district_a,
                       hospital=self.hospital_a)
        resp = self.post_json(URL + 'open/', {'pet_id': pet.id})
        self.expect_fail(resp, 400, '不可诊疗')

    def test_open_other_hospital_pet_indistinguishable_from_missing(self):
        """别院的动物与「不存在的 id」必须完全不可区分（存在性预言机）。"""
        other = make_institution(type='hospital', district=self.district_a,
                                 name='甲区第二医院')
        foreign = make_pet(status='in_treatment', district=self.district_a,
                           hospital=other)
        r1 = self.post_json(URL + 'open/', {'pet_id': foreign.id})
        r2 = self.post_json(URL + 'open/', {'pet_id': 99999999})
        b1 = self.expect_fail(r1, 404)
        b2 = self.expect_fail(r2, 404)
        self.assertEqual(b1['message'], b2['message'])

    def test_open_signature_snapshot_from_account(self):
        """R46 口径 2：兽医证号与电子章**挂账号**，落库时快照。"""
        self.hospital_user_a.vet_license = '鄂兽医执字第 20210356 号'
        self.hospital_user_a.save(update_fields=['vet_license'])
        self.hospital_a.seal_text = '甲区医院电子章'
        self.hospital_a.save(update_fields=['seal_text'])

        detail = self._open()
        treatment = Treatment.objects.get(id=detail['id'])
        self.assertEqual(treatment.vet_license, '鄂兽医执字第 20210356 号')
        self.assertEqual(treatment.hospital_seal, '甲区医院电子章')

    def test_open_requires_hospital_role(self):
        self.login_as(self.shelter_user_a)
        resp = self.post_json(URL + 'open/', {'pet_id': self.pet.id})
        self.assertEqual(resp.status_code, 403)


# ============================================================
# 第一步：诊疗前检查
# ============================================================
class CheckStepTest(FlowBase):
    def test_draft_does_not_advance_stage(self):
        """暂存**不动** `checked_at` —— 否则「暂存一下就变诊疗中」，
        待接诊列表会莫名其妙清空。"""
        detail = self._open()
        body = self.ok(self._check(detail['id'], submit=False))['data']
        self.assertEqual(body['stage'], 'checking')
        self.assertFalse(body['checkedAt'])

    def test_submit_sets_checked_at(self):
        detail = self._open()
        body = self.ok(self._check(detail['id']))['data']
        self.assertEqual(body['stage'], 'treating')
        self.assertTrue(body['checkedAt'])

    def test_submit_requires_temperature(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature_flag': 'normal'})
        self.expect_fail(resp, 400, '体温检测')

    def test_submit_requires_temperature_flag(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/', {'temperature': 38.5})
        self.expect_fail(resp, 400, '体温判定')

    def test_non_numeric_temperature_rejected(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': '正常', 'temperature_flag': 'normal'})
        self.expect_fail(resp, 400, '必须是数字')

    def test_out_of_range_temperature_rejected(self):
        """405 这种「小数点打漏了」必须拦 —— 存进去只会污染体温趋势。"""
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': 405, 'temperature_flag': 'fever'})
        self.expect_fail(resp, 400, '超出合理范围')

    def test_invalid_temperature_flag_rejected(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': 38.5, 'temperature_flag': 'hot'})
        self.expect_fail(resp, 400, '不合法')

    def test_common_findings_conflict_rejected(self):
        """「无异常」与其它项互斥 —— 前端 chips 拦不住时服务端必须拦。"""
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'common_findings': ['无异常', '有跳蚤']})
        self.expect_fail(resp, 400, '不能与其它')

    def test_common_findings_saved_as_snapshot(self):
        detail = self._open()
        body = self.ok(self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'common_findings': ['体表寄生虫', '有跳蚤']}))['data']
        self.assertEqual(body['commonFindings'], '体表寄生虫、有跳蚤')
        self.assertEqual(body['commonFindingsList'], ['体表寄生虫', '有跳蚤'])

    def test_infectious_test_of_wrong_species_rejected(self):
        """猫的病种传给狗必须 400 —— 否则会存下一个界面上渲染不出来的键。"""
        dog = make_pet(status='in_treatment', district=self.district_a,
                       hospital=self.hospital_a, species='狗')
        detail = self._open(dog)
        resp = self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'infectious_tests': {'fpv': 'neg'}})
        self.expect_fail(resp, 400, '不适用')

    def test_infectious_tests_filled_with_none(self):
        """没传的项补 `none` —— 「没做」必须显式存在，不能靠键缺失暗示。"""
        detail = self._open()
        body = self.ok(self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'infectious_tests': {'fpv': 'neg'}}))['data']
        self.assertEqual(body['infectious']['fpv'], 'neg')
        self.assertEqual(body['infectious']['fhv'], 'none')
        self.assertEqual(body['infectious']['fcv'], 'none')

    def test_base_info_written_back_to_pet(self):
        detail = self._open()
        self.ok(self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'weight': '3.2', 'breed': '中华田园猫'}))
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.weight, '3.2')
        self.assertEqual(self.pet.breed, '中华田园猫')

    def test_check_rejected_after_dispose(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self.ok(self.post_json(f'{URL}{tid}/dispose/', {'disposal': 'pending_adopt'}))
        resp = self._check(tid)
        self.expect_fail(resp, 400, '已办结')


# ============================================================
# 第二步：医嘱与处方
# ============================================================
class PlanStepTest(FlowBase):
    def test_plan_requires_date_when_rx_present(self):
        """「时间与处方互为必填」—— 有处方没日期 = 待补全，拦住。"""
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'other', 'rx': [self.med_a.id]}])
        self.expect_fail(resp, 400, '未填预计执行时间')

    def test_item_without_rx_is_skipped(self):
        """三样全空的项目 = 跳过，不落库 —— 落库就成了「必执行」，
        会把「诊疗完成」永久卡住。"""
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        body = self.ok(self._plan(tid, [
            {'key': 'other', 'planned_date': '2026-03-01', 'rx': [self.med_a.id]},
            {'key': 'deworming', 'planned_date': '2026-03-01'},
        ]))['data']
        keys = {i['itemKey'] for i in body['items']}
        self.assertEqual(keys, {'other'})

    def test_plan_rejects_unknown_item_key(self):
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'massage', 'planned_date': '2026-03-01'}])
        self.expect_fail(resp, 400, '不合法')

    def test_plan_rejects_rx_without_hospital_stock(self):
        """本院没库存的药不能开进处方（原型「无库存 = 不可选」）。"""
        empty = make_material(category='medicine', name='阿托品注射液',
                              district=self.district_a)
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'other', 'planned_date': '2026-03-01',
                                 'rx': [empty.id]}])
        self.expect_fail(resp, 400, '库存为 0')

    def test_plan_rejects_chip_category_as_rx(self):
        """芯片不能当处方药开 —— 它走「芯片号」那条路。"""
        chip_material = make_material(category='chip', name='宠物芯片',
                                      district=self.district_a)
        make_hospital_txn(chip_material, self.hospital_a, 'receive', 5)
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'other', 'planned_date': '2026-03-01',
                                 'rx': [chip_material.id]}])
        self.expect_fail(resp, 400, '不是可开处方的物资')

    def test_plan_rejected_after_diagnosed(self):
        """已诊断必须先「撤回诊断」才能改 —— 否则改完处方再执行，
        库存按新处方扣，账实不符且无痕迹。"""
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)
        resp = self._plan(tid, [{'key': 'other', 'planned_date': '2026-03-01',
                                 'rx': [self.med_a.id]}])
        self.expect_fail(resp, 400, '撤回诊断')

    def test_plan_syncs_legacy_item_flags(self):
        """打印页 / 监管统计 / 政府端列表读的是旧 `items_*` 四个布尔。"""
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        self.ok(self._plan(tid, [
            {'key': 'vaccine', 'planned_date': '2026-03-01', 'rx': [self.vaccine.id]},
            {'key': 'other', 'planned_date': '2026-03-01', 'rx': [self.med_a.id]},
        ]))
        treatment = Treatment.objects.get(id=tid)
        self.assertTrue(treatment.items_vaccine)
        self.assertFalse(treatment.items_sterilization)
        self.assertFalse(treatment.items_chip)
        # `other` 没有对应的旧列，不该被误映射到某一列上
        self.assertFalse(treatment.items_deworming)

    def test_chip_item_requires_chip_no(self):
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'chip', 'planned_date': '2026-03-01'}])
        self.expect_fail(resp, 400, '必须填写芯片号')


class DiagnoseTest(FlowBase):
    def test_diagnose_requires_advice_or_items(self):
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        self.ok(self._plan(tid, [], advice=''))
        resp = self.post_json(f'{URL}{tid}/diagnose/', {})
        self.expect_fail(resp, 400, '至少填写医嘱')

    def test_diagnose_requires_check_submitted(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/diagnose/', {})
        self.expect_fail(resp, 400, '诊疗前检查')

    def test_diagnose_sets_timestamp(self):
        detail = self._open()
        tid = detail['id']
        body = self._diagnosed(tid)
        self.assertTrue(body['diagnosedAt'])

    def test_undiagnose_clears(self):
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)
        body = self.ok(self.post_json(f'{URL}{tid}/undiagnose/', {}))['data']
        self.assertFalse(body['diagnosedAt'])

    def test_undiagnose_blocked_when_item_executed(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        resp = self.post_json(f'{URL}{tid}/undiagnose/', {})
        self.expect_fail(resp, 400, '先撤销执行')


# ============================================================
# 第三步：执行（扣库存）
# ============================================================
class ExecuteStepTest(FlowBase):
    def test_execute_consumes_hospital_stock(self):
        """**业务结果判据**：库存真的少了 1。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        before = self._stock(self.med_a)

        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.assertEqual(self._stock(self.med_a), before - 1)

    def test_execute_marks_item_done(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        body = self.ok(self.post_json(
            f'{URL}{tid}/items/{item_id}/execute/', {}))['data']
        self.assertTrue(body['items'][0]['isDone'])
        self.assertTrue(body['items'][0]['doneAt'])

    def test_execute_twice_rejected(self):
        """重复执行 = 重复扣库存。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {})
        self.expect_fail(resp, 400, '无需重复执行')

    def test_execute_requires_diagnose(self):
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        body = self.ok(self._plan(tid, [
            {'key': 'other', 'planned_date': '2026-03-01', 'rx': [self.med_a.id]}]))['data']
        item_id = self._first_item_id(body)
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {})
        self.expect_fail(resp, 400, '诊断完成')

    def test_insufficient_stock_leaves_nothing_behind(self):
        """两段式：第 2 条药库存不足时，第 1 条**不能**被扣。

        边校验边落库的话，第 1 条已扣、项目未完成（`is_done` 还是 False），
        用户再点一次执行 → 第 1 条被扣两次。
        """
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        self.ok(self._plan(tid, [{
            'key': 'other', 'planned_date': '2026-03-01',
            'rx': [self.med_a.id, self.med_b.id]}]))
        self.ok(self.post_json(f'{URL}{tid}/diagnose/', {}))
        item = TreatmentItem.objects.get(treatment_id=tid)
        # 处方开好之后把 med_b 抽干 —— 模拟「开方后被别处用掉」
        make_hospital_txn(self.med_b, self.hospital_a, 'consume', 20)

        before_a = self._stock(self.med_a)
        resp = self.post_json(f'{URL}{tid}/items/{item.id}/execute/', {})
        self.expect_fail(resp, 400, '库存不足')
        self.assertEqual(self._stock(self.med_a), before_a, '第一条药被扣了')

    def test_execute_chip_binds_pet_and_consumes_material(self):
        """芯片执行：绑档案 + 状态变 used + **芯片物料库存 -1**。"""
        chip_material = make_material(category='chip', name='宠物芯片',
                                      district=self.district_a)
        make_hospital_txn(chip_material, self.hospital_a, 'receive', 5)
        chip = make_chip(number='CHIPFLOW0001', institution=self.hospital_a,
                         material=chip_material)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        before = self._stock(chip_material)

        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))

        chip.refresh_from_db()
        self.pet.refresh_from_db()
        self.assertEqual(chip.status, 'used')
        self.assertEqual(chip.pet_id, self.pet.id)
        self.assertEqual(self.pet.chip_no, chip.number)
        self.assertEqual(self._stock(chip_material), before - 1)

    def test_execute_chip_of_other_hospital_rejected(self):
        other = make_institution(type='hospital', district=self.district_a,
                                 name='甲区第二医院')
        chip = make_chip(number='CHIPFLOW0002', institution=other)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {})
        self.expect_fail(resp, 400, '不属于')

    def test_execute_chip_already_used_rejected(self):
        chip = make_chip(number='CHIPFLOW0003', status='used',
                         institution=self.hospital_a)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {})
        self.expect_fail(resp, 400, '已被使用')

    def test_execute_item_of_other_treatment_404(self):
        """处置项目 id 与诊疗单不匹配时按不存在处理（不做跨单越权）。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        resp = self.post_json(f'{URL}{tid}/items/99999999/execute/', {})
        self.expect_fail(resp, 404)


# ============================================================
# 撤销执行
# ============================================================
class UndoStepTest(FlowBase):
    def test_undo_returns_stock(self):
        """**业务结果判据**：库存真的加回来了。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        before = self._stock(self.med_a)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/undo/', {}))
        self.assertEqual(self._stock(self.med_a), before)

    def test_undo_marks_item_not_done(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        body = self.ok(self.post_json(
            f'{URL}{tid}/items/{item_id}/undo/', {}))['data']
        self.assertFalse(body['items'][0]['isDone'])

    def test_undo_releases_chip(self):
        chip = make_chip(number='CHIPUNDO0001', institution=self.hospital_a)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/undo/', {}))

        chip.refresh_from_db()
        self.pet.refresh_from_db()
        self.assertEqual(chip.status, 'available')
        self.assertIsNone(chip.pet_id)
        self.assertEqual(self.pet.chip_no, '')

    def test_undo_chip_rebound_to_other_pet_rejected(self):
        """芯片已被别的档案绑走时不能退回 —— 否则一颗芯片两条档案。"""
        chip = make_chip(number='CHIPUNDO0002', institution=self.hospital_a)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))

        other_pet = make_pet(status='in_treatment', district=self.district_a,
                             hospital=self.hospital_a)
        chip.pet = other_pet
        chip.save(update_fields=['pet'])

        resp = self.post_json(f'{URL}{tid}/items/{item_id}/undo/', {})
        self.expect_fail(resp, 400, '已绑定其它动物')

    def test_undo_not_executed_rejected(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/undo/', {})
        self.expect_fail(resp, 400, '尚未执行')


# ============================================================
# 诊疗完成 / 办结
# ============================================================
class DoneAndDisposeTest(FlowBase):
    def test_done_requires_all_must_items(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        resp = self.post_json(f'{URL}{tid}/done/', {})
        self.expect_fail(resp, 400, '未完成')

    def test_done_after_execute(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        body = self.ok(self.post_json(f'{URL}{tid}/done/', {}))['data']
        self.assertEqual(body['stage'], 'done')
        self.assertTrue(body['doneAt'])
        self.assertFalse(body['isDisposed'])

    def test_dispose_requires_done(self):
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)
        resp = self.post_json(f'{URL}{tid}/dispose/', {'disposal': 'pending_adopt'})
        self.expect_fail(resp, 400, '诊疗完成')

    def test_dispose_changes_pet_status(self):
        """**业务结果判据**：动物的状态真的变成了所选去向。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self.ok(self.post_json(f'{URL}{tid}/dispose/', {'disposal': 'pending_adopt'}))

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_adopt')
        treatment = Treatment.objects.get(id=tid)
        self.assertEqual(treatment.status, 'completed')
        self.assertEqual(treatment.disposal, 'pending_adopt')
        self.assertTrue(treatment.disposed_at)

    def test_dispose_dead_status_is_writable(self):
        """`dead`（死亡/无害化）是新拆出来的状态，必须真的能写出来 ——
        「枚举值存在、写入路径缺失」的判据。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self.ok(self.post_json(f'{URL}{tid}/dispose/', {'disposal': 'dead'}))

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'dead')

    def test_dispose_invalid_key_rejected(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        resp = self.post_json(f'{URL}{tid}/dispose/', {'disposal': 'released_to_moon'})
        self.expect_fail(resp, 400, '不合法')

    def test_dispose_twice_rejected(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self.ok(self.post_json(f'{URL}{tid}/dispose/', {'disposal': 'pending_adopt'}))
        resp = self.post_json(f'{URL}{tid}/dispose/', {'disposal': 'released'})
        self.expect_fail(resp, 400, '已办结')


# ============================================================
# 看板 / 列表
# ============================================================
class BoardTest(FlowBase):
    def test_board_lists_unopened_pets_as_pending(self):
        """还没开单的在院动物必须出现在「待接诊」—— 不出现 = 接不了诊。"""
        body = self.ok(self.get_json(URL + 'board/'))['data']
        codes = [r['petCode'] for r in body['pending']]
        self.assertIn(self.pet.code, codes)
        self.assertEqual(body['counts']['pending'], len(body['pending']))

    def test_board_groups_by_stage(self):
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)

        body = self.ok(self.get_json(URL + 'board/'))['data']
        self.assertEqual([r['petCode'] for r in body['treating']], [self.pet.code])
        self.assertNotIn(self.pet.code, [r['petCode'] for r in body['pending']])

    def test_board_marks_disposed(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        body = self.ok(self.get_json(URL + 'board/'))['data']
        row = [r for r in body['done'] if r['petCode'] == self.pet.code][0]
        self.assertFalse(row['isDisposed'])

        self.ok(self.post_json(f'{URL}{tid}/dispose/', {'disposal': 'released'}))
        body = self.ok(self.get_json(URL + 'board/'))['data']
        row = [r for r in body['done'] if r['petCode'] == self.pet.code][0]
        self.assertTrue(row['isDisposed'])
        self.assertEqual(row['disposal'], 'released')

    def test_board_isolates_hospitals(self):
        """别院的动物不出现在本院看板上（双院同区县，靠机构隔离）。"""
        other = make_institution(type='hospital', district=self.district_a,
                                 name='甲区第二医院')
        make_pet(status='in_treatment', district=self.district_a, hospital=other)
        body = self.ok(self.get_json(URL + 'board/'))['data']
        codes = [r['petCode'] for r in body['pending']]
        self.assertIn(self.pet.code, codes)
        self.assertEqual(len(codes), 1)


class ListStageFilterTest(FlowBase):
    def test_stage_filter(self):
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)
        body = self.ok(self.get_json(f'{URL}?stage=treating'))['data']
        self.assertEqual([r['id'] for r in body], [tid])
        body = self.ok(self.get_json(f'{URL}?stage=checking'))['data']
        self.assertEqual(body, [])

    def test_unknown_stage_rejected(self):
        """非法筛选值必须 4xx，**不能静默忽略**（静默忽略 = 用户以为筛了）。"""
        resp = self.get_json(f'{URL}?stage=whatever')
        self.expect_fail(resp, 400)


# ============================================================
# 权限 / 隔离
# ============================================================
class PermissionTest(FlowBase):
    def test_shelter_cannot_write(self):
        detail = self._open()
        tid = detail['id']
        self.login_as(self.shelter_user_a)
        resp = self.post_json(f'{URL}{tid}/check/',
                              {'temperature': 38.5, 'temperature_flag': 'normal'})
        self.assertEqual(resp.status_code, 403)

    def test_gov_cannot_write(self):
        detail = self._open()
        tid = detail['id']
        self.login_as(self.gov_a)
        resp = self.post_json(f'{URL}{tid}/diagnose/', {})
        self.assertEqual(resp.status_code, 403)

    def test_other_hospital_cannot_write(self):
        """同区县的第二家医院拿到诊疗单 id 也必须 404（不是 403）。"""
        detail = self._open()
        tid = detail['id']
        other = make_institution(type='hospital', district=self.district_a,
                                 name='甲区第二医院')
        other_user = make_user('hospital_second_t', role='hospital',
                               district=self.district_a, institution=other)
        self.login_as(other_user)
        resp = self.post_json(f'{URL}{tid}/diagnose/', {})
        self.expect_fail(resp, 404)

    def test_government_can_read_detail(self):
        detail = self._open()
        tid = detail['id']
        self.login_as(self.gov_a)
        body = self.ok(self.get_json(f'{URL}{tid}/'))['data']
        self.assertEqual(body['id'], tid)


class ChipStockReconcileTest(FlowBase):
    def test_available_chip_count_matches_material_stock(self):
        """芯片执行后，「可用芯片数」与「芯片物料库存」必须**同向变化**。

        第四十六轮重构的目标就是这两者恒等 —— 只动一边（只扣库存不动芯片、
        或只动芯片不扣库存）都会让它们悄悄错开，而且不报错。
        """
        chip_material = make_material(category='chip', name='对账芯片',
                                      district=self.district_a)
        make_hospital_txn(chip_material, self.hospital_a, 'receive', 3)
        chips = [make_chip(number=f'CHIPRECON{i:04d}',
                           institution=self.hospital_a, material=chip_material)
                 for i in range(3)]

        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chips[0].number,
            'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)

        before_chips = Chip.objects.filter(
            material=chip_material, institution=self.hospital_a,
            status='available').count()
        before_stock = self._stock(chip_material)
        self.assertEqual(before_chips, before_stock)

        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))

        after_chips = Chip.objects.filter(
            material=chip_material, institution=self.hospital_a,
            status='available').count()
        self.assertEqual(after_chips, before_chips - 1)
        self.assertEqual(self._stock(chip_material), before_stock - 1)
        self.assertEqual(after_chips, self._stock(chip_material))
