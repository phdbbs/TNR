"""上传图片必须**按内容**校验：非图片文件不得落库。

Django 的 ``ImageField`` 只在走 ModelForm 时才校验，**直接赋值不做任何检查**。
各上传接口此前都写成

    capture.group_photo = request.FILES['group_photo']

于是把 ``.txt``（或把任意文件改名成 ``.png``）原样写进 ``media/``：
前端 ``<img src>`` 直接裂图，数据库里看不出任何异常，事后无从追溯。

GUI 实测（Playwright 真实点击「上传整体合影」并选一个 ``.txt``）抓到：
提交返回成功、``Capture.group_photo`` 落库、媒体目录里多出一个非图片文件。
修法是 ``services.validate_image_upload()``（Pillow 实际解码，不信任文件名），
各接口在**写库之前**调用，非法时整批拒绝。
"""
import io
import time
from datetime import timezone as dt_timezone

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from business.models import Adoption, AdoptionHallListing, Capture, CheckIn, Pet
from business.services import validate_image_upload
from business.tests.base import BusinessTestBase, make_pet


def png_file(name='photo.png', w=8, h=8, color=(200, 120, 80)):
    """一张真正能被解码的 PNG。"""
    buf = io.BytesIO()
    Image.new('RGB', (w, h), color).save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


def text_file(name='fake.png'):
    """**扩展名伪装成 png** 的文本文件：按文件名/Content-Type 判断都会放行。"""
    return SimpleUploadedFile(name, b'this is definitely not an image',
                              content_type='image/png')


def codes(n, start=801):
    """格式合法的宠物编号（TNR + YYMMDD + 3 位序号）。"""
    ymd = timezone.localdate().strftime('%y%m%d')
    return [f'TNR{ymd}{start + i:03d}' for i in range(n)]


class ValidateImageUploadUnitTest(BusinessTestBase):
    """校验函数本身：认内容不认文件名。"""

    def test_accepts_real_png(self):
        self.assertIsNone(validate_image_upload(png_file(), '整体合影'))

    def test_rejects_text_named_as_png(self):
        err = validate_image_upload(text_file(), '整体合影')
        self.assertIsNotNone(err)
        self.assertIn('不是有效的图片文件', err)

    def test_rejects_oversize(self):
        f = png_file()
        f.size = 11 * 1024 * 1024          # 直接改属性模拟大文件
        err = validate_image_upload(f, '整体合影')
        self.assertIsNotNone(err)
        self.assertIn('超过', err)

    def test_none_is_ok(self):
        self.assertIsNone(validate_image_upload(None))

    def test_file_pointer_reset_after_check(self):
        """校验后指针必须复位，否则后续 save() 会把空内容写进磁盘。"""
        f = png_file()
        validate_image_upload(f)
        self.assertEqual(f.tell(), 0)


class CaptureUploadValidationTest(BusinessTestBase):
    """新增捕捉：整体合影与单只照片都要校验。"""

    def setUp(self):
        self.login_as(self.shelter_user_a)

    def _payload(self, **over):
        c = codes(2)
        data = {
            'shelter_id': self.shelter_a.id,
            'district_id': self.district_a.id,
            'property_name': '阳光物业',
            'community_name': '阳光花园小区',
            'contact_person': '张建国',
            'contact_phone': '13871234501',
            'pet_count': 2,
            'signature': 'data:image/png;base64,TESTSIGNATURE',
            'pet_codes': c,
            'pet_species_' + c[0]: '猫',
            'pet_gender_' + c[0]: '母',
            'pet_species_' + c[1]: '狗',
            'pet_gender_' + c[1]: '公',
        }
        data.update(over)
        return data

    def test_rejects_non_image_group_photo(self):
        c = codes(2)
        data = self._payload(group_photo=text_file('group.png'))
        data['pet_photo_' + c[0]] = png_file('one.png')
        data['pet_photo_' + c[1]] = png_file('two.png')
        resp = self.client.post('/api/business/captures/create/', data)
        self.expect_fail(resp, message='不是有效的图片文件')
        self.assertEqual(Capture.objects.count(), 0)
        self.assertEqual(Pet.objects.count(), 0)

    def test_rejects_non_image_pet_photo(self):
        c = codes(2)
        data = self._payload(group_photo=png_file('group.png'))
        data['pet_photo_' + c[0]] = text_file('one.png')
        data['pet_photo_' + c[1]] = png_file('two.png')
        resp = self.client.post('/api/business/captures/create/', data)
        self.expect_fail(resp, message='不是有效的图片文件')
        # 两段式：第 1 张非法时整批不落库，不留半成品
        self.assertEqual(Capture.objects.count(), 0)
        self.assertEqual(Pet.objects.count(), 0)

    def test_accepts_real_images(self):
        c = codes(2)
        data = self._payload(group_photo=png_file('group.png'))
        data['pet_photo_' + c[0]] = png_file('one.png')
        data['pet_photo_' + c[1]] = png_file('two.png')
        body = self.ok(self.client.post('/api/business/captures/create/', data))
        self.assertEqual(len(body['data']['pet_codes']), 2)
        cap = Capture.objects.get(ledger_no=body['data']['capture']['ledger_no'])
        self.assertTrue(cap.group_photo)
        pet = Pet.objects.get(code=c[0])
        self.assertTrue(pet.photo_capture)
        self.assertEqual(pet.species, '猫')
        self.assertEqual(pet.gender, '母')

    def test_missing_any_required_capture_material_rejected_without_writes(self):
        c = codes(2)
        base = self._payload(group_photo=png_file('group.png'))
        base['pet_photo_' + c[0]] = png_file('one.png')
        # 第二只单照缺失：服务端必须在 Capture/Pet 写入前拒绝。
        before_capture = Capture.objects.count()
        before_pet = Pet.objects.count()
        resp = self.client.post('/api/business/captures/create/', base)
        self.expect_fail(resp, message='单只照片')
        self.assertEqual(Capture.objects.count(), before_capture)
        self.assertEqual(Pet.objects.count(), before_pet)

        # 合影缺失也必须拒绝，不能只依赖页面上的红色星号。
        base = self._payload()
        base['pet_photo_' + c[0]] = png_file('one.png')
        base['pet_photo_' + c[1]] = png_file('two.png')
        resp = self.client.post('/api/business/captures/create/', base)
        self.expect_fail(resp, message='整体合影')
        self.assertEqual(Capture.objects.count(), before_capture)
        self.assertEqual(Pet.objects.count(), before_pet)

        # 签字缺失同样拒绝。
        base = self._payload(group_photo=png_file('group.png'))
        base.pop('signature', None)
        base['pet_photo_' + c[0]] = png_file('one.png')
        base['pet_photo_' + c[1]] = png_file('two.png')
        resp = self.client.post('/api/business/captures/create/', base)
        self.expect_fail(resp, message='电子签名')
        self.assertEqual(Capture.objects.count(), before_capture)
        self.assertEqual(Pet.objects.count(), before_pet)


class CaptureUpdateUploadValidationTest(BusinessTestBase):
    """编辑捕捉：换合影同样要校验。"""

    def test_rejects_non_image_group_photo(self):
        self.login_as(self.shelter_user_a)
        cap = Capture.objects.create(
            district=self.district_a, shelter=self.shelter_a,
            shelter_name=self.shelter_a.name, community_name='甲区小区',
            property_name='甲区物业', contact_person='张三', contact_phone='13800000000',
            pet_count=0, pet_codes='', ledger_no='CAP-IMG-001', status='pending',
        )
        resp = self.client.post(f'/api/business/captures/{cap.id}/update/',
                                {'group_photo': text_file('g.png')})
        self.expect_fail(resp, message='不是有效的图片文件')
        cap.refresh_from_db()
        self.assertFalse(cap.group_photo)


class CheckInUploadValidationTest(BusinessTestBase):
    """回访打卡照片：非法时连打卡记录都不该创建。"""

    def test_rejects_non_image_and_creates_nothing(self):
        pet = make_pet(code='TNRIMGCHK001', status='adopted', district=self.district_a)
        Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter=self.adopter,
            adopter_name='王领养', adopter_phone='13900000000',
            ledger_no='ADP-IMG-001', district=self.district_a, status='completed',
        )
        self.login_as(self.adopter)
        resp = self.client.post('/api/business/checkins/create/',
                                {'pet_id': pet.id, 'month': '2026-01',
                                 'photo': text_file('c.png')})
        self.expect_fail(resp, message='不是有效的图片文件')
        self.assertEqual(CheckIn.objects.count(), 0)


class AdoptionPhotoUploadValidationTest(BusinessTestBase):
    """医院领养资料照片：非法时不得写进宠物档案。"""

    def test_rejects_non_image(self):
        pet = make_pet(code='TNRIMGADP001', status='pending_adopt',
                       district=self.district_a, hospital=self.hospital_a)
        listing = AdoptionHallListing.objects.create(
            pet=pet, hospital=self.hospital_a,
            hospital_name=self.hospital_a.name, intro='原简介')
        self.login_as(self.hospital_user_a)
        resp = self.client.post(f'/api/business/adoptions/{pet.id}/edit-info/',
                                {'intro': '很亲人', 'photo_before': text_file('b.png')})
        self.expect_fail(resp, message='不是有效的图片文件')
        pet.refresh_from_db()
        self.assertFalse(pet.photo_before)
        # 照片非法时**整单都不该落库**：原实现先 save() 再校验照片，
        # 于是接口返回失败、界面弹红字，简介其实已经改掉了 —— 所见非所得。
        listing.refresh_from_db()
        self.assertEqual(listing.intro, '原简介')

    def test_accepts_real_image(self):
        pet = make_pet(code='TNRIMGADP002', status='pending_adopt',
                       district=self.district_a, hospital=self.hospital_a)
        AdoptionHallListing.objects.create(pet=pet, hospital=self.hospital_a,
                                           hospital_name=self.hospital_a.name)
        self.login_as(self.hospital_user_a)
        self.ok(self.client.post(f'/api/business/adoptions/{pet.id}/edit-info/',
                                 {'intro': '很亲人', 'photo_before': png_file('b.png')}))
        pet.refresh_from_db()
        self.assertTrue(pet.photo_before)
