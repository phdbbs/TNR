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
from business.models import (
    Adoption, AdoptionApplication, Euthanasia, OwnerReturn, Release, Treatment,
)
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
        # 第六十四轮起医院侧申请列表只返回**已通过**的申请（审核在捕捉点），
        # 所以夹具要给 approved —— 否则这条「与孪生接口口径一致」的用例
        # 会退化成「两边都是空集」，测不到机构收敛。
        cls.app_a = AdoptionApplication.objects.create(
            pet=cls.a_in, hospital=cls.hospital_a, applicant_name='甲院申请人',
            status='approved')
        cls.app_a2 = AdoptionApplication.objects.create(
            pet=cls.a2_in, hospital=cls.hospital_a2, applicant_name='乙院申请人',
            status='approved')
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
        """档案列表与一宠一档详情都必须走这份判据。

        ⚠ 详情那一支在第六十三轮搬进了 `services_lifecycle.pet_for_lifecycle`
        （接口与打印页共用）。这条用例原先只读 `views_portal.py` 并数
        `hospital_pet_scope(` 的出现次数 —— 搬迁后计数掉到 2，用例红了，
        **红的是「文件布局变了」，不是「判据漏了」**。所以改成按端点名：
        档案列表在 portal，详情在 shared 模块，两边都得有带参数的
        `hospital_pet_scope(user)` 调用，且都不许退回区县口径。
        """
        for rel in ('business/views_portal.py', 'business/services_lifecycle.py'):
            src = self._read(rel)
            self.assertIn('hospital_pet_scope(user)', src,
                          f'{rel} 的医院分支没有走共用判据（漏收口）')
            self.assertNotIn("Q(district_id=user.district_id)", src,
                             f'{rel} 的医院分支不得再按区县收敛')

    def test_adoption_list_has_institution_branch(self):
        src = self._read('business/views_adoption.py')
        self.assertIn("if user.role == 'hospital':", src)
        # 申请列表第六十四轮起还多一个 `status='approved'` 条件，所以按前缀匹配
        self.assertRegex(src, r'qs\.filter\(hospital_id=user\.institution_id')


class HospitalCrossDistrictDischargeTest(BusinessTestBase):
    """医院端能给自己在治的**跨区县**动物办出院单据（第四十八轮）。

    转运允许跨区县送医（`Transfer.to_hospital` 可以是别的区县的医院），
    所以「本院在治、但动物不在本区县」是常态。出院单据原先走
    `get_active_pet`（按区县取单）—— 这类动物会「看板看得见、点开 404」，
    而且不报错，页面上只是「该动物不存在」。

    取单口径必须与看板一致：`get_hospital_scoped_pet`（本院在治 ∪ 本院诊疗过）。
    写操作另外要求 `pet.hospital_id == user.institution_id`。
    """

    def setUp(self):
        # 乙区捕捉的动物，转运到甲区医院治疗
        self.pet = make_pet(district=self.district_b, shelter=self.shelter_b,
                            hospital=self.hospital_a, status='in_treatment')

    def test_owner_return_across_district(self):
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json('/api/business/owner-returns/create/', {
            'pet_id': self.pet.id, 'owner_name': '王主人',
            'reason': '主人到院认领'}))
        rec = OwnerReturn.objects.get(id=body['data']['id'])
        self.assertEqual(rec.pet_id, self.pet.id)
        self.assertEqual(rec.district_id, self.district_b.id,
                         '归属区县跟着**动物**走，不跟操作员')
        self.assertEqual(rec.institution_id, self.hospital_a.id,
                         '归属机构从业务对象推导，不取操作员机构')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'owner_returned')

    def test_adoption_register_across_district(self):
        self.login_as(self.hospital_user_a)
        body = self.ok(self.post_json('/api/business/adoptions/register/', {
            'pet_id': self.pet.id, 'adopter_name': '李领养',
            'adopter_phone': '13800001111'}))
        rec = Adoption.objects.get(id=body['data']['id'])
        self.assertEqual(rec.hospital_id, self.hospital_a.id)
        self.assertEqual(rec.institution_id, self.hospital_a.id)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_claim')

    def test_other_hospital_cannot_register_across_district(self):
        """反向对照：别家医院对同一只动物必须 404。

        没有这条的话，把医院分支写成「只校验状态、不校验本院」
        也能让上面两条通过 —— 那就是横向越权。
        """
        self.login_as(self.hospital_user_b)
        self.expect_fail(self.post_json('/api/business/adoptions/register/', {
            'pet_id': self.pet.id, 'adopter_name': '李领养',
            'adopter_phone': '13800001111'}), status=404, message='无权访问')
        self.assertFalse(Adoption.objects.filter(pet=self.pet).exists())

    def test_other_hospital_cannot_owner_return_across_district(self):
        self.login_as(self.hospital_user_b)
        self.expect_fail(self.post_json('/api/business/owner-returns/create/', {
            'pet_id': self.pet.id, 'owner_name': '王主人'}),
            status=404, message='无权访问')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'in_treatment', '被拒时不能有副作用')


class HospitalDischargeListScopeTest(BusinessTestBase):
    """出院四类单据的**列表**口径：医院只看本院，且不能套区县。

    「能办、能写库、但查不到自己办的单据」是最难发现的一类缺陷：
    接口返回 200 + 空数组，出院页显示「该去向暂无动物」，看起来只是
    「还没人办过」。第四十八轮实测就栽在这里 —— `owner_return_create`
    放开了医院，`owner_return_list` 的角色白名单没跟着放开，
    医院点「出院管理」直接看到「无权访问该接口」。

    另一半是区县：跨区县送医时单据的 `district` 是**动物**的区县
    （与本院不同），先按区县过滤再按机构过滤，本院刚办的单据立刻消失。
    """

    def setUp(self):
        # 同区县第二家医院：横向越权的必要前提
        self.hospital_a2 = make_institution(type='hospital',
                                            district=self.district_a,
                                            name='甲区第二医院')
        # 本院在治、但动物属于**另一个区县**（跨区县送医）
        self.pet_a = make_pet(district=self.district_b, shelter=self.shelter_b,
                              hospital=self.hospital_a, status='in_treatment')
        # 同区县另一家医院在治的动物
        self.pet_a2 = make_pet(district=self.district_a, shelter=self.shelter_a,
                               hospital=self.hospital_a2, status='in_treatment')

    def _make_docs(self, pet, institution, suffix):
        OwnerReturn.objects.create(
            pet=pet, pet_code=pet.code, owner_name='主人' + suffix, reason='认领',
            district=pet.district, institution=institution,
            ledger_no='RET-' + suffix)
        Euthanasia.objects.create(
            pet=pet, pet_code=pet.code, reason='病重', district=pet.district,
            hospital=institution, institution=institution,
            ledger_no='EUT-' + suffix)
        Release.objects.create(
            pet=pet, pet_code=pet.code, community_name='小区' + suffix,
            status='pending', district=pet.district, institution=institution,
            ledger_no='REL-' + suffix)

    def _ledgers(self, url):
        self.login_as(self.hospital_user_a)
        return {row['ledger_no'] for row in self.ok(self.get_json(url))['data']}

    def test_owner_return_list_includes_own_cross_district(self):
        self._make_docs(self.pet_a, self.hospital_a, 'A')
        self._make_docs(self.pet_a2, self.hospital_a2, 'B')
        got = self._ledgers('/api/business/owner-returns/')
        self.assertIn('RET-A', got,
                      '本院在跨区县动物上办的领回单必须查得到（不能套区县口径）')
        self.assertNotIn('RET-B', got,
                         '别院的领回单含主人姓名 / 身份证，不能横向读')

    def test_euthanasia_list_includes_own_cross_district(self):
        self._make_docs(self.pet_a, self.hospital_a, 'A')
        self._make_docs(self.pet_a2, self.hospital_a2, 'B')
        got = self._ledgers('/api/business/euthanasia/')
        self.assertIn('EUT-A', got)
        self.assertNotIn('EUT-B', got)

    def test_release_list_includes_own_cross_district(self):
        self._make_docs(self.pet_a, self.hospital_a, 'A')
        self._make_docs(self.pet_a2, self.hospital_a2, 'B')
        got = self._ledgers('/api/business/releases/')
        self.assertIn('REL-A', got)
        self.assertNotIn('REL-B', got)

    def test_other_hospital_sees_only_its_own(self):
        """反向对照：B 院看到的必须是自己的，而不是 A 院的。"""
        self._make_docs(self.pet_a, self.hospital_a, 'A')
        self._make_docs(self.pet_a2, self.hospital_a2, 'B')
        self.login_as(self.hospital_user_b)
        got = {row['ledger_no'] for row in self.ok(
            self.get_json('/api/business/owner-returns/'))['data']}
        self.assertNotIn('RET-A', got)
        self.assertEqual(got, set(), '乙区医院不该看到甲区的任何领回单')

    def test_hospital_without_institution_sees_nothing(self):
        """没挂机构的医院账号必须是**空集**，而不是全部。"""
        self._make_docs(self.pet_a, self.hospital_a, 'A')
        orphan = make_user('hs_no_inst2', role='hospital',
                           district=self.district_a, institution=None)
        self.login_as(orphan)
        for url in ('/api/business/owner-returns/',
                    '/api/business/euthanasia/',
                    '/api/business/releases/'):
            self.assertEqual(self.ok(self.get_json(url))['data'], [],
                             f'{url} 对未绑定机构的医院账号必须返回空集')
