"""平台端「出院单据状态维护」判据（第五十七轮后端 + 第六十一轮页面）。

本模块是**唯一**能把已办结的出院单据退回去的合法出口，所以判据落在
「三半一起回退」上：单据状态、`Pet.status`、诊疗单 `disposed_at`。
只回退其中一半比不回退更糟 —— 一张「已作废」的领养单挂着一只「已领养」
的动物，出院管理与一宠一档各说一套，正是这个功能要消除的矛盾。

正向流程（单据怎么走到终态）由 `business/tests/test_discharge_views.py` 钉死，
本文件直接用 ORM 造「已办结」的单据形状，只测**回退方向**。
"""
from urllib.parse import urlencode

from django.utils import timezone

from business.models import (
    Adoption, AdoptionHallListing, Euthanasia, Pet, Release, ReturnOrder, Treatment,
)
from business.tests.base import BusinessTestBase, make_pet
from core.models import AuditLog
from supervision.doc_maintenance import (
    DOC_LABELS, REASON_MAX, TRANSITIONS, status_labels,
)

LIST_URL = '/api/supervision/discharge-docs/'
SET_URL = '/api/supervision/discharge-docs/status/'


class DocMaintenanceBase(BusinessTestBase):
    def setUp(self):
        self.login_as(self.platform_admin)

    # ---- 单据形状 ----
    def _closed_treatment(self, pet, advised='pending_adopt'):
        """已办结的诊疗单：回退时它必须跟着退回「待出院」。"""
        return Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='completed', done_at=timezone.now(),
            disposal_advised=advised, disposal=advised,
            disposed_at=timezone.now(), district=self.district_a)

    def _adoption(self, code='DMA-ADP-1', status='completed', pet_status='adopted'):
        pet = make_pet(code=code, status=pet_status, district=self.district_a,
                       shelter=self.shelter_a, hospital=self.hospital_a)
        treatment = self._closed_treatment(pet, 'pending_adopt')
        doc = Adoption.objects.create(
            pet=pet, pet_code=code, adopter_name='李领养',
            adopter_phone='13800004001', hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status=status, outbound_at=timezone.localdate(),
            adopted_at=timezone.localdate(), handover_name='王医生',
            signature='data:image/png;base64,SIG', ledger_no=f'ADP-{code}',
            district=self.district_a)
        return doc, pet, treatment

    def _euthanasia(self, code='DMA-EUT-1', outbound=True):
        pet = make_pet(code=code, status='euthanized', district=self.district_a,
                       shelter=self.shelter_a, hospital=self.hospital_a)
        treatment = self._closed_treatment(pet, 'euthanized')
        doc = Euthanasia.objects.create(
            pet=pet, pet_code=code, kind='euthanasia', hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            reason='严重外伤无法救治', euthanized_at=timezone.localdate(),
            outbound_at=timezone.localdate() if outbound else None,
            handover_name='王医生', receiving_org='市无害化处理中心',
            receiver_name='赵师傅', signature='data:image/png;base64,SIG',
            body_received=outbound, ledger_no=f'EUT-{code}',
            district=self.district_a)
        return doc, pet, treatment

    def _return_order(self, code='DMA-RTN-1', status='received'):
        pet = make_pet(code=code, status='pending_release',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=None)
        treatment = self._closed_treatment(pet, 'released')
        doc = ReturnOrder.objects.create(
            shelter=self.shelter_a, shelter_name=self.shelter_a.name,
            hospital=self.hospital_a, hospital_name=self.hospital_a.name,
            institution=self.hospital_a, pet_codes=code, pet_count=1,
            status=status, received_at=timezone.localdate(),
            receiver_name='李捕捉', signature='data:image/png;base64,SIG',
            ledger_no=f'RTN-{code}', district=self.district_a)
        return doc, pet, treatment

    def _release(self, code='DMA-REL-1', status='released'):
        pet = make_pet(code=code, status='released', district=self.district_a,
                       shelter=self.shelter_a)
        treatment = self._closed_treatment(pet, 'released')
        doc = Release.objects.create(
            pet=pet, pet_code=code, community=self.community_a,
            community_name=self.community_a.name, institution=self.shelter_a,
            status=status, transferred_at=timezone.now(),
            released_at=timezone.localdate(), receiver_name='张物业',
            signature='data:image/png;base64,SIG', ledger_no=f'REL-{code}',
            district=self.district_a)
        return doc, pet, treatment

    # ---- 调用 ----
    def _set(self, doc_type, doc_id, target, reason='去向登记错误，业务方要求退回重办'):
        return self.post_json(SET_URL, {'doc_type': doc_type, 'doc_id': doc_id,
                                        'target': target, 'reason': reason})

    def _rows(self, **params):
        query = urlencode({k: v for k, v in params.items() if v != ''})
        return self.ok(self.client.get(f'{LIST_URL}?{query}'))['data']


class DocListTest(DocMaintenanceBase):
    """列表页 = 维护入口的唯一取数：每行必须自带「能退回哪些状态」。"""

    def test_list_covers_all_four_types(self):
        self._adoption()
        self._euthanasia()
        self._return_order()
        self._release()
        rows = self._rows()
        self.assertEqual(len(rows), 4, f'四类单据应各出一行：{rows}')
        got = {r['doc_type'] for r in rows}
        self.assertEqual(got, set(DOC_LABELS),
                         '缺类型 = 页面上那类单据永远进不了维护入口')
        for r in rows:
            self.assertEqual(r['doc_label'], DOC_LABELS[r['doc_type']])
            self.assertTrue(r['status_display'], '状态要中文，不能让平台端读英文码')
            self.assertEqual(len(r['targets']), len(r['target_labels']),
                             'targets 与文案必须一一对应，否则按钮会错位')
            self.assertTrue(r['targets'], '已办结单据本页的意义就是可退回')

    def test_return_order_row_survives_without_pet_fk(self):
        """运回单是「多宠一单」，没有 `pet` 外键 —— 列表不能按单宠单据的写法取属性。

        `doc.pet_id` 在这里会抛 `AttributeError`，整个接口 500，
        而 500 在页面上只显示「运回单加载失败」，四类单据一起看不见。
        """
        self._return_order()
        rows = self._rows(doc_type='return_order')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['targets'], ['pending'])
        self.assertEqual(rows[0]['advised'], '',
                         '多宠单据没有单一建议去向，留空而不是猜第一只')

    def test_list_filters_by_type_and_keyword(self):
        self._adoption(code='DMA-ADP-K1')
        self._release(code='DMA-REL-K1')
        self.assertEqual({r['doc_type'] for r in self._rows(doc_type='release')},
                         {'release'})
        hit = self._rows(q='ADP-K1')
        self.assertEqual([r['pet_code'] for r in hit], ['DMA-ADP-K1'])
        self.assertEqual(self._rows(q='不存在的编号'), [])

    def test_reversible_only_skips_docs_without_targets(self):
        """「只看当前可退回」不能把已作废 / 还在办的单据显示成可维护。"""
        self._adoption(code='DMA-ADP-X1', status='cancelled',
                       pet_status='pending_adopt')
        self._release(code='DMA-REL-X1', status='pending')
        self._euthanasia(code='DMA-EUT-X1', outbound=False)
        rows = self._rows(reversible='1')
        # 死亡记录「已登记 · 待出库」在白名单里也没有回退方向（前进是业务方的流程）
        self.assertEqual({r['doc_type'] for r in rows}, set(),
                         f'无可退回单据时不应出任何行：{rows}')
        self.assertEqual(len(self._rows()), 3, '不加筛选仍要看得见全部单据')

    def test_list_rejects_unknown_doc_type(self):
        self.expect_fail(self.client.get(f'{LIST_URL}?doc_type=owner_return'),
                         message='单据类型不合法')

    def test_list_carries_advice_of_the_latest_pending_treatment(self):
        """行上的「建议去向」= 该动物**待办结**诊疗单的建议（与出院分流同一口径）。

        已办结的旧单不算：维护页要给出的是「退回后回去院会落到哪个去向」，
        那张单的 `disposal` 早已写死，再拿它当建议等于把历史当成现状。
        """
        doc, pet, _ = self._adoption()
        row = next(r for r in self._rows(doc_type='adoption') if r['id'] == doc.id)
        self.assertEqual(row['advised'], '', '没有待办结单时建议列留空')

        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='dead', district=self.district_a)
        row = next(r for r in self._rows(doc_type='adoption') if r['id'] == doc.id)
        self.assertEqual(row['advised'], 'dead')
        self.assertEqual(row['advised_display'], '死亡 / 无害化')


class DocTransitionAdoptionTest(DocMaintenanceBase):
    def test_outbound_rollback_restores_all_three_halves(self):
        doc, pet, treatment = self._adoption()
        body = self.ok(self._set('adoption', doc.id, 'pending_claim'))
        self.assertEqual(body['data']['status'], 'pending_claim')
        self.assertEqual(body['data']['targets'], ['cancelled'],
                         '退回后应只剩「作废」这一条退路')

        doc.refresh_from_db()
        self.assertEqual(doc.status, 'pending_claim')
        # 交接凭证必须一并撤回：留着签字和日期，重新出库会被当成「早已办过」
        self.assertIsNone(doc.outbound_at)
        self.assertIsNone(doc.adopted_at)
        self.assertEqual(doc.handover_name, '')
        self.assertEqual(doc.signature, '')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_claim')
        treatment.refresh_from_db()
        self.assertIsNone(treatment.disposed_at, '诊疗单要退回「待出院」')
        self.assertEqual(treatment.disposal, '')
        self.assertEqual(treatment.disposal_advised, 'pending_adopt',
                         '回退不得抹掉医生原本的建议 —— 那是重新开单的分流依据')

    def test_cancel_rollback_returns_pet_to_adoptable_and_hall(self):
        doc, pet, treatment = self._adoption()
        listing = AdoptionHallListing.objects.create(
            pet=pet, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, is_active=False)
        self.ok(self._set('adoption', doc.id, 'cancelled'))
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'cancelled')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')
        listing.refresh_from_db()
        self.assertTrue(listing.is_active, '作废后动物要回领养大厅')
        treatment.refresh_from_db()
        self.assertIsNone(treatment.disposed_at)

    def test_cancel_adoptable_hall_when_no_listing_exists(self):
        """建档时没上架过的动物：作废要**新建**上架记录，不能静默什么都不做。"""
        doc, pet, _ = self._adoption()
        self.ok(self._set('adoption', doc.id, 'cancelled'))
        listing = AdoptionHallListing.objects.filter(pet=pet).first()
        self.assertIsNotNone(listing, '没上架过的动物回不到大厅')
        self.assertTrue(listing.is_active)

    def test_pending_claim_rollback_skips_pet_status_guard(self):
        """「已预约」还没出库，动物本来就还在院（pending_claim）。"""
        doc, pet, _ = self._adoption(status='pending_claim',
                                     pet_status='pending_claim')
        self.ok(self._set('adoption', doc.id, 'cancelled'))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')

    def test_outbound_rollback_refused_when_pet_taken_by_other_flow(self):
        """动物已被别的归宿接走（这里已放归）→ 一张作废的领养单不能把它拽回来。"""
        doc, pet, treatment = self._adoption()
        Pet.objects.filter(pk=pet.pk).update(status='released')
        self.expect_fail(self._set('adoption', doc.id, 'pending_claim'),
                         message='不能撤销领养出库')
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed', '拒绝不得改动单据')
        treatment.refresh_from_db()
        self.assertIsNotNone(treatment.disposed_at)


class DocTransitionDeathTest(DocMaintenanceBase):
    def test_outbound_rollback_clears_handover_keeps_clinical_fact(self):
        doc, pet, treatment = self._euthanasia()
        self.ok(self._set('euthanasia', doc.id, 'registered'))
        doc.refresh_from_db()
        self.assertIsNone(doc.outbound_at)
        self.assertEqual(doc.receiving_org, '')
        self.assertEqual(doc.receiver_name, '')
        self.assertEqual(doc.signature, '')
        self.assertFalse(doc.body_received,
                         '「遗体已领取」必须跟着凭证回退，否则台账自相矛盾')
        self.assertIsNone(doc.body_received_at)
        self.assertIsNone(doc.body_received_by)
        self.assertEqual(doc.reason, '严重外伤无法救治', '临床登记不属于凭证字段')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'euthanized',
                         '已安乐死是临床事实，不因凭证回退而复活')
        treatment.refresh_from_db()
        self.assertIsNotNone(treatment.disposed_at,
                            '打回待出院会让一只死掉的动物出现在出院候选里')

    def test_registered_record_has_no_target(self):
        doc, _, _ = self._euthanasia(outbound=False)
        row = next(r for r in self._rows(doc_type='euthanasia') if r['id'] == doc.id)
        self.assertEqual(row['targets'], [])
        self.expect_fail(self._set('euthanasia', doc.id, 'outbound'),
                         message='不能退回')


class DocTransitionReturnOrderTest(DocMaintenanceBase):
    def test_received_rollback_reopens_order_and_occupies_pet(self):
        doc, pet, treatment = self._return_order()
        self.ok(self._set('return_order', doc.id, 'pending'))
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'pending')
        self.assertIsNone(doc.received_at)
        self.assertEqual(doc.receiver_name, '')
        self.assertEqual(doc.signature, '')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'return_pending', '动物要回到「待运回捕捉点」')
        self.assertEqual(pet.hospital_id, self.hospital_a.id,
                         '签收时清空的医院要挂回发出医院')
        treatment.refresh_from_db()
        self.assertIsNone(treatment.disposed_at)

    def test_rollback_refused_when_pet_already_released(self):
        """签收后捕捉点已经放归 → 重新打开运回单等于两张单同时承诺同一只动物。"""
        doc, pet, _ = self._return_order()
        Pet.objects.filter(pk=pet.pk).update(status='released')
        self.expect_fail(self._set('return_order', doc.id, 'pending'),
                         message='不属于本单据可回退的状态')
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'received')

    def test_rollback_refused_when_pending_release_order_exists(self):
        doc, pet, _ = self._return_order()
        Release.objects.create(
            pet=pet, pet_code=pet.code, institution=self.shelter_a,
            status='pending', district=self.district_a)
        self.expect_fail(self._set('return_order', doc.id, 'pending'),
                         message='已有未办结的放归单')
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'received')

    def test_rejected_or_withdrawn_order_can_be_reopened(self):
        for status in ('rejected', 'withdrawn'):
            with self.subTest(status=status):
                doc, pet, _ = self._return_order(code=f'DMA-RTN-{status}',
                                                 status=status)
                Pet.objects.filter(pk=pet.pk).update(status='in_treatment')
                Treatment.objects.filter(pet=pet).update(disposed_at=None)
                self.ok(self._set('return_order', doc.id, 'pending'))
                doc.refresh_from_db()
                self.assertEqual(doc.status, 'pending')


class DocTransitionReleaseTest(DocMaintenanceBase):
    def test_released_rollback_to_transferred_keeps_pet_pending_release(self):
        doc, pet, treatment = self._release()
        self.ok(self._set('release', doc.id, 'transferred'))
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'transferred')
        self.assertIsNone(doc.released_at)
        self.assertEqual(doc.signature, '')
        self.assertIsNotNone(doc.transferred_at, '「已回小区」这一步仍然成立')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_release')
        treatment.refresh_from_db()
        self.assertIsNotNone(treatment.disposed_at,
                            '动物人已离院，打回待出院会凭空造出一只院里的待出院动物')

    def test_rollback_to_pending_pulls_pet_back_to_shelter_side(self):
        doc, pet, _ = self._release()
        self.ok(self._set('release', doc.id, 'pending'))
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'pending')
        self.assertIsNone(doc.transferred_at)
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_release')

    def test_rollback_does_not_touch_pet_taken_by_other_doc(self):
        """放归单退回时动物已被领养单接走（status=adopted）→ 档案状态由那张单负责。"""
        doc, pet, _ = self._release()
        Pet.objects.filter(pk=pet.pk).update(status='adopted')
        self.ok(self._set('release', doc.id, 'pending'))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'adopted')


class DocMaintenanceGuardTest(DocMaintenanceBase):
    """参数与权限：这类操作直接改动监管台账里的终态记录。"""

    def test_reason_is_required(self):
        doc, _, _ = self._adoption()
        self.expect_fail(self._set('adoption', doc.id, 'pending_claim', reason=''),
                         message='请填写维护原因')
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed')

    def test_reason_length_is_capped(self):
        doc, _, _ = self._adoption()
        self.expect_fail(self._set('adoption', doc.id, 'pending_claim',
                                   reason='长' * (REASON_MAX + 1)),
                         message='不能超过')
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed')

    def test_target_must_be_in_whitelist(self):
        doc, _, _ = self._adoption()
        for target in ('pending', 'released', '', 'deleted'):
            self.expect_fail(self._set('adoption', doc.id, target),
                             message='不能退回' if target else '请选择要退回的状态')
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed')

    def test_forward_transitions_are_not_the_platform_job(self):
        """平台端只纠错：把待办单据往前推是业务角色自己的流程。"""
        doc, _, _ = self._release(status='pending')
        self.expect_fail(self._set('release', doc.id, 'released'),
                         message='不能退回')

    def test_unknown_doc_type_or_id(self):
        self.expect_fail(self._set('owner_return', 1, 'cancelled'),
                         message='单据类型不合法')
        doc, _, _ = self._adoption()
        self.expect_fail(self._set('adoption', doc.id + 9999, 'pending_claim'),
                         message='不存在')
        self.expect_fail(self.post_json(SET_URL, {
            'doc_type': 'adoption', 'doc_id': 'abc', 'target': 'pending_claim',
            'reason': 'x'}), message='主键不合法')

    def test_get_is_rejected(self):
        resp = self.client.get(SET_URL)
        self.assertEqual(resp.status_code, 405)

    def test_only_platform_admin_may_maintain(self):
        doc, _, _ = self._adoption()
        for user, role in ((self.hospital_user_a, 'hospital'),
                           (self.shelter_user_a, 'shelter'),
                           (self.gov_a, 'gov_district'),
                           (self.gov_city, 'gov_city')):
            self.login_as(user)
            self.assertEqual(
                self.post_json(SET_URL, {'doc_type': 'adoption', 'doc_id': doc.id,
                                         'target': 'pending_claim',
                                         'reason': '越权尝试'}).status_code, 403,
                f'{role} 不应有单据回退能力')
            rows = self.client.get(LIST_URL)
            self.assertEqual(rows.status_code, 403, f'{role} 不应读到维护列表')
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed')

    def test_transition_whitelist_is_the_single_source_for_rows(self):
        """白名单每加一条迁移，列表就要能把它交给前端 —— 否则新迁移永远点不到。"""
        for doc_type, table in TRANSITIONS.items():
            labels = status_labels(doc_type)
            for current, targets in table.items():
                self.assertIn(current, labels,
                              f'{doc_type} 的白名单状态「{current}」不在文案表里')
                for target in targets:
                    self.assertIn(target, labels,
                                  f'{doc_type} 的退回目标「{target}」没有中文文案')


class DocMaintenanceAuditTest(DocMaintenanceBase):
    def test_rollback_is_written_to_audit_with_reason(self):
        """无理由不可执行 = 有理由必须可复原。审计里要能看出旧→新与是谁做的。"""
        doc, pet, _ = self._adoption()
        self.ok(self._set('adoption', doc.id, 'pending_claim',
                          reason='TNR261021 领养人临时取消，业务方要求退回重办'))
        log = AuditLog.objects.filter(path='/api/supervision/discharge-docs/status/',
                                      success=True).first()
        self.assertIsNotNone(log, '单据回退必须留痕')
        self.assertEqual(log.module, '出院单据维护')
        self.assertEqual(log.action_flag, AuditLog.ACTION_CHANGE)
        self.assertEqual(log.user, self.platform_admin)
        self.assertEqual(log.district, self.district_a,
                         '归属区县取单据的，不是操作人的（平台管理员不挂区县）')
        self.assertIn('已完成 → 待领出', log.detail)
        self.assertIn('业务方要求退回重办', log.detail)
        self.assertIn(pet.code, log.detail)

    def test_failed_rollback_is_recorded_as_failure(self):
        doc, _, _ = self._adoption()
        self.expect_fail(self._set('adoption', doc.id, 'released'),
                         message='不能退回')
        log = AuditLog.objects.filter(
            path='/api/supervision/discharge-docs/status/').first()
        self.assertIsNotNone(log)
        self.assertFalse(log.success)
