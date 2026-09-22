"""multipart 上传不受 `DATA_UPLOAD_MAX_MEMORY_SIZE` 限制（第二十六轮）。

**线上缺陷**：微信内置浏览器（Android / MicroMessenger 8.0.78）在手机端提交
「新增捕捉登记」时，用户看到的是「**点提交没反应**」。nginx 访问日志显示：

    POST /api/business/captures/create/ HTTP/1.1" 400 143     ← 连续 8 次

gunicorn 日志给出了确切原因：

    File "/opt/tnr/business/views_capture.py", line 410, in capture_create
        data = parse_json_body(request)
    File "/opt/tnr/business/services.py", line 46, in parse_json_body
        data = json.loads(request.body)
    File ".../django/http/request.py", line 338, in body
        raise RequestDataTooBig(
    django.core.exceptions.RequestDataTooBig: Request body exceeded
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE.

根因：`parse_json_body()` **无条件**读 `request.body`，而 `request.body` 会把
整个请求体读进内存并按 `CONTENT_LENGTH` 校验 `DATA_UPLOAD_MAX_MEMORY_SIZE`
（Django 默认 2.5MB）。手机端上传的是 `capture="environment"` 拍的**原图**，
单张 3~5MB 很常见 —— 必然超限。而 `RequestDataTooBig` 是 `SuspiciousOperation`
的子类，不在 `parse_json_body` 原来的 `except (JSONDecodeError, ValueError,
TypeError)` 里，于是冒泡成裸 400。

**为什么本地/桌面测试没发现**：不带照片时请求体只有几 KB；桌面选的文件通常
也小。只有「手机原相机 + 真机上传」才会稳定触发。

修法：`multipart/form-data` 一律不读 `request.body`。multipart 的有用数据在
`request.POST` / `request.FILES` 里，它们**流式**解析，且按官方定义
`DATA_UPLOAD_MAX_MEMORY_SIZE` 是「**不含文件上传部分**」的 ——
`MultiPartParser` 只对非文件字段累加 `num_bytes_read`
（`django/http/multipartparser.py:220-249`），所以大图不会触发该限制。

本文件把三层都钉住：
  1. **机制**：直接读 `request.body` 确实会抛 `RequestDataTooBig`（回归防线）；
  2. **单元**：`parse_json_body` 对 multipart 不读 body、返回 `{}`；
  3. **端到端**：带大图提交捕捉登记 / 编辑领养信息都必须成功。
"""
import io
import random
from unittest import mock

from django.core.exceptions import RequestDataTooBig
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, override_settings

from business.models import Capture, Pet
from business.services import parse_json_body
from business.tests.base import BusinessTestBase, make_pet

CAPTURE_CREATE_URL = '/api/business/captures/create/'

# 阈值压到 64KB：让用例跑得快，同时保持「文件远大于阈值」这个关键关系
SMALL_LIMIT = 64 * 1024


def make_big_image(name='big.png', min_bytes=200 * 1024):
    """造一张**体积明显超过阈值**的真图。

    用随机噪声而不是纯色：纯色 PNG 压缩后只有几百字节，根本超不过阈值，
    用例会退化成「其实没触发那条路径」的假绿。噪声图压缩率低，体积稳定。
    """
    from PIL import Image

    side = 300
    img = Image.new('RGB', (side, side))
    px = img.load()
    rnd = random.Random(20260922)          # 固定种子：体积可复现
    for y in range(side):
        for x in range(side):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    data = buf.getvalue()
    assert len(data) >= min_bytes, f'夹具图只有 {len(data)} 字节，不足以触发该路径'
    return SimpleUploadedFile(name, data, content_type='image/png')


class RequestDataTooBigMechanismTest(BusinessTestBase):
    """机制层：证明「直接读 request.body」就是崩溃点。"""

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024)
    def test_reading_body_of_large_multipart_raises(self):
        """`request.body` 按 CONTENT_LENGTH 判定，超限即抛。

        这条测试是**回归防线**：一旦 Django 改了这里的语义，它会立刻失败，
        提醒我们重新评估 `parse_json_body` 的修法是否还成立。
        """
        req = RequestFactory().generic(
            'POST', '/api/business/captures/create/',
            data=b'x' * 8192, content_type='multipart/form-data; boundary=xyz')
        with self.assertRaises(RequestDataTooBig):
            _ = req.body

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024)
    def test_post_fields_are_not_subject_to_the_same_limit(self):
        """同样超限的 multipart，`request.POST` 却能正常解析。

        这正是修法成立的前提：非文件字段只有几十字节，
        `MultiPartParser` 只累加**非文件**部分。
        """
        from django.test.client import encode_multipart

        body = encode_multipart('xyz', {'property_name': '金地物业'})
        req = RequestFactory().generic(
            'POST', '/api/business/captures/create/',
            data=body, content_type='multipart/form-data; boundary=xyz')
        self.assertEqual(req.POST.get('property_name'), '金地物业')


class ParseJsonBodyMultipartTest(BusinessTestBase):
    """单元层：`parse_json_body` 对 multipart 不读 body。"""

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024)
    def test_multipart_returns_empty_without_reading_body(self):
        """超大 multipart 必须返回 `{}` 而不是抛 `RequestDataTooBig`。

        修复前这里会直接抛异常 —— 这就是线上 400 的来源。
        """
        req = RequestFactory().generic(
            'POST', '/api/business/captures/create/',
            data=b'x' * 8192, content_type='multipart/form-data; boundary=xyz')
        self.assertEqual(parse_json_body(req), {})

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024)
    def test_multipart_sets_empty_audit_payload(self):
        """审计载荷要显式置空（而不是残留未设置的属性）。

        审计中间件读 `request.audit_payload`；保持与修复前一致的 `{}`，
        归属区县仍由响应体里的 `capture.districtId` 推导，口径不变。
        """
        req = RequestFactory().generic(
            'POST', '/api/x/', data=b'x' * 8192,
            content_type='multipart/form-data; boundary=xyz')
        parse_json_body(req)
        self.assertEqual(req.audit_payload, {})

    def test_multipart_content_type_with_charset_is_also_skipped(self):
        req = RequestFactory().generic(
            'POST', '/api/x/', data=b'x' * 16,
            content_type='Multipart/Form-Data; boundary=xyz; charset=utf-8')
        self.assertEqual(parse_json_body(req), {})

    def test_json_body_still_parsed(self):
        """正向对照：JSON 请求必须照旧解析（否则整片接口全废）。"""
        req = RequestFactory().post(
            '/api/x/', data='{"district_id": 7}', content_type='application/json')
        self.assertEqual(parse_json_body(req), {'district_id': 7})
        self.assertEqual(req.audit_payload, {'district_id': 7})

    def test_invalid_json_still_returns_empty(self):
        """非 JSON 体（如 form-urlencoded）仍回退空字典，行为不变。"""
        req = RequestFactory().post(
            '/api/x/', data='a=1&b=2', content_type='application/x-www-form-urlencoded')
        self.assertEqual(parse_json_body(req), {})

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024)
    def test_oversized_json_body_degrades_to_readable_error(self):
        """超大 **JSON** 体也不该炸成裸 400。

        这条走的是 `except RequestDataTooBig` 兜底：返回 `{}`，由视图给出
        可读的字段校验错误（而不是 Django 那句英文的 body exceeded）。
        """
        req = RequestFactory().post(
            '/api/x/', data=b'{"a": "' + b'x' * 8192 + b'"}',
            content_type='application/json')
        self.assertEqual(parse_json_body(req), {})


@override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=SMALL_LIMIT)
class LargeUploadEndToEndTest(BusinessTestBase):
    """端到端：带大图（远超阈值）提交必须成功。"""

    def _capture_payload(self, **overrides):
        payload = {
            'district_id': str(self.district_a.id),
            'shelter_id': str(self.shelter_a.id),
            'community_name': '测试小区',
            'property_name': '金地物业',
            'contact_person': '王经理',
            'contact_phone': '13800001111',
            'pet_count': '1',
            'pet_codes': 'TNR260922900',
            'signature': 'data:image/png;base64,iVBORw0KGgo=',
            'pet_species_TNR260922900': '猫',
            'group_photo': make_big_image('group.png'),
        }
        payload.update(overrides)
        return payload

    def test_capture_create_with_large_photo_succeeds(self):
        """手机原图（> 阈值）提交捕捉登记：必须 200 且真的落库。

        修复前这里是 `RequestDataTooBig` → 400（143 字节），
        界面上表现为「点提交没反应」。
        """
        self.login_as(self.shelter_user_a)
        resp = self.client.post(CAPTURE_CREATE_URL, self._capture_payload())

        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body.get('success'), body)
        # 不能只是"返回成功"——必须真的落库，且字段没丢
        capture = Capture.objects.get(ledger_no=body['data']['capture']['ledgerNo'])
        self.assertEqual(capture.district_id, self.district_a.id)
        self.assertEqual(capture.shelter_id, self.shelter_a.id)
        self.assertEqual(capture.community_name, '测试小区')
        self.assertEqual(capture.property_name, '金地物业')
        self.assertEqual(capture.pet_count, 1)
        self.assertTrue(capture.group_photo, '合照应当被保存')
        self.assertEqual(
            Pet.objects.filter(capture=capture, is_deleted=False).count(), 1)

    def test_capture_create_never_reports_body_too_big(self):
        """错误文案里绝不能出现「body exceeded」这种 Django 内部措辞。"""
        self.login_as(self.shelter_user_a)
        resp = self.client.post(CAPTURE_CREATE_URL, self._capture_payload())
        text = resp.content.decode('utf-8', 'replace')
        self.assertNotIn('exceeded', text.lower())
        self.assertNotIn('DATA_UPLOAD_MAX_MEMORY_SIZE', text)

    def test_adoption_info_edit_with_large_photo_succeeds(self):
        """另一个 multipart 接口（领养上架信息 + 照片）同样不能 400。

        字段名用视图真正读取的 `photo_before`（`views_adoption.py:121-126`），
        并断言照片**确实存下来了** —— 用错字段名的话文件会被忽略，
        用例就成了"假绿"（请求体里照样有这个文件，机制上仍能触发，
        但断言不到业务结果）。
        """
        pet = make_pet(code='TESTPETBIG1', status='in_treatment',
                       district=self.district_a, hospital=self.hospital_a)
        self.login_as(self.hospital_user_a)
        resp = self.client.post(
            f'/api/business/adoptions/{pet.id}/edit-info/',
            {
                'intro': '性格亲人，已绝育免疫',
                'personality': '活泼亲人',
                'body_condition': '健康良好',
                'flow_doc': '提交申请 → 审核 → 签署协议 → 确认领出',
                'is_active': 'true',
                'photo_before': make_big_image('listing.png'),
            })
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json().get('success'), resp.content)
        pet.refresh_from_db()
        self.assertTrue(pet.photo_before, '捕捉前照片应当被保存')

    def test_small_multipart_still_works(self):
        """正向对照：不带大图时行为不变（防止修法把整条路径改坏）。"""
        self.login_as(self.shelter_user_a)
        payload = self._capture_payload(group_photo='')
        resp = self.client.post(CAPTURE_CREATE_URL, payload)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json().get('success'), resp.content)

    def test_json_capture_create_still_works(self):
        """正向对照：JSON 提交（不带文件）路径不受影响。"""
        self.login_as(self.shelter_user_a)
        payload = self._capture_payload()
        payload.pop('group_photo')
        payload['pet_codes'] = ['TNR260922901']
        resp = self.post_json(CAPTURE_CREATE_URL, payload)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json().get('success'), resp.content)


class AuditDistrictSurvivesMultipartTest(BusinessTestBase):
    """审计归属区县不能因为「multipart 不设 audit_payload」而丢失。

    `resolve_district()` 的顺序是 `obj.district` → `request.audit_district`
    → `sniff_district_id(response_data, request_data)` → 操作员区县。
    `capture_create` 不设 `audit_district`，所以归属取自**响应体**里的
    `capture.districtId`。这条用例确认改法没有动到这个口径。
    """

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=SMALL_LIMIT)
    def test_capture_create_response_carries_district_id(self):
        self.login_as(self.shelter_user_a)
        resp = self.client.post(CAPTURE_CREATE_URL, {
            'district_id': str(self.district_a.id),
            'shelter_id': str(self.shelter_a.id),
            'community_name': '测试小区',
            'property_name': '金地物业',
            'contact_person': '王经理',
            'contact_phone': '13800001111',
            'pet_count': '1',
            'pet_codes': 'TNR260922902',
            'pet_species_TNR260922902': '猫',
            'signature': 'data:image/png;base64,iVBORw0KGgo=',
            'group_photo': make_big_image('group2.png'),
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        capture = resp.json()['data']['capture']
        self.assertEqual(capture.get('districtId'), self.district_a.id)
