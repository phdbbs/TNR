"""共享测试基座：最小化夹具工厂 + API 调用助手。

设计原则：
- 不依赖 seed_data，每个测试类自建所需最小数据，结果确定、运行快；
- 编号自动递增，保证跨用例唯一；
- 统一 JSON 请求与断言助手，减少重复样板。
"""
import json

from django.test import TestCase

from accounts.models import User
from business.models import Pet
from core.models import District, Institution

SEQ = {"district": 0, "institution": 0, "inst_code": 0, "user": 0, "pet": 0,
       "material": 0, "chip": 0, "capture": 0}


def make_image_file(name='photo.png', w=8, h=8, color=(200, 120, 80)):
    """返回一个**内容真实**的图片上传文件。

    不要用 `SimpleUploadedFile('x.png', b'fakeimage')`：上传接口现在会按**内容**
    校验（`services.validate_image_upload` 用 Pillow 实际解码，不信任扩展名），
    假字节会被正确拒绝，用例会以「不是有效的图片文件」失败。夹具必须给真图。
    """
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    buf = io.BytesIO()
    Image.new('RGB', (w, h), color).save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


def _next(key, fmt):
    SEQ[key] += 1
    return fmt.format(SEQ[key])


def make_district(name=None, code=None, is_city=False, status="active"):
    return District.objects.create(
        name=name or _next("district", "测试区{}"),
        code=code or _next("district", "T{:03d}"),
        is_city=is_city,
        status=status,
    )


def make_institution(type="shelter", district=None, name=None, status="active", **kw):
    """造一个机构。

    默认**带业务编号**：`Institution.code` 在模型上可空，但生产库里每个机构
    都有编号（seed_data 按 code 幂等匹配、`institution_create` 也会自动生成），
    「没有 code」本身是一种要被巡检报出来的异常状态。夹具若默认不写 code，
    就等于让每条用例都跑在一个**现实中不该存在**的数据形态上 ——
    `check_data_integrity` 的第一版回归测试正是因此把 5 个正常夹具机构
    全报成了「机构缺少业务编号」。
    编号用 `TI###` 前缀，与生产前缀 `I###`/`C###` 区分开，
    免得干扰 `_next_institution_code()` 的取号断言。
    """
    return Institution.objects.create(
        code=kw.pop("code", None) or _next("inst_code", "TI{:03d}"),
        name=name or _next("institution", {"shelter": "捕捉点{}", "hospital": "医院{}", "community": "小区{}"}[type]),
        type=type,
        district=district or make_district(),
        status=status,
        **kw,
    )


def make_user(username=None, role="shelter", district=None, institution=None,
              password="123456", **kw):
    return User.objects.create_user(
        username=username or _next("user", "u{:04d}"),
        password=password,
        role=role,
        district=district,
        institution=institution,
        **kw,
    )


def make_pet(code=None, status="in_transit", district=None, shelter=None,
             hospital=None, capture=None, species="猫", **kw):
    SEQ["pet"] += 1
    return Pet.objects.create(
        code=code or f"TESTPET{SEQ['pet']:05d}",
        status=status,
        district=district or make_district(),
        shelter=shelter,
        hospital=hospital,
        capture=capture,
        species=species,
        **kw,
    )


def make_capture(district=None, shelter=None, pet_codes=None, **kw):
    from business.models import Capture
    shelter = shelter or make_institution(type="shelter")
    return Capture.objects.create(
        district=district or shelter.district,
        shelter=shelter,
        shelter_name=shelter.name,
        pet_count=len(pet_codes or []),
        pet_codes=",".join(pet_codes or []),
        status="completed",
        ledger_no=kw.pop("ledger_no", None) or _next("capture", "CAP-T{:04d}"),
        **kw,
    )


def make_material(category="vaccine", name=None, district=None, shelter_stock=100, **kw):
    from business.models import Material
    return Material.objects.create(
        name=name or _next("material", "物料{}"),
        category=category,
        unit=kw.pop("unit", "支"),
        shelter_stock=shelter_stock,
        district=district or make_district(),
        **kw,
    )


def make_chip(number=None, status="available", pet=None):
    from business.models import Chip
    SEQ["chip"] += 1
    return Chip.objects.create(
        number=number or f"CHIP{SEQ['chip']:010d}",
        status=status,
        pet=pet,
    )


def make_adoption(adopter, district=None, pet=None, status="completed", **kw):
    """造一条领养记录。

    ⚠ **领养人的区县归属由领养记录表达，不由 `User.district` 表达。**
    领养人账号自身没有区县（注册流程不采集，生产实测 1/1 为空），
    凡是要「按区县找领养人」的地方都必须经由 `Adoption.district` 推导 ——
    见 `supervision.views._notice_recipients()`。
    夹具若图省事直接给 `make_user(role='adopter', district=...)`，
    就等于让用例跑在一个**生产上不存在**的数据形态上。
    """
    from business.models import Adoption
    pet = pet or make_pet(district=district or make_district())
    return Adoption.objects.create(
        pet=pet,
        pet_code=pet.code,
        adopter=adopter,
        adopter_name=kw.pop("adopter_name", adopter.username),
        status=status,
        district=district or pet.district,
        **kw,
    )


def make_hospital_txn(material, hospital, type="receive", quantity=10, **kw):
    """直接造一条医院侧流水（receive 增加医院库存，consume 减少）。"""
    from business.models import MaterialTransaction
    from django.utils import timezone
    return MaterialTransaction.objects.create(
        material=material,
        material_name=material.name,
        quantity=quantity,
        type=type,
        hospital=hospital,
        date=kw.pop("date", timezone.localdate()),
        district=kw.pop("district", material.district),
        **kw,
    )


class ApiMixin:
    """登录态 + JSON API 助手。"""

    def login_as(self, user):
        """强制登录（绕过登录视图，登录视图本身在 accounts 测试覆盖）。"""
        self.client.force_login(user)
        return user

    def post_json(self, url, payload=None):
        # 新增捕捉的真实提交是 multipart：签字、整体合影、每只单照均为必传。
        # 旧的业务单测大量使用 JSON 助手，这里把它们转换成同一条真实上传路径，
        # 避免测试继续跑在「没有任何照片也能成功」的不存在形态上。需要专门验证
        # 缺上传材料时，直接用 self.client.post()，不要通过这个兼容助手。
        if url.rstrip('/') == '/api/business/captures/create':
            payload = dict(payload or {})
            if not payload.pop('_skip_capture_media', False):
                from business.services import MAX_CAPTURE_BATCH, generate_pet_codes
                try:
                    count = int(payload.get('pet_count') or 0)
                except (TypeError, ValueError, OverflowError):
                    count = 0
                # ⚠ 只为**合法数量**补材料。参数契约类用例会故意提交 '9'*24 这种
                #   探测值（`test_param_contract.py`），夹具若跟着它去生成编号和
                #   图片，等于在测试进程里跑 10^24 次循环 —— 实测整包被 OOM
                #   SIGKILL，看起来像「测试挂了」，其实是夹具自己吃光了内存。
                #   这类用例要的就是服务端 400，走原来的 JSON 路径即可。
                if 0 < count <= MAX_CAPTURE_BATCH:
                    raw_codes = payload.get('pet_codes')
                    if isinstance(raw_codes, (list, tuple)):
                        codes = [str(c).strip() for c in raw_codes if str(c).strip()]
                    elif raw_codes:
                        codes = [c.strip() for c in str(raw_codes).split(',') if c.strip()]
                    else:
                        preview = self.client.get(
                            f'/api/business/captures/codes-preview/?count={count}')
                        codes = preview.json().get('data', []) if preview.status_code == 200 else []
                        if not codes:
                            # 预览接口本身属于被测链路；测试夹具不能因为它暂时
                            # 不可用就退回「无文件 JSON」这种已被禁止的形态。
                            codes = generate_pet_codes(count)
                    if codes:
                        # 统一成重复表单字段，真实 multipart 读取端用 getlist()
                        # 才能得到完整编号列表；保留逗号串会被 Django 当成一个值。
                        payload['pet_codes'] = codes
                        payload.setdefault('signature', 'data:image/png;base64,TESTSIGNATURE')
                        payload['group_photo'] = make_image_file('test-group.png')
                        for i, code in enumerate(codes):
                            payload[f'pet_photo_{code}'] = make_image_file(f'test-pet-{i}.png')
                        return self.client.post(url, data=payload)
        return self.client.post(
            url, data=json.dumps(payload or {}), content_type="application/json"
        )

    def get_json(self, url):
        return self.client.get(url)

    def ok(self, resp, msg=None):
        self.assertEqual(resp.status_code, 200, msg or resp.content)
        body = resp.json()
        self.assertTrue(body.get("success"), msg or body)
        return body

    def expect_fail(self, resp, status=400, message=None, msg=None):
        self.assertEqual(resp.status_code, status, msg or resp.content)
        body = resp.json()
        self.assertFalse(body.get("success"), msg or body)
        if message is not None:
            self.assertIn(message, body.get("message", ""), msg)
        return body


class BusinessTestBase(ApiMixin, TestCase):
    """业务域测试基类。"""

    @classmethod
    def setUpTestData(cls):
        cls.city = make_district(name="全市", code="TCITY", is_city=True)
        cls.district_a = make_district(name="甲区", code="TA")
        cls.district_b = make_district(name="乙区", code="TB")

        cls.shelter_a = make_institution(type="shelter", district=cls.district_a, name="甲区捕捉点")
        cls.shelter_b = make_institution(type="shelter", district=cls.district_b, name="乙区捕捉点")
        cls.hospital_a = make_institution(type="hospital", district=cls.district_a, name="甲区医院")
        cls.hospital_b = make_institution(type="hospital", district=cls.district_b, name="乙区医院")
        cls.community_a = make_institution(type="community", district=cls.district_a, name="甲区小区")

        cls.gov_city = make_user("gov_city_t", role="gov_city", district=cls.city)
        cls.gov_a = make_user("gov_a_t", role="gov_district", district=cls.district_a)
        cls.gov_b = make_user("gov_b_t", role="gov_district", district=cls.district_b)
        cls.shelter_user_a = make_user("shelter_a_t", role="shelter",
                                       district=cls.district_a, institution=cls.shelter_a)
        cls.shelter_user_b = make_user("shelter_b_t", role="shelter",
                                       district=cls.district_b, institution=cls.shelter_b)
        cls.hospital_user_a = make_user("hospital_a_t", role="hospital",
                                        district=cls.district_a, institution=cls.hospital_a)
        cls.hospital_user_b = make_user("hospital_b_t", role="hospital",
                                        district=cls.district_b, institution=cls.hospital_b)
        cls.adopter = make_user("adopter_t", role="adopter")
