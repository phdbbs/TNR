"""转运拆分下发/签收/驳回/重发视图测试。"""
from business.models import Pet, Transfer
from business.tests.base import BusinessTestBase, make_institution, make_pet

TRANSFER_URL = '/api/business/transfers/'


class TransferCreateTest(BusinessTestBase):
    def _make_transit_pets(self, count=2, **kw):
        return [make_pet(district=self.district_a, shelter=self.shelter_a, **kw)
                for _ in range(count)]

    def test_single_hospital_format(self):
        pets = self._make_transit_pets()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [p.code for p in pets],
            'pet_count': 2,
        }))
        self.assertEqual(len(body['data']), 1)
        transfer = Transfer.objects.get(id=body['data'][0]['id'])
        self.assertEqual(transfer.status, 'pending')
        self.assertEqual(transfer.pet_count, 2)
        self.assertTrue(transfer.ledger_no.startswith('TRF-'))

        pet = pets[0]
        pet.refresh_from_db()
        self.assertEqual(pet.hospital, self.hospital_a)
        self.assertEqual(pet.status, 'in_transit', '签收前应保持在途')

    def test_comma_separated_codes(self):
        pets = self._make_transit_pets(1)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': f'{pets[0].code} , ',
        }))
        self.assertEqual(body['data'][0]['petCount'], 1)

    def test_split_to_multiple_hospitals(self):
        pets = self._make_transit_pets(2)
        self.login_as(self.gov_city)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'items': [
                {'hospital_id': self.hospital_a.id, 'pet_ids': [pets[0].id]},
                {'hospital_id': self.hospital_b.id, 'pet_ids': [pets[1].id]},
            ],
        }))
        self.assertEqual(len(body['data']), 2)
        pets[0].refresh_from_db()
        pets[1].refresh_from_db()
        self.assertEqual(pets[0].hospital_id, self.hospital_a.id)
        self.assertEqual(pets[1].hospital_id, self.hospital_b.id)

    def test_duplicate_pet_assigned_only_once(self):
        pet = self._make_transit_pets(1)[0]
        self.login_as(self.gov_city)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'items': [
                {'hospital_id': self.hospital_a.id, 'pet_ids': [pet.id]},
                {'hospital_id': self.hospital_b.id, 'pet_ids': [pet.id]},
            ],
        }))
        self.assertEqual(len(body['data']), 1, '同一宠物只能进一张转运单')
        self.assertEqual(body['data'][0]['petCount'], 1)

    def test_only_in_transit_pets_transferred(self):
        treated = make_pet(district=self.district_a, status='in_treatment',
                           hospital=self.hospital_a)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [treated.code],
        }))
        self.assertEqual(len(body['data']), 0, '非在途宠物不应生成转运单')
        treated.refresh_from_db()
        self.assertEqual(treated.status, 'in_treatment')

    def test_missing_items_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}create/', {'from_shelter_id': self.shelter_a.id}),
                  message='缺少转运明细')

    def test_missing_shelter_rejected(self):
        self.login_as(self.gov_a)  # 政府账号无机构
        self.expect_fail(self.post_json(f'{TRANSFER_URL}create/', {
            'to_hospital_id': self.hospital_a.id, 'pet_codes': ['X1'],
        }), message='缺少捕捉点信息')

    def test_unknown_shelter_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': 999999,
            'to_hospital_id': self.hospital_a.id, 'pet_codes': ['X1'],
        }), message='捕捉点不存在')


class TransferListScopingTest(BusinessTestBase):
    def _create_transfer(self, shelter, hospital, pet_code='X1'):
        return Transfer.objects.create(
            from_shelter=shelter, to_hospital=hospital,
            pet_codes=pet_code, pet_count=1, district=shelter.district)

    def test_hospital_sees_only_inbound(self):
        t_in = self._create_transfer(self.shelter_a, self.hospital_a)
        self._create_transfer(self.shelter_a, self.hospital_b)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json(TRANSFER_URL))['data']
        self.assertEqual([t['id'] for t in data], [t_in.id])

    def test_shelter_sees_only_outbound(self):
        t_out = self._create_transfer(self.shelter_a, self.hospital_a)
        self._create_transfer(self.shelter_b, self.hospital_a)
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(TRANSFER_URL))['data']
        self.assertEqual([t['id'] for t in data], [t_out.id])

    def test_status_filter(self):
        transfer = self._create_transfer(self.shelter_a, self.hospital_a)
        transfer.status = 'received'
        transfer.save()
        self._create_transfer(self.shelter_a, self.hospital_a)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json(TRANSFER_URL + '?status=received'))['data']
        self.assertEqual(len(data), 1)


class TransferReceiveTest(BusinessTestBase):
    def _make_pending(self):
        pet = make_pet(district=self.district_a, shelter=self.shelter_a)
        transfer = Transfer.objects.create(
            from_shelter=self.shelter_a, to_hospital=self.hospital_a,
            pet_codes=pet.code, pet_count=1, district=self.district_a)
        return pet, transfer

    def test_receive_success(self):
        pet, transfer = self._make_pending()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/'))
        self.assertEqual(body['data']['status'], 'received')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment')
        transfer.refresh_from_db()
        self.assertIsNotNone(transfer.received_at)

    def test_receive_wrong_hospital(self):
        _, transfer = self._make_pending()
        self.login_as(self.hospital_user_b)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/'),
                  message='无权签收此转运记录')

    def test_receive_twice_rejected(self):
        _, transfer = self._make_pending()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/'))
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/'),
                  message='不可签收')

    def test_receive_unknown(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}999999/receive/'),
                  message='转运记录不存在', status=404)

    def test_shelter_cannot_receive(self):
        _, transfer = self._make_pending()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/'), status=403)


class TransferRejectResendTest(BusinessTestBase):
    def _make_pending(self):
        pet = make_pet(district=self.district_a, shelter=self.shelter_a)
        transfer = Transfer.objects.create(
            from_shelter=self.shelter_a, to_hospital=self.hospital_a,
            pet_codes=pet.code, pet_count=1, district=self.district_a)
        return pet, transfer

    def test_reject_resets_pet(self):
        pet, transfer = self._make_pending()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/reject/',
                                      {'reason': '容量不足'}))
        self.assertEqual(body['data']['status'], 'rejected')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_transit')
        transfer.refresh_from_db()
        self.assertEqual(transfer.reject_reason, '容量不足')

    def test_resend_creates_new_transfer(self):
        pet, transfer = self._make_pending()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/reject/', {'reason': 'x'}))
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/resend/'))
        new_id = body['data']['id']
        self.assertNotEqual(new_id, transfer.id)
        new_transfer = Transfer.objects.get(id=new_id)
        self.assertEqual(new_transfer.status, 'pending')
        self.assertEqual(new_transfer.to_hospital, self.hospital_a)
        pet.refresh_from_db()
        self.assertEqual(pet.hospital, self.hospital_a)
        self.assertEqual(pet.status, 'in_transit')

    def test_resend_pending_rejected(self):
        _, transfer = self._make_pending()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/resend/'),
                  message='不可重新下发')

    def test_resend_by_other_shelter_rejected(self):
        _, transfer = self._make_pending()
        transfer.status = 'rejected'
        transfer.save()
        self.login_as(self.shelter_user_b)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/resend/'),
                  message='无权重新下发')

    def test_hospital_cannot_resend(self):
        _, transfer = self._make_pending()
        transfer.status = 'rejected'
        transfer.save()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/resend/'), status=403)
