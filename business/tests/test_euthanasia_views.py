"""安乐死处置视图测试。"""
from business.models import Euthanasia
from business.tests.base import BusinessTestBase, make_pet

URL = '/api/business/euthanasia/'


class EuthanasiaCreateTest(BusinessTestBase):
    def _pet(self, status='in_treatment'):
        return make_pet(district=self.district_a, hospital=self.hospital_a,
                        status=status)

    def test_create_success(self):
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{URL}create/', {
            'pet_id': pet.id, 'reason': '病重无法救治',
            'condition': '多器官衰竭', 'euthanized_at': '2026-03-01',
        }))
        record = Euthanasia.objects.get(id=body['data']['id'])
        self.assertEqual(record.hospital, self.hospital_a)
        self.assertFalse(record.body_received)
        self.assertTrue(record.ledger_no.startswith('EUT-'))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'euthanized')

    def test_create_missing_reason(self):
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'reason': '  '}),
                  message='安乐死原因不能为空')

    def test_create_adopted_pet_rejected(self):
        pet = self._pet(status='adopted')
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'reason': 'x'}),
                  message='不可安乐死')

    def test_create_released_pet_rejected(self):
        pet = self._pet(status='released')
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'reason': 'x'}),
                  message='不可安乐死')

    def test_shelter_cannot_create(self):
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'reason': 'x'}),
                  status=403)

    def test_list_hospital_scoped(self):
        pet_a = self._pet()
        pet_b = make_pet(district=self.district_b, hospital=self.hospital_b)
        Euthanasia.objects.create(pet=pet_a, pet_code=pet_a.code,
                                  hospital=self.hospital_a, reason='x',
                                  district=self.district_a)
        Euthanasia.objects.create(pet=pet_b, pet_code=pet_b.code,
                                  hospital=self.hospital_b, reason='x',
                                  district=self.district_b)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json(URL))['data']
        self.assertEqual({e['hospital_id'] for e in data}, {self.hospital_a.id})


class BodyReceiveTest(BusinessTestBase):
    def _record(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='euthanized')
        return Euthanasia.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            reason='病重', district=self.district_a)

    def test_receive_success(self):
        record = self._record()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}{record.id}/body-receive/',
                                      {'receiver_name': '甲区捕捉点'}))
        self.assertTrue(body['data']['body_received'])
        record.refresh_from_db()
        self.assertEqual(record.body_received_by, self.shelter_user_a)
        self.assertEqual(record.body_received_by_name, '甲区捕捉点')
        self.assertIsNotNone(record.body_received_at)

    def test_receive_twice_rejected(self):
        record = self._record()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{record.id}/body-receive/', {}))
        self.expect_fail(self.post_json(f'{URL}{record.id}/body-receive/', {}),
                  message='遗体已被领取')

    def test_receive_unknown_404(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}999999/body-receive/', {}), status=404)

    def test_hospital_cannot_receive_body(self):
        record = self._record()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}{record.id}/body-receive/', {}), status=403)
