"""门户页面、门户辅助 API 与宠物全生命周期溯源测试。"""
from business.models import (
    Adoption, AdoptionHallListing, Message, Release, Transfer, Treatment,
)
from business.tests.base import (
    BusinessTestBase, make_capture, make_institution, make_pet, make_user,
)


class PortalPageTest(BusinessTestBase):
    def test_portal_redirects_by_role(self):
        cases = [
            (self.shelter_user_a, '/shelter/'),
            (self.hospital_user_a, '/hospital/'),
            (self.adopter, '/adopter/'),
            (self.gov_city, '/gov/'),
        ]
        for user, url in cases:
            self.client.force_login(user)
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, f'{user.username} → {url}')
            self.assertIn('text/html', resp['Content-Type'])

    def test_wrong_role_redirected(self):
        self.client.force_login(self.adopter)
        resp = self.client.get('/gov/')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, '/')

    def test_anonymous_page_redirects_to_login(self):
        resp = self.client.get('/shelter/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)

    def test_portal_public_page(self):
        resp = self.client.get('/portal/')
        self.assertEqual(resp.status_code, 200)

    def test_adoption_hall_public_page_anonymous(self):
        resp = self.client.get('/adopter/hall/')
        self.assertEqual(resp.status_code, 200)

    def test_adoption_hall_public_page_adopter_gets_full_portal(self):
        self.client.force_login(self.adopter)
        resp = self.client.get('/adopter/hall/')
        self.assertEqual(resp.status_code, 200)


class PortalApiTest(BusinessTestBase):
    def test_hospital_pets_scoped(self):
        pet_a = make_pet(district=self.district_a, hospital=self.hospital_a)
        make_pet(district=self.district_b, hospital=self.hospital_b)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json('/api/business/pets/'))['data']
        self.assertEqual([p['id'] for p in data], [pet_a.id])

    def test_hospital_pets_status_filter(self):
        make_pet(district=self.district_a, hospital=self.hospital_a,
                 status='in_treatment')
        released = make_pet(district=self.district_a, hospital=self.hospital_a,
                            status='released')
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json('/api/business/pets/?status=released'))['data']
        self.assertEqual([p['id'] for p in data], [released.id])

    def test_hall_listings_adopter_sees_active_only(self):
        pet1 = make_pet(district=self.district_a, status='pending_adopt')
        pet2 = make_pet(district=self.district_a, status='pending_adopt')
        active = AdoptionHallListing.objects.create(pet=pet1, hospital=self.hospital_a)
        AdoptionHallListing.objects.create(pet=pet2, hospital=self.hospital_a,
                                           is_active=False)
        self.login_as(self.adopter)
        data = self.ok(self.get_json('/api/business/hall-listings/'))['data']
        self.assertEqual([l['id'] for l in data], [active.id])

    def test_hall_listings_hospital_sees_all_own(self):
        pet1 = make_pet(district=self.district_a, status='pending_adopt')
        pet2 = make_pet(district=self.district_a, status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet1, hospital=self.hospital_a)
        AdoptionHallListing.objects.create(pet=pet2, hospital=self.hospital_a,
                                           is_active=False)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json('/api/business/hall-listings/'))['data']
        self.assertEqual(len(data), 2, '医院端应包含已下架的上架信息')

    def test_my_adoptions(self):
        pet = make_pet(district=self.district_a, status='adopted')
        Adoption.objects.create(pet=pet, pet_code=pet.code, adopter=self.adopter,
                                adopter_name='领养人', adopter_phone='13800000001',
                                hospital=self.hospital_a, status='completed',
                                district=self.district_a)
        self.login_as(self.adopter)
        data = self.ok(self.get_json('/api/business/portal/adoptions/'))['data']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['pet']['id'], pet.id)

    def test_messages_and_mark_read(self):
        message = Message.objects.create(user=self.adopter, type='system',
                                         title='欢迎', content='内容')
        self.login_as(self.adopter)
        data = self.ok(self.get_json('/api/business/portal/messages/'))['data']
        self.assertEqual([m['id'] for m in data], [message.id])

        # GET 标记已读 → 405
        self.expect_fail(self.client.get(f'/api/business/portal/messages/{message.id}/read/'),
                  status=405)
        # 他人消息 → 404
        other = Message.objects.create(user=self.gov_city, type='system',
                                       title='x', content='y')
        self.expect_fail(self.post_json(f'/api/business/portal/messages/{other.id}/read/'),
                  status=404)
        # 正常标记
        self.ok(self.post_json(f'/api/business/portal/messages/{message.id}/read/'))
        message.refresh_from_db()
        self.assertTrue(message.is_read)


class PetLifecycleTest(BusinessTestBase):
    def _full_history_pet(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a,
                               community=self.community_a,
                               pet_codes=['LIFE-PET-01'], ledger_no='CAP-LIFE-1')
        pet = make_pet(code='LIFE-PET-01', district=self.district_a,
                       shelter=self.shelter_a, hospital=self.hospital_a,
                       status='released', capture=capture)
        Transfer.objects.create(
            from_shelter=self.shelter_a, to_hospital=self.hospital_a,
            pet_codes=pet.code, pet_count=1, status='received',
            ledger_no='TRF-LIFE-1', district=self.district_a)
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            items_sterilization=True, items_chip=True, chip_no='LIFECHIP01',
            status='completed', ledger_no='TRE-LIFE-1', district=self.district_a)
        Release.objects.create(
            pet=pet, pet_code=pet.code, community=self.community_a,
            community_name=self.community_a.name, status='released',
            ledger_no='REL-LIFE-1', district=self.district_a)
        return pet

    def test_lifecycle_events_ordered(self):
        pet = self._full_history_pet()
        self.login_as(self.adopter)
        body = self.ok(self.get_json(
            f'/api/business/pets/{pet.id}/lifecycle/'))
        events = body['data']['events']
        types = [e['type'] for e in events]
        self.assertIn('capture', types)
        self.assertIn('transfer', types)
        self.assertIn('treatment', types)
        self.assertIn('release', types)
        # 事件按日期升序（捕捉最先产生）
        self.assertEqual(types[0], 'capture')
        capture_event = events[0]
        self.assertEqual(capture_event['ledger_no'], 'CAP-LIFE-1')
        self.assertEqual(capture_event['shelter_name'], self.shelter_a.name)
        treatment_event = next(e for e in events if e['type'] == 'treatment')
        self.assertEqual(treatment_event['items'], ['绝育', '芯片'])
        self.assertEqual(treatment_event['chip_no'], 'LIFECHIP01')

    def test_lifecycle_unknown_pet_404(self):
        self.login_as(self.adopter)
        self.expect_fail(self.get_json('/api/business/pets/999999/lifecycle/'),
                  status=404, message='宠物不存在')

    def test_lifecycle_minimal_pet_no_events(self):
        pet = make_pet(district=self.district_a)
        self.login_as(self.adopter)
        body = self.ok(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'))
        self.assertEqual(body['data']['events'], [])

    def test_lifecycle_requires_login(self):
        pet = make_pet(district=self.district_a)
        self.expect_fail(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'),
                  status=401)
