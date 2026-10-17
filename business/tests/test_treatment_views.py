"""诊疗视图测试：四项登记、库存联动、芯片绑定、状态机。"""
from django.db.models import Max

from business import services_treatment as st
from business.models import (
    Chip, MaterialTransaction, Treatment, TreatmentItem,
)
from business.services import get_hospital_stock
from business.tests.base import (
    assert_doc_no,
    BusinessTestBase, make_chip, make_hospital_txn, make_material, make_pet,
    make_user,
)

URL = '/api/business/treatments/'


class TreatmentFlowViewTest(BusinessTestBase):
    """诊疗视图（三步流程）：库存联动、芯片绑定、状态机、绝育病历。

    第四十七轮：旧「快速登记」的 `treatments/create/` 已删除，本类整体迁到
    三步流程（open → check → plan → diagnose → execute → done → dispose）。
    每条判据都落在**业务结果**上（库存真变了、芯片真绑了、病历真落了），
    不停在状态码 —— 本地全绿 + 生产 200 也可能功能 100% 不可用。

    ⚠ 旧口径「一次请求提交整张单、当场扣库存」的两条判据在这里**没有对应物**：
    * 「数量」—— 新流程每个处方药品固定 1 条（`TreatmentRx.quantity`），
      没有「填 0 / 填 3」这个入口，`test_invalid_quantity_rejected` 随之删除；
    * 「被拒时不留诊疗记录」—— 新流程开单是**独立一步**（`open`），
      草稿行本来就在，所以判据改成「不留处置项目 / 不扣库存」。
    """

    def _pet(self, **kw):
        defaults = dict(district=self.district_a, hospital=self.hospital_a,
                        status='in_treatment')
        defaults.update(kw)
        return make_pet(**defaults)

    def _open(self, pet):
        self.login_as(self.hospital_user_a)
        return self.ok(self.post_json(f'{URL}open/', {'pet_id': pet.id}))['data']

    def _plan(self, tid, items, **extra):
        body = {'items': items}
        body.update(extra)
        return self.post_json(f'{URL}{tid}/plan/', body)

    def _rx(self, tid, key, material, date='2026-03-01'):
        return self._plan(tid, [{'key': key, 'planned_date': date,
                                 'rx': [material.id]}])

    def _diagnose(self, tid):
        """过第一步检查单 → 诊断完成，返回 detail（执行阶段的前置）。"""
        self.ok(self.post_json(f'{URL}{tid}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal'}))
        return self.ok(self.post_json(f'{URL}{tid}/diagnose/', {}))['data']

    def _execute_all(self, tid):
        detail = self._diagnose(tid)
        for item in detail['items']:
            self.ok(self.post_json(f'{URL}{tid}/items/{item["id"]}/execute/', {}))
        return detail

    def _execute_first(self, tid):
        """只执行第一项 —— 负向用例用它拿「被拒」的响应（不能 `self.ok`）。"""
        detail = self._diagnose(tid)
        return self.post_json(
            f'{URL}{tid}/items/{detail["items"][0]["id"]}/execute/', {})

    # ---------- 开单 ----------

    def test_open_creates_in_progress_treatment(self):
        pet = self._pet()
        detail = self._open(pet)
        treatment = Treatment.objects.get(id=detail['id'])
        self.assertEqual(treatment.status, 'in_progress')
        assert_doc_no(self, treatment.ledger_no, 'TRE')
        self.assertEqual(treatment.hospital, self.hospital_a)
        self.assertEqual(treatment.pet_code, pet.code)
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment')

    def test_missing_pet_id(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}open/', {}), message='缺少宠物ID')

    def test_unknown_pet(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}open/', {'pet_id': 999999}),
                         status=404, message='无权访问')

    def test_cross_district_pet_404(self):
        pet = make_pet(district=self.district_b, hospital=self.hospital_b,
                       status='in_treatment')
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}open/', {'pet_id': pet.id}),
                         status=404, message='无权访问')

    def test_deleted_pet_rejected(self):
        pet = self._pet(is_deleted=True)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}open/', {'pet_id': pet.id}),
                         status=404, message='无权访问')

    def test_pending_adopt_pet_can_be_treated(self):
        pet = self._pet(status='pending_adopt')
        detail = self._open(pet)
        self.assertEqual(detail['petCode'], pet.code)

    def test_released_pet_rejected(self):
        pet = self._pet(status='released')
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}open/', {'pet_id': pet.id}),
                         message='不可诊疗')

    def test_shelter_cannot_open(self):
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}open/', {'pet_id': pet.id}),
                         status=403)

    def test_hospital_quirk_no_hospital_info(self):
        """账号没绑医院时要报「未绑定医院」，不能报成「宠物不存在」。"""
        pet = make_pet(district=self.district_a, status='in_treatment',
                       hospital=None)
        user = make_user(role='hospital', district=self.district_a)
        self.login_as(user)
        self.expect_fail(self.post_json(f'{URL}open/', {'pet_id': pet.id}),
                         message='未绑定医院')

    # ---------- 绝育病历（第四十七轮并入） ----------

    STER_RECORD = {
        'surgery_date': '2026-03-01', 'surgeon': '赵医生',
        'diagnosis': '健康', 'anesthesia': '吸入麻醉',
        'procedure': '常规绝育', 'recovery': '良好',
    }

    def test_sterilization_record_saved_through_plan(self):
        """填了病历就必须落库 —— 这条项目没有处方，按 `has_rx` 判会被静默跳过，
        随后 `save_sterilization()` 又把六列清空（「填完保存再打开是空的」）。"""
        pet = self._pet()
        tid = self._open(pet)['id']
        self.ok(self._plan(tid, [{'key': 'sterilization'}],
                           sterilization=self.STER_RECORD))

        treatment = Treatment.objects.get(id=tid)
        self.assertTrue(treatment.items_sterilization)
        self.assertEqual(treatment.sterilization_surgeon, '赵医生')
        self.assertEqual(str(treatment.sterilization_surgery_date), '2026-03-01')
        self.assertEqual(treatment.sterilization_anesthesia, '吸入麻醉')
        item = TreatmentItem.objects.get(treatment=treatment,
                                         item_key='sterilization')
        self.assertTrue(st.item_is_must(item), '绝育病历类项目必须算「必执行」')

    def test_sterilization_record_cleared_when_record_removed(self):
        """撤掉病历时六列必须清空 —— 否则会印出「没勾绝育却有手术日期」的单子。"""
        pet = self._pet()
        tid = self._open(pet)['id']
        self.ok(self._plan(tid, [{'key': 'sterilization'}],
                           sterilization=self.STER_RECORD))
        # UI 每次都发全部项目（空项目由服务端跳过）；`items: []` 是 no-op
        self.ok(self._plan(tid, [{'key': 'other'},
                                  {'key': 'sterilization'}]))

        treatment = Treatment.objects.get(id=tid)
        self.assertFalse(treatment.items_sterilization)
        self.assertEqual(treatment.sterilization_surgeon, '')
        self.assertIsNone(treatment.sterilization_surgery_date)
        self.assertFalse(TreatmentItem.objects.filter(
            treatment=treatment, item_key='sterilization').exists())

    def test_partial_sterilization_record_rejected(self):
        """病历填一半必须整单拒绝（0/6 或 6/6，不能 3/6）。"""
        pet = self._pet()
        tid = self._open(pet)['id']
        partial = dict(self.STER_RECORD)
        partial.pop('surgeon')
        resp = self._plan(tid, [{'key': 'sterilization'}], sterilization=partial)
        self.expect_fail(resp, message='不能为空')

    # ---------- 库存联动 ----------

    def test_vaccine_consumes_hospital_stock(self):
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 10)
        tid = self._open(pet)['id']
        self.ok(self._rx(tid, 'vaccine', material))
        self._execute_all(tid)

        treatment = Treatment.objects.get(id=tid)
        self.assertTrue(treatment.items_vaccine)
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 9)
        txn = MaterialTransaction.objects.filter(
            material=material, hospital=self.hospital_a, type='consume').first()
        self.assertIsNotNone(txn)
        # 消耗流水也必须有单号（`CON…`）。原实现三个 `adjust_stock` 调用点
        # 都没传 `ledger_no` —— 于是消耗流水**一个号都没有**，打印出来是
        # 「单据号：—」，归档时无法编号。而 `consume_prefix='CON'` 早在
        # 平台端「编号规则」页就声明了（枚举值存在、写入路径缺失）。
        assert_doc_no(self, txn.ledger_no, 'CON')

    def test_vaccine_out_of_stock_rejected(self):
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a)
        tid = self._open(pet)['id']
        self.expect_fail(self._rx(tid, 'vaccine', material), message='库存为 0')
        self.assertFalse(MaterialTransaction.objects.filter(
            material=material, hospital=self.hospital_a, type='consume').exists())

    def test_out_of_stock_leaves_no_items(self):
        """被拒时不能留下处置项目 —— 否则「库存没扣、项目却要做」。"""
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a)
        tid = self._open(pet)['id']
        self.expect_fail(self._rx(tid, 'vaccine', material), message='库存为 0')
        self.assertFalse(TreatmentItem.objects.filter(treatment_id=tid).exists())

    def test_partial_failure_does_not_deduct_stock(self):
        """前一项库存充足、后一项不足时，前一项的库存也不能被扣掉。"""
        pet = self._pet()
        vaccine = make_material(category='vaccine', district=self.district_a)
        dewormer = make_material(category='dewormer', district=self.district_a)
        make_hospital_txn(vaccine, self.hospital_a, 'receive', 10)
        # 驱虫药库存为 0，必然失败
        tid = self._open(pet)['id']
        resp = self._plan(tid, [
            {'key': 'vaccine', 'planned_date': '2026-03-01',
             'rx': [vaccine.id]},
            {'key': 'deworming', 'planned_date': '2026-03-01',
             'rx': [dewormer.id]},
        ])
        self.expect_fail(resp, message='库存为 0')

        self.assertEqual(get_hospital_stock(vaccine, self.hospital_a), 10,
                         '整单被拒时疫苗库存不应被扣减')
        self.assertFalse(TreatmentItem.objects.filter(treatment_id=tid).exists())
        self.assertFalse(MaterialTransaction.objects.filter(
            type='consume').exists())

    def test_deworming_consumes_stock(self):
        pet = self._pet()
        material = make_material(category='dewormer', district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 5)
        tid = self._open(pet)['id']
        self.ok(self._rx(tid, 'deworming', material))
        self._execute_all(tid)
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 4)

    # ---------- 芯片 ----------

    def _chip_tid(self, pet, chip):
        tid = self._open(pet)['id']
        self.ok(self._plan(tid, [{'key': 'chip',
                                  'planned_date': '2026-03-01',
                                  'chip_no': chip.number}]))
        return tid

    def test_chip_binds_pet_and_consumes(self):
        pet = self._pet()
        chip_material = make_material(category='chip', district=self.district_a)
        make_hospital_txn(chip_material, self.hospital_a, 'receive', 5)
        # 芯片必须带 `material` 才会扣「芯片物料」库存 —— 那是新形态；
        # NULL 的是重构前的存量行（当时芯片还没有物料归属），扣不了也不该扣。
        chip = make_chip(material=chip_material)
        tid = self._chip_tid(pet, chip)
        self._execute_all(tid)

        chip.refresh_from_db()
        pet.refresh_from_db()
        self.assertEqual(chip.status, 'used')
        self.assertEqual(chip.pet, pet)
        self.assertEqual(pet.chip_no, chip.number)
        self.assertEqual(get_hospital_stock(chip_material, self.hospital_a), 4)

    def test_reused_chip_rejected(self):
        pet = self._pet()
        chip = make_chip(status='used')
        tid = self._chip_tid(pet, chip)
        self.expect_fail(self._execute_first(tid), message='已被使用')

    def test_missing_chip_rejected(self):
        """芯片号在库里查不到 —— 执行时拒绝（开方只记号码，不验存在）。"""
        pet = self._pet()
        tid = self._open(pet)['id']
        self.ok(self._plan(tid, [{'key': 'chip',
                                  'planned_date': '2026-03-01',
                                  'chip_no': 'GHOST-CHIP'}]))
        self.expect_fail(self._execute_first(tid), message='不存在')

    def test_in_transit_chip_rejected(self):
        """**在途**芯片不能植入（第四十六轮新增的第三态）。

        在途 = 库存已从发起方扣掉、接收方还没签收 —— 此时它不属于任何一方，
        谁都不该能用。重构前只有「可用 / 已使用」两态，漏了这一态：
        货还在路上就能被植进动物，而两边的库存账都对不上。
        """
        pet = self._pet()
        chip = make_chip(status='in_transit')
        tid = self._chip_tid(pet, chip)
        self.expect_fail(self._execute_first(tid), message='在途')

    def test_chip_of_other_hospital_rejected(self):
        """**别院持有的**芯片不能植入（第四十六轮归属校验）。

        **反向对照**：上面 `test_chip_binds_pet_and_consumes` 用的是
        无归属的存量芯片（放行），这一条用的是带归属且不属于本院的（拦下）——
        只测一边的话，把判据写成「一律放行」或「一律拒绝」都能通过。
        """
        pet = self._pet()
        chip = make_chip(institution=self.hospital_b)
        tid = self._chip_tid(pet, chip)
        self.expect_fail(self._execute_first(tid), message='不属于')


class TreatmentListTest(BusinessTestBase):
    def test_hospital_sees_own_only(self):
        pet_a = make_pet(district=self.district_a, hospital=self.hospital_a,
                         status='in_treatment')
        pet_b = make_pet(district=self.district_b, hospital=self.hospital_b,
                         status='in_treatment')
        Treatment.objects.create(pet=pet_a, pet_code=pet_a.code,
                                 hospital=self.hospital_a, district=self.district_a)
        Treatment.objects.create(pet=pet_b, pet_code=pet_b.code,
                                 hospital=self.hospital_b, district=self.district_b)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json(URL))['data']
        self.assertEqual({t['hospital_id'] for t in data}, {self.hospital_a.id})

    def test_status_filter(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        Treatment.objects.create(pet=pet, pet_code=pet.code,
                                 hospital=self.hospital_a, district=self.district_a)
        self.login_as(self.gov_city)
        data = self.ok(self.get_json(URL + '?status=in_progress'))['data']
        self.assertTrue(all(t['status'] == 'in_progress' for t in data))


class TreatmentDetailTest(BusinessTestBase):
    def test_detail_includes_pet(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        treatment = Treatment.objects.create(pet=pet, pet_code=pet.code,
                                             hospital=self.hospital_a,
                                             district=self.district_a)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.get_json(f'{URL}{treatment.id}/'))
        self.assertEqual(body['data']['pet']['id'], pet.id)

    def test_unknown_404(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.get_json(f'{URL}999999/'), status=404, message='诊疗记录不存在')

    def test_other_hospital_404(self):
        """本院之外的诊疗记录不可读取（原实现无任何权限校验）。"""
        pet = make_pet(district=self.district_b, hospital=self.hospital_b,
                       status='in_treatment')
        treatment = Treatment.objects.create(pet=pet, pet_code=pet.code,
                                             hospital=self.hospital_b,
                                             district=self.district_b)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.get_json(f'{URL}{treatment.id}/'),
                         status=404, message='无权访问')

    def test_district_gov_scoped(self):
        """区级监管只能看本区诊疗记录。"""
        pet = make_pet(district=self.district_b, hospital=self.hospital_b,
                       status='in_treatment')
        treatment = Treatment.objects.create(pet=pet, pet_code=pet.code,
                                             hospital=self.hospital_b,
                                             district=self.district_b)
        self.login_as(self.gov_a)
        self.expect_fail(self.get_json(f'{URL}{treatment.id}/'),
                         status=404, message='无权访问')

    def test_city_gov_can_read_any(self):
        pet = make_pet(district=self.district_b, hospital=self.hospital_b,
                       status='in_treatment')
        treatment = Treatment.objects.create(pet=pet, pet_code=pet.code,
                                             hospital=self.hospital_b,
                                             district=self.district_b)
        self.login_as(self.gov_city)
        body = self.ok(self.get_json(f'{URL}{treatment.id}/'))
        self.assertEqual(body['data']['id'], treatment.id)
