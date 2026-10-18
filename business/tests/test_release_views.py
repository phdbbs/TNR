"""放归闭环视图测试。"""
from business.models import Release
from business.tests.base import (
    assert_doc_no,
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
        assert_doc_no(self, release.ledger_no, 'REL')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment', '确认放归前状态不变')

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
                  message='不可放归')

    def test_create_unknown_pet(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': 999999}),
                  status=404, message='无权访问')

    def test_create_cross_district_404(self):
        pet = make_pet(district=self.district_b, shelter=self.shelter_b,
                       hospital=self.hospital_b, status='in_treatment')
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}),
                  status=404, message='无权访问')

    def test_create_deleted_pet_rejected(self):
        pet = self._pet()
        pet.is_deleted = True
        pet.save(update_fields=['is_deleted'])
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}),
                  status=404, message='无权访问')

    def test_create_blocked_when_active_adoption(self):
        """宠物有待领出的领养记录时不允许同时进入放归流程。"""
        from business.models import Adoption
        pet = self._pet()
        Adoption.objects.create(pet=pet, pet_code=pet.code, adopter_name='领养人',
                                adopter_phone='13800005000', status='pending_claim',
                                ledger_no='REL-GUARD', district=self.district_a)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}),
                  message='未完结的领养记录')

    def test_create_duplicate_pending_rejected(self):
        pet = self._pet()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{URL}create/', {'pet_id': pet.id}))
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}),
                  message='已有待放归记录')


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

    def test_confirm_cross_district_404(self):
        _, release = self._pending()
        self.login_as(self.shelter_user_b)
        self.expect_fail(self.post_json(f'{URL}{release.id}/confirm/', {}),
                  status=404, message='无权访问')

    def test_hospital_cannot_confirm(self):
        """确认放归由小区/捕捉点侧完成，医院无权限。"""
        _, release = self._pending()
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}{release.id}/confirm/'), status=403)


class ReleaseStageTest(BusinessTestBase):
    """放归两段流程（第四十八轮）：`pending → transferred → released`。

    原先只有一步（`pending → released`）—— 小区接收人还没签字、动物还在车上，
    台账上已经写「已放归」了。这里把两段各自的入口、幂等、越权都钉住。

    ⚠ 判据落在**业务结果**（状态真变了、动物状态**没**提前变），不停在状态码。
    """

    def _release(self, status='pending', pet=None):
        pet = pet or make_pet(district=self.district_a, shelter=self.shelter_a,
                              hospital=self.hospital_a, status='in_treatment')
        return Release.objects.create(
            pet=pet, pet_code=pet.code, community=self.community_a,
            community_name=self.community_a.name, status=status,
            # 区县跟着**动物**走（与 `release_create` 的 `pet.district_id` 同口径）——
            # 写死 district_a 的话，「跨区县取不到」这条用例会假绿。
            ledger_no=f'REL-STG-{status}', district=pet.district)

    def test_transfer_moves_pending_to_transferred(self):
        release = self._release()
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{URL}{release.id}/transfer/', {}))
        self.assertEqual(body['data']['status'], 'transferred')
        release.refresh_from_db()
        self.assertEqual(release.status, 'transferred')
        self.assertIsNotNone(release.transferred_at, '转运时间必须落库')
        release.pet.refresh_from_db()
        self.assertEqual(release.pet.status, 'in_treatment',
                         '只是转运回小区、还没放归，动物状态不能提前变成已放归')

    def test_transfer_twice_rejected(self):
        release = self._release()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{URL}{release.id}/transfer/', {}))
        self.expect_fail(self.post_json(f'{URL}{release.id}/transfer/', {}),
                         message='不可转运回小区')

    def test_confirm_after_transfer_sets_released(self):
        release = self._release(status='transferred')
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{release.id}/confirm/', {'receiver_name': '张物业'}))
        release.refresh_from_db()
        self.assertEqual(release.status, 'released')
        self.assertIsNotNone(release.released_at)
        release.pet.refresh_from_db()
        self.assertEqual(release.pet.status, 'released')

    def test_confirm_pending_still_allowed_for_legacy(self):
        """旧数据没有中间态 —— 新增 `transferred` 不能把它们卡死。

        反向对照：只测「transferred 能确认」的话，把判据写成
        「只认 transferred」也能通过，而生产上的历史放归单会全部办不了。
        """
        release = self._release(status='pending')
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}{release.id}/confirm/', {}))
        release.refresh_from_db()
        self.assertEqual(release.status, 'released')

    def test_transfer_rejected_after_released(self):
        release = self._release(status='released')
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}{release.id}/transfer/', {}),
                         message='不可转运回小区')

    def test_confirm_rejected_after_released(self):
        release = self._release(status='released')
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}{release.id}/confirm/', {}),
                         message='不可确认')

    def test_hospital_can_transfer_but_not_confirm(self):
        """医院端只做第一段。

        这条同时是**前端契约**的依据：医院端「出院管理 → 放归」里
        已转运的行不能渲染「确认放归」按钮 —— 那个按钮必然 403，
        用户会以为系统坏了。
        """
        release = self._release(status='transferred')
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}{release.id}/confirm/', {}), status=403)

    def test_transfer_cross_district_404(self):
        pet = make_pet(district=self.district_b, hospital=self.hospital_b,
                       status='in_treatment')
        release = self._release(pet=pet)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}{release.id}/transfer/', {}),
                         status=404, message='无权访问')
