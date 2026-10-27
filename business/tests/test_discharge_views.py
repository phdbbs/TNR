"""出院四条去向（第五十四轮）：领养出库 / 死亡出库 / 运回捕捉点。

这些判据盯的是「单据真的改变了动物状态」，而不是接口返回 200：
出院是动物离开医院的唯一出口，单据与 `Pet.status` 一旦脱节，
动物会同时挂在两条路径上（既被领养又能放归），或凭空从在院名单消失。
"""
from django.utils import timezone

from business.models import (
    Adoption, AdoptionApplication, AdoptionHallListing, Euthanasia, OwnerReturn,
    Pet, Release, ReturnOrder, Treatment,
)
from business.services_discharge import DISCHARGE_LABELS
from business.tests.base import (
    BusinessTestBase, assert_doc_no, make_capture, make_institution, make_pet,
)

RTN_URL = '/api/business/return-orders/'
ADOPT_URL = '/api/business/adoptions/'
EUTH_URL = '/api/business/euthanasia/'


class ReturnOrderBase(BusinessTestBase):
    def setUp(self):
        self.login_as(self.hospital_user_a)
        self.pet = make_pet(
            code='RTN-A-001', status='pending_adopt', district=self.district_a,
            shelter=self.shelter_a, hospital=self.hospital_a)
        Treatment.objects.create(
            pet=self.pet, pet_code=self.pet.code,
            hospital=self.hospital_a, hospital_name=self.hospital_a.name,
            institution=self.hospital_a, status='completed',
            done_at=timezone.now(), disposed_at=timezone.now(),
            district=self.district_a)

    def _create_order(self, pet_ids=None, note='诊疗完成，运回放归', shelter_id=None):
        payload = {
            'pet_ids': pet_ids if pet_ids is not None else [self.pet.id],
            'note': note,
        }
        if shelter_id is not None:
            payload['shelter_id'] = shelter_id
        return self.post_json(f'{RTN_URL}create/', payload)

    def _order(self, resp):
        body = self.ok(resp)
        return ReturnOrder.objects.get(id=body['data']['id'])

    def _receive(self, order, **payload):
        data = {'receiver_name': '李捕捉', 'receiver_phone': '13800001000',
                'signature': 'data:image/png;base64,SIG',
                'received_at': timezone.localdate().isoformat()}
        data.update(payload)
        return self.post_json(f'{RTN_URL}{order.id}/receive/', data)


class ReturnOrderCreateTest(ReturnOrderBase):
    def test_create_marks_pets_return_pending_and_numbered(self):
        order = self._order(self._create_order())
        assert_doc_no(self, order.ledger_no, 'RTN')
        self.assertEqual(order.status, 'pending')
        self.assertEqual(order.pet_count, 1)
        self.assertEqual(order.pet_codes, self.pet.code)
        self.assertEqual(order.hospital, self.hospital_a)
        self.assertEqual(order.shelter, self.shelter_a)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'return_pending')

    def test_create_allows_in_treatment_pet_with_done_treatment(self):
        pet = make_pet(code='RTN-A-002', status='in_treatment',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='completed', done_at=timezone.now(), district=self.district_a,
            disposal_advised='released')
        self._order(self._create_order([pet.id]))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'return_pending')

    def test_create_rejects_in_treatment_without_finished_treatment(self):
        pet = make_pet(code='RTN-A-003', status='in_treatment',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        self.expect_fail(self._create_order([pet.id]),
                         message='尚未完成诊疗')

    def test_create_rejects_other_hospital_pet(self):
        pet = make_pet(code='RTN-B-001', status='pending_adopt',
                       district=self.district_b, shelter=self.shelter_b,
                       hospital=self.hospital_b)
        self.expect_fail(self._create_order([pet.id]),
                         message='当前不在本院')

    def test_create_rejects_duplicate_animal_on_open_order(self):
        self._order(self._create_order())
        self.expect_fail(self._create_order(), message='已在另一张未办结的运回单上')

    def test_create_rejects_active_adoption(self):
        Adoption.objects.create(
            pet=self.pet, pet_code=self.pet.code, adopter_name='领养人',
            adopter_phone='13800002000', status='pending_claim',
            hospital=self.hospital_a, district=self.district_a)
        self.expect_fail(self._create_order(), message='未完结的领养记录')

    def test_create_rejects_approved_application(self):
        AdoptionApplication.objects.create(
            pet=self.pet, pet_code=self.pet.code, applicant_name='申请人',
            applicant_phone='13800005000', status='approved',
            hospital=self.hospital_a, hospital_name=self.hospital_a.name)
        self.expect_fail(self._create_order(), message='已批准的领养申请')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_adopt', '拒绝后不得改动物状态')

    def test_create_rejects_mixed_shelters(self):
        second = make_pet(code='RTN-A-004', status='pending_adopt',
                          district=self.district_a, shelter=self.shelter_b,
                          hospital=self.hospital_a)
        self.expect_fail(self._create_order([self.pet.id, second.id]),
                         message='同一个捕捉点')

    def test_create_requires_animal_selection(self):
        self.expect_fail(self.post_json(f'{RTN_URL}create/', {'pet_ids': []}),
                         message='至少选择一只')


class ReturnOrderShelterTest(ReturnOrderBase):
    def test_list_is_scoped_to_own_shelter(self):
        order = self._order(self._create_order())

        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(RTN_URL))
        self.assertEqual([o['id'] for o in body['data']], [order.id])

        self.login_as(self.shelter_user_b)
        body = self.ok(self.get_json(RTN_URL))
        self.assertEqual(body['data'], [], '乙区捕捉点不应看到甲区的运回单')

    def test_receive_requires_signature_and_receiver(self):
        order = self._order(self._create_order())
        self.login_as(self.shelter_user_a)
        self.expect_fail(self._receive(order, receiver_name=''), message='接收人')
        self.expect_fail(self._receive(order, signature=''), message='签字')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'return_pending', '校验失败不得改动物状态')

    def test_receive_moves_pet_to_pending_release(self):
        order = self._order(self._create_order())
        self.login_as(self.shelter_user_a)
        body = self.ok(self._receive(order))
        self.assertEqual(body['data']['status'], 'received')

        order.refresh_from_db()
        self.assertEqual(order.status, 'received')
        self.assertEqual(order.receiver_name, '李捕捉')
        self.assertTrue(order.signature)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_release')
        self.assertIsNone(self.pet.hospital_id, '接收后动物应离开医院')
        self.assertEqual(self.pet.shelter_id, self.shelter_a.id)

    def test_receive_by_other_shelter_is_404(self):
        order = self._order(self._create_order())
        self.login_as(self.shelter_user_b)
        self.post_json(f'{RTN_URL}{order.id}/receive/', {
            'receiver_name': '李捕捉', 'signature': 'data:image/png;base64,SIG'})
        resp = self._receive(order)
        self.expect_fail(resp, status=404, message='无权访问')

    def test_receive_twice_rejected(self):
        order = self._order(self._create_order())
        self.login_as(self.shelter_user_a)
        self.ok(self._receive(order))
        self.expect_fail(self._receive(order), message='不可确认接收')

    def test_reject_restores_pending_adopt(self):
        order = self._order(self._create_order())
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{RTN_URL}{order.id}/reject/', {}),
                         message='驳回原因')
        body = self.ok(self.post_json(f'{RTN_URL}{order.id}/reject/',
                                      {'reject_reason': '动物状态与单据不符'}))
        self.assertEqual(body['data']['status'], 'rejected')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_adopt', '办结过的动物应退回待领养')

    def test_reject_restores_in_treatment_when_not_disposed(self):
        pet = make_pet(code='RTN-A-005', status='in_treatment',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='completed', done_at=timezone.now(), district=self.district_a,
            disposal_advised='released')
        order = self._order(self._create_order([pet.id]))

        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{RTN_URL}{order.id}/reject/',
                               {'reject_reason': '诊疗记录未办结'}))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment',
                         '还没办结的诊疗不能因驳回而凭空变成待领养')

    def test_withdraw_restores_pet_and_blocks_after_receive(self):
        order = self._order(self._create_order())
        body = self.ok(self.post_json(f'{RTN_URL}{order.id}/withdraw/', {}))
        self.assertEqual(body['data']['status'], 'withdrawn')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_adopt')
        self.expect_fail(self.post_json(f'{RTN_URL}{order.id}/withdraw/', {}),
                         message='不可撤回')

        received = self._order(self._create_order())
        self.login_as(self.shelter_user_a)
        self.ok(self._receive(received))
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{RTN_URL}{received.id}/withdraw/', {}),
                         message='不可撤回')

    def test_receive_closes_done_treatment(self):
        """第五十六轮：诊疗页只记建议去向，捕捉点签收才是出院终态。"""
        pet = make_pet(code='RTN-A-006', status='in_treatment',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        treatment = Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='released', district=self.district_a)
        order = self._order(self._create_order([pet.id]))

        self.login_as(self.shelter_user_a)
        self.ok(self._receive(order))

        treatment.refresh_from_db()
        self.assertIsNotNone(treatment.disposed_at, '签收后诊疗单必须办结')
        self.assertEqual(treatment.disposal, 'released')
        self.assertEqual(treatment.status, 'completed')

    def test_shelter_release_accepts_received_return_order(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a,
                               community_name='阳光花园', pet_codes=[self.pet.code])
        self.pet.capture = capture
        self.pet.save(update_fields=['capture'])
        order = self._order(self._create_order())
        self.login_as(self.shelter_user_a)
        self.ok(self._receive(order))

        body = self.ok(self.post_json('/api/business/releases/create/',
                                      {'pet_id': self.pet.id}))
        self.assertEqual(Release.objects.get(id=body['data']['id']).status, 'pending')


class ReturnOrderShelterScopeTest(ReturnOrderBase):
    """运回单的接收捕捉点：市级，或本院所在区县；其他区县一律不可选。"""

    def setUp(self):
        super().setUp()
        self.city_shelter = make_institution(
            type='shelter', district=self.city, name='市级捕捉点')

    def test_options_include_city_and_own_district_only(self):
        body = self.ok(self.get_json(f'{RTN_URL}shelters/'))
        ids = {s['id'] for s in body['data']}
        self.assertIn(self.city_shelter.id, ids, '市级捕捉点必须可选')
        self.assertIn(self.shelter_a.id, ids, '本院所在区县捕捉点必须可选')
        self.assertNotIn(self.shelter_b.id, ids, '其他区县捕捉点不得出现')
        city_item = next(s for s in body['data'] if s['id'] == self.city_shelter.id)
        self.assertTrue(city_item['is_city'])
        self.assertEqual(city_item['district_name'], self.city.name)

    def test_create_accepts_city_shelter(self):
        order = self._order(self._create_order(shelter_id=self.city_shelter.id))
        self.assertEqual(order.shelter_id, self.city_shelter.id)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'return_pending')

    def test_create_rejects_other_district_shelter(self):
        self.expect_fail(self._create_order(shelter_id=self.shelter_b.id),
                         message='只能选择市级捕捉点')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_adopt', '拒绝后不得改动物状态')
        self.assertFalse(ReturnOrder.objects.exists())

    def test_create_rejects_unknown_shelter(self):
        self.expect_fail(self._create_order(shelter_id=999999),
                         message='不存在或已停用')


class ShelterReleaseCandidateTest(BusinessTestBase):
    """捕捉点放归只能选医院已运回、运回单已签收的动物。"""

    def test_release_rejects_in_treatment_pet(self):
        pet = make_pet(code='REL-C-001', status='in_treatment',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json('/api/business/releases/create/',
                                        {'pet_id': pet.id}),
                         message='经本站签收')
        self.assertFalse(Release.objects.filter(pet=pet).exists())


class PetStageDerivedTest(BusinessTestBase):
    """一宠一档的阶段按未办结单据派生，而不是死读 `Pet.status`。

    真实案例 TNR261018002：诊疗与出院流程早已推进，`Pet.status` 却停在
    `in_treatment`，档案因此显示「待诊疗」，详情里却有放归记录。
    """

    def _stage(self, pet):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'))
        return body['data']['pet_brief']['stage'], \
            body['data']['pet_brief']['stage_display']

    def test_open_return_order_wins(self):
        pet = make_pet(code='STG-A-001', status='return_pending',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        ReturnOrder.objects.create(
            shelter=self.shelter_a, shelter_name=self.shelter_a.name,
            hospital=self.hospital_a, hospital_name=self.hospital_a.name,
            institution=self.hospital_a, pet_codes=pet.code, pet_count=1,
            status='pending', district=self.district_a)
        self.assertEqual(self._stage(pet), ('return_pending', '待运回捕捉点'))

    def test_transferred_release_shows_pending_release(self):
        pet = make_pet(code='TNR261018002', status='in_treatment',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        Release.objects.create(
            pet=pet, pet_code=pet.code, institution=self.shelter_a,
            status='transferred', district=self.district_a,
            ledger_no='REL-HIST-002')
        self.assertEqual(self._stage(pet), ('pending_release', '待放归'))

    def test_done_treatment_without_discharge_shows_pending_discharge(self):
        pet = make_pet(code='STG-A-003', status='in_treatment',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='pending_adopt', district=self.district_a)
        self.assertEqual(self._stage(pet), ('pending_discharge', '待出院'))

    def test_terminal_status_is_not_overridden_by_stale_documents(self):
        pet = make_pet(code='STG-A-004', status='released',
                       district=self.district_a, shelter=self.shelter_a)
        Release.objects.create(
            pet=pet, pet_code=pet.code, institution=self.shelter_a,
            status='transferred', district=self.district_a,
            ledger_no='REL-HIST-004')
        self.assertEqual(self._stage(pet), ('released', '已放归'))


class AdoptionOutboundTest(BusinessTestBase):
    def setUp(self):
        self.login_as(self.hospital_user_a)
        self.pet = make_pet(code='ADP-A-001', status='pending_claim',
                            district=self.district_a, shelter=self.shelter_a,
                            hospital=self.hospital_a)
        self.adoption = Adoption.objects.create(
            pet=self.pet, pet_code=self.pet.code, adopter=self.adopter,
            adopter_name='领养人', adopter_phone='13800003000',
            hospital=self.hospital_a, hospital_name=self.hospital_a.name,
            institution=self.hospital_a, status='pending_claim',
            district=self.district_a)

    def _payload(self, **kw):
        data = {'handover_name': '王医生',
                'signature': 'data:image/png;base64,SIG',
                'outbound_at': timezone.localdate().isoformat()}
        data.update(kw)
        return data

    def test_outbound_requires_handover_fields(self):
        url = f'{ADOPT_URL}{self.adoption.id}/outbound/'
        self.expect_fail(self.post_json(url, self._payload(handover_name='')),
                         message='交接人')
        self.expect_fail(self.post_json(url, self._payload(signature='')),
                         message='签字')
        self.expect_fail(self.post_json(url, self._payload(outbound_at='')),
                         message='出库日期')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_claim', '校验失败不得改动物状态')

    def test_outbound_completes_adoption_and_marks_pet_adopted(self):
        body = self.ok(self.post_json(
            f'{ADOPT_URL}{self.adoption.id}/outbound/', self._payload()))
        data = body['data']
        self.assertEqual(data['status'], 'completed')
        self.assertEqual(data['handover_name'], '王医生')
        self.assertTrue(data['signature'])
        self.assertEqual(data['outbound_at'], timezone.localdate().isoformat())
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'adopted')

    def test_outbound_closes_done_treatment_without_relisting_hall(self):
        """领养出库才是出院终态；已领养的动物不得被「建议领养」重新上架大厅。"""
        treatment = Treatment.objects.create(
            pet=self.pet, pet_code=self.pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='pending_adopt', district=self.district_a)
        listing = AdoptionHallListing.objects.create(
            pet=self.pet, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, is_active=False,
            published_at=timezone.localdate())

        self.ok(self.post_json(
            f'{ADOPT_URL}{self.adoption.id}/outbound/', self._payload()))

        treatment.refresh_from_db()
        self.assertIsNotNone(treatment.disposed_at, '出库后诊疗单必须办结')
        self.assertEqual(treatment.disposal, 'pending_adopt')
        listing.refresh_from_db()
        self.assertFalse(listing.is_active, '已领养的动物不得重新上架领养大厅')

    def test_outbound_twice_rejected(self):
        url = f'{ADOPT_URL}{self.adoption.id}/outbound/'
        self.ok(self.post_json(url, self._payload()))
        self.expect_fail(self.post_json(url, self._payload()), message='不可出库')

    def test_outbound_from_other_hospital_is_404(self):
        self.login_as(self.hospital_user_b)
        resp = self.post_json(f'{ADOPT_URL}{self.adoption.id}/outbound/',
                              self._payload())
        self.expect_fail(resp, status=404, message='无权访问')

    def test_application_outbound_creates_completed_adoption(self):
        app_pet = make_pet(code='ADP-A-002', status='pending_adopt',
                           district=self.district_a, shelter=self.shelter_a,
                           hospital=self.hospital_a)
        application = AdoptionApplication.objects.create(
            pet=app_pet, pet_code=app_pet.code, applicant=self.adopter,
            applicant_name='申请人', applicant_phone='13800004000',
            status='approved', hospital=self.hospital_a,
            hospital_name=self.hospital_a.name)
        url = f'{ADOPT_URL}applications/{application.id}/outbound/'
        body = self.ok(self.post_json(url, self._payload()))
        self.assertEqual(body['data']['status'], 'completed')
        self.assertEqual(body['data']['pet_id'], app_pet.id)
        app_pet.refresh_from_db()
        self.assertEqual(app_pet.status, 'adopted')
        self.expect_fail(self.post_json(url, self._payload()),
                         message='已完成领养出库')

    def test_application_outbound_closes_done_treatment(self):
        app_pet = make_pet(code='ADP-A-006', status='pending_adopt',
                           district=self.district_a, shelter=self.shelter_a,
                           hospital=self.hospital_a)
        treatment = Treatment.objects.create(
            pet=app_pet, pet_code=app_pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='pending_adopt', district=self.district_a)
        application = AdoptionApplication.objects.create(
            pet=app_pet, pet_code=app_pet.code, applicant=self.adopter,
            applicant_name='申请人', applicant_phone='13800004002',
            status='approved', hospital=self.hospital_a,
            hospital_name=self.hospital_a.name)
        self.ok(self.post_json(
            f'{ADOPT_URL}applications/{application.id}/outbound/', self._payload()))
        treatment.refresh_from_db()
        self.assertIsNotNone(treatment.disposed_at)
        self.assertEqual(treatment.disposal, 'pending_adopt')

    def test_application_outbound_rejects_open_return_order(self):
        app_pet = make_pet(code='ADP-A-003', status='return_pending',
                           district=self.district_a, shelter=self.shelter_a,
                           hospital=self.hospital_a)
        application = AdoptionApplication.objects.create(
            pet=app_pet, pet_code=app_pet.code, applicant=self.adopter,
            applicant_name='申请人', applicant_phone='13800004001',
            status='approved', hospital=self.hospital_a,
            hospital_name=self.hospital_a.name)
        ReturnOrder.objects.create(
            shelter=self.shelter_a, shelter_name=self.shelter_a.name,
            hospital=self.hospital_a, hospital_name=self.hospital_a.name,
            institution=self.hospital_a, pet_codes=app_pet.code, pet_count=1,
            status='pending', district=self.district_a)
        self.expect_fail(
            self.post_json(f'{ADOPT_URL}applications/{application.id}/outbound/',
                           self._payload()),
            message='运回捕捉点')
        app_pet.refresh_from_db()
        self.assertEqual(app_pet.status, 'return_pending',
                         '拦截后不得把动物改成已领养')


class EuthanasiaOutboundTest(BusinessTestBase):
    def setUp(self):
        self.login_as(self.hospital_user_a)
        self.pet = make_pet(code='EUT-A-001', status='euthanized',
                            district=self.district_a, shelter=self.shelter_a,
                            hospital=self.hospital_a)
        self.record = Euthanasia.objects.create(
            pet=self.pet, pet_code=self.pet.code, kind='euthanasia',
            hospital=self.hospital_a, hospital_name=self.hospital_a.name,
            institution=self.hospital_a, reason='严重外伤无法救治',
            euthanized_at=timezone.localdate(), district=self.district_a)

    def _payload(self, **kw):
        data = {'handover_name': '王医生',
                'receiving_org': '市无害化处理中心',
                'receiver_name': '赵师傅',
                'signature': 'data:image/png;base64,SIG',
                'outbound_at': timezone.localdate().isoformat()}
        data.update(kw)
        return data

    def test_outbound_requires_every_handover_field(self):
        url = f'{EUTH_URL}{self.record.id}/outbound/'
        for field, message in (('handover_name', '交接人'),
                               ('receiving_org', '接收机构'),
                               ('receiver_name', '接收人'),
                               ('signature', '签字'),
                               ('outbound_at', '出库日期')):
            self.expect_fail(self.post_json(url, self._payload(**{field: ''})),
                             message=message)
        self.record.refresh_from_db()
        self.assertIsNone(self.record.outbound_at)

    def test_outbound_records_handover_and_body_receipt(self):
        body = self.ok(self.post_json(
            f'{EUTH_URL}{self.record.id}/outbound/', self._payload()))
        data = body['data']
        self.assertEqual(data['receiving_org'], '市无害化处理中心')
        self.assertEqual(data['receiver_name'], '赵师傅')
        self.assertTrue(data['signature'])
        self.record.refresh_from_db()
        self.assertEqual(self.record.outbound_at, timezone.localdate())
        self.assertTrue(self.record.body_received, '出库即遗体已交接')
        self.assertEqual(self.record.body_received_at, timezone.localdate())
        self.assertEqual(self.record.body_received_by, self.hospital_user_a)

    def test_outbound_closes_done_treatment(self):
        """死亡出库单提交才是出院终态（诊疗页只记了建议去向）。"""
        treatment = Treatment.objects.create(
            pet=self.pet, pet_code=self.pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='euthanized', district=self.district_a)
        self.ok(self.post_json(
            f'{EUTH_URL}{self.record.id}/outbound/', self._payload()))
        treatment.refresh_from_db()
        self.assertIsNotNone(treatment.disposed_at)
        self.assertEqual(treatment.disposal, 'euthanized')

    def test_dead_outbound_closes_treatment_as_dead(self):
        self.pet.status = 'dead'
        self.pet.save(update_fields=['status'])
        self.record.kind = 'dead'
        self.record.save(update_fields=['kind'])
        treatment = Treatment.objects.create(
            pet=self.pet, pet_code=self.pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='dead', district=self.district_a)
        self.ok(self.post_json(
            f'{EUTH_URL}{self.record.id}/outbound/', self._payload()))
        treatment.refresh_from_db()
        self.assertEqual(treatment.disposal, 'dead',
                         '死亡/无害化与安乐死必须分得开')

    def test_outbound_twice_rejected(self):
        url = f'{EUTH_URL}{self.record.id}/outbound/'
        self.ok(self.post_json(url, self._payload()))
        self.expect_fail(self.post_json(url, self._payload()), message='重复操作')

    def test_outbound_from_other_hospital_is_404(self):
        self.login_as(self.hospital_user_b)
        resp = self.post_json(f'{EUTH_URL}{self.record.id}/outbound/',
                              self._payload())
        self.expect_fail(resp, status=404, message='无权访问')

    def test_create_rejects_pet_on_open_return_order(self):
        pet = make_pet(code='EUT-A-002', status='return_pending',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        ReturnOrder.objects.create(
            shelter=self.shelter_a, shelter_name=self.shelter_a.name,
            hospital=self.hospital_a, hospital_name=self.hospital_a.name,
            institution=self.hospital_a, pet_codes=pet.code, pet_count=1,
            status='pending', district=self.district_a)
        self.expect_fail(self.post_json(f'{EUTH_URL}create/', {
            'pet_id': pet.id, 'kind': 'euthanasia',
            'reason': '伤病',
            'euthanized_at': timezone.localdate().isoformat(),
        }), message='请先撤回或等待捕捉点处理')

    def test_create_rejects_approved_application(self):
        pet = make_pet(code='EUT-A-003', status='pending_adopt',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        AdoptionApplication.objects.create(
            pet=pet, pet_code=pet.code, applicant_name='申请人',
            applicant_phone='13800006000', status='approved',
            hospital=self.hospital_a, hospital_name=self.hospital_a.name)
        self.expect_fail(self.post_json(f'{EUTH_URL}create/', {
            'pet_id': pet.id, 'kind': 'euthanasia', 'reason': '伤病',
            'euthanized_at': timezone.localdate().isoformat(),
        }), message='已批准的领养申请')


class DisposalReservationTest(BusinessTestBase):
    """已批准的线上申请会预留动物，但申请单不改 `Pet.status`。

    运回 / 放归 / 领回 / 再次批准都必须看得见这张申请，否则同一只动物会
    同时被领养人和其他去向承诺，两条路径都办结后互相覆盖宠物状态。
    """

    def setUp(self):
        self.login_as(self.hospital_user_a)
        self.pet = make_pet(code='RSV-A-001', status='pending_adopt',
                            district=self.district_a, shelter=self.shelter_a,
                            hospital=self.hospital_a)
        Treatment.objects.create(
            pet=self.pet, pet_code=self.pet.code,
            hospital=self.hospital_a, hospital_name=self.hospital_a.name,
            institution=self.hospital_a, status='completed',
            done_at=timezone.now(), disposed_at=timezone.now(),
            district=self.district_a)
        self.application = AdoptionApplication.objects.create(
            pet=self.pet, pet_code=self.pet.code, applicant=self.adopter,
            applicant_name='申请人', applicant_phone='13800007000',
            status='approved', hospital=self.hospital_a,
            hospital_name=self.hospital_a.name)

    def test_release_rejects_approved_application(self):
        self.expect_fail(self.post_json('/api/business/releases/create/',
                                        {'pet_id': self.pet.id}),
                         message='已批准的领养申请')

    def test_owner_return_rejects_approved_application(self):
        self.expect_fail(self.post_json('/api/business/owner-returns/create/', {
            'pet_id': self.pet.id, 'owner_name': '主人',
            'owner_phone': '13800008000', 'reason': '主人认领',
        }), message='已批准的领养申请')

    def test_owner_return_creation_closes_done_treatment(self):
        """医院端领回单当场完成交接，因此创建即出院终态。"""
        pet = make_pet(code='OWN-A-001', status='in_treatment',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        treatment = Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='owner_returned', district=self.district_a)

        self.ok(self.post_json('/api/business/owner-returns/create/', {
            'pet_id': pet.id, 'owner_name': '张三',
            'owner_phone': '13800001234', 'reason': '主人认领',
        }))

        treatment.refresh_from_db()
        self.assertIsNotNone(treatment.disposed_at, '领回后诊疗单必须办结')
        self.assertEqual(treatment.disposal, 'owner_returned')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'owner_returned')

    def test_review_cannot_approve_second_application(self):
        second = AdoptionApplication.objects.create(
            pet=self.pet, pet_code=self.pet.code,
            applicant_name='另一位申请人', applicant_phone='13800009000',
            status='pending', hospital=self.hospital_a,
            hospital_name=self.hospital_a.name)
        # 第六十四轮：审核在捕捉点，医院没有这个权力（此处 setUp 登录的是医院）
        self.login_as(self.shelter_user_a)
        self.expect_fail(
            self.post_json(f'{ADOPT_URL}applications/{second.id}/review/',
                           {'action': 'approve'}),
            message='已批准的领养申请')
        second.refresh_from_db()
        self.assertEqual(second.status, 'pending', '拒绝后申请单不得变成已通过')


class DischargeAdviceGateTest(BusinessTestBase):
    """出院四个去向的候选 = 最后一张待办结诊疗单的「建议去向」（第五十七轮）。

    判据必须在后端：前端列表分流只是让人少白点一次，直接 POST 才是真缺口 ——
    建议「放归」的动物若能从这里开出领养单，同一只动物就有了两个归宿，
    而且两张单据都能走到「已办结」，台账上互相覆盖动物状态。
    """

    def setUp(self):
        self.login_as(self.hospital_user_a)

    def _pending(self, code, advised='pending_adopt'):
        """一只「诊疗完成、未办结」的动物（= 待出院），建议去向 = `advised`。"""
        pet = make_pet(code=code, status='in_treatment', district=self.district_a,
                       shelter=self.shelter_a, hospital=self.hospital_a)
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised=advised, district=self.district_a)
        return pet

    def _doc(self, kind, pet):
        """按去向开对应出院单据，返回响应（成功与失败都由用例自己判）。"""
        if kind == 'adoption':
            return self.post_json(f'{ADOPT_URL}register/', {
                'pet_id': pet.id, 'adopter_name': '李领养',
                'adopter_phone': '13800002101'})
        if kind == 'death':
            return self.post_json(f'{EUTH_URL}create/', {
                'pet_id': pet.id, 'kind': 'euthanasia',
                'reason': '伤病无法救治',
                'euthanized_at': timezone.localdate().isoformat()})
        if kind == 'owner':
            return self.post_json('/api/business/owner-returns/create/', {
                'pet_id': pet.id, 'owner_name': '张领回',
                'owner_phone': '13800002102'})
        if kind == 'release':
            return self.post_json('/api/business/releases/create/',
                                  {'pet_id': pet.id})
        return self.post_json(f'{RTN_URL}create/', {'pet_ids': [pet.id]})

    def _reject_others(self, pet, blocked):
        """`blocked` 里每个去向都要 400，而且**一条单据都不落库**。

        `return`（运回单）与 `release`（医院直接放归）共用服务层去向 key
        `release`，所以文案里的去向名按 `DISCHARGE_LABELS` 取。
        """
        for kind in blocked:
            label = DISCHARGE_LABELS['release' if kind == 'return' else kind]
            self.expect_fail(self._doc(kind, pet), message=f'不能按「{label}」办理出院')
        self.assertEqual(Adoption.objects.count(), 0, '拒绝后不得留下领养单')
        self.assertEqual(Euthanasia.objects.count(), 0, '拒绝后不得留下死亡登记')
        self.assertEqual(ReturnOrder.objects.count(), 0, '拒绝后不得留下运回单')
        self.assertEqual(Release.objects.count(), 0, '拒绝后不得留下放归单')
        self.assertEqual(OwnerReturn.objects.count(), 0, '拒绝后不得留下领回单')

    def test_adoption_advice_blocks_other_three(self):
        pet = self._pending('ADV-A-001', advised='pending_adopt')
        self._reject_others(pet, ('death', 'owner', 'release', 'return'))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment', '拒绝不得推动动物出院')
        self.ok(self._doc('adoption', pet))

    def test_release_advice_blocks_other_three(self):
        pet = self._pending('ADV-A-002', advised='released')
        self._reject_others(pet, ('adoption', 'death', 'owner'))
        self.ok(self._doc('return', pet))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'return_pending')

    def test_owner_advice_blocks_other_three(self):
        pet = self._pending('ADV-A-003', advised='owner_returned')
        self._reject_others(pet, ('adoption', 'death', 'release', 'return'))
        self.ok(self._doc('owner', pet))

    def test_death_advice_accepts_both_euthanasia_and_dead(self):
        """「安乐死」与「死亡 / 无害化」共用一张登记表单，两个建议 key 都要放行。"""
        for advised in ('euthanized', 'dead'):
            pet = self._pending(f'ADV-A-004-{advised}', advised=advised)
            self._reject_others(pet, ('adoption', 'owner', 'release', 'return'))
            self.ok(self._doc('death', pet))
            Euthanasia.objects.all().delete()

    def test_blank_advice_blocks_all_four(self):
        """没补录建议 = 四个去向全部拒绝，并引导先去诊疗看板补录。

        放行其中任何一个（旧行为）就等于「没判断也能出院」，
        而界面按建议分流，操作员根本不知道自己点的是哪条路径。
        """
        pet = self._pending('ADV-A-005', advised='')
        for kind in ('adoption', 'death', 'owner', 'release', 'return'):
            self.expect_fail(self._doc(kind, pet), message='没有记录建议去向')
        self.assertEqual(Adoption.objects.count(), 0)
        self.assertEqual(Euthanasia.objects.count(), 0)
        self.assertEqual(ReturnOrder.objects.count(), 0)
        self.assertEqual(Release.objects.count(), 0)
        self.assertEqual(OwnerReturn.objects.count(), 0)
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment')

    def test_latest_pending_treatment_decides(self):
        """多张待办结单要看**最后一张**的建议（复查后医生改了判断）。"""
        pet = self._pending('ADV-A-006', advised='released')
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='pending_adopt', district=self.district_a)
        self.expect_fail(self._doc('return', pet), message='不能按「放归」办理出院')
        self.ok(self._doc('adoption', pet))

    def test_pet_without_pending_treatment_is_not_gated(self):
        """不在待出院流程里的动物不做建议校验（存量行 / 没走诊疗流程）。

        一刀切会让这批动物**再也办不了出院**：它们的诊疗单要么没有、
        要么早已办结，可比的对不存在。
        """
        pet = make_pet(code='ADV-A-007', status='pending_adopt',
                       district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a)
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='completed', done_at=timezone.now(),
            disposed_at=timezone.now(), disposal='pending_adopt',
            district=self.district_a)
        self.ok(self._doc('adoption', pet))


class ReleaseConfirmClosesTreatmentTest(BusinessTestBase):
    """小区签字确认放归才是出院终态（直接放归、没走运回单的路径）。"""

    def setUp(self):
        self.pet = make_pet(code='REL-A-001', status='pending_release',
                            district=self.district_a, shelter=self.shelter_a)
        self.treatment = Treatment.objects.create(
            pet=self.pet, pet_code=self.pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='in_progress', done_at=timezone.now(),
            disposal_advised='released', district=self.district_a)
        self.release = Release.objects.create(
            pet=self.pet, pet_code=self.pet.code,
            institution=self.shelter_a, status='pending',
            district=self.district_a, ledger_no='REL-TEST-001')

    def test_confirm_closes_done_treatment(self):
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(
            f'/api/business/releases/{self.release.id}/confirm/', {
                'receiver_name': '张物业', 'receiver_phone': '13800003001',
                'signature': 'data:image/png;base64,SIG',
            }))
        self.treatment.refresh_from_db()
        self.assertIsNotNone(self.treatment.disposed_at)
        self.assertEqual(self.treatment.disposal, 'released')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'released')
