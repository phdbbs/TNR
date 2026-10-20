"""领养业务视图测试：公开大厅、线下登记、在线申请、审核、信息编辑。"""
import io
import json

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from accounts.models import User
from business.models import (
    Adoption, AdoptionApplication, AdoptionHallListing, AdoptionPhoto, Message,
    Blacklist, Release,
)
from business.tests.base import (
    assert_doc_no,
    BusinessTestBase, make_institution, make_pet,
)
from business.services import pet_has_active_adoption

HALL_URL = '/api/business/adoptions/hall/'
HALL_LISTINGS_URL = '/api/business/hall-listings/'


def png_upload(name='photo.png'):
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), (200, 120, 80)).save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


class AdoptionHallPublicTest(BusinessTestBase):
    def test_public_list_without_login(self):
        pet = make_pet(district=self.district_a, status='pending_adopt',
                       hospital=self.hospital_a)
        AdoptionHallListing.objects.create(pet=pet, hospital=self.hospital_a)
        resp = self.client.get(HALL_URL)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['pet']['id'], pet.id)

    def test_inactive_listings_hidden(self):
        pet = make_pet(district=self.district_a, status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet, is_active=False)
        self.assertEqual(len(self.client.get(HALL_URL).json()['data']), 0)

    def test_detail_and_404(self):
        pet = make_pet(district=self.district_a, status='pending_adopt')
        listing = AdoptionHallListing.objects.create(pet=pet)
        resp = self.client.get(f'{HALL_URL}{listing.id}/')
        self.assertEqual(resp.json()['data']['pet']['id'], pet.id)
        self.expect_fail(self.client.get(f'{HALL_URL}999999/'), status=404,
                  message='领养信息不存在')


class AdoptionRegisterTest(BusinessTestBase):
    URL = '/api/business/adoptions/register/'

    def _pet(self):
        return make_pet(district=self.district_a, shelter=self.shelter_a,
                        hospital=self.hospital_a, status='pending_adopt')

    def _payload(self, pet, **kw):
        payload = {'pet_id': pet.id, 'adopter_name': '王领养',
                   'adopter_phone': '13866667777',
                   'adopter_id_card': '110101199001011234',
                   'qualification': '有稳定住所'}
        payload.update(kw)
        return payload

    def test_register_creates_adopter_account_and_record(self):
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(self.URL, self._payload(pet)))
        adoption = Adoption.objects.get(id=body['data']['id'])
        self.assertEqual(adoption.status, 'pending_claim')
        assert_doc_no(self, adoption.ledger_no, 'ADP')

        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_claim')

        adopter = adoption.adopter
        self.assertEqual(adopter.role, 'adopter')
        self.assertEqual(adopter.username, '13866667777')
        self.assertEqual(adopter.phone, '13866667777')
        self.assertTrue(adopter.check_password('123456'))
        self.assertTrue(Message.objects.filter(user=adopter).exists())

    def test_register_reuses_existing_adopter_by_phone(self):
        existing = User.objects.create_user(username='existing_adopter',
                                            password='x', role='adopter',
                                            phone='13866667777')
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(self.URL, self._payload(pet)))
        adoption = Adoption.objects.get(id=body['data']['id'])
        self.assertEqual(adoption.adopter, existing)

    def test_register_deactivates_hall_listing(self):
        pet = self._pet()
        AdoptionHallListing.objects.create(pet=pet)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.URL, self._payload(pet)))
        pet.refresh_from_db()
        self.assertFalse(pet.hall_listing.is_active)

    def test_register_blacklist_blocked(self):
        from business.models import Blacklist
        Blacklist.objects.create(name='王老赖', phone='13866667777', reason='弃养',
                                 district=self.district_a)
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)),
                  message='该领养人已在黑名单中')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')

    def test_register_missing_fields(self):
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, self._payload(pet, adopter_name='')),
                  message='领养人姓名不能为空')
        self.expect_fail(self.post_json(self.URL, self._payload(pet, adopter_phone='')),
                  message='领养人电话不能为空')

    def test_register_wrong_status(self):
        pet = make_pet(district=self.district_a, status='released')
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)), message='不可领养')

    def test_hospital_can_register_own_pet(self):
        """第四十八轮：医院端**也能直接开领养单**（出院模块的「领养」子模块）。

        原先只有捕捉点 / 政府端能登记，医院只能等捕捉点开好单、再来
        「确认领出」—— 但领养人往往就是到医院现场挑的，医院反而没有入口。
        """
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(self.URL, self._payload(pet)))
        adoption = Adoption.objects.get(id=body['data']['id'])
        self.assertEqual(adoption.hospital_id, self.hospital_a.id)
        self.assertEqual(adoption.institution_id, self.hospital_a.id)
        self.assertEqual(adoption.status, 'pending_claim')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_claim')

    def test_other_hospital_cannot_register(self):
        """反向对照：别家医院对本院的动物必须 404。

        没有这条的话，把医院分支写成「放开 403 就行」也能让上面那条通过 ——
        那就是横向越权（A 院能替 B 院开领养单）。
        """
        pet = self._pet()
        self.login_as(self.hospital_user_b)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)),
                         status=404, message='无权访问')
        self.assertFalse(Adoption.objects.filter(pet=pet).exists())


class AdoptionConfirmClaimTest(BusinessTestBase):
    def _pending_claim(self, district=None, hospital=None, user=None):
        district = district or self.district_a
        hospital = hospital or self.hospital_a
        pet = make_pet(district=district, hospital=hospital,
                       status='pending_claim')
        adopter = User.objects.create_user(username=f'claim_adopter_{pet.id}',
                                           password='x', role='adopter')
        adoption = Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter=adopter,
            adopter_name='领养人', adopter_phone='13800001111',
            hospital=hospital, status='pending_claim',
            ledger_no=f'ADP-{pet.id}', district=district)
        return pet, adopter, adoption

    def test_confirm_cross_district_hospital(self):
        """跨区县送医：甲区的动物由乙区医院受理领养，乙区医院必须能确认领出。

        `Adoption.district` 是**动物**的区县，取单按区县收敛时，出院流程里
        「开领养单 → 当场确认领出」（医院端同一个函数连着调）第二步永远 404，
        单据开出来了、动物领不出去，领养记录永久卡在 `pending_claim`。
        与 `test_confirm_wrong_hospital_rejected` 成对：放宽的是机构归属，
        不是隔离。
        """
        pet, _, adoption = self._pending_claim(
            district=self.district_a, hospital=self.hospital_b)
        self.login_as(self.hospital_user_b)
        body = self.ok(self.post_json(
            f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}))
        self.assertEqual(body['data']['status'], 'completed')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'adopted')

    def test_confirm_success(self):
        pet, adopter, adoption = self._pending_claim()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(
            f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}))
        self.assertEqual(body['data']['status'], 'completed')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'adopted')
        adoption.refresh_from_db()
        self.assertIsNotNone(adoption.adopted_at)
        self.assertTrue(Message.objects.filter(user=adopter, title='领养完成').exists())

    def test_confirm_twice_rejected(self):
        _, _, adoption = self._pending_claim()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}))
        self.expect_fail(self.post_json(f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}),
                  message='不可确认领出')

    def test_confirm_wrong_hospital_rejected(self):
        _, _, adoption = self._pending_claim()
        # 跨区县越权按约定返回 404（不暴露记录是否存在）
        self.login_as(self.hospital_user_b)
        self.expect_fail(self.post_json(f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}),
                  status=404, message='无权访问')

    def test_adopter_cannot_confirm(self):
        _, _, adoption = self._pending_claim()
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(f'/api/business/adoptions/{adoption.id}/confirm-claim/', {}),
                  status=403)


class AdoptionApplyTest(BusinessTestBase):
    URL = '/api/business/adoptions/apply/'

    def _pet(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_adopt')
        # 在线申请要求宠物仍处于大厅上架状态
        AdoptionHallListing.objects.create(pet=pet, hospital=self.hospital_a,
                                           is_active=True)
        return pet

    def _payload(self, pet, **kw):
        payload = {'pet_id': pet.id, 'applicant_name': '李申请',
                   'applicant_phone': '13755556666', 'reason': '喜欢'}
        payload.update(kw)
        return payload

    def test_apply_success(self):
        pet = self._pet()
        self.login_as(self.adopter)
        body = self.ok(self.post_json(self.URL, self._payload(pet)))
        application = AdoptionApplication.objects.get(id=body['data']['id'])
        self.assertEqual(application.status, 'pending')
        self.assertEqual(application.hospital, self.hospital_a)

    def test_apply_duplicate_rejected(self):
        pet = self._pet()
        self.login_as(self.adopter)
        self.ok(self.post_json(self.URL, self._payload(pet)))
        self.expect_fail(self.post_json(self.URL, self._payload(pet)),
                  message='请勿重复提交')

    def test_apply_rejected_then_can_reapply(self):
        pet = self._pet()
        self.login_as(self.adopter)
        first = self.ok(self.post_json(self.URL, self._payload(pet)))['data']['id']
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(
            f'/api/business/adoptions/applications/{first}/review/',
            {'action': 'reject', 'review_note': '不符合'}))
        self.login_as(self.adopter)
        body2 = self.post_json(self.URL, self._payload(pet))
        self.assertTrue(body2.json()['success'],
                        '被拒后可再次申请: ' + body2.content.decode())

    def test_apply_cancelled_then_can_reapply(self):
        """撤回（`cancelled`）与「被拒」（`rejected`）必须**同一口径**。

        重复校验原先只 `.exclude(status='rejected')`。第四十六轮补上「撤回」
        这条路径后，若不同步放行 `cancelled`，领养人一撤回就**永久无法再申请
        同一只动物** —— 而提示语还是「请勿重复提交」，看起来像正常防重复，
        实际是死锁。这条用例与 `test_apply_rejected_then_can_reapply` 成对，
        缺了它，「撤回」这个新功能就是个陷阱。
        """
        pet = self._pet()
        self.login_as(self.adopter)
        first = self.ok(self.post_json(self.URL, self._payload(pet)))['data']['id']
        self.ok(self.post_json(
            f'/api/business/adoptions/applications/{first}/cancel/', {}))
        self.assertEqual(
            AdoptionApplication.objects.get(id=first).status, 'cancelled')
        body2 = self.post_json(self.URL, self._payload(pet))
        self.assertTrue(body2.json()['success'],
                        '撤回后可再次申请: ' + body2.content.decode())

    def test_apply_pending_still_blocks_reapply(self):
        """反向对照：不能为了放行 `cancelled` 把整个重复校验拆掉。

        `test_apply_cancelled_then_can_reapply` 只证明「撤回后能再申请」；
        若判据被写成「一律放行」，那条也会绿。这里断言 `pending` 仍然拦。
        """
        pet = self._pet()
        self.login_as(self.adopter)
        self.ok(self.post_json(self.URL, self._payload(pet)))
        self.expect_fail(self.post_json(self.URL, self._payload(pet)),
                         message='请勿重复提交')

    def test_apply_blacklist_blocked(self):
        from business.models import Blacklist
        Blacklist.objects.create(name='李申请', phone='13755556666', reason='虚假信息',
                                 district=self.district_a)
        pet = self._pet()
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)),
                  message='您已被列入领养黑名单')

    def test_apply_missing_name(self):
        pet = self._pet()
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(self.URL, self._payload(pet, applicant_name='')),
                  message='请填写姓名')

    def test_apply_wrong_status(self):
        pet = make_pet(district=self.district_a, status='adopted')
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)), message='不可申请领养')

    def test_hospital_cannot_apply(self):
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(self.URL, self._payload(pet)), status=403)


class AdoptionApplicationReviewTest(BusinessTestBase):
    def _application(self, status='pending'):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_adopt')
        applicant = User.objects.create_user(username='app_user', password='x',
                                             role='adopter')
        return AdoptionApplication.objects.create(
            pet=pet, pet_code=pet.code, applicant=applicant,
            applicant_name='申请者', applicant_phone='13911112222',
            hospital=self.hospital_a, status=status)
    def test_review_approve_sends_message(self):
        application = self._application()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'approve', 'review_note': '资质符合'}))
        self.assertEqual(body['data']['status'], 'approved')
        application.refresh_from_db()
        self.assertEqual(application.reviewed_by, self.hospital_user_a)
        self.assertTrue(Message.objects.filter(user=application.applicant).exists())

    def test_review_reject(self):
        application = self._application()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'reject', 'review_note': '资质不足'}))
        self.assertEqual(body['data']['status'], 'rejected')
        self.assertIn('资质不足',
                      Message.objects.filter(user=application.applicant).first().content)

    def test_review_invalid_action(self):
        application = self._application()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'maybe'}), message='无效的审核操作')

    def test_review_twice_rejected(self):
        application = self._application()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'approve'}))
        self.expect_fail(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'approve'}), message='该申请已处理')

    def test_review_wrong_hospital(self):
        application = self._application()
        # 跨区县越权按约定返回 404（不暴露记录是否存在）
        self.login_as(self.hospital_user_b)
        self.expect_fail(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'approve'}), status=404, message='无权访问')

    def test_application_list_scoping(self):
        mine = self._application()
        other_hospital = make_institution(type='hospital', district=self.district_b)
        pet_other = make_pet(district=self.district_b, hospital=other_hospital,
                             status='pending_adopt')
        applicant = User.objects.create_user(username='app_user2', password='x',
                                             role='adopter')
        AdoptionApplication.objects.create(
            pet=pet_other, pet_code=pet_other.code, applicant=applicant,
            applicant_name='别人', applicant_phone='13800000001',
            hospital=other_hospital)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json('/api/business/adoptions/applications/'))['data']
        self.assertEqual([a['id'] for a in data], [mine.id])

    def test_my_applications(self):
        application = self._application()
        self.login_as(application.applicant)
        data = self.ok(self.get_json('/api/business/adoptions/my-applications/'))['data']
        self.assertEqual([a['id'] for a in data], [application.id])
        self.assertEqual(data[0]['pet']['id'], application.pet_id)


class AdoptionApplicationCancelTest(BusinessTestBase):
    """领养人撤回自己的申请（`pending` → `cancelled`）。

    第四十六轮新增：`AdoptionApplication.status` 里的 `cancelled` 此前
    **没有任何产生端**（模型声明了、领养人端也映射了标签与徽章，却没有
    接口也没有按钮），属于「枚举值存在、写入路径缺失」。
    """

    def _application(self, status='pending', applicant=None):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_adopt')
        applicant = applicant or User.objects.create_user(
            username='cancel_user', password='x', role='adopter')
        return AdoptionApplication.objects.create(
            pet=pet, pet_code=pet.code, applicant=applicant,
            applicant_name='撤回者', applicant_phone='13922223333',
            hospital=self.hospital_a, status=status)

    @staticmethod
    def _url(pk):
        return f'/api/business/adoptions/applications/{pk}/cancel/'

    def test_owner_can_cancel_pending(self):
        application = self._application()
        self.login_as(application.applicant)
        body = self.ok(self.post_json(self._url(application.id), {}))
        self.assertEqual(body['data']['status'], 'cancelled')
        application.refresh_from_db()
        self.assertEqual(application.status, 'cancelled')

    def test_cannot_cancel_approved(self):
        """已通过的申请要走机构侧「撤销领养」，领养人自己不该有这个权力。"""
        application = self._application(status='approved')
        self.login_as(application.applicant)
        self.expect_fail(self.post_json(self._url(application.id), {}),
                         message='无法撤回')
        application.refresh_from_db()
        self.assertEqual(application.status, 'approved')

    def test_cannot_cancel_twice(self):
        application = self._application()
        self.login_as(application.applicant)
        self.ok(self.post_json(self._url(application.id), {}))
        self.expect_fail(self.post_json(self._url(application.id), {}),
                         message='无法撤回')

    def test_cannot_cancel_someone_elses_application(self):
        """归属必须从**申请单**推导，不能从「操作者是领养人」推导。

        否则领养人 A 能撤回 B 的申请（同族缺陷本项目已踩过四次）。
        """
        application = self._application()
        other = User.objects.create_user(username='other_adopter', password='x',
                                         role='adopter')
        self.login_as(other)
        self.expect_fail(self.post_json(self._url(application.id), {}),
                         status=404, message='无权访问')
        application.refresh_from_db()
        self.assertEqual(application.status, 'pending')

    def test_others_and_missing_are_indistinguishable(self):
        """存在性预言机：「别人的申请」与「不存在」必须完全不可区分。"""
        application = self._application()
        other = User.objects.create_user(username='other_adopter2', password='x',
                                         role='adopter')
        self.login_as(other)
        body_other = self.post_json(self._url(application.id), {})
        body_missing = self.post_json(self._url(99999999), {})
        self.assertEqual(body_other.status_code, body_missing.status_code)
        self.assertEqual(body_other.json().get('message'),
                         body_missing.json().get('message'))

    def test_hospital_cannot_cancel(self):
        application = self._application()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(self._url(application.id), {}), status=403)
        application.refresh_from_db()
        self.assertEqual(application.status, 'pending')

    def test_cancel_does_not_touch_pet_or_listing(self):
        """撤回只是申请单作废：待审核申请并不占用动物，所以动物状态与
        大厅上架状态都不该被改动。"""
        application = self._application()
        pet = application.pet
        before_status = pet.status
        self.login_as(application.applicant)
        self.ok(self.post_json(self._url(application.id), {}))
        pet.refresh_from_db()
        self.assertEqual(pet.status, before_status)
        self.assertFalse(pet_has_active_adoption(pet))


class AdoptionInfoEditTest(BusinessTestBase):
    def test_edit_creates_listing(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_adopt')
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(
            f'/api/business/adoptions/{pet.id}/edit-info/',
            {'intro': '亲人', 'is_active': True}))
        listing = AdoptionHallListing.objects.get(pet=pet)
        self.assertEqual(listing.intro, '亲人')
        self.assertTrue(listing.is_active)
        self.assertIsNotNone(listing.published_at)

    def test_edit_deactivate(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet, is_active=True)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'/api/business/adoptions/{pet.id}/edit-info/',
                               {'is_active': False}))
        pet.refresh_from_db()
        self.assertFalse(pet.hall_listing.is_active)

    def test_edit_unknown_pet_404(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json('/api/business/adoptions/999999/edit-info/', {}),
                  status=404, message='宠物不存在')

    def test_edit_cross_district_pet_staying_here(self):
        """跨区县送医的动物：列表看得见，资料维护也必须能在本院办。"""
        pet = make_pet(district=self.district_b, shelter=self.shelter_b,
                       hospital=self.hospital_a, status='pending_adopt')
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(
            f'/api/business/adoptions/{pet.id}/edit-info/',
            {'intro': '跨区县送医', 'is_active': True}))
        listing = AdoptionHallListing.objects.get(pet=pet)
        self.assertEqual(listing.intro, '跨区县送医')
        self.assertTrue(listing.is_active)


class AdoptionHallPhotoPolicyTest(BusinessTestBase):
    """大厅照片：系统照片自动带入、医院控制显隐、只补拍说明类照片。"""

    def _listing(self, code='TNRHALLPHOT01', hidden=None):
        pet = make_pet(code=code, district=self.district_a,
                       hospital=self.hospital_a, status='pending_adopt')
        pet.photo_capture = png_upload('capture.png')
        pet.photo_before = png_upload('before.png')
        pet.photo_after = png_upload('after.png')
        pet.photo_treatment = png_upload('treatment.png')
        pet.save()
        return pet, AdoptionHallListing.objects.create(
            pet=pet, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, is_active=True,
            hidden_photos=hidden or [])

    def test_system_photos_come_from_pet_archive(self):
        """捕捉 / 诊疗环节已有的照片自动出现在大厅，不需要医院再传一遍。"""
        pet, listing = self._listing()
        data = self.client.get(f'{HALL_URL}{listing.id}/').json()['data']
        self.assertEqual(
            [p['field'] for p in data['photos']],
            ['photo_capture', 'photo_before', 'photo_after',
             'photo_treatment'])
        self.assertEqual(data['photos'][0]['label'], '捕捉照片')
        self.assertEqual(data['photos'][0]['url'], pet.photo_capture.url)
        self.assertEqual(data['extra_photos'], [])

    def test_hidden_system_photo_disappears_from_public_payload(self):
        """隐藏不只是从清单里删掉：`pet` 上的同名字段也必须抹空。"""
        _, listing = self._listing(hidden=['photo_before'])
        data = self.client.get(f'{HALL_URL}{listing.id}/').json()['data']
        self.assertEqual([p['field'] for p in data['photos']],
                         ['photo_capture', 'photo_after', 'photo_treatment'])
        self.assertEqual(data['pet']['photo_before'], '')
        self.assertEqual(data['pet']['photoBefore'], '')
        self.assertNotIn('hidden_photos', data)
        self.assertNotIn('hiddenPhotos', data)

    def test_hospital_view_keeps_hidden_photos_and_flags(self):
        """本院维护界面要看得到全部系统照片，并带 visible 标记回显开关。"""
        _, listing = self._listing(hidden=['photo_before'])
        self.login_as(self.hospital_user_a)
        rows = self.ok(self.get_json(HALL_LISTINGS_URL))['data']
        row = next(r for r in rows if r['id'] == listing.id)
        self.assertEqual(row['hidden_photos'], ['photo_before'])
        flags = {p['field']: p['visible'] for p in row['photos']}
        self.assertFalse(flags['photo_before'])
        self.assertTrue(flags['photo_capture'])
        self.assertTrue(row['pet']['photo_before'])

    def test_extra_photo_upload_and_delete(self):
        _, listing = self._listing()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.client.post(
            f'/api/business/adoptions/{listing.pet_id}/edit-info/',
            {'extra_photos': [png_upload('a.png'), png_upload('b.png')]}))
        self.assertEqual(len(body['data']['extra_photos']), 2)
        self.assertEqual(AdoptionPhoto.objects.filter(listing=listing).count(), 2)

        public = self.client.get(f'{HALL_URL}{listing.id}/').json()['data']
        self.assertEqual([p['field'] for p in public['photos']].count('extra'), 2)

        first = AdoptionPhoto.objects.filter(listing=listing).first()
        self.ok(self.client.post(
            f'/api/business/adoptions/{listing.pet_id}/edit-info/',
            {'delete_photo_ids': json.dumps([first.id])}))
        self.assertFalse(AdoptionPhoto.objects.filter(id=first.id).exists())
        self.assertEqual(AdoptionPhoto.objects.filter(listing=listing).count(), 1)

    def test_delete_cannot_touch_other_listings_photos(self):
        _, listing = self._listing(code='TNRHALLPHOT02')
        other_pet = make_pet(code='TNRHALLPHOT03', district=self.district_a,
                             hospital=self.hospital_a, status='pending_adopt')
        other = AdoptionHallListing.objects.create(
            pet=other_pet, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name)
        photo = AdoptionPhoto.objects.create(listing=other, image=png_upload())
        self.login_as(self.hospital_user_a)
        self.ok(self.client.post(
            f'/api/business/adoptions/{listing.pet_id}/edit-info/',
            {'delete_photo_ids': json.dumps([photo.id])}))
        self.assertTrue(AdoptionPhoto.objects.filter(id=photo.id).exists())

    def test_rejects_unknown_hidden_photo(self):
        _, listing = self._listing()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(
            f'/api/business/adoptions/{listing.pet_id}/edit-info/',
            {'hidden_photos': ['photo_evil']}),
            message='照片显示设置包含未知照片')
        listing.refresh_from_db()
        self.assertEqual(listing.hidden_photos, [])

    def test_rejects_more_than_six_extra_photos(self):
        _, listing = self._listing()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.client.post(
            f'/api/business/adoptions/{listing.pet_id}/edit-info/',
            {'extra_photos': [png_upload(f'{i}.png') for i in range(7)]}),
            message='最多上传 6 张')
        self.assertEqual(AdoptionPhoto.objects.filter(listing=listing).count(), 0)

    def test_multipart_is_active_false_actually_unpublishes(self):
        """form-data 里 `is_active=false` 是字符串，`bool('false')` 为真。"""
        _, listing = self._listing()
        self.login_as(self.hospital_user_a)
        self.ok(self.client.post(
            f'/api/business/adoptions/{listing.pet_id}/edit-info/',
            {'is_active': 'false'}))
        listing.refresh_from_db()
        self.assertFalse(listing.is_active)


class AdoptionListTest(BusinessTestBase):
    def test_list_keyword_and_status(self):
        pet = make_pet(district=self.district_a, status='adopted')
        adopter = User.objects.create_user(username='list_adopter', password='x',
                                           role='adopter')
        adoption = Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter=adopter,
            adopter_name='张三丰', adopter_phone='13800007777',
            hospital=self.hospital_a, status='completed',
            ledger_no='ADP-T2', district=self.district_a)
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json('/api/business/adoptions/'))['data']
        self.assertEqual(len(data), 1)
        data = self.ok(self.get_json('/api/business/adoptions/?keyword=张三丰'))['data']
        self.assertEqual([a['id'] for a in data], [adoption.id])
        data = self.ok(self.get_json('/api/business/adoptions/?status=pending_claim'))['data']
        self.assertEqual(data, [])


class AdoptionReclaimTest(BusinessTestBase):
    """违规收回：撤销领养 + 收回动物 + 重新上架 + 记入黑名单。"""

    URL = '/api/business/adoptions/{}/reclaim/'

    def _completed_adoption(self):
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a, status='adopted')
        adopter = User.objects.create_user(username='reclaim_adopter', password='x',
                                           role='adopter', phone='13800008888')
        listing = AdoptionHallListing.objects.create(pet=pet, is_active=False)
        adoption = Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter=adopter,
            adopter_name='违规领养人', adopter_phone='13800008888',
            adopter_id_card='110101199001019999',
            hospital=self.hospital_a, status='completed',
            ledger_no='ADP-T3', district=self.district_a)
        return pet, adoption, listing

    def test_reclaim_full_effects(self):
        pet, adoption, listing = self._completed_adoption()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(self.URL.format(adoption.id),
                                      {'reason': '回访发现弃养'}))

        adoption.refresh_from_db()
        self.assertEqual(adoption.status, 'cancelled')

        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')

        listing.refresh_from_db()
        self.assertTrue(listing.is_active, '收回后应重新上架领养大厅')

        self.assertTrue(
            Blacklist.objects.filter(phone='13800008888', is_deleted=False).exists(),
            '违规领养人应被记入黑名单')
        bl = Blacklist.objects.get(phone='13800008888', is_deleted=False)
        self.assertEqual(bl.reason, '回访发现弃养')
        self.assertIn('收回动物', body['message'])

    def test_reclaim_requires_reason(self):
        _, adoption, _ = self._completed_adoption()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL.format(adoption.id), {}),
                         message='请填写收回原因')

    def test_reclaim_twice_rejected(self):
        _, adoption, _ = self._completed_adoption()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.URL.format(adoption.id), {'reason': '弃养'}))
        self.expect_fail(self.post_json(self.URL.format(adoption.id), {'reason': '弃养'}),
                         message='请勿重复操作')

    def test_reclaim_no_duplicate_blacklist(self):
        """同一人已有黑名单记录时不再重复插入。"""
        Blacklist.objects.create(name='违规领养人', phone='13800008888',
                                 reason='历史违规', district=self.district_a)
        _, adoption, _ = self._completed_adoption()
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.URL.format(adoption.id), {'reason': '再次违规'}))
        self.assertEqual(
            Blacklist.objects.filter(phone='13800008888', is_deleted=False).count(), 1)

    def test_reclaim_cross_district_404(self):
        _, adoption, _ = self._completed_adoption()
        self.login_as(self.shelter_user_b)
        self.expect_fail(self.post_json(self.URL.format(adoption.id), {'reason': '弃养'}),
                         status=404, message='无权访问')

    def test_hospital_cannot_reclaim(self):
        _, adoption, _ = self._completed_adoption()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(self.URL.format(adoption.id), {'reason': '弃养'}),
                         status=403)


class AdoptionScopeGuardTest(BusinessTestBase):
    """跨区县越权与逻辑删除宠物的防线。"""

    def _pet_other_district(self):
        return make_pet(district=self.district_b, shelter=self.shelter_b,
                        hospital=self.hospital_b, status='pending_adopt')

    def test_register_cross_district_404(self):
        pet = self._pet_other_district()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json('/api/business/adoptions/register/', {
            'pet_id': pet.id, 'adopter_name': '甲', 'adopter_phone': '13800000010',
        }), status=404, message='无权访问')

    def test_edit_info_cross_district_404(self):
        pet = self._pet_other_district()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(
            f'/api/business/adoptions/{pet.id}/edit-info/', {'intro': 'x'}),
            status=404, message='无权访问')

    def test_register_deleted_pet_rejected(self):
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a, status='pending_adopt',
                       is_deleted=True)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json('/api/business/adoptions/register/', {
            'pet_id': pet.id, 'adopter_name': '甲', 'adopter_phone': '13800000011',
        }), status=404, message='无权访问')

    def test_register_blocked_when_pending_release(self):
        """放归单未办结（两段任一态）的动物不接受领养登记 —— 双重承诺。

        两段都要测：判据原先只认 `pending`，动物一登记「转运回小区」进入
        `transferred`，放归单还没签字就能把同一只动物领养出去 —— 先放归、
        后领养的台账互相打架。
        """
        for status in ('pending', 'transferred'):
            with self.subTest(status=status):
                pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                               hospital=self.hospital_a, status='pending_adopt')
                Release.objects.create(pet=pet, pet_code=pet.code,
                                       community=self.community_a,
                                       community_name=self.community_a.name,
                                       status=status, district=self.district_a)
                self.login_as(self.shelter_user_a)
                self.expect_fail(self.post_json('/api/business/adoptions/register/', {
                    'pet_id': pet.id, 'adopter_name': '甲', 'adopter_phone': '13800000012',
                }), message='未办结')

    def test_register_allowed_after_release_finished(self):
        """反向对照：已办结（released）的放归单不算占用，不能一律拒绝。

        把判据写成「这张动物只要有放归记录就拒绝」也能让上一条用例变绿，
        但放归之后转领养（小区猫回站再找领养）就全堵死了。
        """
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a, status='pending_adopt')
        Release.objects.create(pet=pet, pet_code=pet.code, community=self.community_a,
                               community_name=self.community_a.name, status='released',
                               district=self.district_a)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json('/api/business/adoptions/register/', {
            'pet_id': pet.id, 'adopter_name': '甲', 'adopter_phone': '13800000013',
        }))

    def test_register_blocked_when_already_pending_claim(self):
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a, status='pending_adopt')
        Adoption.objects.create(pet=pet, pet_code=pet.code, adopter_name='前任',
                                adopter_phone='13800000013', status='pending_claim',
                                ledger_no='ADP-T9', district=self.district_a)
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json('/api/business/adoptions/register/', {
            'pet_id': pet.id, 'adopter_name': '甲', 'adopter_phone': '13800000014',
        }), message='未完结的领养记录')

    def test_hall_hides_deleted_and_adopted_pets(self):
        ok_pet = make_pet(district=self.district_a, status='pending_adopt')
        AdoptionHallListing.objects.create(pet=ok_pet, is_active=True)
        dead_pet = make_pet(district=self.district_a, status='pending_adopt',
                            is_deleted=True)
        AdoptionHallListing.objects.create(pet=dead_pet, is_active=True)
        done_pet = make_pet(district=self.district_a, status='adopted')
        AdoptionHallListing.objects.create(pet=done_pet, is_active=True)

        data = self.client.get(HALL_URL).json()['data']
        self.assertEqual([d['pet']['id'] for d in data], [ok_pet.id])

    def test_apply_requires_active_listing(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_adopt')
        self.login_as(self.adopter)
        self.expect_fail(self.post_json('/api/business/adoptions/apply/', {
            'pet_id': pet.id, 'applicant_name': '李', 'applicant_phone': '13700000001',
        }), message='未在领养大厅上架')

    def test_approve_rejected_when_pet_taken(self):
        """宠物已被领出时，审核通过应被拦截，避免重复批准。"""
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='pending_claim')
        applicant = User.objects.create_user(username='guard_app', password='x',
                                             role='adopter')
        application = AdoptionApplication.objects.create(
            pet=pet, pet_code=pet.code, applicant=applicant,
            applicant_name='申请者', applicant_phone='13900000001',
            hospital=self.hospital_a, status='pending')
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(
            f'/api/business/adoptions/applications/{application.id}/review/',
            {'action': 'approve'}), message='已不可领养')
        application.refresh_from_db()
        self.assertEqual(application.status, 'pending')
