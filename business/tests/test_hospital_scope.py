"""第二十轮：医院角色的数据可见范围必须收敛到**本机构**，而不是本区县。

**同一区县可能有多家医院** —— 襄城区就有两家（爱心宠物医院 / 瑞鹏宠物医院）。
按区县收敛会让 A 院读到 B 院名下动物的完整档案，**含主人姓名与电话**
（`intake_contact` / `intake_property_name`）。

第二十轮实测（修复前）：

    aixin_hosp   /pets/archive/  16 条 = 本院 5 + 瑞鹏 2 + 无归属 9
    ruipeng_hosp /pets/archive/  16 条 = 本院 2 + 爱心 5 + 无归属 9

而**同一个「动物」概念的其它接口都是机构级**：`/pets/`（注释写明「医院只看
分配给自己的宠物」）、`/hall-listings/`、`/treatments/`、`/transfers/`、
`/materials/transactions/`、`/euthanasia/`。所以真相不是「医院就该看全区」，
而是**同类接口之间口径漂移**。

本文件锁死三处：

1. ``/pets/archive/``（一宠一档）—— 医院只看本院在治 ∪ 本院经手过；
2. ``/pets/<id>/lifecycle/``（溯源）—— 同上；
3. ``/adoptions/``（领养记录）—— **医院**只看本院，与**孪生接口**
   ``adoption_application_list``（领养申请列表）的医院分支口径一致。

**捕捉点与政府端的口径本轮未改动**，并且有测试锁死「不许改窄」：
捕捉点是本区县的收容入口（``/pets/`` 的注释写明「只看本机构所在区县」），
把它缩成机构级会让它看不到本区县、但动物并非由它捕捉的领养记录。

判据共用 ``business.services.hospital_pet_scope()``，实现只留一份。
"""
from business.models import Adoption, AdoptionApplication, Treatment
from business.tests.base import (
    BusinessTestBase, make_institution, make_pet, make_user,
)

ARCHIVE = '/api/business/pets/archive/'
ADOPTIONS = '/api/business/adoptions/'
APPLICATIONS = '/api/business/adoptions/applications/'


class HospitalScopeBase(BusinessTestBase):
    """在**同一个区县**里放两家医院，各带一只在治 + 一只已出院（但本院诊疗过）动物。

    横向越权的必要前提是「同区县存在多个同类型机构」，父类基类的
    `hospital_a` / `hospital_b` 分属两个区县，测不出来，所以这里补第二家。
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        d = cls.district_a
        cls.hospital_a2 = make_institution(
            type='hospital', district=d, name='甲区第二医院')
        cls.user_a = cls.hospital_user_a          # 甲区医院
        cls.user_a2 = make_user('hs_a2', role='hospital', district=d,
                                institution=cls.hospital_a2)
        # 没有挂靠机构的医院账号：必须看到空集，而不是「全部」
        cls.user_none = make_user('hs_none', role='hospital', district=d,
                                  institution=None)

        # A 院：在治 1 只
        cls.a_in = make_pet(district=d, shelter=cls.shelter_a,
                            hospital=cls.hospital_a, status='in_treatment')
        # A 院：已出院（hospital 被清空），但 A 院诊疗过 —— 必须仍可查
        cls.a_out = make_pet(district=d, shelter=cls.shelter_a,
                             hospital=None, status='released')
        # 第二家医院：在治 1 只
        cls.a2_in = make_pet(district=d, shelter=cls.shelter_a,
                             hospital=cls.hospital_a2, status='in_treatment')
        # 第二家医院：已出院但由它诊疗过
        cls.a2_out = make_pet(district=d, shelter=cls.shelter_a,
                              hospital=None, status='released')
        # 谁都没经手过：只属于本区县
        cls.plain = make_pet(district=d, shelter=cls.shelter_a,
                             hospital=None, status='in_transit')
        # 本区县、但动物**不挂靠任何捕捉点**（`shelter=None`）——
        # 判别「捕捉点按区县」与「捕捉点按本机构」的关键夹具
        cls.a_other = make_pet(district=d, shelter=None, status='adopted')
        # 别的区县，用于验证区县隔离没被削弱
        cls.b_pet = make_pet(district=cls.district_b, status='in_transit')

        Treatment.objects.create(pet=cls.a_out, district=d, hospital=cls.hospital_a)
        # **同一只动物、同一家医院的第二条诊疗记录** —— 没有它，
        # `hospital_pet_scope()` 的反向 JOIN 根本产生不出重复行，
        # 「档案没有重复行」那条断言就是空转的（变异验证 M4 实测抓到）。
        Treatment.objects.create(pet=cls.a_out, district=d, hospital=cls.hospital_a)
        Treatment.objects.create(pet=cls.a2_out, district=d, hospital=cls.hospital_a2)

        # 两家医院各一条领养记录 + 各一条领养申请（申请单由 B 院受理）
        cls.adopt_a = Adoption.objects.create(
            pet=cls.a_in, district=d, hospital=cls.hospital_a,
            adopter_name='甲院领养人', ledger_no='ADP-T-A')
        cls.adopt_a2 = Adoption.objects.create(
            pet=cls.a2_in, district=d, hospital=cls.hospital_a2,
            adopter_name='乙院领养人', ledger_no='ADP-T-B')
        # 本区县、但动物不挂靠本捕捉点，且由第二家医院受理
        cls.adopt_other = Adoption.objects.create(
            pet=cls.a_other, district=d, hospital=cls.hospital_a2,
            adopter_name='无捕捉点领养人', ledger_no='ADP-T-D')
        # 受理医院已被删除（`Adoption.hospital` 是 SET_NULL）
        cls.adopt_nohosp = Adoption.objects.create(
            pet=cls.plain, district=d, hospital=None,
            adopter_name='受理医院已删除', ledger_no='ADP-T-E')
        cls.app_a = AdoptionApplication.objects.create(
            pet=cls.a_in, hospital=cls.hospital_a, applicant_name='甲院申请人')
        cls.app_a2 = AdoptionApplication.objects.create(
            pet=cls.a2_in, hospital=cls.hospital_a2, applicant_name='乙院申请人')
        # 乙区的一条领养记录（甲的捕捉点不该看到）
        cls.adopt_b = Adoption.objects.create(
            pet=cls.b_pet, district=cls.district_b, hospital=cls.hospital_b,
            adopter_name='乙区领养人', ledger_no='ADP-T-C')

    def ids_of(self, user, url):
        self.login_as(user)
        return {row['id'] for row in self.ok(self.get_json(url))['data']}

    def rows_of(self, user, url):
        """返回**原始列表**（不去重），用于查重复行。"""
        self.login_as(user)
        return [row['id'] for row in self.ok(self.get_json(url))['data']]


class HospitalArchiveScopeTest(HospitalScopeBase):
    """``/pets/archive/``：医院只看本院在治 ∪ 本院经手过。"""

    def test_hospital_sees_own_and_handled_only(self):
        ids = self.ids_of(self.user_a, ARCHIVE)
        self.assertIn(self.a_in.id, ids, '本院在治的动物必须在档案里')
        self.assertIn(self.a_out.id, ids,
                      '本院经手过但已出院的动物必须仍可查 —— 只按 hospital_id 会丢掉历史')
        self.assertNotIn(self.a2_in.id, ids,
                         '不得读到同区县另一家医院在治动物的档案')
        self.assertNotIn(self.a2_out.id, ids,
                         '不得读到同区县另一家医院经手过动物的档案')
        self.assertNotIn(self.plain.id, ids, '不得读到与本院无关的动物')
        self.assertNotIn(self.b_pet.id, ids, '不得跨区县')

    def test_two_hospitals_in_same_district_are_disjoint(self):
        """同区县两家医院的可见集合必须**不相交** —— 这就是横向越权的判据。"""
        a = self.ids_of(self.user_a, ARCHIVE)
        b = self.ids_of(self.user_a2, ARCHIVE)
        self.assertEqual(a & b, set(), f'两家医院看到了同一批动物：{a & b}')

    def test_hospital_without_institution_sees_nothing(self):
        """没有挂靠机构时必须是**空集**。

        若判据写成 `Q(hospital_id=None)`，就会匹配上所有「尚未分配医院」的动物
        —— 等于完全不设限，比按区县还宽。
        """
        self.assertEqual(self.ids_of(self.user_none, ARCHIVE), set())

    def test_no_duplicate_rows(self):
        """`hospital_pet_scope()` 走的是反向外键 JOIN，**必须配 `.distinct()`**。

        漏掉它不会少数据，只会让同一只动物按诊疗次数出现多行 ——
        「一宠一档」的台账里出现重复行，比少一行更难被发现。
        """
        for user in (self.user_a, self.user_a2):
            rows = self.rows_of(user, ARCHIVE)
            self.assertEqual(len(rows), len(set(rows)),
                             f'{user.username} 的档案出现重复行：{rows}')

    def test_shelter_still_sees_whole_district(self):
        """捕捉点**不变**：它是本区县的收容入口，看全区的动物（与 `/pets/` 同口径）。"""
        ids = self.ids_of(self.shelter_user_a, ARCHIVE)
        for pet in (self.a_in, self.a2_in, self.plain):
            self.assertIn(pet.id, ids)
        self.assertNotIn(self.b_pet.id, ids, '捕捉点不得跨区县')

    def test_gov_scope_unchanged(self):
        city = self.ids_of(self.gov_city, ARCHIVE)
        self.assertIn(self.b_pet.id, city, '市级看全部')
        district = self.ids_of(self.gov_a, ARCHIVE)
        self.assertIn(self.a_in.id, district)
        self.assertNotIn(self.b_pet.id, district, '区级只看本区')


class HospitalLifecycleScopeTest(HospitalScopeBase):
    """``/pets/<id>/lifecycle/``：医院只看本院在治 ∪ 本院经手过。"""

    def url(self, pet):
        return f'/api/business/pets/{pet.id}/lifecycle/'

    def test_other_hospital_pet_404(self):
        self.login_as(self.user_a)
        self.expect_fail(self.get_json(self.url(self.a2_in)), status=404)
        self.expect_fail(self.get_json(self.url(self.a2_out)), status=404)

    def test_own_and_handled_pet_ok(self):
        self.login_as(self.user_a)
        self.ok(self.get_json(self.url(self.a_in)))
        self.ok(self.get_json(self.url(self.a_out)))

    def test_unrelated_pet_404(self):
        self.login_as(self.user_a)
        self.expect_fail(self.get_json(self.url(self.plain)), status=404)

    def test_other_district_404(self):
        self.login_as(self.user_a)
        self.expect_fail(self.get_json(self.url(self.b_pet)), status=404)


class HospitalAdoptionScopeTest(HospitalScopeBase):
    """``/adoptions/``：**医院**分支收敛到本机构；捕捉点 / 政府端口径不变。"""

    def test_hospital_sees_own_adoptions_only(self):
        ids = self.ids_of(self.user_a, ADOPTIONS)
        self.assertIn(self.adopt_a.id, ids)
        self.assertNotIn(self.adopt_a2.id, ids,
                         '不得读到同区县另一家医院的领养记录')
        self.assertNotIn(self.adopt_other.id, ids,
                         '不得读到同区县另一家医院受理的领养记录')

    def test_hospital_branch_matches_twin(self):
        """医院分支与孪生接口 ``adoption_application_list`` 的口径一致。

        第二十轮之前 `adoption_list` 没有这一支，医院按区县口径。
        """
        for user, mine in ((self.user_a, {self.adopt_a.id}),
                           (self.user_a2, {self.adopt_a2.id, self.adopt_other.id})):
            self.assertEqual(self.ids_of(user, ADOPTIONS), mine,
                             f'{user.username} 的领养记录范围不对')
            self.assertEqual(self.ids_of(user, APPLICATIONS),
                             {self.app_a.id if user is self.user_a else self.app_a2.id})

    def test_hospital_without_institution_sees_nothing(self):
        """没有挂靠机构时必须是**空集**。

        若判据写成 `filter(hospital_id=None)`，会匹配上所有「受理医院已被删除」的
        记录（`Adoption.hospital` 是 `SET_NULL`）—— 等于不设限。
        """
        self.assertEqual(self.ids_of(self.user_none, ADOPTIONS), set())

    def test_shelter_scope_unchanged_district_wide(self):
        """捕捉点**保持区县口径，本轮未改动**。

        这一支不是漂移：``/pets/``（``hospital_pets``）明确写着「捕捉点只看
        **本机构所在区县**的宠物」，与这里按 ``Adoption.district`` 收敛一致。
        真正与它们不一致的是孪生接口（按 ``pet__shelter_id``，更窄）——
        把这里改成跟那个窄口径对齐，等于把「区县级收容入口」缩成「机构级」。
        **窄不等于对**：`adopt_other` 就是那只「本区县、但动物不挂靠本捕捉点」的
        记录，捕捉点必须仍能看到它。
        """
        ids = self.ids_of(self.shelter_user_a, ADOPTIONS)
        self.assertIn(self.adopt_a.id, ids)
        self.assertIn(self.adopt_a2.id, ids)
        self.assertIn(self.adopt_other.id, ids,
                      '本区县、但动物不属于本捕捉点的领养记录仍应可见'
                      '（此处失败说明捕捉点被误缩成机构级）')
        self.assertNotIn(self.adopt_b.id, ids, '不得跨区县')

    def test_gov_scope_unchanged(self):
        self.assertIn(self.adopt_a.id, self.ids_of(self.gov_city, ADOPTIONS))
        self.assertIn(self.adopt_a2.id, self.ids_of(self.gov_city, ADOPTIONS))
        d = self.ids_of(self.gov_a, ADOPTIONS)
        self.assertIn(self.adopt_a.id, d)
        self.assertIn(self.adopt_a2.id, d, '区级看本区全部')
        self.assertNotIn(self.adopt_b.id, d, '区级不得跨区县')


class HospitalScopeContractTest(HospitalScopeBase):
    """源码级契约：判据必须**共用一份实现**，两端（列表 + 详情）都要用。"""

    def _read(self, rel):
        import pathlib
        from django.conf import settings
        return (pathlib.Path(settings.BASE_DIR) / rel).read_text(encoding='utf-8')

    def test_single_shared_predicate(self):
        import inspect
        from business import services
        src = inspect.getsource(services.hospital_pet_scope)
        # 无机构时必须返回空集，不能退化成「按 None 过滤」
        self.assertIn('Q(pk__in=[])', src)
        self.assertIn('treatments__hospital_id', src,
                      '必须并上「本院诊疗过」，否则医院查不到自己经手过的历史动物')

    def test_both_pet_endpoints_use_it(self):
        src = self._read('business/views_portal.py')
        self.assertEqual(src.count('hospital_pet_scope('), 3,
                         '定义处 1 次 + 档案 1 次 + 溯源 1 次；少一处就是漏收口')
        self.assertNotIn("Q(district_id=user.district_id)", src,
                         '医院分支不得再按区县收敛')

    def test_adoption_list_has_institution_branch(self):
        src = self._read('business/views_adoption.py')
        self.assertIn("if user.role == 'hospital':", src)
        self.assertIn('qs.filter(hospital_id=user.institution_id)', src)
