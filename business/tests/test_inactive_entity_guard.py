"""第十八轮：实体「停用」状态的有效性。

背景：`Institution.status` 在整个后端**没有任何读取点** —— 只有
`institution_create` 写 `status='active'` 和 `institution_toggle_status`
翻转这两处**写入**，业务侧一次都没读过。后果是「停用」只是个徽标：

- 停用一家医院后，新建转运单的医院下拉照旧列出它、`transfer_create`
  照旧接受，动物被「送」到一家已经不运营的医院；
- 停用一个捕捉点后，照旧能对它登记新的捕捉单；
- `institution_toggle_status` 也**不联动其下账号**（操作员照旧能登录）。

同族还有一处：停用区县仍可作为捕捉单的归属区县 —— `resolve_district_scope()`
只校验 `is_city`、不看 `status`，而捕捉点端那个下拉只过滤了 `!isCity`。
（`_inactive_district_error()` 的注释曾声称「前端所有区县下拉都写了
`d.status === 'active'`」，实测不成立。）

设计要点（沿用第十七轮的约定）：
- 判据提成**共用函数**（`services.inactive_institution_error` /
  `inactive_district_error`），创建与编辑不各写一份；
- 只拦**新引用**，不拦存量 —— 停用医院上已下发的转运单仍要能签收，
  停在停用捕捉点/停用区县下的历史捕捉单仍要能改名、能转运；
- 拦截必须让**连带写入**也不发生：转运单没落库，宠物的 `hospital`
  也不能被改走（否则动物卡在一家停用医院上，既回不到捕捉点的可转运池，
  也没人能签收它）。
"""
from business.models import Capture, Pet, Transfer
from business.tests.base import (
    BusinessTestBase, make_capture, make_district, make_institution, make_pet,
    make_user,
)

CAPTURES_URL = '/api/business/captures/'
TRANSFERS_URL = '/api/business/transfers/'


class InactiveShelterGuardTest(BusinessTestBase):
    """停用的捕捉点不能再登记新的捕捉单。"""

    def setUp(self):
        # 夹具必须**每例新建**：`setUpTestData` 里的对象是类级共享的，
        # 就地改 `status` 会把内存里的属性带进下一个用例 ——
        # 数据库会回滚，对象属性不会。
        self.inactive_shelter = make_institution(
            type='shelter', district=self.district_a, name='停用捕捉点',
            status='inactive')
        self.inactive_shelter_user = make_user(
            'inactive_shelter_t', role='shelter',
            district=self.district_a, institution=self.inactive_shelter)

    def _payload(self, **kw):
        payload = {
            'shelter_id': self.inactive_shelter.id,
            'property_name': '阳光物业',
            'community_name': '甲区小区',
            'address': '幸福路1号',
            'contact_person': '物业张三',
            'contact_phone': '13800001234',
            'pet_count': 2,
            'species': '猫',
        }
        payload.update(kw)
        return payload

    def test_create_capture_at_inactive_shelter_rejected(self):
        self.login_as(self.inactive_shelter_user)
        before = Capture.objects.count()
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/', self._payload()),
            message='已停用')
        self.assertEqual(Capture.objects.count(), before,
                         '停用捕捉点仍落库了捕捉单')
        self.assertEqual(
            Pet.objects.filter(shelter=self.inactive_shelter).count(), 0,
            '捕捉单被拒了，但宠物档案已经生成 —— 连带写入必须一起不发生')

    def test_create_capture_at_active_shelter_allowed(self):
        """正向对照：启用状态不受影响。"""
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{CAPTURES_URL}create/', self._payload(
            shelter_id=self.shelter_a.id,
            community_id=self.community_a.id,
            community_name=self.community_a.name)))

    def test_create_capture_at_inactive_shelter_without_shelter_id_rejected(self):
        """不传 `shelter_id` 时回退到账号绑定的机构，同样要被拦住。"""
        self.login_as(self.inactive_shelter_user)
        before = Capture.objects.count()
        payload = self._payload()
        payload.pop('shelter_id')
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/', payload), message='已停用')
        self.assertEqual(Capture.objects.count(), before)

    def test_transfer_from_inactive_shelter_rejected(self):
        """停用的捕捉点也不能再**发起**转运（与创建捕捉单是同一条判据）。"""
        pets = [make_pet(district=self.district_a, shelter=self.inactive_shelter)]
        self.login_as(self.inactive_shelter_user)
        before = Transfer.objects.count()
        self.expect_fail(self.post_json(f'{TRANSFERS_URL}create/', {
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [p.code for p in pets],
            'pet_count': 1,
        }), message='已停用')
        self.assertEqual(Transfer.objects.count(), before)
        pets[0].refresh_from_db()
        self.assertIsNone(pets[0].hospital)

    def test_history_capture_at_inactive_shelter_still_editable(self):
        """正向对照（存量）：停在停用捕捉点下的历史捕捉单仍要能改名。

        只拦创建、不拦编辑 —— 否则一次停用就把历史业务全锁死，
        只能靠改库。
        """
        capture = make_capture(district=self.district_a,
                               shelter=self.inactive_shelter)
        self.login_as(self.inactive_shelter_user)
        self.ok(self.post_json(
            f'{CAPTURES_URL}{capture.id}/update/', {'property_name': '改名后物业'}))
        capture.refresh_from_db()
        self.assertEqual(capture.property_name, '改名后物业')


class InactiveHospitalGuardTest(BusinessTestBase):
    """停用的医院不能再接收新的转运单。"""

    def setUp(self):
        self.inactive_hospital = make_institution(
            type='hospital', district=self.district_a, name='停用医院',
            status='inactive')
        self.active_hospital = make_institution(
            type='hospital', district=self.district_a, name='正常医院')
        self.active_hospital_user = make_user(
            'active_hosp_t', role='hospital',
            district=self.district_a, institution=self.active_hospital)

    def _pets(self, count=2):
        return [make_pet(district=self.district_a, shelter=self.shelter_a)
                for _ in range(count)]

    def test_transfer_to_inactive_hospital_rejected(self):
        pets = self._pets()
        self.login_as(self.shelter_user_a)
        before = Transfer.objects.count()
        self.expect_fail(self.post_json(f'{TRANSFERS_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.inactive_hospital.id,
            'pet_codes': [p.code for p in pets],
            'pet_count': 2,
        }), message='已停用')
        self.assertEqual(Transfer.objects.count(), before,
                         '停用医院仍收到了转运单')
        # 连带写入也必须没发生
        for pet in pets:
            pet.refresh_from_db()
            self.assertIsNone(
                pet.hospital,
                '转运单被拒了，宠物的 hospital 却被改成了停用医院 ——\n'
                '动物会卡在一家没人运营的医院上，既回不到可转运池也没人能签收')

    def test_transfer_to_active_hospital_allowed(self):
        """正向对照。"""
        pets = self._pets()
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFERS_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.active_hospital.id,
            'pet_codes': [p.code for p in pets],
            'pet_count': 2,
        }))
        self.assertEqual(len(body['data']), 1)

    def test_split_transfer_with_one_inactive_hospital_rejected_wholesale(self):
        """拆分下发：只要有一家停用医院，**整单**拒绝，不能留下半截转运单。

        `transfer_create` 是边校验边落库的（循环里 create + 改宠物归属），
        所以这条判据必须前置到写库之前，并且覆盖**所有** item。
        """
        pets = self._pets(3)
        self.login_as(self.shelter_user_a)
        before = Transfer.objects.count()
        self.expect_fail(self.post_json(f'{TRANSFERS_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'items': [
                {'hospital_id': self.active_hospital.id,
                 'pet_codes': [pets[0].code, pets[1].code]},
                {'hospital_id': self.inactive_hospital.id,
                 'pet_codes': [pets[2].code]},
            ],
        }), message='已停用')
        self.assertEqual(
            Transfer.objects.count(), before,
            '前一段（正常医院）已经落库了 —— 停用医院的校验必须全部前置，\n'
            '否则一次请求会留下半截转运单')
        for pet in pets:
            pet.refresh_from_db()
            self.assertIsNone(pet.hospital)

    def test_pending_transfer_to_now_inactive_hospital_still_receivable(self):
        """正向对照（存量）：停用**之前**已下发的转运单，医院仍要能签收。

        停用只拦新引用。已经躺在医院门口的动物不能被一纸停用变成孤儿。
        """
        pets = self._pets(1)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFERS_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.active_hospital.id,
            'pet_codes': [pets[0].code],
            'pet_count': 1,
        }))
        transfer_id = body['data'][0]['id']

        # 下发之后医院才被停用
        self.active_hospital.status = 'inactive'
        self.active_hospital.save(update_fields=['status'])

        self.login_as(self.active_hospital_user)
        self.ok(self.post_json(f'{TRANSFERS_URL}{transfer_id}/receive/'))
        transfer = Transfer.objects.get(id=transfer_id)
        self.assertEqual(transfer.status, 'received')


class InactiveDistrictOnCaptureTest(BusinessTestBase):
    """停用的区县不能再作为新捕捉单的归属区县。"""

    def setUp(self):
        self.inactive_district = make_district(name='停用区', status='inactive')
        # 挂在「全市（市级）」的捕捉点：归属区县完全由操作员指定，
        # 所以能单独测到「目标区县已停用」这条判据 —— 否则会先撞上
        # 「捕捉点 ↔ 归属区县不一致」那条。
        self.city_shelter = make_institution(
            type='shelter', district=self.city, name='市级捕捉点')

    def _payload(self, **kw):
        payload = {
            'shelter_id': self.city_shelter.id,
            'property_name': '阳光物业',
            'community_name': '甲区小区',
            'address': '幸福路1号',
            'contact_person': '物业张三',
            'contact_phone': '13800001234',
            'pet_count': 1,
            'species': '猫',
        }
        payload.update(kw)
        return payload

    def test_create_capture_into_inactive_district_rejected(self):
        """显式提交停用区县 → 拒绝（`resolve_district_scope` 只看 `is_city`）。"""
        self.login_as(self.gov_city)
        before = Capture.objects.count()
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/', self._payload(
                district_id=self.inactive_district.id)),
            message='所选区县已停用')
        self.assertEqual(Capture.objects.count(), before)

    def test_create_capture_into_active_district_allowed(self):
        """正向对照。"""
        self.login_as(self.gov_city)
        self.ok(self.post_json(f'{CAPTURES_URL}create/', self._payload(
            district_id=self.district_a.id)))

    def test_update_capture_into_inactive_district_rejected(self):
        """编辑换区县时目标区县也必须是启用状态。"""
        capture = make_capture(district=self.district_a, shelter=self.city_shelter)
        self.login_as(self.gov_city)
        self.expect_fail(self.post_json(
            f'{CAPTURES_URL}{capture.id}/update/',
            {'district_id': self.inactive_district.id}),
            message='所选区县已停用')
        capture.refresh_from_db()
        self.assertEqual(capture.district_id, self.district_a.id)

    def test_update_capture_without_district_change_allowed(self):
        """正向对照（存量）：原地改名不受区县状态影响。

        判据只放在「确实换区县」分支内 —— 否则停在停用区县里的历史捕捉单
        连名字都改不了。
        """
        capture = make_capture(district=self.district_a, shelter=self.shelter_a)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(
            f'{CAPTURES_URL}{capture.id}/update/', {'property_name': '改名后物业'}))
        capture.refresh_from_db()
        self.assertEqual(capture.property_name, '改名后物业')
