"""放养闭环视图测试。"""
from business.models import Release
from business.tests.base import (
    BusinessTestBase, make_capture, make_institution, make_pet,
)

URL = '/api/business/releases/'


class ReleaseCreateTest(BusinessTestBase):
    def _pet(self, with_capture_community=True):
        capture = make_capture(
            district=self.district_a, shelter=self.shelter_a,
            community=self.community_a if with_capture_community else None)
        return make_pet(district=self.district_a, shelter=self.shelter_a,
                        hospital=self.hospital_a, status='in_treatment',
                        capture=capture)

    def test_create_matches_capture_community(self):
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{URL}create/', {'pet_id': pet.id}))
        release = Release.objects.get(id=body['data']['id'])
        self.assertEqual(release.status, 'pending')
        self.assertEqual(release.community, self.community_a)
        self.assertTrue(release.ledger_no.startswith('REL-'))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment', '确认放养前状态不变')

    def test_create_with_explicit_community(self):
        pet = self._pet(with_capture_community=False)
        other_community = make_institution(type='community', district=self.district_a)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}create/', {
            'pet_id': pet.id, 'community_id': other_community.id,
            'receiver_name': '李物业',
        }))
        self.assertEqual(Release.objects.get(id=body['data']['id']).community,
                         other_community)

    def test_create_without_community_rejected(self):
        pet = self._pet(with_capture_community=False)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}),
                  message='无法匹配原小区')

    def test_create_wrong_status_rejected(self):
        pet = make_pet(district=self.district_a, status='adopted')
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}),
                  message='不可放养')

    def test_create_unknown_pet(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': 999999}),
                  message='宠物不存在')


class ReleaseConfirmTest(BusinessTestBase):
    def _pending(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a,
                               community=self.community_a)
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a, status='in_treatment', capture=capture)
        release = Release.objects.create(
            pet=pet, pet_code=pet.code, community=self.community_a,
            community_name=self.community_a.name, status='pending',
            ledger_no='REL-T1', district=self.district_a)
        return pet, release

    def test_confirm_success(self):
        pet, release = self._pending()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}{release.id}/confirm/', {
            'receiver_name': '张物业', 'receiver_phone': '13800003001',
            'signature': 'base64sig',
        }))
        self.assertEqual(body['data']['status'], 'released')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'released')
        release.refresh_from_db()
        self.assertIsNotNone(release.released_at)
        self.assertEqual(release.receiver_name, '张物业')

    def test_confirm_twice_rejected(self):
        _, release = self._pending()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{release.id}/confirm/', {}))
        self.expect_fail(self.post_json(f'{URL}{release.id}/confirm/', {}),
                  message='不可确认')

    def test_confirm_unknown_404(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}999999/confirm/'), status=404)

    def test_hospital_cannot_confirm(self):
        """确认放养由小区/捕捉点侧完成，医院无权限。"""
        _, release = self._pending()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}{release.id}/confirm/'), status=403)
