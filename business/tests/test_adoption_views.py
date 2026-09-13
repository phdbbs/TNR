"""领养业务视图测试：公开大厅、线下登记、在线申请、审核、信息编辑。"""
from accounts.models import User
from business.models import (
    Adoption, AdoptionApplication, AdoptionHallListing, Message,
)
from business.tests.base import (
    BusinessTestBase, make_institution, make_pet,
)

HALL_URL = '/api/business/adoptions/hall/'


class AdoptionHallPublicTest(BusinessTestBase):
    def test_public_list_without_login(self):
        pet = make_pet(district=self.district_a, status='pending_adopt',
                       hospital=self.hospital_a)
        AdoptionHallListing.objects.create(pet=pet, hospital=self.hospital_a)
        resp = self.client.get(HALL_URL)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['pet']['id'], pet.id)

    def test_inactive_listings_hidden(self):
        pet = make_pet(district=self.district_a, status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet, is_active=False)
        self.assertEqual(len(self.client.get(HALL_URL).json()['data']), 0)

    def test_detail_and_404(self):
        pet = make_pet(district=self.district_a, status='pending_adopt')
        listing = AdoptionHallListing.objects.create(pet=pet)
        resp = self.client.get(f'{HALL_URL}{listing.id}/')
        self.assertEqual(resp.json()['data']['pet']['id'], pet.id)
        self.expect_fail(self.client.get(f'{HALL_URL}999999/'), status=404,
                  message='领养信息不存在')


class AdoptionRegisterTest(BusinessTestBase):
    URL = '/api/business/adoptions/register/'

    def _pet(self):
        return make_pet(district=self.district_a, shelter=self.shelter_a,
                        hospital=self.hospital_a, status='pending_adopt')

    def _payload(self, pet, **kw):
        payload = {'pet_id': pet.id, 'adopter_name': '王领养',
                   'adopter_phone': '13866667777',
                   'adopter_id_card': '110101199001011234',
                   'qualification': '有稳定住所'}
        payload.update(kw)
        return payload

    def test_register_creates_adopter_account_and_record(self):
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(self.URL, self._payload(pet)))
        adoption = Adoption.objects.get(id=body['data']['id'])
        self.assertEqual(adoption.status, 'pending_claim')
        self.assertTrue(adoption.ledger_no.startswith('ADP-'))

        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_claim')

        adopter = adoption.adopter
        self.assertEqual(adopter.role, 'adopter')
        self.assertEqual(adopter.username, '13866667777')
        self.assertEqual(adopter.phone, '13866667777')
        self.assertTrue(adopter.check_password('123456'))
        self.assertTrue(Message.objects.filter(user=adopter).exists())

    def test_register_reuses_existing_adopter_by_phone(self):
        existing = User.objects.create_user(username='existing_adopter',
                                            password='x', role='adopter',
                                            phone='13866667777')
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(self.URL, self._payload(pet)))
        adoption = Adoption.objects.get(id=body['data']['id'])
        self.assertEqual(adoption.adopter, existing)

    def test_register_deactivates_hall_listing(self):
        pet = self._pet()
        AdoptionHallListing.objects.create(pet=pet)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.URL, self._payload(pet)))
        pet.refresh_from_db()
        self.assertFalse(pet.hall_listing.is_active)

    def test_register_blacklist_blocked(self):
        from business.models import Blacklist
        Blacklist.objects.create(name='王老赖', phone='13866667777', reason='弃养',
                                 district=self.district_a)
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)),
                  message='该领养人已在黑名单中')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')

    def test_register_missing_fields(self):
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, self._payload(pet, adopter_name='')),
                  message='领养人姓名不能为空')
        self.expect_fail(self.post_json(self.URL, self._payload(pet, adopter_phone='')),
                  message='领养人电话不能为空')

    def test_register_wrong_status(self):
        pet = make_pet(district=self.district_a, status='released')
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)), message='不可领养')

    def test_hospital_cannot_register(self):
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)), status=403)


class AdoptionConfirmClaimTest(BusinessTestBase):
    def _pending_claim(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_claim')
        adopter = User.objects.create_user(username='claim_adopter', password='x',
                                           role='adopter')
        adoption = Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter=adopter,
            adopter_name='领养人', adopter_phone='13800001111',
            hospital=self.hospital_a, status='pending_claim',
            ledger_no='ADP-T1', district=self.district_a)
        return pet, adopter, adoption

    def test_confirm_success(self):
        pet, adopter, adoption = self._pending_claim()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(
            f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}))
        self.assertEqual(body['data']['status'], 'completed')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'adopted')
        adoption.refresh_from_db()
        self.assertIsNotNone(adoption.adopted_at)
        self.assertTrue(Message.objects.filter(user=adopter, title='领养完成').exists())

    def test_confirm_twice_rejected(self):
        _, _, adoption = self._pending_claim()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}))
        self.expect_fail(self.post_json(f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}),
                  message='不可确认领出')

    def test_confirm_wrong_hospital_rejected(self):
        _, _, adoption = self._pending_claim()
        self.login_as(self.hospital_user_b)
        self.expect_fail(self.post_json(f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}),
                  message='无权确认此领养记录')

    def test_adopter_cannot_confirm(self):
        _, _, adoption = self._pending_claim()
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}),
                  status=403)


class AdoptionApplyTest(BusinessTestBase):
    URL = '/api/business/adoptions/apply/'

    def _pet(self):
        return make_pet(district=self.district_a, hospital=self.hospital_a,
                        status='pending_adopt')

    def _payload(self, pet, **kw):
        payload = {'pet_id': pet.id, 'applicant_name': '李申请',
                   'applicant_phone': '13755556666', 'reason': '喜欢'}
        payload.update(kw)
        return payload

    def test_apply_success(self):
        pet = self._pet()
        self.login_as(self.adopter)
        body = self.ok(self.post_json(self.URL, self._payload(pet)))
        application = AdoptionApplication.objects.get(id=body['data']['id'])
        self.assertEqual(application.status, 'pending')
        self.assertEqual(application.hospital, self.hospital_a)

    def test_apply_duplicate_rejected(self):
        pet = self._pet()
        self.login_as(self.adopter)
        self.ok(self.post_json(self.URL, self._payload(pet)))
        self.expect_fail(self.post_json(self.URL, self._payload(pet)),
                  message='请勿重复提交')

    def test_apply_rejected_then_can_reapply(self):
        pet = self._pet()
        self.login_as(self.adopter)
        first = self.ok(self.post_json(self.URL, self._payload(pet)))['data']['id']
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(
            f'/api/business/adoptions/applications/{first}/review/',
            {'action': 'reject', 'review_note': '不符合'}))
        self.login_as(self.adopter)
        body2 = self.post_json(self.URL, self._payload(pet))
        self.assertTrue(body2.json()['success'],
                        '被拒后可再次申请: ' + body2.content.decode())

    def test_apply_blacklist_blocked(self):
        from business.models import Blacklist
        Blacklist.objects.create(name='李申请', phone='13755556666', reason='虚假信息',
                                 district=self.district_a)
        pet = self._pet()
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)),
                  message='您已被列入领养黑名单')

    def test_apply_missing_name(self):
        pet = self._pet()
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(self.URL, self._payload(pet, applicant_name='')),
                  message='请填写姓名')

    def test_apply_wrong_status(self):
        pet = make_pet(district=self.district_a, status='adopted')
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)), message='不可申请领养')

    def test_hospital_cannot_apply(self):
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)), status=403)


class AdoptionApplicationReviewTest(BusinessTestBase):
    def _application(self, status='pending'):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_adopt')
        applicant = User.objects.create_user(username='app_user', password='x',
                                             role='adopter')
        return AdoptionApplication.objects.create(
            pet=pet, pet_code=pet.code, applicant=applicant,
            applicant_name='申请者', applicant_phone='13911112222',
            hospital=self.hospital_a, status=status)
    def test_review_approve_sends_message(self):
        application = self._application()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'approve', 'review_note': '资质符合'}))
        self.assertEqual(body['data']['status'], 'approved')
        application.refresh_from_db()
        self.assertEqual(application.reviewed_by, self.hospital_user_a)
        self.assertTrue(Message.objects.filter(user=application.applicant).exists())

    def test_review_reject(self):
        application = self._application()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'reject', 'review_note': '资质不足'}))
        self.assertEqual(body['data']['status'], 'rejected')
        self.assertIn('资质不足',
                      Message.objects.filter(user=application.applicant).first().content)

    def test_review_invalid_action(self):
        application = self._application()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'maybe'}), message='无效的审核操作')

    def test_review_twice_rejected(self):
        application = self._application()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'approve'}))
        self.expect_fail(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'approve'}), message='该申请已处理')

    def test_review_wrong_hospital(self):
        application = self._application()
        self.login_as(self.hospital_user_b)
        self.expect_fail(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'approve'}), message='无权处理此申请')

    def test_application_list_scoping(self):
        mine = self._application()
        other_hospital = make_institution(type='hospital', district=self.district_b)
        pet_other = make_pet(district=self.district_b, hospital=other_hospital,
                             status='pending_adopt')
        applicant = User.objects.create_user(username='app_user2', password='x',
                                             role='adopter')
        AdoptionApplication.objects.create(
            pet=pet_other, pet_code=pet_other.code, applicant=applicant,
            applicant_name='别人', applicant_phone='13800000001',
            hospital=other_hospital)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json('/api/business/adoptions/applications/'))['data']
        self.assertEqual([a['id'] for a in data], [mine.id])

    def test_my_applications(self):
        application = self._application()
        self.login_as(application.applicant)
        data = self.ok(self.get_json('/api/business/adoptions/my-applications/'))['data']
        self.assertEqual([a['id'] for a in data], [application.id])
        self.assertEqual(data[0]['pet']['id'], application.pet_id)


class AdoptionInfoEditTest(BusinessTestBase):
    def test_edit_creates_listing(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_adopt')
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(
            f'/api/business/adoptions/{pet.id}/edit-info/',
            {'intro': '亲人', 'is_active': True}))
        listing = AdoptionHallListing.objects.get(pet=pet)
        self.assertEqual(listing.intro, '亲人')
        self.assertTrue(listing.is_active)
        self.assertIsNotNone(listing.published_at)

    def test_edit_deactivate(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet, is_active=True)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'/api/business/adoptions/{pet.id}/edit-info/',
                               {'is_active': False}))
        pet.refresh_from_db()
        self.assertFalse(pet.hall_listing.is_active)

    def test_edit_unknown_pet_404(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json('/api/business/adoptions/999999/edit-info/', {}),
                  status=404, message='宠物不存在')


class AdoptionListTest(BusinessTestBase):
    def test_list_keyword_and_status(self):
        pet = make_pet(district=self.district_a, status='adopted')
        adopter = User.objects.create_user(username='list_adopter', password='x',
                                           role='adopter')
        adoption = Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter=adopter,
            adopter_name='张三丰', adopter_phone='13800007777',
            hospital=self.hospital_a, status='completed',
            ledger_no='ADP-T2', district=self.district_a)
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json('/api/business/adoptions/'))['data']
        self.assertEqual(len(data), 1)
        data = self.ok(self.get_json('/api/business/adoptions/?keyword=张三丰'))['data']
        self.assertEqual([a['id'] for a in data], [adoption.id])
        data = self.ok(self.get_json('/api/business/adoptions/?status=pending_claim'))['data']
        self.assertEqual(data, [])
