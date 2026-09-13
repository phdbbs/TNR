"""回访打卡视图测试。"""
from django.utils import timezone

from business.models import Adoption, CheckIn
from business.tests.base import BusinessTestBase, make_pet, make_user

URL = '/api/business/checkins/'


class CheckInCreateTest(BusinessTestBase):
    def _adopted(self, adopter=None):
        pet = make_pet(district=self.district_a, status='adopted')
        adopter = adopter or make_user(role='adopter')
        Adoption.objects.create(pet=pet, pet_code=pet.code, adopter=adopter,
                                adopter_name='领养人', adopter_phone='13800001234',
                                hospital=self.hospital_a, status='completed',
                                district=self.district_a)
        return pet, adopter

    def test_create_success(self):
        pet, adopter = self._adopted()
        self.login_as(adopter)
        body = self.ok(self.post_json(f'{URL}create/', {
            'pet_id': pet.id, 'month': '2026-05', 'note': '适应良好'}))
        checkin = CheckIn.objects.get(id=body['data']['id'])
        self.assertEqual(checkin.status, 'pending')
        self.assertEqual(checkin.month, '2026-05')
        self.assertEqual(checkin.adopter, adopter)
        self.assertEqual(checkin.pet_code, pet.code)

    def test_month_defaults_to_current(self):
        pet, adopter = self._adopted()
        self.login_as(adopter)
        body = self.ok(self.post_json(f'{URL}create/', {'pet_id': pet.id}))
        self.assertEqual(CheckIn.objects.get(id=body['data']['id']).month,
                         timezone.localdate().strftime('%Y-%m'))

    def test_duplicate_month_rejected(self):
        pet, adopter = self._adopted()
        self.login_as(adopter)
        self.ok(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'month': '2026-05'}))
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'month': '2026-05'}),
                  message='已打卡，请勿重复提交')

    def test_not_adopter_of_pet_rejected(self):
        pet, adopter = self._adopted()
        stranger = make_user(role='adopter')
        self.login_as(stranger)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}),
                  message='无权为该宠物打卡')

    def test_unknown_pet(self):
        adopter = make_user(role='adopter')
        self.login_as(adopter)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': 999999}),
                  message='宠物不存在')

    def test_missing_pet_id(self):
        adopter = make_user(role='adopter')
        self.login_as(adopter)
        self.expect_fail(self.post_json(f'{URL}create/', {}), message='缺少宠物ID')

    def test_shelter_cannot_create(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': 1}), status=403)


class CheckInReviewTest(BusinessTestBase):
    def _checkin(self):
        pet = make_pet(district=self.district_a, status='adopted')
        adopter = make_user(role='adopter')
        return CheckIn.objects.create(pet=pet, pet_code=pet.code, adopter=adopter,
                                      adopter_name='领养人', month='2026-05')

    def test_approve(self):
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}{checkin.id}/review/',
                                      {'status': 'approved', 'note': '通过'}))
        self.assertEqual(body['data']['status'], 'approved')
        checkin.refresh_from_db()
        self.assertEqual(checkin.operator, self.shelter_user_a)

    def test_reject(self):
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}{checkin.id}/review/',
                                      {'status': 'rejected'}))
        self.assertEqual(body['data']['status'], 'rejected')

    def test_invalid_status(self):
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}{checkin.id}/review/', {'status': 'ok'}),
                  message='status 必须为 approved 或 rejected')

    def test_double_review_rejected(self):
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{checkin.id}/review/', {'status': 'approved'}))
        self.expect_fail(self.post_json(f'{URL}{checkin.id}/review/', {'status': 'rejected'}),
                  message='不可重复审核')

    def test_unknown_404(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}999999/review/', {'status': 'approved'}),
                  status=404)

    def test_adopter_cannot_review(self):
        checkin = self._checkin()
        self.login_as(checkin.adopter)
        self.expect_fail(self.post_json(f'{URL}{checkin.id}/review/', {'status': 'approved'}),
                  status=403)


class CheckInListTest(BusinessTestBase):
    def test_adopter_sees_own_only(self):
        pet = make_pet(district=self.district_a, status='adopted')
        adopter1 = make_user(role='adopter')
        adopter2 = make_user(role='adopter')
        CheckIn.objects.create(pet=pet, pet_code=pet.code, adopter=adopter1,
                               month='2026-05')
        CheckIn.objects.create(pet=pet, pet_code=pet.code, adopter=adopter2,
                               month='2026-05')
        self.login_as(adopter1)
        data = self.ok(self.get_json(URL))['data']
        self.assertEqual([c['adopter_id'] for c in data], [adopter1.id])

    def test_status_filter(self):
        pet = make_pet(district=self.district_a)
        adopter = make_user(role='adopter')
        checkin = CheckIn.objects.create(pet=pet, pet_code=pet.code, adopter=adopter,
                                         month='2026-05', status='approved')
        self.login_as(self.gov_city)
        data = self.ok(self.get_json(URL + '?status=approved'))['data']
        self.assertEqual(len(data), 1)
