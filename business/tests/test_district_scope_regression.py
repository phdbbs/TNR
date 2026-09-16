"""区县角色（gov_district）对无 district 字段的模型做列表/详情查询时，
不得抛 FieldError，且数据必须按所属宠物区县收敛。

回归背景：CheckIn / AdoptionHallListing 模型本身没有 district 外键，
但 get_district_filtered_queryset() 原先直接 filter(district_id=...)，
对市级用户正常返回（因为市级走 ``return all()`` 分支），
对区县用户则抛 FieldError → 接口 500。
修复后模型声明 DISTRICT_LOOKUP='pet__district'，过滤沿宠物派生区县。
"""
from business.models import AdoptionHallListing, CheckIn
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
