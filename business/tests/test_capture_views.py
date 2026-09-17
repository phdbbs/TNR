"""捕捉登记与主人领回视图测试。"""
import tempfile
from datetime import timezone as dt_timezone
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from business.models import Capture, OwnerReturn, Pet, Transfer
from business.tests.base import (
    BusinessTestBase, make_capture, make_district, make_image_file,
    make_institution, make_pet, make_user,
)
from core.models import Institution

CAPTURES_URL = '/api/business/captures/'


class CapturePermissionTest(BusinessTestBase):
    def test_anonymous_gets_401(self):
        resp = self.get_json(CAPTURES_URL)
        self.expect_fail(resp, status=401, message='请先登录')

    def test_adopter_gets_403(self):
        self.login_as(self.adopter)
        self.expect_fail(self.get_json(CAPTURES_URL), status=403, message='无权访问该接口')

    def test_hospital_cannot_list_captures(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.get_json(CAPTURES_URL), status=403)

    def test_hospital_can_view_capture_detail(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a, pet_codes=['X1'])
        pet = make_pet(capture=capture, district=self.district_a)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.get_json(f'{CAPTURES_URL}{capture.id}/'))
        self.assertEqual(len(body['data']['pets']), 1)
        self.assertEqual(body['data']['pets'][0]['id'], pet.id)


class CaptureCreateTest(BusinessTestBase):
    def _payload(self, **kw):
        payload = {
            'shelter_id': self.shelter_a.id,
            'property_name': '阳光物业',
            'community_id': self.community_a.id,
            'community_name': self.community_a.name,
            'address': '幸福路1号',
            'geo_address': '甲区幸福路1号',
            'latitude': 32.05,
            'longitude': 112.13,
            'contact_person': '物业张三',
            'contact_phone': '13800001234',
            'pet_count': 2,
            'species': '猫',
            'signature': 'data:image/png;base64,AAAA',
        }
        payload.update(kw)
        return payload

    def test_create_success(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', self._payload()))
        self.assertEqual(len(body['data']['pet_codes']), 2)
        self.assertIn('2 条宠物档案', body['message'])

        capture = Capture.objects.get(id=body['data']['capture']['id'])
        # 新建捕捉单尚未提交转运，状态应为「待转运」而非一律「已完成」
        self.assertEqual(capture.status, 'pending')
        self.assertTrue(capture.ledger_no.startswith('CAP-'))
        self.assertEqual(capture.shelter, self.shelter_a)
        self.assertEqual(capture.pet_count, 2)

        pets = Pet.objects.filter(code__in=body['data']['pet_codes'])
        self.assertEqual(pets.count(), 2)
        for pet in pets:
            self.assertEqual(pet.status, 'in_transit')
            self.assertEqual(pet.capture, capture)
            self.assertEqual(pet.shelter, self.shelter_a)
            self.assertEqual(pet.district, self.district_a)

    def test_defaults_from_operator_profile(self):
        self.login_as(self.shelter_user_a)
        payload = self._payload(pet_count=1)
        payload.pop('shelter_id')  # 不传捕捉点，应回退到账号绑定的机构
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', payload))
        capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.assertEqual(capture.district, self.district_a)
        self.assertEqual(capture.shelter, self.shelter_a)

    def test_required_fields_validated(self):
        self.login_as(self.shelter_user_a)
        for field, message in (('property_name', '物业名称不能为空'),
                               ('community_name', '所在小区不能为空'),
                               ('contact_person', '物业交接人不能为空'),
                               ('contact_phone', '联系电话不能为空')):
            self.expect_fail(
                self.post_json(f'{CAPTURES_URL}create/', self._payload(**{field: '  '})),
                message=message)

    def test_geo_address_becomes_default_address(self):
        """定位地址应作为默认地址落库（address 未手填时用 geo_address 兜底）。"""
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(
            f'{CAPTURES_URL}create/',
            self._payload(address='', geo_address='甲区幸福路1号阳光小区')))
        capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.assertEqual(capture.address, '甲区幸福路1号阳光小区')
        self.assertEqual(capture.geo_address, '甲区幸福路1号阳光小区')
        self.assertEqual(capture.latitude, 32.05)
        self.assertEqual(capture.longitude, 112.13)
        self.assertEqual(capture.signature, 'data:image/png;base64,AAAA')

    def test_manual_address_not_overridden(self):
        """用户手填的详细地址优先于定位地址。"""
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(
            f'{CAPTURES_URL}create/',
            self._payload(address='幸福路1号3栋202', geo_address='甲区幸福路1号')))
        capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.assertEqual(capture.address, '幸福路1号3栋202')
        self.assertEqual(capture.geo_address, '甲区幸福路1号')

    def test_pet_count_zero_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', self._payload(pet_count=0)),
                  message='动物数量必须大于0')

    def test_pet_count_over_100_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', self._payload(pet_count=101)),
                  message='单批捕捉数量不能超过100只')

    def test_unknown_shelter_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', self._payload(shelter_id=999999)),
                  message='捕捉点不存在')

    def test_operator_without_shelter_rejected(self):
        user = make_user(role='shelter', district=self.district_a)
        self.login_as(user)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', {'pet_count': 1}),
                  message='缺少捕捉点信息')

    def test_unknown_district_rejected(self):
        """提交不存在的区县要拒绝，不能落成 NULL 外键。"""
        self.login_as(self.shelter_user_a)
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/',
                           self._payload(district_id=999999)),
            message='归属区县不存在')

    def test_city_district_rejected(self):
        """捕捉单不能归属到「全市（市级）」——那会让本区县政府看不到本区数据。"""
        self.login_as(self.shelter_user_a)
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/',
                           self._payload(district_id=self.city.id)),
            message='归属区县不能是市级')

    def test_district_defaults_to_shelter_district(self):
        """未提交 district_id 时，区县取捕捉点所在区县，而不是操作员区县。

        现场两个捕捉点操作员都挂在「全市（市级）」下，若以操作员区县为准，
        登记出来的捕捉单会全部落到市级，本区县政府将看不到它们。
        """
        operator = make_user(role='shelter', district=self.city,
                             institution=self.shelter_a)
        self.login_as(operator)
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', self._payload()))
        capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.assertEqual(capture.district, self.district_a,
                         '应落到捕捉点所在的甲区，而不是操作员所属的市级区县')
        self.assertEqual(
            set(Pet.objects.filter(capture=capture).values_list('district_id', flat=True)),
            {self.district_a.id}, '宠物档案的区县应与捕捉单一致')

    def test_cross_district_create_rejected(self):
        """非市级操作员不能把捕捉单归属到别的区县。"""
        self.login_as(self.shelter_user_a)
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/',
                           self._payload(district_id=self.district_b.id)),
            message='无权将记录归属到其他区县')

    def test_city_level_operator_can_choose_district(self):
        """市级账号可以为任意具体区县登记捕捉单。"""
        self.login_as(self.gov_city)
        payload = self._payload(district_id=self.district_b.id,
                                shelter_id=self.shelter_b.id)
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', payload))
        self.assertEqual(Capture.objects.get(id=body['data']['capture']['id']).district,
                         self.district_b)

    def test_non_shelter_institution_rejected(self):
        user = make_user(role='shelter', district=self.district_a,
                         institution=self.hospital_a)
        self.login_as(user)
        self.expect_fail(self.post_json(f'{CAPTURES_URL}create/', {'pet_count': 1}),
                  message='捕捉点不存在')

    def test_broken_json_body_rejected(self):
        self.login_as(self.shelter_user_a)
        resp = self.client.post(f'{CAPTURES_URL}create/', data='not-json',
                                content_type='application/json')
        self.expect_fail(resp, message='动物数量必须大于0')

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_group_photo_upload(self):
        self.login_as(self.shelter_user_a)
        upload = make_image_file('photo.png')
        resp = self.client.post(
            f'{CAPTURES_URL}create/',
            data={**{k: str(v) for k, v in self._payload().items()}, 'group_photo': upload},
            format='multipart',
        )
        body = self.ok(resp)
        capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.assertTrue(capture.group_photo)


class CaptureListTest(BusinessTestBase):
    def test_district_scoped(self):
        make_capture(district=self.district_a, shelter=self.shelter_a)
        make_capture(district=self.district_b, shelter=self.shelter_b)
        self.login_as(self.shelter_user_a)
        self.assertEqual(len(self.ok(self.get_json(CAPTURES_URL))['data']), 1)

    def test_gov_city_sees_all(self):
        make_capture(district=self.district_a, shelter=self.shelter_a)
        make_capture(district=self.district_b, shelter=self.shelter_b)
        self.login_as(self.gov_city)
        self.assertEqual(len(self.ok(self.get_json(CAPTURES_URL))['data']), 2)

    def test_keyword_filter(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a,
                               ledger_no='CAP-KW-0001')
        make_capture(district=self.district_a, shelter=self.shelter_a)
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(CAPTURES_URL + '?keyword=KW'))['data']
        self.assertEqual([c['id'] for c in data], [capture.id])


class PetCodesPreviewTest(BusinessTestBase):
    def test_preview_returns_codes(self):
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(f'{CAPTURES_URL}codes-preview/?count=3'))['data']
        self.assertEqual(len(data), 3)
        for code in data:
            self.assertTrue(code.startswith('TNR'))

    def test_preview_invalid_count(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.get_json(f'{CAPTURES_URL}codes-preview/?count=0'),
                  message='数量必须大于0')
        self.expect_fail(self.get_json(f'{CAPTURES_URL}codes-preview/'),
                  message='数量必须大于0')


class OwnerReturnTest(BusinessTestBase):
    URL = f'{CAPTURES_URL}0/owner-return/'

    def _register(self, pet, **kw):
        self.login_as(self.shelter_user_a)
        payload = {'pet_id': pet.id, 'owner_name': '原主人', 'owner_phone': '13811112222',
                   'reason': '走失找回'}
        payload.update(kw)
        return self.post_json(self.URL, payload)

    def test_success(self):
        pet = make_pet(district=self.district_a)
        body = self.ok(self._register(pet))
        self.assertTrue(body['data']['ledger_no'].startswith('RET-'))
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'owner_returned')
        record = OwnerReturn.objects.get(id=body['data']['id'])
        self.assertEqual(record.pet, pet)
        self.assertEqual(record.owner_name, '原主人')

    def test_records_address_and_return_time(self):
        """回收单要求采集住址与回收时间，两者必须落库（此前被静默丢弃）。"""
        pet = make_pet(district=self.district_a)
        body = self.ok(self._register(pet,
                                      owner_address='甲区幸福路 12 号 3 单元 501',
                                      return_time='2026-09-16T14:30'))
        record = OwnerReturn.objects.get(id=body['data']['id'])
        self.assertEqual(record.owner_address, '甲区幸福路 12 号 3 单元 501')
        self.assertIsNotNone(record.return_time)
        self.assertEqual(record.return_time.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M'),
                         '2026-09-16T06:30')

    def test_return_time_accepts_iso_with_seconds(self):
        pet = make_pet(district=self.district_a)
        body = self.ok(self._register(pet, return_time='2026-09-16T14:30:45'))
        record = OwnerReturn.objects.get(id=body['data']['id'])
        self.assertIsNotNone(record.return_time)

    def test_return_time_blank_is_none(self):
        """时间可留空，不应因空串而报错。"""
        pet = make_pet(district=self.district_a)
        body = self.ok(self._register(pet, return_time=''))
        record = OwnerReturn.objects.get(id=body['data']['id'])
        self.assertIsNone(record.return_time)

    def test_return_time_garbage_is_none(self):
        """非法时间不应 500，按未填写处理。"""
        pet = make_pet(district=self.district_a)
        body = self.ok(self._register(pet, return_time='不是时间'))
        record = OwnerReturn.objects.get(id=body['data']['id'])
        self.assertIsNone(record.return_time)

    def test_return_time_is_aware(self):
        """时间入库必须带时区，否则会写进 naive datetime。"""
        pet = make_pet(district=self.district_a)
        body = self.ok(self._register(pet, return_time='2026-09-16T14:30'))
        record = OwnerReturn.objects.get(id=body['data']['id'])
        self.assertTrue(timezone.is_aware(record.return_time))

    def test_missing_owner_name(self):
        pet = make_pet(district=self.district_a)
        self.expect_fail(self._register(pet, owner_name='  '), message='主人姓名不能为空')

    def test_missing_pet_id(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, {'owner_name': 'x'}), message='缺少宠物ID')

    def test_unknown_pet(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, {'pet_id': 999999, 'owner_name': 'x'}),
                  status=404, message='宠物不存在')

    def test_cross_district_pet_404(self):
        """主人领回也需按区县范围校验。"""
        pet = make_pet(district=self.district_b, status='in_transit')
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.URL, {'pet_id': pet.id, 'owner_name': 'x'}),
                  status=404, message='无权访问')

    def test_transferred_pet_blocked(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a)
        self.expect_fail(self._register(pet), message='该宠物已提交转运单')

    def test_repeated_return_blocked(self):
        pet = make_pet(district=self.district_a, status='owner_returned')
        self.expect_fail(self._register(pet), message='不可重复登记')

    def test_wrong_status_blocked(self):
        pet = make_pet(district=self.district_a, status='adopted')
        self.expect_fail(self._register(pet), message='不可领回')

    def test_blacklist_blocks_return(self):
        from business.models import Blacklist
        Blacklist.objects.create(name='老赖', phone='13900009999', reason='弃养',
                                 district=self.district_a)
        pet = make_pet(district=self.district_a)
        self.expect_fail(self._register(pet, owner_phone='13900009999'),
                  message='该主人已在黑名单中')


class OwnerReturnListTest(BusinessTestBase):
    def test_list_and_keyword(self):
        pet = make_pet(district=self.district_a)
        record = OwnerReturn.objects.create(
            pet=pet, pet_code=pet.code, owner_name='王找找', owner_phone='13800007777',
            reason='找回', ledger_no='RET-KW-0001', district=self.district_a)
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json('/api/business/owner-returns/'))['data']
        self.assertEqual(len(data), 1)
        data = self.ok(self.get_json('/api/business/owner-returns/?keyword=王找找'))['data']
        self.assertEqual([r['id'] for r in data], [record.id])


class CaptureStatusTest(BusinessTestBase):
    """捕捉单状态必须按转运情况推导，不能一律「已完成」。"""

    def _new_capture(self, pet_count=2):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', {
            'property_name': '状态测试物业', 'community_name': '状态测试小区',
            'contact_person': '王五', 'contact_phone': '13800000000',
            'pet_count': pet_count,
        }))
        return Capture.objects.get(id=body['data']['capture']['id']), body['data']['pet_codes']

    def _transfer(self, codes):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json('/api/business/transfers/create/', {
            'to_hospital_id': self.hospital_a.id, 'pet_codes': codes,
        }))
        return body['data'][0]['id']

    def test_new_capture_is_pending(self):
        capture, _ = self._new_capture()
        self.assertEqual(capture.status, 'pending')
        self.assertEqual(capture.get_status_display(), '待转运')

    def test_partial_transfer_sets_partial(self):
        capture, codes = self._new_capture(pet_count=2)
        self._transfer(codes[:1])
        capture.refresh_from_db()
        self.assertEqual(capture.status, 'partial')

    def test_all_transferred_sets_completed(self):
        capture, codes = self._new_capture(pet_count=2)
        self._transfer(codes)
        capture.refresh_from_db()
        self.assertEqual(capture.status, 'completed')

    def test_rejected_transfer_rolls_back_to_pending(self):
        """医院全部退回后，捕捉单应回到「待转运」。"""
        capture, codes = self._new_capture(pet_count=2)
        transfer_id = self._transfer(codes)
        capture.refresh_from_db()
        self.assertEqual(capture.status, 'completed')

        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'/api/business/transfers/{transfer_id}/reject/',
                               {'reason': '笼位不足'}))
        capture.refresh_from_db()
        self.assertEqual(capture.status, 'pending')

        # 退回后宠物必须解除医院归属，真正回到「未转运」
        for pet in Pet.objects.filter(code__in=codes):
            self.assertIsNone(pet.hospital_id)
            self.assertEqual(pet.status, 'in_transit')

    def test_rejected_pet_can_be_owner_returned(self):
        """退回后的宠物应能办理主人领回（此前因残留医院归属被拦）。"""
        capture, codes = self._new_capture(pet_count=1)
        transfer_id = self._transfer(codes)
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'/api/business/transfers/{transfer_id}/reject/',
                               {'reason': '不符合收治条件'}))

        pet = Pet.objects.get(code=codes[0])
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{CAPTURES_URL}{capture.id}/owner-return/',
                               {'pet_id': pet.id, 'owner_name': '原主人',
                                'owner_phone': '13811110000', 'reason': '主人找回'}))


class CaptureUpdateTest(BusinessTestBase):
    """编辑捕捉记录：主要信息可改、小区为文本、已转运禁止修改。"""

    def setUp(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', {
            'property_name': '原物业', 'community_name': '原小区',
            'address': '原地址1号', 'geo_address': '甲区原地址1号',
            'latitude': 32.05, 'longitude': 112.13,
            'contact_person': '张三', 'contact_phone': '13800000001',
            'pet_count': 2, 'signature': 'data:image/png;base64,ORIG',
        }))
        self.capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.codes = body['data']['pet_codes']
        self.url = f'{CAPTURES_URL}{self.capture.id}/update/'

    def _transfer_all(self):
        self.ok(self.post_json('/api/business/transfers/create/', {
            'to_hospital_id': self.hospital_a.id, 'pet_codes': self.codes,
        }))

    def test_update_main_fields(self):
        body = self.ok(self.post_json(self.url, {
            'property_name': '新物业', 'community_name': '新小区（手填）',
            'address': '新地址88号', 'contact_person': '李四',
            'contact_phone': '13900000002',
        }))
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.property_name, '新物业')
        # 小区改为文本输入后直接落库，不再依赖 community 外键
        self.assertEqual(self.capture.community_name, '新小区（手填）')
        self.assertEqual(self.capture.address, '新地址88号')
        self.assertEqual(self.capture.contact_person, '李四')
        self.assertEqual(body['data']['communityName'], '新小区（手填）')

    def test_update_requires_required_fields(self):
        for field, message in (('property_name', '物业名称不能为空'),
                               ('community_name', '所在小区不能为空'),
                               ('contact_person', '物业交接人不能为空'),
                               ('contact_phone', '联系电话不能为空')):
            self.expect_fail(self.post_json(self.url, {field: '  '}), message=message)

    def test_update_does_not_change_pet_count(self):
        self.ok(self.post_json(self.url, {'pet_count': 99}))
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.pet_count, 2)

    def test_update_does_not_change_status_manually(self):
        """状态由转运情况推导，手工传值应被忽略。"""
        self.ok(self.post_json(self.url, {'status': 'completed'}))
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.status, 'pending')

    def test_blank_address_falls_back_to_geo_address(self):
        self.ok(self.post_json(self.url, {'address': ''}))
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.address, '甲区原地址1号')

    def test_transferred_capture_cannot_be_updated(self):
        self._transfer_all()
        self.expect_fail(self.post_json(self.url, {'property_name': '改名'}),
                         message='不可修改')
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.property_name, '原物业')

    def test_can_update_again_after_full_rejection(self):
        self._transfer_all()
        transfer = Transfer.objects.filter(capture=self.capture).first()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'/api/business/transfers/{transfer.id}/reject/',
                               {'reason': '退回'}))
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.url, {'property_name': '退回后可改'}))
        self.capture.refresh_from_db()
        self.assertEqual(self.capture.property_name, '退回后可改')

    def test_cross_district_update_404(self):
        self.login_as(self.shelter_user_b)
        self.expect_fail(self.post_json(self.url, {'property_name': 'x'}),
                         status=404, message='捕捉记录不存在')

    def test_get_method_rejected(self):
        self.expect_fail(self.get_json(self.url), status=405, message='仅支持 POST 请求')


class CaptureDeleteTest(BusinessTestBase):
    """删除：逻辑删除 + 已转运不可删 + 医院全部退回后可删。"""

    def setUp(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', {
            'property_name': '待删物业', 'community_name': '待删小区',
            'contact_person': '赵六', 'contact_phone': '13800000003',
            'pet_count': 2,
        }))
        self.capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.codes = body['data']['pet_codes']
        self.url = f'{CAPTURES_URL}{self.capture.id}/delete/'

    def _transfer_all(self):
        body = self.ok(self.post_json('/api/business/transfers/create/', {
            'to_hospital_id': self.hospital_a.id, 'pet_codes': self.codes,
        }))
        return body['data'][0]['id']

    def test_logical_delete_keeps_row(self):
        self.ok(self.post_json(self.url))
        self.capture.refresh_from_db()
        self.assertTrue(self.capture.is_deleted)
        self.assertIsNotNone(self.capture.deleted_at)
        self.assertEqual(self.capture.status, 'void')
        self.assertEqual(self.capture.get_status_display(), '已作废')
        # 物理记录仍在数据库中
        self.assertTrue(Capture.objects.filter(id=self.capture.id).exists())

    def test_delete_marks_pets_deleted(self):
        self.ok(self.post_json(self.url))
        for pet in Pet.objects.filter(code__in=self.codes):
            self.assertTrue(pet.is_deleted)
            self.assertIsNotNone(pet.deleted_at)
        # 宠物物理记录同样保留
        self.assertEqual(Pet.objects.filter(code__in=self.codes).count(), 2)

    def test_deleted_capture_hidden_from_list(self):
        self.ok(self.post_json(self.url))
        data = self.ok(self.get_json(CAPTURES_URL))['data']
        self.assertNotIn(self.capture.id, [c['id'] for c in data])

    def test_deleted_pets_hidden_from_hospital_list(self):
        self.ok(self.post_json(self.url))
        self.login_as(self.shelter_user_a)
        pets = self.ok(self.get_json('/api/business/pets/'))['data']
        self.assertFalse(set(self.codes) & {p['code'] for p in pets})

    def test_transferred_capture_cannot_be_deleted(self):
        self._transfer_all()
        self.expect_fail(self.post_json(self.url), message='不可删除')
        self.capture.refresh_from_db()
        self.assertFalse(self.capture.is_deleted)

    def test_can_delete_after_full_rejection(self):
        transfer_id = self._transfer_all()
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'/api/business/transfers/{transfer_id}/reject/',
                               {'reason': '退回后可删'}))
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.url))
        self.capture.refresh_from_db()
        self.assertTrue(self.capture.is_deleted)

    def test_duplicate_delete_rejected(self):
        self.ok(self.post_json(self.url))
        self.expect_fail(self.post_json(self.url), message='已删除')

    def test_deleted_capture_cannot_be_updated(self):
        self.ok(self.post_json(self.url))
        self.expect_fail(self.post_json(f'{CAPTURES_URL}{self.capture.id}/update/',
                                        {'property_name': 'x'}),
                         message='已删除')

    def test_cross_district_delete_404(self):
        self.login_as(self.shelter_user_b)
        self.expect_fail(self.post_json(self.url), status=404, message='捕捉记录不存在')

    def test_get_method_rejected(self):
        self.expect_fail(self.get_json(self.url), status=405, message='仅支持 POST 请求')


class CaptureDetailTest(BusinessTestBase):
    """详情必须覆盖新增录入表单的全部内容，并给出可否编辑/删除。"""

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_detail_contains_all_form_fields(self):
        self.login_as(self.shelter_user_a)
        upload = make_image_file('group.png')
        resp = self.client.post(f'{CAPTURES_URL}create/', data={
            'property_name': '详情物业', 'community_name': '详情小区',
            'address': '详情路9号', 'geo_address': '甲区详情路9号',
            'latitude': '32.06', 'longitude': '112.14',
            'contact_person': '孙七', 'contact_phone': '13800000004',
            'pet_count': '1', 'signature': 'data:image/png;base64,SIG',
            'group_photo': upload,
        }, format='multipart')
        body = self.ok(resp)
        capture_id = body['data']['capture']['id']
        code = body['data']['pet_codes'][0]

        detail = self.ok(self.get_json(f'{CAPTURES_URL}{capture_id}/'))['data']
        self.assertEqual(detail['property_name'], '详情物业')
        self.assertEqual(detail['community_name'], '详情小区')
        self.assertEqual(detail['address'], '详情路9号')
        self.assertEqual(detail['geo_address'], '甲区详情路9号')
        self.assertEqual(detail['latitude'], 32.06)
        self.assertEqual(detail['longitude'], 112.14)
        self.assertEqual(detail['contact_person'], '孙七')
        self.assertEqual(detail['contact_phone'], '13800000004')
        self.assertEqual(detail['signature'], 'data:image/png;base64,SIG')
        self.assertTrue(detail['group_photo'])
        self.assertEqual(detail['pet_count'], 1)
        self.assertEqual(detail['petCodes'], [code])
        self.assertEqual(len(detail['pets']), 1)
        self.assertEqual(detail['statusDisplay'], '待转运')
        self.assertTrue(detail['canEdit'])
        self.assertTrue(detail['canDelete'])

    def test_detail_flags_locked_after_transfer(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{CAPTURES_URL}create/', {
            'property_name': '锁定物业', 'community_name': '锁定小区',
            'contact_person': '周八', 'contact_phone': '13800000005', 'pet_count': 1,
        }))
        capture_id = body['data']['capture']['id']
        codes = body['data']['pet_codes']
        self.ok(self.post_json('/api/business/transfers/create/', {
            'to_hospital_id': self.hospital_a.id, 'pet_codes': codes,
        }))
        detail = self.ok(self.get_json(f'{CAPTURES_URL}{capture_id}/'))['data']
        self.assertFalse(detail['canEdit'])
        self.assertFalse(detail['canDelete'])
        self.assertEqual(detail['transferState']['transferred'], 1)


class CaptureListSearchTest(BusinessTestBase):
    """列表搜索：小区按文字模糊匹配，关键词覆盖地址/物业/联系人等。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cap1 = make_capture(district=cls.district_a, shelter=cls.shelter_a,
                                ledger_no='CAP-S1', community_name='阳光花园小区',
                                property_name='阳光物业', address='幸福路1号',
                                contact_person='张三', pet_codes=['TNR-S-1'])
        cls.cap2 = make_capture(district=cls.district_a, shelter=cls.shelter_a,
                                ledger_no='CAP-S2', community_name='月光新村',
                                property_name='月光物业', address='月光路2号',
                                contact_person='李四', pet_codes=['TNR-S-2'])

    def _ids(self, query):
        self.login_as(self.shelter_user_a)
        return [c['id'] for c in self.ok(self.get_json(CAPTURES_URL + query))['data']]

    def test_community_partial_text_match(self):
        self.assertEqual(self._ids('?community=阳光'), [self.cap1.id])

    def test_community_match_is_contains_not_exact(self):
        self.assertEqual(self._ids('?community=花园'), [self.cap1.id])

    def test_keyword_matches_address_and_contact(self):
        self.assertIn(self.cap1.id, self._ids('?keyword=幸福路'))
        self.assertIn(self.cap2.id, self._ids('?keyword=李四'))

    def test_keyword_no_duplicate_rows(self):
        """多字段 OR 命中同一行时不能返回重复记录。"""
        ids = self._ids('?keyword=阳光')
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [self.cap1.id])

    def test_list_includes_transfer_flags(self):
        self.login_as(self.shelter_user_a)
        row = [c for c in self.ok(self.get_json(CAPTURES_URL))['data']
               if c['id'] == self.cap1.id][0]
        self.assertIn('transferState', row)
        self.assertTrue(row['canEdit'])
        self.assertTrue(row['canDelete'])

    def test_list_excludes_signature_payload(self):
        """列表接口不应回传体积巨大的签字 base64。"""
        self.login_as(self.shelter_user_a)
        rows = self.ok(self.get_json(CAPTURES_URL))['data']
        self.assertTrue(rows)
        self.assertNotIn('signature', rows[0])


GEOCODE_IP_URL = '/api/business/geocode/ip/'


class GeocodeIpTest(BusinessTestBase):
    """IP 定位兜底接口：浏览器在 HTTP 非安全源下精确定位不可用时的降级。"""

    def test_anonymous_gets_401(self):
        self.expect_fail(self.get_json(GEOCODE_IP_URL), status=401, message='请先登录')

    def test_adopter_gets_403(self):
        self.login_as(self.adopter)
        self.expect_fail(self.get_json(GEOCODE_IP_URL), status=403, message='无权访问该接口')

    @mock.patch('business.views_capture.amap_regeo')
    @mock.patch('business.views_capture.amap_ip_location')
    def test_success_returns_city_precision(self, m_ip, m_regeo):
        m_ip.return_value = {
            'province': '浙江省', 'city': '杭州市', 'adcode': '330100',
            'latitude': 30.25, 'longitude': 119.75,
        }
        m_regeo.return_value = {
            'address': '浙江省杭州市余杭区', 'province': '浙江省',
            'city': '杭州市', 'district': '余杭区',
        }
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(GEOCODE_IP_URL))
        self.assertEqual(body['data']['precision'], 'city')
        self.assertEqual(body['data']['latitude'], 30.25)
        self.assertEqual(body['data']['longitude'], 119.75)
        self.assertEqual(body['data']['address'], '浙江省杭州市余杭区')
        self.assertEqual(body['data']['district'], '余杭区')
        m_regeo.assert_called_once_with(119.75, 30.25)

    @mock.patch('business.views_capture.amap_ip_location',
                side_effect=ValueError('IP定位服务返回错误：DAILY_QUERY_OVER_LIMIT'))
    def test_ip_location_fail_returns_400(self, m_ip):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.get_json(GEOCODE_IP_URL), status=400,
                         message='IP定位服务返回错误：DAILY_QUERY_OVER_LIMIT')

    @mock.patch('business.views_capture.amap_ip_location')
    @mock.patch('business.views_capture.amap_regeo',
                side_effect=ValueError('地图服务请求失败：超时'))
    def test_regeo_fail_still_returns_coordinates(self, m_regeo, m_ip):
        m_ip.return_value = {
            'province': '浙江省', 'city': '杭州市', 'adcode': '330100',
            'latitude': 30.25, 'longitude': 119.75,
        }
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(GEOCODE_IP_URL))
        self.assertEqual(body['data']['latitude'], 30.25)
        self.assertEqual(body['data']['longitude'], 119.75)
        self.assertEqual(body['data']['address'], '')
        self.assertEqual(body['data']['precision'], 'city')
