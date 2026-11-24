"""平台端「入院单据维护」判据。

与出院维护对称，重点在**副作用**而非状态码：
* 捕捉单退回草稿 —— 宠物逻辑失效（不在待转运列表）但照片/编号一字不动；
  重新提交后恢复。只回退单据不打删除标记 = 动物仍在待转运列表 = 白做。
* 领回单退回草稿 —— 宠物恢复到「领回前状态」，不是拍脑袋的 in_transit；
  诊疗单的 disposed_at 一并清掉。
* 转运单 —— 仅未签收可退回；已签收（动物已入院）必须拒绝。
* 诊疗单 —— 只读，任何迁移都拒绝（手术/处方/库存记录不可撕毁）。
"""
from urllib.parse import urlencode

from django.utils import timezone

from business.models import Capture, OwnerReturn, Pet, Transfer, Treatment
from business.tests.base import BusinessTestBase, make_pet
from core.models import AuditLog
from supervision.admission_doc_maintenance import (
    DOC_LABELS, REASON_MAX, TRANSITIONS, RESUBMIT_TARGET, status_labels,
)

LIST_URL = '/api/supervision/admission-docs/'
SET_URL = '/api/supervision/admission-docs/status/'


class AdmissionDocBase(BusinessTestBase):
    def setUp(self):
        self.login_as(self.platform_admin)

    def _capture(self, code='ADM-CAP-1', status='pending', n=2):
        codes = [f'{code}-{i}' for i in range(n)]
        pets = [make_pet(code=c, status='in_transit', district=self.district_a,
                         shelter=self.shelter_a) for c in codes]
        cap = Capture.objects.create(
            district=self.district_a, shelter=self.shelter_a,
            shelter_name=self.shelter_a.name, institution=self.shelter_a,
            pet_codes=','.join(codes), pet_count=n, status=status,
            ledger_no=code)
        for p in pets:
            p.capture = cap
            p.save(update_fields=['capture'])
        return cap, pets

    def _owner_return(self, code='ADM-RET-1', before='in_treatment',
                      pet_status='owner_returned', with_treatment=True):
        pet = make_pet(code=code, status=pet_status, district=self.district_a,
                       shelter=self.shelter_a, hospital=self.hospital_a)
        t = None
        if with_treatment:
            t = Treatment.objects.create(
                pet=pet, pet_code=code, hospital=self.hospital_a,
                hospital_name=self.hospital_a.name, institution=self.hospital_a,
                status='completed', done_at=timezone.now(),
                disposal='owner_returned', disposed_at=timezone.now(),
                district=self.district_a)
        doc = OwnerReturn.objects.create(
            pet=pet, pet_code=code, owner_name='李四', reason='测试',
            district=self.district_a, institution=self.hospital_a,
            status='effective', pet_status_before=before, ledger_no=code)
        return doc, pet, t

    def _transfer(self, code='ADM-TRF-1', status='pending'):
        return Transfer.objects.create(
            district=self.district_a, from_shelter=self.shelter_a,
            to_hospital=self.hospital_a,
            from_shelter_name=self.shelter_a.name,
            to_hospital_name=self.hospital_a.name, institution=self.shelter_a,
            pet_codes=code, pet_count=1, status=status, ledger_no=code)

    def _set(self, doc_type, doc_id, target, reason='录入错误，业务方要求纠正'):
        return self.post_json(SET_URL, {'doc_type': doc_type, 'doc_id': doc_id,
                                        'target': target, 'reason': reason})

    def _rows(self, **params):
        query = urlencode({k: v for k, v in params.items() if v != ''})
        return self.ok(self.client.get(f'{LIST_URL}?{query}'))['data']


class AdmissionListTest(AdmissionDocBase):
    def test_list_covers_all_four_types(self):
        cap, _ = self._capture()
        orr, _, _ = self._owner_return()
        tr = self._transfer()
        t = make_pet(code='ADM-TRE-1', district=self.district_a)
        Treatment.objects.create(pet=t, pet_code=t.code, hospital=self.hospital_a,
                                 status='in_progress', district=self.district_a,
                                 ledger_no='ADM-TRE-1')
        types = {r['doc_type'] for r in self._rows(limit=500)}
        self.assertTrue(set(DOC_LABELS).issubset(types),
                        f'列表没覆盖全部单据类型：{types}')

    def test_list_filter_by_type_and_keyword(self):
        cap, _ = self._capture(code='ADM-FILTER-1')
        rows = self._rows(doc_type='capture', q='ADM-FILTER-1')
        self.assertTrue(rows)
        self.assertTrue(all(r['doc_type'] == 'capture' for r in rows))
        self.assertEqual(len(rows), 1)

    def test_list_rejects_unknown_type(self):
        resp = self.client.get(f'{LIST_URL}?doc_type=bogus')
        self.assertFalse(resp.json()['success'])

    def test_treatment_row_has_no_targets(self):
        """诊疗单只读：列表里绝不能给出任何可操作状态。"""
        pet = make_pet(code='ADM-TRE-RO', district=self.district_a)
        t = Treatment.objects.create(pet=pet, pet_code=pet.code, hospital=self.hospital_a,
                                     status='completed', district=self.district_a,
                                     ledger_no='ADM-TRE-RO')
        rows = self._rows(doc_type='treatment', q='ADM-TRE-RO')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['targets'], [])


class CaptureDraftTest(AdmissionDocBase):
    def test_to_draft_keeps_photo_and_code_but_invalidates_pet(self):
        cap, pets = self._capture()
        # 给宠物挂照片路径与谱系信息，退回草稿后这些都必须在
        pets[0].description = '左后腿有白斑'
        pets[0].save(update_fields=['description'])
        resp = self._set('capture', cap.id, 'draft')
        self.assertTrue(resp.json()['success'], resp.json())
        cap.refresh_from_db()
        self.assertEqual(cap.status, 'draft', '单据没写草稿状态')
        self.assertIn('draft', [t[0] for t in Capture.STATUS_CHOICES])
        for p in pets:
            p.refresh_from_db()
            self.assertTrue(p.is_deleted, f'{p.code} 没失效，仍在待转运列表')
        pets[0].refresh_from_db()
        self.assertEqual(pets[0].description, '左后腿有白斑', '草稿退回不该抹掉宠物资料')
        self.assertEqual(cap.pet_codes, ','.join(p.code for p in pets), '编号串被改动')

    def test_resubmit_restores_pets(self):
        cap, pets = self._capture()
        self._set('capture', cap.id, 'draft')
        self._set('capture', cap.id, RESUBMIT_TARGET, reason='已改正')
        cap.refresh_from_db()
        self.assertEqual(cap.status, 'pending', '重新提交没回到待转运')
        for p in pets:
            p.refresh_from_db()
            self.assertFalse(p.is_deleted, f'{p.code} 没恢复，重新提交无效')
            self.assertEqual(p.status, 'in_transit')

    def test_draft_shown_as_draft_in_list(self):
        """草稿单的宠物已失效，派生状态会是 pending；列表必须显示草稿。"""
        cap, _ = self._capture()
        self._set('capture', cap.id, 'draft')
        rows = self._rows(doc_type='capture', q=cap.ledger_no)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'draft',
                         '列表仍显示待转运，管理员找不到草稿单')

    def test_capture_with_transferred_pet_refused(self):
        """已有动物在转运流程里的捕捉单不能退回草稿（会撕断下游链）。"""
        cap, pets = self._capture()
        Transfer.objects.create(
            district=self.district_a, from_shelter=self.shelter_a,
            to_hospital=self.hospital_a, institution=self.shelter_a,
            pet_codes=pets[0].code, pet_count=1, status='received',
            ledger_no='ADM-BLOCK')
        resp = self._set('capture', cap.id, 'draft')
        self.assertFalse(resp.json()['success'])
        cap.refresh_from_db()
        self.assertNotEqual(cap.status, 'draft')


class OwnerReturnDraftTest(AdmissionDocBase):
    def test_to_draft_restores_exact_pre_status(self):
        for before in ('in_treatment', 'pending_adopt', 'in_transit'):
            doc, pet, t = self._owner_return(code=f'ADM-RET-{before}', before=before)
            resp = self._set('owner_return', doc.id, 'draft')
            self.assertTrue(resp.json()['success'], resp.json())
            doc.refresh_from_db(); pet.refresh_from_db(); t.refresh_from_db()
            self.assertEqual(doc.status, 'draft')
            self.assertEqual(pet.status, before,
                             f'领回前是 {before}，退回却被猜成别的状态')
            self.assertIsNone(t.disposed_at, '诊疗单没退回待出院，看板会显示已出院孤儿')

    def test_resubmit_sets_owner_returned_again(self):
        doc, pet, t = self._owner_return(before='pending_adopt')
        self._set('owner_return', doc.id, 'draft')
        resp = self._set('owner_return', doc.id, RESUBMIT_TARGET, reason='电话改正')
        self.assertTrue(resp.json()['success'], resp.json())
        doc.refresh_from_db(); pet.refresh_from_db(); t.refresh_from_db()
        self.assertEqual(doc.status, 'effective')
        self.assertEqual(pet.status, 'owner_returned')
        self.assertIsNotNone(t.disposed_at, '重新提交后诊疗单没补办结')

    def test_legacy_draft_without_before_falls_back_to_transit(self):
        """存量草稿可能没有 pet_status_before，退回时必须安全兜底。"""
        doc, pet, _ = self._owner_return(code='ADM-RET-LEGACY', before='')
        doc.pet_status_before = ''
        doc.save(update_fields=['pet_status_before'])
        resp = self._set('owner_return', doc.id, 'draft')
        self.assertTrue(resp.json()['success'])
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_transit')


class TransferReturnTest(AdmissionDocBase):
    def test_pending_transfer_can_return(self):
        tr = self._transfer(status='pending')
        resp = self._set('transfer', tr.id, 'void')
        self.assertTrue(resp.json()['success'], resp.json())
        tr.refresh_from_db()
        self.assertEqual(tr.status, 'void')

    def test_received_transfer_refused(self):
        """医院已签收 = 动物已入院，绝不能退回在途。"""
        tr = self._transfer(status='received')
        resp = self._set('transfer', tr.id, 'void')
        self.assertFalse(resp.json()['success'])
        tr.refresh_from_db()
        self.assertEqual(tr.status, 'received')

    def test_partial_transfer_refused(self):
        tr = self._transfer(status='partial')
        resp = self._set('transfer', tr.id, 'void')
        self.assertFalse(resp.json()['success'])

    def test_applier_itself_blocks_received(self):
        """双防线各自要有判据：即使白名单误放行已签收，applier 也必须拒绝。

        上一版只测视图拒绝，删掉 applier 里的签收守卫后测试仍绿（白名单先
        拦住了）—— 等于第二道防线没人看守。这里直连 applier 钉死它。
        """
        from supervision.admission_doc_maintenance import (
            MaintenanceError, _transfer_return,
        )
        tr = self._transfer(status='received')
        with self.assertRaises(MaintenanceError):
            _transfer_return(tr, self.platform_admin)
        tr.refresh_from_db()
        self.assertEqual(tr.status, 'received', 'applier 竟改了已签收的单')


class AdmissionGuardTest(AdmissionDocBase):
    def test_treatment_never_rollback(self):
        pet = make_pet(code='ADM-GUARD-TRE', district=self.district_a)
        t = Treatment.objects.create(pet=pet, pet_code=pet.code, hospital=self.hospital_a,
                                     status='completed', done_at=timezone.now(),
                                     district=self.district_a, ledger_no='ADM-GUARD-TRE')
        resp = self._set('treatment', t.id, 'draft')
        self.assertFalse(resp.json()['success'])
        t.refresh_from_db()
        self.assertEqual(t.status, 'completed')

    def test_reason_required_and_capped(self):
        cap, _ = self._capture()
        resp = self.post_json(SET_URL, {'doc_type': 'capture', 'doc_id': cap.id,
                                        'target': 'draft', 'reason': ''})
        self.assertFalse(resp.json()['success'])
        resp = self.post_json(SET_URL, {'doc_type': 'capture', 'doc_id': cap.id,
                                        'target': 'draft', 'reason': 'x' * (REASON_MAX + 1)})
        self.assertFalse(resp.json()['success'])

    def test_target_must_be_in_whitelist(self):
        cap, _ = self._capture()
        resp = self._set('capture', cap.id, 'void')  # 不允许直接作废
        self.assertFalse(resp.json()['success'])

    def test_get_rejected(self):
        resp = self.client.get(SET_URL)
        self.assertFalse(resp.json()['success'])

    def test_unknown_doc_type_or_missing_id(self):
        resp = self.post_json(SET_URL, {'doc_type': 'bogus', 'doc_id': 1,
                                        'target': 'draft', 'reason': 'x'})
        self.assertFalse(resp.json()['success'])
        resp = self.post_json(SET_URL, {'doc_type': 'capture', 'doc_id': 'abc',
                                        'target': 'draft', 'reason': 'x'})
        self.assertFalse(resp.json()['success'])

    def test_only_platform_admin_may_maintain(self):
        cap, _ = self._capture()
        self.login_as(self.shelter_user_a)
        resp = self._set('capture', cap.id, 'draft')
        self.assertIn(resp.status_code, (302, 403))

    def test_whitelist_is_single_source_for_rows(self):
        """行数据的可操作项必须来自白名单，不能前端或列表各写一份。"""
        cap, _ = self._capture()
        rows = self._rows(doc_type='capture', q=cap.ledger_no)
        self.assertEqual(rows[0]['targets'], list(TRANSITIONS['capture']['pending']))


class AdmissionAuditTest(AdmissionDocBase):
    def test_operation_written_to_audit_with_reason(self):
        cap, _ = self._capture()
        before = AuditLog.objects.count()
        resp = self._set('capture', cap.id, 'draft', reason='主人信息录入错误')
        self.assertTrue(resp.json()['success'])
        self.assertGreater(AuditLog.objects.count(), before)
        log = AuditLog.objects.order_by('-id').first()
        self.assertIn('主人信息录入错误', log.detail)
        self.assertIn('待转运', log.detail)
        self.assertIn('草稿', log.detail)
