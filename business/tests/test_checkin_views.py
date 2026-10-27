"""回访打卡视图测试。"""
from django.utils import timezone

from business.models import Adoption, CheckIn, Message
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
                                      {'status': 'rejected',
                                       'review_note': '照片过暗，请重拍'}))
        self.assertEqual(body['data']['status'], 'rejected')

    def test_reject_without_reason_refused(self):
        """驳回**不带原因**必须挡回，且不能把记录改成 rejected。

        领养人只看到「已驳回」时不知道该补什么；而驳回后这一月允许重交，
        没有原因就成了「原样重交 → 再次驳回」的空转。
        """
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}{checkin.id}/review/',
                                        {'status': 'rejected'}),
                  message='驳回需要填写原因')
        checkin.refresh_from_db()
        self.assertEqual(checkin.status, 'pending')

    def test_reject_reason_is_trimmed_stored_and_pushed(self):
        """原因要落库 + 下发（snake 与 camel 两个键）+ 推站内消息。

        接口响应里少 `review_note`，捕捉点端「详情」抽屉与领养人端打卡历史
        就都显示不出来；少一条消息，不看打卡页的领养人永远不知道被驳回了。
        """
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}{checkin.id}/review/', {
            'status': 'rejected', 'review_note': '  照片过暗，请重拍  '}))
        self.assertEqual(body['data']['review_note'], '照片过暗，请重拍')
        self.assertEqual(body['data']['reviewNote'], '照片过暗，请重拍')
        checkin.refresh_from_db()
        self.assertEqual(checkin.review_note, '照片过暗，请重拍')
        self.assertEqual(checkin.operator, self.shelter_user_a)
        msg = Message.objects.get(user_id=checkin.adopter_id)
        self.assertEqual(msg.type, 'checkin_reminder')
        self.assertIn('照片过暗，请重拍', msg.content)
        self.assertIn(checkin.pet_code, msg.content)

    def test_approve_keeps_reason_optional(self):
        """通过不需要原因，但也不能因此把已通过记录写成 rejected。"""
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{checkin.id}/review/', {'status': 'approved'}))
        checkin.refresh_from_db()
        self.assertEqual(checkin.status, 'approved')
        self.assertEqual(checkin.review_note, '')
        self.assertFalse(Message.objects.filter(user_id=checkin.adopter_id).exists())

    def test_invalid_status(self):
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}{checkin.id}/review/', {'status': 'ok'}),
                  message='status 必须为 approved 或 rejected')

    def test_double_review_rejected(self):
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{checkin.id}/review/', {'status': 'approved'}))
        self.expect_fail(self.post_json(f'{URL}{checkin.id}/review/',
                                        {'status': 'rejected',
                                         'review_note': '照片不清'}),
                  message='不可重复审核')

    def test_unknown_404(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}999999/review/', {'status': 'approved'}),
                  status=404)

    def test_cross_district_review_404(self):
        """打卡记录无 district 字段，按所属宠物区县收敛范围。"""
        pet = make_pet(district=self.district_b, status='adopted')
        adopter = make_user(role='adopter')
        checkin = CheckIn.objects.create(pet=pet, pet_code=pet.code,
                                         adopter=adopter, month='2026-05')
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}{checkin.id}/review/',
                                        {'status': 'approved'}),
                         status=404, message='无权访问')
        checkin.refresh_from_db()
        self.assertEqual(checkin.status, 'pending')

    def test_adopter_cannot_review(self):
        checkin = self._checkin()
        self.login_as(checkin.adopter)
        self.expect_fail(self.post_json(f'{URL}{checkin.id}/review/', {'status': 'approved'}),
                  status=403)


class CheckInResubmitAfterRejectTest(BusinessTestBase):
    """驳回后**同月可重交**，待审核/已通过同月不可重交。

    判据 `status__in=('pending','approved')` 与领养人端「待打卡月份」
    （adopter.js 里 `c.status !== 'rejected'`）必须同口径：
    两边不一致时，要么驳回成了死路（界面显示待打卡、提交被服务端拦回），
    要么同一月能交无数条（判据只看 month）。
    """

    def _adopted(self):
        pet = make_pet(district=self.district_a, status='adopted')
        adopter = make_user(role='adopter')
        Adoption.objects.create(pet=pet, pet_code=pet.code, adopter=adopter,
                                adopter_name='领养人', hospital=self.hospital_a,
                                status='completed', district=self.district_a)
        return pet, adopter

    def test_rejected_month_can_be_checked_in_again(self):
        pet, adopter = self._adopted()
        self.login_as(adopter)
        first = self.ok(self.post_json(f'{URL}create/',
                                       {'pet_id': pet.id, 'month': '2026-05'}))
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{first["data"]["id"]}/review/', {
            'status': 'rejected', 'review_note': '近况写得太少'}))
        self.login_as(adopter)
        body = self.ok(self.post_json(f'{URL}create/',
                                      {'pet_id': pet.id, 'month': '2026-05'}))
        self.assertEqual(body['data']['status'], 'pending')
        # 旧的驳回记录保留 —— 「驳回过 + 补交过」是回访管控要看两条痕迹
        rows = list(CheckIn.objects.filter(pet=pet, month='2026-05')
                    .order_by('id'))
        self.assertEqual([c.status for c in rows], ['rejected', 'pending'])

    def test_pending_month_still_blocks_duplicate(self):
        pet, adopter = self._adopted()
        self.login_as(adopter)
        self.ok(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'month': '2026-05'}))
        self.expect_fail(self.post_json(f'{URL}create/',
                                        {'pet_id': pet.id, 'month': '2026-05'}),
                  message='已打卡，请勿重复提交')
        self.assertEqual(CheckIn.objects.filter(pet=pet, month='2026-05').count(), 1)

    def test_approved_month_still_blocks_duplicate(self):
        pet, adopter = self._adopted()
        self.login_as(adopter)
        created = self.ok(self.post_json(f'{URL}create/',
                                         {'pet_id': pet.id, 'month': '2026-05'}))
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{created["data"]["id"]}/review/',
                               {'status': 'approved'}))
        self.login_as(adopter)
        self.expect_fail(self.post_json(f'{URL}create/',
                                        {'pet_id': pet.id, 'month': '2026-05'}),
                  message='已打卡，请勿重复提交')

    def test_reject_does_not_open_other_month(self):
        """重交只放开**被驳回的那一个月**：别的月份该拦还得拦。"""
        pet, adopter = self._adopted()
        self.login_as(adopter)
        first = self.ok(self.post_json(f'{URL}create/',
                                       {'pet_id': pet.id, 'month': '2026-05'}))
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{first["data"]["id"]}/review/', {
            'status': 'rejected', 'review_note': '近况写得太少'}))
        self.login_as(adopter)
        self.ok(self.post_json(f'{URL}create/', {'pet_id': pet.id, 'month': '2026-06'}))
        self.expect_fail(self.post_json(f'{URL}create/',
                                        {'pet_id': pet.id, 'month': '2026-06'}),
                  message='已打卡，请勿重复提交')


class CheckInListTest(BusinessTestBase):
    def _checkin(self):
        pet = make_pet(district=self.district_a, status='adopted')
        adopter = make_user(role='adopter')
        return CheckIn.objects.create(pet=pet, pet_code=pet.code, adopter=adopter,
                                      adopter_name='领养人', month='2026-05')

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

    def test_reviewer_name_reaches_the_reviewing_portal(self):
        """机构端列表必须带审核人姓名：详情抽屉那行「审核人」读的就是它。

        `CheckIn` 只有 `operator` 外键，列表不补名字时抽屉永远显示「—」，
        机构事后处理回访纠纷就查不到是谁判的。
        """
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{checkin.id}/review/',
                               {'status': 'approved'}))
        row = self.ok(self.get_json(URL))['data'][0]
        self.assertEqual(row['operator_name'], self.shelter_user_a.username)

    def test_reviewer_name_not_given_to_adopter(self):
        """领养人只该知道**为什么**驳回，不该拿到审核员的账号名。"""
        checkin = self._checkin()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{checkin.id}/review/',
                               {'status': 'rejected', 'review_note': '照片过暗'}))
        self.login_as(checkin.adopter)
        row = self.ok(self.get_json(URL))['data'][0]
        self.assertNotIn('operator_name', row)
        self.assertEqual(row['review_note'], '照片过暗')
