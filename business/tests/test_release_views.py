"""放归闭环视图测试。"""
from django.utils import timezone

from business.models import Release, ReturnOrder, Treatment
from business.tests.base import (
    assert_doc_no,
    BusinessTestBase, make_capture, make_district, make_institution,
    make_pet, make_user,
)

URL = '/api/business/releases/'
RTN_URL = '/api/business/return-orders/'


class ReleaseCreateTest(BusinessTestBase):
    def _pet(self, with_capture_community=True, status='in_treatment'):
        capture = make_capture(
            district=self.district_a, shelter=self.shelter_a,
            community=self.community_a if with_capture_community else None)
        return make_pet(district=self.district_a, shelter=self.shelter_a,
                        hospital=self.hospital_a, status=status,
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
        pet = self._pet(with_capture_community=False, status='pending_release')
        other_community = make_institution(type='community', district=self.district_a)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}create/', {
            'pet_id': pet.id, 'community_id': other_community.id,
            'receiver_name': '李物业',
        }))
        self.assertEqual(Release.objects.get(id=body['data']['id']).community,
                         other_community)

    def test_create_without_community_rejected(self):
        pet = self._pet(with_capture_community=False, status='pending_release')
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
                  message='未办结的放归记录')


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
            # 归属机构同样照 `release_create` 落（`pet.hospital_id or pet.shelter_id`），
            # 否则夹具与真实写入口径不同，跨区县送医那条正常路径就测不到。
            institution=pet.hospital or pet.shelter,
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

    def test_create_blocked_while_transferred(self):
        """转运之后（`transferred`）同样不许再开一张放归单。

        拆两段之前，重复放归的判据是「有没有 pending 单」；拆段之后动物一登记
        转运，`pending` 就没了，于是同一只动物能同时挂两张未办结的放归单，
        两张都能走到「已放归」，台账上同一只动物放归两次。
        口径统一在 `Release.OPEN_STATUSES`。
        """
        release = self._release()
        pet = release.pet
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{URL}{release.id}/transfer/', {}))
        self.assertEqual(pet.status, 'in_treatment')
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}),
                         message='未办结的放归记录')

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

    def test_owning_hospital_can_transfer_across_district(self):
        """跨区县送医：甲区捕捉的动物在乙区医院，乙区医院必须能「转运回小区」。

        `Release.district` 是**动物**的区县、`institution` 才是本院，所以出院页
        （按机构收口，见 `release_list`）看得见这一行，取单若只按区县就永远 404
        —— 单据卡在「待放归」，动物还在院里出不去。
        与上面那条成对：放行的是**归属机构**，不是所有医院。
        """
        pet = make_pet(district=self.district_a, hospital=self.hospital_b,
                       status='in_treatment')
        release = self._release(pet=pet)
        self.login_as(self.hospital_user_b)
        body = self.ok(self.post_json(f'{URL}{release.id}/transfer/', {}))
        self.assertEqual(body['data']['status'], 'transferred')
        release.refresh_from_db()
        self.assertEqual(release.status, 'transferred')


class CrossDistrictReturnReleaseTest(BusinessTestBase):
    """**跨区县运回签收**之后，签收方必须能把放归一路办到终态（第五十九轮界面实测）。

    业务链路（真实发生的一条，2026-10-26 界面录入时卡死）：
      乙区捕捉 → 送**甲区**医院（转运允许跨区县）→ 诊疗完成、建议放归
      → 医院开运回单，接收方按需求选**本院所在区县**的捕捉点（甲区站）
      → 甲区站签收：`pet.shelter` = 甲区站，`pet.district` **仍是乙区**
        （放归要回原捕捉小区、政府按捕捉地统计，这个字段不能跟着接收站改）
      → 甲区站发起放归 → 转运回小区 → 小区签字确认。

    断链的原因：签收前只按**区县**给捕捉点收敛数据（`get_active_pet` /
    `get_district_filtered_queryset`），而最后三步的操作方是「动物挂名但区县
    不同」的甲区站 —— 于是动物凭空从「发起放归」备选框消失（界面显示
    「暂无可放归动物」），直接 POST 回 404，签收方既看不到自己也办不了。
    同一只动物对市级捕捉点却一直是可见的：市级分支早就并上了
    「本机构名下 ∪ 机构区县」，差别不是业务规则，而是这条链路只在市级暴露过。

    所以口径必须统一成 **本区县 ∪ 挂本站名下**（`services.shelter_scope`），
    并且**列表与写接口一起改**：只放开列表会变成「点得开却报错」，
    只放开写接口会变成「报错但界面里还在」，两种都是现场最难查的形态。
    """

    def setUp(self):
        """造一只「乙区捕捉、甲区医院诊疗、甲区捕捉点签收」的动物。"""
        self.community_b = make_institution(type='community',
                                            district=self.district_b,
                                            name='乙区小区')
        pet = make_pet(code='REL-XD-001', status='in_treatment',
                       district=self.district_b, shelter=self.shelter_b,
                       hospital=self.hospital_a,
                       capture=make_capture(district=self.district_b,
                                            shelter=self.shelter_b,
                                            community_name='乙区某小区'))
        # 诊疗完成、建议去向=放归，尚未办结（办结由运回签收那一步做）
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, institution=self.hospital_a,
            status='completed', done_at=timezone.now(),
            district=self.district_b, disposal_advised='released')
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json(f'{RTN_URL}create/', {
            'pet_ids': [pet.id], 'shelter_id': self.shelter_a.id}))
        order = ReturnOrder.objects.get(id=body['data']['id'])
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{RTN_URL}{order.id}/receive/', {
            'receiver_name': '李捕捉', 'receiver_phone': '13800001000',
            'signature': 'data:image/png;base64,SIG'}))
        pet.refresh_from_db()
        self.pet = pet
        # 前置事实：签收方与动物区县**不同** —— 后面所有断言的区分度都来自这里
        self.assertEqual(pet.shelter_id, self.shelter_a.id)
        self.assertEqual(pet.district_id, self.district_b.id)
        self.assertEqual(pet.status, 'pending_release')

    def _codes(self, user, url, key='code'):
        self.login_as(user)
        body = self.ok(self.get_json(url))
        return [r.get(key) for r in body['data']]

    def test_signing_shelter_lists_the_pet_it_received(self):
        """放归备选框的数据源（`/pets/`）必须包含本站签收的动物。"""
        codes = self._codes(self.shelter_user_a,
                            '/api/business/pets/?status=pending_release')
        self.assertIn(self.pet.code, codes,
                      '签收方看不到自己签收的动物 = 界面「暂无可放归动物」')

    def test_signing_shelter_pet_archive_lists_the_pet(self):
        """同一只动物在「全量台账 → 一宠一档」里也必须在（两个页面不能互相打脸）。"""
        # 台账行的编号字段是 `ledger_no`（=`Pet.code`），与 `/pets/` 的 `code` 不同名
        codes = self._codes(self.shelter_user_a, '/api/business/pets/archive/',
                            key='ledger_no')
        self.assertIn(self.pet.code, codes)

    def test_signing_shelter_can_create_transfer_and_confirm_release(self):
        """发起放归 → 转运回小区 → 小区确认，三步都要由**签收方**办得动。"""
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}create/', {'pet_id': self.pet.id}))
        release = Release.objects.get(id=body['data']['id'])
        assert_doc_no(self, release.ledger_no, 'REL')
        self.assertEqual(release.community_name, '乙区某小区',
                         '放归还回原捕捉小区，小区文本取捕捉单登记值')
        self.assertEqual(release.district_id, self.district_b.id,
                         '单据区县跟着动物的捕捉地（不是签收站）')
        self.assertEqual(release.institution_id, self.shelter_a.id)

        # 列表口径：开完单必须查得到，否则界面上那一行不出现、点不到「确认放归」
        self.login_as(self.shelter_user_a)
        listed = self.ok(self.get_json(URL))['data']
        self.assertIn(release.id, [r['id'] for r in listed])

        self.ok(self.post_json(f'{URL}{release.id}/transfer/', {}))
        release.refresh_from_db()
        self.assertEqual(release.status, 'transferred')
        self.ok(self.post_json(f'{URL}{release.id}/confirm/', {
            'receiver_name': '张物业', 'signature': 'data:image/png;base64,SIG'}))
        release.refresh_from_db()
        self.assertEqual(release.status, 'released')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'released')
        self.assertEqual(self.pet.district_id, self.district_b.id,
                         '办完放归也不改动物区县（捕捉地口径）')

    def test_pet_list_carries_capture_community(self):
        """动物列表必须随带捕捉单登记的**小区名**。

        界面「放归小区」承诺「默认带出捕捉单登记的小区，可修改」，而它原先只从
        **本站**的 `/captures/` 里找 —— 捕捉单按机构收敛，跨区县运回签收进来的
        动物，它的捕捉单在别的站（实测：市级站捕捉、樊城区，签收方是襄城站），
        于是带不出来、界面拦在「请填写放归小区」，流程看着像坏了，
        而服务端其实知道答案（`release_create` 会取捕捉单登记值）。
        """
        self.login_as(self.shelter_user_a)
        rows = self.ok(self.get_json('/api/business/pets/'))['data']
        row = next(r for r in rows if r['code'] == self.pet.code)
        self.assertEqual(row.get('captureCommunity') or row.get('capture_community'),
                         '乙区某小区',
                         '小区名要随动物下发，不能让前端去查别机构的捕捉单')

    def test_third_district_shelter_still_cannot_reach_it(self):
        """放开的是「挂本站名下」，不是「全市捕捉点都能办」——丙区站三处都要拒。

        没有这条反向对照，把 `shelter_scope` 写成「一律不按区县收敛」也能让
        上面三条用例全绿，而跨机构越权（读别的站正在放归的动物、甚至替它签字）
        就静默上线了。
        """
        district_c = make_district(name='丙区', code='TC')
        shelter_c = make_institution(type='shelter', district=district_c,
                                     name='丙区捕捉点')
        user_c = make_user('shelter_c_t', role='shelter',
                           district=district_c, institution=shelter_c)
        release = Release.objects.create(
            pet=self.pet, pet_code=self.pet.code,
            community_name='乙区某小区', status='pending',
            institution=self.shelter_a, district=self.district_b,
            ledger_no='REL-XD-C')

        self.assertNotIn(self.pet.code,
                         self._codes(user_c, '/api/business/pets/?status=pending_release'))
        self.assertNotIn(self.pet.code,
                         self._codes(user_c, '/api/business/pets/archive/',
                                     key='ledger_no'))
        self.login_as(user_c)
        self.assertNotIn(release.id,
                         [r['id'] for r in self.ok(self.get_json(URL))['data']],
                         '丙区捕捉点不应看到别站开出的放归单')
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': self.pet.id}),
                         status=404, message='无权访问')
        self.expect_fail(self.post_json(f'{URL}{release.id}/transfer/', {}),
                         status=404, message='无权访问')
        self.expect_fail(self.post_json(f'{URL}{release.id}/confirm/', {}),
                         status=404, message='无权访问')
        release.refresh_from_db()
        self.assertEqual(release.status, 'pending', '越权请求不得改变单据状态')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_release', '越权请求不得改变动物状态')
