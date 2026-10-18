"""安乐死处置视图测试。"""
from business.models import Euthanasia
from business.tests.base import BusinessTestBase, assert_doc_no, make_pet

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
        assert_doc_no(self, record.ledger_no, 'EUT')
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

    def test_cross_district_pet_404(self):
        pet = make_pet(district=self.district_b, hospital=self.hospital_b,
                       status='in_treatment')
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'reason': 'x'}),
                         status=404, message='无权访问')

    def test_deleted_pet_rejected(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment', is_deleted=True)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'reason': 'x'}),
                         status=404, message='无权访问')


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

    def test_receive_cross_district_404(self):
        record = self._record()
        self.login_as(self.shelter_user_b)
        self.expect_fail(self.post_json(f'{URL}{record.id}/body-receive/', {}),
                         status=404, message='无权访问')


class EuthanasiaKindTest(BusinessTestBase):
    """死亡与安乐死共用一张表，靠 `kind` 区分（第四十八轮）。

    两者是**分开报**的两张表：`euthanized`（安乐死）与 `dead`（死亡/无害化）。
    合并成一个状态的话，死亡报表会把安乐死的数量算进去，而且打印出来的
    单据标题也会错（安乐死单被印成「死亡 / 无害化处理单」）。
    """

    def _pet(self, status='in_treatment'):
        return make_pet(district=self.district_a, hospital=self.hospital_a,
                        status=status)

    def test_default_kind_is_euthanasia(self):
        """不传 `kind` 的旧调用必须仍然走安乐死（向后兼容）。"""
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{URL}create/',
                                      {'pet_id': pet.id, 'reason': '病重'}))
        self.assertEqual(body['data']['kind'], 'euthanasia')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'euthanized')

    def test_kind_dead_sets_pet_dead(self):
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{URL}create/', {
            'pet_id': pet.id, 'reason': '送医途中死亡', 'kind': 'dead'}))
        self.assertEqual(body['data']['kind'], 'dead')
        self.assertEqual(Euthanasia.objects.get(id=body['data']['id']).kind, 'dead')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'dead',
                         '死亡与安乐死必须是两个状态，否则两张报表分不开')

    def test_invalid_kind_rejected_without_side_effect(self):
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {
            'pet_id': pet.id, 'reason': 'x', 'kind': 'cremated'}), message='不合法')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment', '被拒时不能有副作用')
        self.assertFalse(Euthanasia.objects.filter(pet=pet).exists())

    def test_institution_derived_from_pet_hospital(self):
        """归属机构从**业务对象**推导，不取操作员机构。

        平台 / 市级账号没有 `institution`，取操作员机构会写成空；
        跨区县送医时也会写成错的医院。
        """
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{URL}create/',
                                      {'pet_id': pet.id, 'reason': 'x'}))
        record = Euthanasia.objects.get(id=body['data']['id'])
        self.assertEqual(record.institution_id, self.hospital_a.id)
