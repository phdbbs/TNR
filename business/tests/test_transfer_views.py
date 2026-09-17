"""转运拆分下发/签收/驳回/撤回视图测试。"""
from business.models import Pet, Transfer
from business.services import busy_transfer_codes, capture_transfer_state
from business.tests.base import (BusinessTestBase, make_capture, make_institution,
                                 make_pet)

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

    def test_note_persisted(self):
        """备注必须真的落库。

        `transfer_create` 的接口文档一直声明接受 ``note``、前端「新建转运单」
        也确实提交它，但模型没有这一列 → 用户填的备注被静默丢弃，
        详情抽屉里的「备注」永远是 `—`。
        """
        pet = self._make_transit_pets(1)[0]
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [pet.code],
            'note': '车厢已消毒，请在 18:00 前接收',
        }))
        transfer = Transfer.objects.get(id=body['data'][0]['id'])
        self.assertEqual(transfer.note, '车厢已消毒，请在 18:00 前接收')
        # 序列化要同时给两套键，前端读的是 camelCase
        self.assertEqual(body['data'][0]['note'], transfer.note)

    def test_note_defaults_to_empty(self):
        """不传备注时落空串，不能是 NULL（详情里要显示 `—` 而不是报错）。"""
        pet = self._make_transit_pets(1)[0]
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [pet.code],
        }))
        self.assertEqual(Transfer.objects.get(id=body['data'][0]['id']).note, '')

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


class TransferRejectAndReassignGuardTest(BusinessTestBase):
    """驳回回退 + 「不可重复下发 / 不可跨区下发」守卫。"""
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

    def test_rejected_pet_returns_to_pending_pool(self):
        """被驳回的动物必须自动回到「待转运」备选框。

        备选框的口径是「在途 且 未被**未结**转运单占用」，
        所以驳回后宠物要满足：状态回 in_transit、解除医院归属、
        且不再被任何未结单据占位。
        """
        pet, transfer = self._make_pending()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/reject/', {'reason': '容量不足'}))

        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_transit')
        self.assertIsNone(pet.hospital, '驳回后应解除医院归属')
        # rejected 不占位 —— 这正是「取消重新下发」后依赖的路径
        self.assertFalse(busy_transfer_codes([pet.code]))
        # 因此它可以被重新选进一张新单
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [pet.code],
            'pet_count': 1,
        }))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_transit')
        self.assertEqual(pet.hospital, self.hospital_a)

    def test_cannot_reassign_pet_already_in_open_transfer(self):
        """已在未结转运单里的动物不能再下发 —— 防止同一动物挂多张未结单。

        旧版「重新下发」没有这层校验，同一张被驳回的单可以反复下发，
        同一只动物就同时挂在了多张 pending 单上。
        """
        pet, transfer = self._make_pending()   # 已有一张 pending 单占着这只动物
        self.assertTrue(busy_transfer_codes([pet.code]))
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [pet.code],
            'pet_count': 1,
        }), message='不能重复下发')
        self.assertEqual(Transfer.objects.filter(status='pending').count(), 1)

    def test_cannot_transfer_pet_of_other_district(self):
        """跨区县下发必须被拦下（原来裸查 code__in，猜到编号就能跨区下发）。"""
        other = make_pet(district=self.district_b, shelter=self.shelter_b)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [other.code],
            'pet_count': 1,
        }), message='不属于本区县')

    def test_resend_endpoint_removed(self):
        """「重新下发」接口已下线，不能再对同一张被驳回的单反复下发。"""
        _, transfer = self._make_pending()
        transfer.status = 'rejected'
        transfer.save()
        self.login_as(self.shelter_user_a)
        res = self.client.post(f'{TRANSFER_URL}{transfer.id}/resend/', {})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(Transfer.objects.count(), 1, '不应新建任何转运单')


class TransferWithdrawTest(BusinessTestBase):
    """撤回：只要医院未签收即可撤回，宠物回退为待转运、捕捉单解锁。

    需求背景：过去只能等医院驳回。撤回后该动物应重新出现在「待转运」列表中，
    可被选进新的转运单。
    """

    def _make_pending(self, with_capture=False):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a) if with_capture else None
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       capture=capture, status='in_transit')
        transfer = Transfer.objects.create(
            from_shelter=self.shelter_a, to_hospital=self.hospital_a,
            pet_codes=pet.code, pet_count=1, district=self.district_a,
            capture=capture)
        # 模拟「已下发转运单」后的宠物状态：归属医院、状态仍在途
        pet.hospital = self.hospital_a
        pet.save(update_fields=['hospital'])
        return pet, transfer

    def test_withdraw_success(self):
        pet, transfer = self._make_pending()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'))
        self.assertEqual(body['data']['status'], 'void')
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'void')
        self.assertEqual(transfer.get_status_display(), '已撤回')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_transit')
        self.assertIsNone(pet.hospital_id, '撤回后必须解除医院归属')

    def test_withdraw_unlocks_capture(self):
        """医院未签收撤回后，捕捉单应回到可编辑/可删除。"""
        _, transfer = self._make_pending(with_capture=True)
        capture = transfer.capture
        state = capture_transfer_state(capture)
        self.assertEqual(state['transferred'], 1)
        self.assertFalse(state['can_edit'], '有在途转运单时不应可编辑')

        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'))

        state = capture_transfer_state(capture)
        self.assertEqual(state['transferred'], 0, 'void 不得计入已转运')
        self.assertTrue(state['can_edit'], '撤回后捕捉单应解锁可编辑')
        self.assertTrue(state['can_delete'])

    def test_withdrawn_pet_can_be_transferred_again(self):
        pet, transfer = self._make_pending()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'))

        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [pet.code],
        }))
        self.assertEqual(len(body['data']), 1, '撤回后的动物应可重新提交转运')
        self.assertEqual(body['data'][0]['status'], 'pending')
        pet.refresh_from_db()
        self.assertEqual(pet.hospital_id, self.hospital_a.id)

    def test_received_transfer_cannot_withdraw(self):
        _, transfer = self._make_pending()
        transfer.status = 'received'
        transfer.save()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'),
                         message='不可撤回')

    def test_withdraw_twice_rejected(self):
        _, transfer = self._make_pending()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'))
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'),
                         message='不可撤回')

    def test_same_district_other_shelter_cannot_withdraw(self):
        """同区县的其他捕捉点不能撤回别人的转运单。"""
        other = make_institution(type='shelter', district=self.district_a, name='甲区另一捕捉点')
        other_user = self.shelter_user_a.__class__.objects.create_user(
            username='shelter_a2_t', password='123456', role='shelter',
            district=self.district_a, institution=other)
        _, transfer = self._make_pending()
        self.login_as(other_user)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'),
                         message='无权撤回')

    def test_other_district_shelter_gets_404(self):
        """跨区县一律 404（不暴露记录是否存在）。"""
        _, transfer = self._make_pending()
        self.login_as(self.shelter_user_b)  # 乙区
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'),
                         status=404, message='不存在')

    def test_other_district_gov_gets_404(self):
        _, transfer = self._make_pending()
        self.login_as(self.gov_b)  # 乙区监管
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'),
                         status=404, message='不存在')

    def test_same_district_gov_can_withdraw(self):
        """本区县监管可代捕捉点撤回。"""
        pet, transfer = self._make_pending()
        self.login_as(self.gov_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_transit')

    def test_hospital_cannot_withdraw(self):
        _, transfer = self._make_pending()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'), status=403)

    def test_adopter_cannot_withdraw(self):
        _, transfer = self._make_pending()
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'), status=403)
