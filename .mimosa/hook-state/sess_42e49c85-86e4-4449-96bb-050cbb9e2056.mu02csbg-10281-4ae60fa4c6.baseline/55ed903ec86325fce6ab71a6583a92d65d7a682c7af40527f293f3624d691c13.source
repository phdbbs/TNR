"""捕捉登记与主人领回视图测试。"""
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from business.models import Capture, OwnerReturn, Pet
from business.tests.base import (
    BusinessTestBase, make_capture, make_district, make_institution,
    make_pet, make_user,
)

CAPTURES_URL = '/api/business/captures/'


class CapturePermissionTest(BusinessTestBase):
    def test_anonymous_gets_401(self):
        resp = self.get_json(CAPTURES_URL)
        self.expect_fail(resp, status=401, message='请先登录')

    def test_adopter_gets_403(self):
        self.login_as(self.adopter)
        self.expect_fail(self.get_json(CAPTURES_URL), status=403, message='无权访问该接口')

    def test_hospital_cannot_list_captures(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.get_json(CAPTURES_URL), status=403)

    def test_hospital_can_view_capture_detail(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a, pet_codes=['X1'])
        pet = make_pet(capture=capture, district=self.district_a)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.get_json(f'{CAPTURES_URL}{capture.id}/'))
        self.assertEqual(len(body['data']['pets']), 1)
        self.assertEqual(body['data']['pets'][0]['id'], pet.id)


class CaptureCreateTest(BusinessTestBase):
    def _payload(self, **kw):
        payload = {
            'shelter_id': self.shelter_a.id,
            'community_id': self.community_a.id,
            'community_name': self.community_a.name,
            'address': '幸福路1号',
            'contact_person': '物业张三',
            'contact_phone': '13800001234',
            'pet_count': 2,
            'species': '猫',
        }
        payload.update(kw)
        return payload

    def test_create_success(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', self._payload()))
        self.assertEqual(len(body['data']['pet_codes']), 2)
        self.assertIn('2 条宠物档案', body['message'])

        capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.assertEqual(capture.status, 'completed')
        self.assertTrue(capture.ledger_no.startswith('CAP-'))
        self.assertEqual(capture.shelter, self.shelter_a)
        self.assertEqual(capture.pet_count, 2)

        pets = Pet.objects.filter(code__in=body['data']['pet_codes'])
        self.assertEqual(pets.count(), 2)
        for pet in pets:
            self.assertEqual(pet.status, 'in_transit')
            self.assertEqual(pet.capture, capture)
            self.assertEqual(pet.shelter, self.shelter_a)
            self.assertEqual(pet.district, self.district_a)

    def test_defaults_from_operator_profile(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', {'pet_count': 1}))
        capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.assertEqual(capture.district, self.district_a)
        self.assertEqual(capture.shelter, self.shelter_a)

    def test_pet_count_zero_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', self._payload(pet_count=0)),
                  message='动物数量必须大于0')

    def test_pet_count_over_100_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', self._payload(pet_count=101)),
                  message='单批捕捉数量不能超过100只')

    def test_unknown_shelter_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', self._payload(shelter_id=999999)),
                  message='捕捉点不存在')

    def test_operator_without_shelter_rejected(self):
        user = make_user(role='shelter', district=self.district_a)
        self.login_as(user)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', {'pet_count': 1}),
                  message='缺少捕捉点信息')

    def test_user_without_district_rejected(self):
        user = make_user(role='gov_city')  # 无区县
        self.login_as(user)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', {'pet_count': 1}),
                  message='缺少区县信息')

    def test_non_shelter_institution_rejected(self):
        user = make_user(role='shelter', district=self.district_a,
                         institution=self.hospital_a)
        self.login_as(user)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', {'pet_count': 1}),
                  message='捕捉点不存在')

    def test_broken_json_body_rejected(self):
        self.login_as(self.shelter_user_a)
        resp = self.client.post(f'{CAPTURES_URL}create/', data='not-json',
                                content_type='application/json')
        self.expect_fail(resp, message='动物数量必须大于0')

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_group_photo_upload(self):
        self.login_as(self.shelter_user_a)
        upload = SimpleUploadedFile('photo.jpg', b'fakeimage', content_type='image/jpeg')
        resp = self.client.post(
            f'{CAPTURES_URL}create/',
            data={**{k: str(v) for k, v in self._payload().items()}, 'group_photo': upload},
            format='multipart',
        )
        body = self.ok(resp)
        capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.assertTrue(capture.group_photo)


class CaptureListTest(BusinessTestBase):
    def test_district_scoped(self):
        make_capture(district=self.district_a, shelter=self.shelter_a)
        make_capture(district=self.district_b, shelter=self.shelter_b)
        self.login_as(self.shelter_user_a)
        self.assertEqual(len(self.ok(self.get_json(CAPTURES_URL))['data']), 1)

    def test_gov_city_sees_all(self):
        make_capture(district=self.district_a, shelter=self.shelter_a)
        make_capture(district=self.district_b, shelter=self.shelter_b)
        self.login_as(self.gov_city)
        self.assertEqual(len(self.ok(self.get_json(CAPTURES_URL))['data']), 2)

    def test_keyword_filter(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a,
                               ledger_no='CAP-KW-0001')
        make_capture(district=self.district_a, shelter=self.shelter_a)
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(CAPTURES_URL + '?keyword=KW'))['data']
        self.assertEqual([c['id'] for c in data], [capture.id])


class PetCodesPreviewTest(BusinessTestBase):
    def test_preview_returns_codes(self):
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(f'{CAPTURES_URL}codes-preview/?count=3'))['data']
        self.assertEqual(len(data), 3)
        for code in data:
            self.assertTrue(code.startswith('TNR'))

    def test_preview_invalid_count(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.get_json(f'{CAPTURES_URL}codes-preview/?count=0'),
                  message='数量必须大于0')
        self.expect_fail(self.get_json(f'{CAPTURES_URL}codes-preview/'),
                  message='数量必须大于0')


class OwnerReturnTest(BusinessTestBase):
    URL = f'{CAPTURES_URL}0/owner-return/'

    def _register(self, pet, **kw):
        self.login_as(self.shelter_user_a)
        payload = {'pet_id': pet.id, 'owner_name': '原主人', 'owner_phone': '13811112222',
                   'reason': '走失找回'}
        payload.update(kw)
        return self.post_json(self.URL, payload)

    def test_success(self):
        pet = make_pet(district=self.district_a)
        body = self.ok(self._register(pet))
        self.assertTrue(body['data']['ledger_no'].startswith('RET-'))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'owner_returned')
        record = OwnerReturn.objects.get(id=body['data']['id'])
        self.assertEqual(record.pet, pet)
        self.assertEqual(record.owner_name, '原主人')

    def test_missing_owner_name(self):
        pet = make_pet(district=self.district_a)
        self.expect_fail(self._register(pet, owner_name='  '), message='主人姓名不能为空')

    def test_missing_pet_id(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, {'owner_name': 'x'}), message='缺少宠物ID')

    def test_unknown_pet(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, {'pet_id': 999999, 'owner_name': 'x'}),
                  message='宠物不存在')

    def test_transferred_pet_blocked(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a)
        self.expect_fail(self._register(pet), message='该宠物已提交转运单')

    def test_repeated_return_blocked(self):
        pet = make_pet(district=self.district_a, status='owner_returned')
        self.expect_fail(self._register(pet), message='不可重复登记')

    def test_wrong_status_blocked(self):
        pet = make_pet(district=self.district_a, status='adopted')
        self.expect_fail(self._register(pet), message='不可领回')

    def test_blacklist_blocks_return(self):
        from business.models import Blacklist
        Blacklist.objects.create(name='老赖', phone='13900009999', reason='弃养',
                                 district=self.district_a)
        pet = make_pet(district=self.district_a)
        self.expect_fail(self._register(pet, owner_phone='13900009999'),
                  message='该主人已在黑名单中')


class OwnerReturnListTest(BusinessTestBase):
    def test_list_and_keyword(self):
        pet = make_pet(district=self.district_a)
        record = OwnerReturn.objects.create(
            pet=pet, pet_code=pet.code, owner_name='王找找', owner_phone='13800007777',
            reason='找回', ledger_no='RET-KW-0001', district=self.district_a)
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json('/api/business/owner-returns/'))['data']
        self.assertEqual(len(data), 1)
        data = self.ok(self.get_json('/api/business/owner-returns/?keyword=王找找'))['data']
        self.assertEqual([r['id'] for r in data], [record.id])
