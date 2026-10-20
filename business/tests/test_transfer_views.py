"""转运拆分下发/签收/驳回/撤回视图测试。"""
from unittest import mock

from django.utils import timezone

from business import views_transfer
from business.models import Pet, Transfer
from business.services import busy_transfer_codes, capture_transfer_state
from business.tests.base import (
    assert_doc_no,BusinessTestBase, make_capture, make_institution,
                                 make_pet, make_user)

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
        assert_doc_no(self, transfer.ledger_no, 'TRF')

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
        hospital_a2 = make_institution(type='hospital', district=self.district_a,
                                       name='甲区二院')
        pets = self._make_transit_pets(2)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'items': [
                {'hospital_id': self.hospital_a.id, 'pet_ids': [pets[0].id]},
                {'hospital_id': hospital_a2.id, 'pet_ids': [pets[1].id]},
            ],
        }))
        self.assertEqual(len(body['data']), 2)
        pets[0].refresh_from_db()
        pets[1].refresh_from_db()
        self.assertEqual(pets[0].hospital_id, self.hospital_a.id)
        self.assertEqual(pets[1].hospital_id, hospital_a2.id)

    def test_duplicate_pet_assigned_only_once(self):
        hospital_a2 = make_institution(type='hospital', district=self.district_a,
                                       name='甲区二院')
        pet = self._make_transit_pets(1)[0]
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'items': [
                {'hospital_id': self.hospital_a.id, 'pet_ids': [pet.id]},
                {'hospital_id': hospital_a2.id, 'pet_ids': [pet.id]},
            ],
        }))
        self.assertEqual(len(body['data']), 1, '同一宠物只能进一张转运单')
        self.assertEqual(body['data'][0]['petCount'], 1)

    def test_district_shelter_cannot_send_to_foreign_hospital(self):
        """区级捕捉点只能转运到本区医院（第五十轮）。"""
        pet = self._make_transit_pets(1)[0]
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_b.id,
            'pet_codes': [pet.code],
        }), message='只能转运到本区医院')
        self.assertFalse(Transfer.objects.filter(pet_codes=pet.code).exists(),
                         '校验失败不应留下半截转运单')

    def test_city_shelter_sends_across_districts_in_one_request(self):
        """市级捕捉点可全市转运（第五十轮）：一次多医院拆分可跨区县。"""
        city_shelter = make_institution(type='shelter', district=self.city,
                                        name='市级捕捉点')
        city_user = make_user('city_shelter_t', role='shelter',
                              district=self.city, institution=city_shelter)
        pets = self._make_transit_pets(2)
        pets[1].district = self.district_b
        pets[1].save(update_fields=['district'])
        self.login_as(city_user)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': city_shelter.id,
            'items': [
                {'hospital_id': self.hospital_a.id, 'pet_ids': [pets[0].id]},
                {'hospital_id': self.hospital_b.id, 'pet_ids': [pets[1].id]},
            ],
        }))
        self.assertEqual(len(body['data']), 2)
        transfers = {t.to_hospital_id: t for t in
                     Transfer.objects.filter(from_shelter=city_shelter)}
        self.assertEqual(set(transfers), {self.hospital_a.id, self.hospital_b.id})
        self.assertEqual(transfers[self.hospital_a.id].district_id,
                         self.district_a.id, '甲区动物的转运单应归属甲区')
        self.assertEqual(transfers[self.hospital_b.id].district_id,
                         self.district_b.id, '乙区动物的转运单应归属乙区')

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
        self.login_as(make_user(role='shelter', district=self.district_a))  # 没挂机构
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
        """他院签收 → 404，且与**不存在的 id** 响应完全一致。

        第二十三轮前这里是 `400「无权签收此转运记录」`，而「不存在」是
        `404「转运记录不存在」` —— 两者可区分，于是枚举 id 就能问出
        他院转运单是否存在（**存在性预言机**）。现已合并为同一个 404。
        """
        _, transfer = self._make_pending()
        self.login_as(self.hospital_user_b)
        other = self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/')
        ghost = self.post_json(f'{TRANSFER_URL}99999999/receive/')
        self.assertEqual(other.status_code, 404, other.content)
        self.assertFalse(other.json()['success'])
        self.assertEqual(other.status_code, ghost.status_code,
                         '「他院的」与「不存在的」状态码不同 = 可枚举')
        self.assertEqual(other.json()['message'], ghost.json()['message'],
                         '「他院的」与「不存在的」文案不同 = 可枚举')
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'pending', '被拒后转运单被签收')

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


class PartialReceiveTest(BusinessTestBase):
    """部分签收（第四十八轮）：一车 3 只、只到了 2 只。

    旧实现只有「整单签收 / 整单驳回」两个动作，两种情况都在制造与实际
    不符的台账：整签 = 对没到的动物也认了账；整驳 = 把已经到了的也退回去。
    """

    def _make_pending(self, count=3):
        pets = [make_pet(district=self.district_a, shelter=self.shelter_a)
                for _ in range(count)]
        transfer = Transfer.objects.create(
            from_shelter=self.shelter_a, to_hospital=self.hospital_a,
            pet_codes=','.join(p.code for p in pets), pet_count=count,
            district=self.district_a)
        return pets, transfer

    def test_partial_receive_keeps_rest_pending(self):
        pets, transfer = self._make_pending(3)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(
            f'{TRANSFER_URL}{transfer.id}/receive/',
            {'pet_codes': [pets[0].code, pets[1].code]}))

        self.assertEqual(body['data']['status'], 'partial')
        transfer.refresh_from_db()
        self.assertEqual(set(transfer.received_codes.split(',')),
                         {pets[0].code, pets[1].code})
        for p in pets[:2]:
            p.refresh_from_db()
            self.assertEqual(p.status, 'in_treatment', '已签收的应进入待诊疗')
        pets[2].refresh_from_db()
        self.assertEqual(pets[2].status, 'in_transit',
                         '未签收的不能被一起改成待诊疗')

    def test_receive_remaining_finishes_the_slip(self):
        pets, transfer = self._make_pending(3)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                               {'pet_codes': [pets[0].code]}))
        body = self.ok(self.post_json(
            f'{TRANSFER_URL}{transfer.id}/receive/',
            {'pet_codes': [pets[1].code, pets[2].code]}))
        self.assertEqual(body['data']['status'], 'received')
        for p in pets:
            p.refresh_from_db()
            self.assertEqual(p.status, 'in_treatment')

    def test_code_not_in_slip_is_rejected_without_side_effect(self):
        """伪造 / 别单的编号必须 400，且**不能**留下半截签收。"""
        pets, transfer = self._make_pending(2)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(
            f'{TRANSFER_URL}{transfer.id}/receive/',
            {'pet_codes': [pets[0].code, 'TNR-FAKE-CODE']}),
            message='不在本单待签收范围内')

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'pending', '被拒后单据状态被改了')
        self.assertEqual(transfer.received_codes, '')
        pets[0].refresh_from_db()
        self.assertEqual(pets[0].status, 'in_transit', '被拒后动物状态被改了')

    def test_receiving_same_pet_twice_is_rejected(self):
        pets, transfer = self._make_pending(2)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                               {'pet_codes': [pets[0].code]}))
        self.expect_fail(self.post_json(
            f'{TRANSFER_URL}{transfer.id}/receive/',
            {'pet_codes': [pets[0].code]}), message='不在本单待签收范围内')

    def test_reject_after_partial_only_returns_unsigned(self):
        """部分签收后驳回剩余：已签收的留在医院，未签收的退回在途。"""
        pets, transfer = self._make_pending(3)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                               {'pet_codes': [pets[0].code]}))
        body = self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/reject/',
                                      {'reason': '剩余未到货'}))
        self.assertEqual(body['data']['status'], 'rejected')

        pets[0].refresh_from_db()
        self.assertEqual(pets[0].status, 'in_treatment',
                         '已签收的动物不能因为「驳回剩余」被退回')
        for p in pets[1:]:
            p.refresh_from_db()
            self.assertEqual(p.status, 'in_transit')
            self.assertIsNone(p.hospital_id, '退回的动物要解除医院归属')

    def test_empty_selection_is_rejected(self):
        _, transfer = self._make_pending(2)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(
            f'{TRANSFER_URL}{transfer.id}/receive/', {'pet_codes': []}),
            message='请至少选择一只动物签收')

    def test_whole_slip_receive_still_works(self):
        """反向对照：不传 `pet_codes` = 整单签收（旧前端 / 旧调用方）。"""
        pets, transfer = self._make_pending(2)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/'))
        self.assertEqual(body['data']['status'], 'received')
        for p in pets:
            p.refresh_from_db()
            self.assertEqual(p.status, 'in_treatment')

    def test_capture_state_counts_partial_receipts(self):
        """服务层口径：部分签收也要如实计入 `received`。

        `capture_transfer_state` 原先只看 `status == 'received'`，
        加了 partial 之后如果不改，捕捉单会显示「已转运 3 / 已签收 0」——
        而实际上 2 只已经躺在医院里了。
        """
        pets, transfer = self._make_pending(3)
        capture = make_capture(district=self.district_a, shelter=self.shelter_a,
                               pet_codes=[p.code for p in pets])
        for p in pets:
            p.capture = capture
            p.save(update_fields=['capture'])
        transfer.capture = capture
        transfer.save(update_fields=['capture'])

        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                               {'pet_codes': [pets[0].code, pets[1].code]}))
        state = capture_transfer_state(capture)
        self.assertEqual(state['transferred'], 3)
        self.assertEqual(state['received'], 2, '部分签收的 2 只没被算作已签收')


class ConcurrentTransferWriteTest(BusinessTestBase):
    """签收/驳回的**并发**窗口 —— 第五十一轮。

    ⚠ 这类用例必须把竞写**注入到「视图读出单据之后、写回之前」**。
    顺序点两次的用例（先签 A 再签 B）撞不进这个窗口：把实现变异回
    「读旧值 → 拼新值 → 整列覆盖」，它们照样全绿，而生产上恰恰是
    那种写法在丢数据。
    """

    def _make_pending(self, count=3):
        pets = [make_pet(district=self.district_a, shelter=self.shelter_a)
                for _ in range(count)]
        transfer = Transfer.objects.create(
            from_shelter=self.shelter_a, to_hospital=self.hospital_a,
            pet_codes=','.join(p.code for p in pets), pet_count=count,
            district=self.district_a)
        return pets, transfer

    def _race_after_load(self, transfer_id, competing):
        """让 `competing` 正好发生在本次请求读完单据之后。"""
        real = views_transfer.get_own_institution_object
        fired = []

        def racing(*args, **kwargs):
            row = real(*args, **kwargs)
            if row is not None and row.id == transfer_id and not fired:
                fired.append(True)
                competing(row)
            return row

        return mock.patch.object(views_transfer, 'get_own_institution_object',
                                 racing)

    def test_competing_receipt_does_not_overwrite_mine(self):
        """两人分点同一车动物：后提交的那笔**不能覆盖**先落库的那批号。

        旧实现两笔都从 `received_codes=''` 出发、各自整列覆盖 ——
        先签的 2 只既不在单据上、又已经是 `in_treatment`（人躺在医院里），
        单据却不认它们：剩余数、状态、捕捉单重算全部跟着错，且不报错。
        """
        pets, transfer = self._make_pending(3)
        self.login_as(self.hospital_user_a)

        def other_operator(row):
            Transfer.objects.filter(pk=row.pk).update(
                received_codes=','.join([pets[2].code]), status='partial',
                received_at=timezone.localdate())
            Pet.objects.filter(code=pets[2].code).update(status='in_treatment')

        with self._race_after_load(transfer.id, other_operator):
            resp = self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                                  {'pet_codes': [pets[0].code]})
        self.assertEqual(resp.status_code, 409, resp.content)

        transfer.refresh_from_db()
        self.assertEqual(transfer.received_codes, pets[2].code,
                         '竞手的签收记录被我这笔覆盖掉了')
        self.assertEqual(transfer.status, 'partial')
        pets[0].refresh_from_db()
        self.assertEqual(pets[0].status, 'in_transit',
                         '抢输的一方不能把自己那几只改成待诊疗')

    def test_two_partial_receipts_on_same_slip_do_not_lose_either(self):
        """**同一版状态**下的两笔部分签收：只有比 `received_codes` 才拦得住。

        上面那条从 `pending` 出发，抢输的一方是靠「状态被人改过了」发现的；
        本条让单据已经是 `partial`，两人各签剩余的一只 —— 竞手写完**状态仍是
        `partial`**，于是「判状态」这条 WHERE 完全没反应，只有把「我读到的
        `received_codes`」也写进 WHERE 才能发现冲突。少了这个判据，先签的那只
        会从单据上消失（整列覆盖），而它已经在医院里了。
        """
        pets, transfer = self._make_pending(3)
        self.login_as(self.hospital_user_a)
        # 先签掉第 1 只，把单据推进到 partial
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                               {'pet_codes': [pets[0].code]}))

        def other_operator(row):
            Transfer.objects.filter(pk=row.pk).update(
                received_codes=','.join([pets[0].code, pets[1].code]))
            Pet.objects.filter(code=pets[1].code).update(status='in_treatment')

        with self._race_after_load(transfer.id, other_operator):
            resp = self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                                  {'pet_codes': [pets[2].code]})
        self.assertEqual(resp.status_code, 409, resp.content)

        transfer.refresh_from_db()
        self.assertEqual(set(transfer.received_codes.split(',')),
                         {pets[0].code, pets[1].code},
                         '竞手刚签的那只被我这笔整列覆盖了')
        pets[2].refresh_from_db()
        self.assertEqual(pets[2].status, 'in_transit')

    def test_conflicted_receipt_can_be_redone_after_refresh(self):
        """**反向对照**：409 之后刷新重签必须能成 —— 否则这条闸变成永久锁死。"""
        pets, transfer = self._make_pending(3)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                               {'pet_codes': [pets[2].code]}))
        body = self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                                      {'pet_codes': [pets[0].code]}))['data']
        self.assertEqual(set(body['receivedCodes'].split(',')),
                         {pets[2].code, pets[0].code})

    def test_reject_losing_to_a_concurrent_receipt_is_refused(self):
        """驳回与签收同时发生：已经有人签了 2 只，整单驳回必须被拒。

        否则那 2 只已经进了诊疗，却挂着一张 `rejected` 的单；
        而 `Pet.objects.filter(code__in=remaining).update(status='in_transit')`
        还会把**别人刚签收**的动物一起退回在途。
        """
        pets, transfer = self._make_pending(3)
        self.login_as(self.hospital_user_a)

        def other_operator(row):
            Transfer.objects.filter(pk=row.pk).update(
                received_codes=','.join([pets[0].code, pets[1].code]),
                status='partial', received_at=timezone.localdate())
            Pet.objects.filter(code__in=[pets[0].code, pets[1].code]).update(
                status='in_treatment')

        with self._race_after_load(transfer.id, other_operator):
            resp = self.post_json(f'{TRANSFER_URL}{transfer.id}/reject/',
                                  {'reason': '车辆不足'})
        self.assertEqual(resp.status_code, 409, resp.content)

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'partial', '已签收的单据被驳回覆盖了')
        for p in pets[:2]:
            p.refresh_from_db()
            self.assertEqual(p.status, 'in_treatment',
                             '别人已签收的动物被驳回一起退回了在途')

    def test_reject_losing_to_a_newer_partial_receipt_is_refused(self):
        """竞手只推进 `received_codes`、**状态没变**时，驳回同样必须被拒。

        上一条用例把竞手写成了 `pending → partial`，所以「比 status」就能拦住 ——
        它抓不到真正危险的那一型：单据**已经是** `partial`，两人在同一版上继续动。
        A 读出 `partial / 已签 p0`，B 在同一瞬间又签了 p1（状态照旧是 `partial`）。
        只钉 `status` 的 WHERE 照样成立，于是 A 按**陈旧的** `remaining`
        （p1、p2、p3）把它们从 `in_treatment` 改回 `in_transit` 且 `hospital=None`：
        p1 已经躺在医院里、甚至已经开了诊疗单，单据却说不认识它。
        A 收到「已驳回」、B 收到「签收成功」，全程没有一行报错。
        """
        pets, transfer = self._make_pending(4)
        self.login_as(self.hospital_user_a)
        # 先把单据推到 partial（A 与 B 都从这一版读出）
        Transfer.objects.filter(pk=transfer.pk).update(
            status='partial', received_codes=pets[0].code,
            received_at=timezone.localdate())
        Pet.objects.filter(code=pets[0].code).update(
            status='in_treatment', hospital=self.hospital_a)

        def other_operator(row):
            # B 又签一只：状态仍是 partial（还差 p2/p3），只有清单前进了
            Transfer.objects.filter(pk=row.pk).update(
                received_codes=','.join([pets[0].code, pets[1].code]))
            Pet.objects.filter(code=pets[1].code).update(
                status='in_treatment', hospital=self.hospital_a)

        with self._race_after_load(transfer.id, other_operator):
            resp = self.post_json(f'{TRANSFER_URL}{transfer.id}/reject/',
                                  {'reason': '笼位不足'})
        self.expect_fail(resp, 409)

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'partial', '抢输的驳回不该改单据状态')
        self.assertEqual(transfer.received_codes,
                         ','.join([pets[0].code, pets[1].code]),
                         '竞手刚签的那只被驳回抹掉了')
        pets[1].refresh_from_db()
        self.assertEqual(pets[1].status, 'in_treatment',
                         '别人刚签收的动物被驳回退回了在途')
        self.assertEqual(pets[1].hospital_id, self.hospital_a.id,
                         '退回还顺手抹掉了归属医院，动物从此无人认领')

    def test_reject_on_unchanged_partial_still_works(self):
        """**反向对照**：没有竞手时，partial 单驳回剩余照旧能成。

        没有这条，上面那条判据可以靠「一律 409」通过，而生产上没人能驳回。
        """
        pets, transfer = self._make_pending(3)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/',
                               {'pet_codes': [pets[0].code]}))
        self.ok(self.post_json(f'{TRANSFER_URL}{transfer.id}/reject/',
                               {'reason': '笼位不足'}))
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'rejected')
        pets[1].refresh_from_db()
        self.assertEqual(pets[1].status, 'in_transit', '未签收的要退回在途')
        pets[0].refresh_from_db()
        self.assertEqual(pets[0].status, 'in_treatment',
                         '已签收的那只不能被「驳回剩余」一起退回')

    def test_receive_failure_after_receipt_leaves_no_half_state(self):
        """签收的后半段炸了，前半段（单据 + 宠物状态）必须一起回滚。

        旧实现整段不在事务里：单据已改 `received`、宠物已 `in_treatment`，
        而捕捉单重算抛错 → 页面 500，台账留下一张「已签收但捕捉单还挂着
        待转运」的单，且没人知道哪一半天。
        """
        pets, transfer = self._make_pending(3)
        self.login_as(self.hospital_user_a)
        with mock.patch.object(views_transfer, 'recalc_captures_for_transfer',
                               side_effect=RuntimeError('重算炸了')):
            with self.assertRaises(RuntimeError):
                self.post_json(f'{TRANSFER_URL}{transfer.id}/receive/', {})

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'pending')
        self.assertEqual(transfer.received_codes, '')
        for p in pets:
            p.refresh_from_db()
            self.assertEqual(p.status, 'in_transit',
                             '单据回滚了但宠物状态留下了 —— 两半不一致')


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

    def test_gov_cannot_withdraw_same_district(self):
        """政府端只读（第四十九轮）：本区县监管也不能代捕捉点撤回。"""
        pet, transfer = self._make_pending()
        self.login_as(self.gov_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'),
                         status=403)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'pending', '被拒的撤回不能已经把单改成 void')

    def test_gov_cannot_withdraw_other_district(self):
        """跨区县的监管账号同样是 403，而不是 404。

        ⚠ 这里回 403 而不是「不存在」并不泄漏信息：角色闸门跑在归属判断**之前**，
        任何区县、任何存在与否都统一 403，观测面上分不出「有这张单」和「没这张单」。
        以前这两支分别是 404 / 200，是「监管代录」留下的口径，已随只读模式一并收掉。
        """
        _, transfer = self._make_pending()
        self.login_as(self.gov_b)  # 乙区监管
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'),
                         status=403)

    def test_hospital_cannot_withdraw(self):
        _, transfer = self._make_pending()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'), status=403)

    def test_adopter_cannot_withdraw(self):
        _, transfer = self._make_pending()
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(f'{TRANSFER_URL}{transfer.id}/withdraw/'), status=403)
