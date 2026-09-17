"""区县角色（gov_district）对无 district 字段的模型做列表/详情查询时，
不得抛 FieldError，且数据必须按所属宠物区县收敛。

回归背景：CheckIn / AdoptionHallListing 模型本身没有 district 外键，
但 get_district_filtered_queryset() 原先直接 filter(district_id=...)，
对市级用户正常返回（因为市级走 ``return all()`` 分支），
对区县用户则抛 FieldError → 接口 500。
修复后模型声明 DISTRICT_LOOKUP='pet__district'，过滤沿宠物派生区县。
"""
from business.models import AdoptionHallListing, Blacklist, CheckIn, Transfer
from business.tests.base import BusinessTestBase, make_pet, make_user


class CheckInDistrictScopeTest(BusinessTestBase):
    """区县政府查看回访打卡列表不得 500，且只看到本区宠物的打卡。"""

    def test_gov_district_list_no_field_error(self):
        """区县角色 GET /api/business/checkins/ 不抛 FieldError（回归核心）。"""
        pet = make_pet(district=self.district_a, status='adopted')
        adopter = make_user(role='adopter')
        CheckIn.objects.create(pet=pet, pet_code=pet.code, adopter=adopter,
                               month='2026-05')
        self.login_as(self.gov_a)
        # 修复前：FieldError → 500
        data = self.ok(self.get_json('/api/business/checkins/'))['data']
        self.assertEqual(len(data), 1)

    def test_gov_district_list_scoped_by_pet_district(self):
        """区县角色只看到本区宠物的打卡，看不到跨区。"""
        pet_a = make_pet(district=self.district_a, status='adopted')
        pet_b = make_pet(district=self.district_b, status='adopted')
        adopter = make_user(role='adopter')
        CheckIn.objects.create(pet=pet_a, pet_code=pet_a.code, adopter=adopter,
                               month='2026-05')
        CheckIn.objects.create(pet=pet_b, pet_code=pet_b.code, adopter=adopter,
                               month='2026-05')
        self.login_as(self.gov_a)
        data = self.ok(self.get_json('/api/business/checkins/'))['data']
        self.assertEqual([c['pet_id'] for c in data], [pet_a.id])

    def test_gov_city_sees_all(self):
        """市级角色看到全部打卡（不受区县收敛）。"""
        pet_a = make_pet(district=self.district_a, status='adopted')
        pet_b = make_pet(district=self.district_b, status='adopted')
        adopter = make_user(role='adopter')
        CheckIn.objects.create(pet=pet_a, pet_code=pet_a.code, adopter=adopter,
                               month='2026-05')
        CheckIn.objects.create(pet=pet_b, pet_code=pet_b.code, adopter=adopter,
                               month='2026-05')
        self.login_as(self.gov_city)
        data = self.ok(self.get_json('/api/business/checkins/'))['data']
        self.assertEqual(len(data), 2)


class HallListingDistrictScopeTest(BusinessTestBase):
    """区县政府查看领养大厅上架列表不得 500，且只看到本区宠物的上架。"""

    def test_gov_district_list_no_field_error(self):
        """区县角色 GET /api/business/hall-listings/ 不抛 FieldError（回归核心）。"""
        pet = make_pet(district=self.district_a, status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet, hospital=self.hospital_a)
        self.login_as(self.gov_a)
        # 修复前：FieldError → 500
        data = self.ok(self.get_json('/api/business/hall-listings/'))['data']
        self.assertEqual(len(data), 1)

    def test_gov_district_list_scoped_by_pet_district(self):
        """区县角色只看到本区宠物的上架记录。"""
        pet_a = make_pet(district=self.district_a, status='pending_adopt')
        pet_b = make_pet(district=self.district_b, status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet_a, hospital=self.hospital_a)
        AdoptionHallListing.objects.create(pet=pet_b, hospital=self.hospital_b)
        self.login_as(self.gov_a)
        data = self.ok(self.get_json('/api/business/hall-listings/'))['data']
        # 序列化同时产出 snake_case 和 camelCase，pet 是嵌套对象
        pet_ids = [l.get('pet_id') or l.get('petId') or l['pet']['id'] for l in data]
        self.assertEqual(pet_ids, [pet_a.id])

    def test_gov_city_sees_all(self):
        """市级角色看到全部上架（不受区县收敛）。"""
        pet_a = make_pet(district=self.district_a, status='pending_adopt')
        pet_b = make_pet(district=self.district_b, status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet_a, hospital=self.hospital_a)
        AdoptionHallListing.objects.create(pet=pet_b, hospital=self.hospital_b)
        self.login_as(self.gov_city)
        data = self.ok(self.get_json('/api/business/hall-listings/'))['data']
        self.assertEqual(len(data), 2)


class CityLevelOperatorDistrictResolutionTest(BusinessTestBase):
    """操作员挂在「全市（市级）」时，业务记录仍必须落到具体区县。

    现场配置：两个捕捉点操作员（`cy_shelter` / `hd_shelter`）的 `district`
    都是「全市（市级）」，只有 `institution` 指向具体捕捉点。任何
    「`data.get('district_id') or user.district_id`」的写法都会把记录落到
    市级 —— 而前端**并不提交** `district_id`，于是本区县政府在区县隔离下
    完全看不到本区数据（捕捉单当初就是这么错的，现场 7 条）。

    正确做法：以「承载最可能正确区县的对象」为锚点 ——
    转运单锚在**发出捕捉点**机构上，黑名单锚在**操作员所属机构**上。
    """

    def setUp(self):
        # 关键：district 是市级，只有 institution 指向甲区捕捉点
        self.city_shelter_user = make_user(
            role='shelter', district=self.city, institution=self.shelter_a)

    def _create_transfer(self, pet):
        return self.post_json('/api/business/transfers/create/', {
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [pet.code],
            'pet_count': 1,
        })

    def test_transfer_create_uses_shelter_district(self):
        """回归核心：转运单归属区县取「发出捕捉点」而非操作员的市级。"""
        pet = make_pet(district=self.district_a, shelter=self.shelter_a)
        self.login_as(self.city_shelter_user)
        self.ok(self._create_transfer(pet))
        transfer = Transfer.objects.get(pet_codes=pet.code)
        self.assertEqual(transfer.district_id, self.district_a.id,
                         '转运单落到市级后，本区县政府在区县隔离下看不到它')

    def test_transfer_visible_to_district_government(self):
        """落到具体区县后，本区县政府才看得到这张转运单。"""
        pet = make_pet(district=self.district_a, shelter=self.shelter_a)
        self.login_as(self.city_shelter_user)
        self.ok(self._create_transfer(pet))
        self.login_as(self.gov_a)
        data = self.ok(self.get_json('/api/business/transfers/'))['data']
        self.assertEqual(len(data), 1)
        # 乙区政府看不到
        self.login_as(self.gov_b)
        self.assertEqual(
            len(self.ok(self.get_json('/api/business/transfers/'))['data']), 0)

    def test_blacklist_create_uses_institution_district(self):
        """黑名单同样不能落到市级。"""
        self.login_as(self.city_shelter_user)
        self.ok(self.post_json('/api/business/blacklist/create/', {
            'name': '张某某', 'phone': '13900000001', 'reason': '弃养领养宠物',
        }))
        bl = Blacklist.objects.get(name='张某某')
        self.assertEqual(bl.district_id, self.district_a.id)
        # 甲区政府能看到，乙区政府看不到
        self.login_as(self.gov_a)
        self.assertEqual(
            len(self.ok(self.get_json('/api/business/blacklist/'))['data']), 1)
        self.login_as(self.gov_b)
        self.assertEqual(
            len(self.ok(self.get_json('/api/business/blacklist/'))['data']), 0)

    def test_explicit_cross_district_submission_rejected(self):
        """市级操作员也不能把记录归属到「全市（市级）」。"""
        pet = make_pet(district=self.district_a, shelter=self.shelter_a)
        self.login_as(self.city_shelter_user)
        resp = self.post_json('/api/business/transfers/create/', {
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': [pet.code],
            'pet_count': 1,
            'district_id': self.city.id,
        })
        self.expect_fail(resp, message='归属区县不能是市级')
        self.assertFalse(Transfer.objects.exists())
