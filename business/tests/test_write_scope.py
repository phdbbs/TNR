"""第二十一轮：**写侧**横向越权 —— 医院只能改「当前挂在自己名下」的对象。

第二十轮收的是**读侧**（同区县另一家医院能不能读到本院档案）。这一轮往对称方向走：
能不能**改**。

侦察方式：把每条「带 id 参数的写接口」的 `role_required` 拉出来，锁定医院够得着的那些，
再用**同区县另一家医院**的记录逐条打。结果只有一个漏网的：

    POST /api/business/adoptions/<宠物id>/edit-info/   200  越权成功 ❌

它用的是 `get_active_pet()` —— **只按区县收敛**（视图注释也写着「按区县范围取宠物」）。
于是一院能改另一院名下宠物的领养上架文案，甚至把它**上架到公开领养大厅**
（`is_active=True`，匿名可见）或把对方已上架的**下架**。

**同类接口全都做了机构判据，只有它没有**（这就是「同类接口漂移」）：

| 接口 | 判据 |
|---|---|
| `adoption_confirm_claim` | `adoption.hospital_id != user.institution_id` |
| `transfer_receive` / `transfer_reject` | `user.institution_id != transfer.to_hospital_id` |
| `material_receive` | `user.institution_id != txn.hospital_id` |
| **`adoption_info_edit`** | **（无）** ← 本轮补上 |

本文件锁两件事：

1. **行为**：对**每一个**医院可达的写接口，拿同区县另一家医院的记录去打，
   必须被拒**且库里的值一字未改**；再拿自己的记录做正向对照，必须放行。
2. **契约**：源码级扫描 —— 医院可达的写接口必须出现机构判据，
   防止以后新增接口又漏。
"""
import datetime
import inspect
import re

from django.urls import get_resolver

from business.models import (
    Adoption, AdoptionApplication, AdoptionHallListing, MaterialTransaction,
    Pet, Transfer, Treatment,
)
from business.tests.base import (
    BusinessTestBase, make_institution, make_material, make_pet, make_user,
)

ROLES = {'gov_city', 'gov_district', 'shelter', 'hospital', 'adopter'}


class HospitalWriteScopeBase(BusinessTestBase):
    """在**同一个区县**里放两家医院，各带一整套可写的业务对象。

    横向越权的必要前提是「同区县存在多个同类型机构」——
    父类基类的 `hospital_a` / `hospital_b` 分属两个区县，测不出横向越权。
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        d = cls.district_a
        cls.hospital_a2 = make_institution(type='hospital', district=d, name='甲区第二医院')
        cls.me = cls.hospital_user_a                       # 甲区医院
        cls.other = make_user('ws_other', role='hospital', district=d,
                              institution=cls.hospital_a2)
        cls.no_inst = make_user('ws_none', role='hospital', district=d, institution=None)

        # ---------- 受害方（另一家医院）名下的记录 ----------
        cls.v_pet = make_pet(district=d, shelter=cls.shelter_a,
                             hospital=cls.hospital_a2, status='pending_adopt')
        cls.v_adoption = Adoption.objects.create(
            pet=cls.v_pet, district=d, hospital=cls.hospital_a2, status='pending_claim',
            ledger_no='ADP-WS-V', adopter_name='受害领养人')
        # 审核通过前会复核宠物是否仍可领养 —— 申请单必须挂在**没有领养记录**的宠物上，
        # 否则「被拒」可能是业务规则（400）而不是越权判据，测试会空转通过。
        cls.v_pet_app = make_pet(district=d, shelter=cls.shelter_a,
                                 hospital=cls.hospital_a2, status='pending_adopt')
        cls.v_app = AdoptionApplication.objects.create(
            pet=cls.v_pet_app, hospital=cls.hospital_a2, status='pending',
            applicant_name='受害申请人')
        cls.v_material = make_material(district=d, name='受害物料')
        cls.v_txn = MaterialTransaction.objects.create(
            type='dispatch', material=cls.v_material, material_name=cls.v_material.name,
            quantity=5, hospital=cls.hospital_a2, district=d,
            date=datetime.date.today(), ledger_no='MT-WS-V')
        cls.v_transfer = Transfer.objects.create(
            to_hospital=cls.hospital_a2, district=d, status='pending', pet_count=1,
            ledger_no='TRF-WS-V', to_hospital_name=cls.hospital_a2.name)
        cls.v_treatment = Treatment.objects.create(
            pet=cls.v_pet, district=d, hospital=cls.hospital_a2)

        # ---------- 正向对照：本方（自己）名下的同类记录 ----------
        cls.o_pet = make_pet(district=d, shelter=cls.shelter_a,
                             hospital=cls.hospital_a, status='pending_adopt')
        cls.o_adoption = Adoption.objects.create(
            pet=cls.o_pet, district=d, hospital=cls.hospital_a, status='pending_claim',
            ledger_no='ADP-WS-O', adopter_name='本方领养人')
        cls.o_pet_app = make_pet(district=d, shelter=cls.shelter_a,
                                 hospital=cls.hospital_a, status='pending_adopt')
        cls.o_app = AdoptionApplication.objects.create(
            pet=cls.o_pet_app, hospital=cls.hospital_a, status='pending',
            applicant_name='本方申请人')
        cls.o_material = make_material(district=d, name='本方物料')
        cls.o_txn = MaterialTransaction.objects.create(
            type='dispatch', material=cls.o_material, material_name=cls.o_material.name,
            quantity=5, hospital=cls.hospital_a, district=d,
            date=datetime.date.today(), ledger_no='MT-WS-O')
        cls.o_transfer = Transfer.objects.create(
            to_hospital=cls.hospital_a, district=d, status='pending', pet_count=1,
            ledger_no='TRF-WS-O', to_hospital_name=cls.hospital_a.name)

    # -- 断言辅助（注意别覆盖 ApiMixin.ok —— 它返回解析后的响应体）--------
    def post_as(self, user, url, body=None):
        """以指定用户身份 POST，返回**响应对象**（不是 body）。"""
        self.login_as(user)
        return self.post_json(url, body or {})

    def assert_refused(self, resp, label):
        """被拒的两种合法形态：404（取不到）/ 200 + success:false（无权限）。"""
        body = resp.json()
        self.assertFalse(body.get('success'), f'{label} 不应成功：{body}')
        return body.get('message') or ''

    def assert_ok(self, resp, label):
        """正向对照：必须放行。"""
        return self.ok(resp, label)


class HospitalWriteCrossInstitutionTest(HospitalWriteScopeBase):
    """同区县另一家医院的记录，**一个都不能写**。"""

    def test_edit_info_cross_institution_refused(self):
        """领养上架信息：本轮修复的那个洞。

        它危害最大 —— 不止改文案，还能 `is_active=True` 把**别人的动物**
        上架到**公开**领养大厅（匿名可见），或把对方已上架的**下架**。
        """
        resp = self.post_as(self.me, f'/api/business/adoptions/{self.v_pet.id}/edit-info/',
                            {'intro': '越权改的文案', 'is_active': True})
        msg = self.assert_refused(resp, 'edit-info 跨机构')
        self.assertIn('无权', msg)
        # 关键：**不能有任何落库**（拦截必须让下游连带写入也不发生）
        self.assertFalse(
            AdoptionHallListing.objects.filter(pet=self.v_pet).exists(),
            '被拒后仍给受害方的宠物建了上架记录')

    def test_confirm_claim_cross_institution_refused(self):
        resp = self.post_as(self.me,
                            f'/api/business/adoptions/{self.v_adoption.id}/confirm-claim/')
        self.assert_refused(resp, 'confirm-claim 跨机构')
        self.v_adoption.refresh_from_db()
        self.assertEqual(self.v_adoption.status, 'pending_claim', '被拒后状态被改')

    def test_application_review_cross_institution_refused(self):
        resp = self.post_as(self.me,
                            f'/api/business/adoptions/applications/{self.v_app.id}/review/',
                            {'action': 'approve'})
        self.assert_refused(resp, 'application review 跨机构')
        self.v_app.refresh_from_db()
        self.assertEqual(self.v_app.status, 'pending', '被拒后申请状态被改')

    def test_material_receive_cross_institution_refused(self):
        before = MaterialTransaction.objects.count()
        resp = self.post_as(self.me, f'/api/business/materials/{self.v_txn.id}/receive/')
        self.assert_refused(resp, 'material receive 跨机构')
        self.assertEqual(MaterialTransaction.objects.count(), before,
                         '被拒后仍写出了签收流水')

    def test_transfer_receive_cross_institution_refused(self):
        resp = self.post_as(self.me, f'/api/business/transfers/{self.v_transfer.id}/receive/')
        self.assert_refused(resp, 'transfer receive 跨机构')
        self.v_transfer.refresh_from_db()
        self.assertEqual(self.v_transfer.status, 'pending', '被拒后转运单被签收')

    def test_transfer_reject_cross_institution_refused(self):
        resp = self.post_as(self.me, f'/api/business/transfers/{self.v_transfer.id}/reject/',
                            {'reason': '越权驳回'})
        self.assert_refused(resp, 'transfer reject 跨机构')
        self.v_transfer.refresh_from_db()
        self.assertEqual(self.v_transfer.status, 'pending', '被拒后转运单被驳回')

    def test_treatment_detail_cross_institution_refused(self):
        resp = self.post_as(self.me, f'/api/business/treatments/{self.v_treatment.id}/')
        self.assert_refused(resp, 'treatment detail 跨机构')

    def test_no_institution_account_writes_nothing(self):
        """没有挂靠机构的医院账号：**空集**，不能因为 `institution_id is None` 就放行。"""
        resp = self.post_as(self.no_inst,
                            f'/api/business/adoptions/{self.v_pet.id}/edit-info/',
                            {'intro': 'x'})
        self.assert_refused(resp, '无机构账号')
        self.assertFalse(AdoptionHallListing.objects.filter(pet=self.v_pet).exists())


class HospitalWriteOwnRecordsTest(HospitalWriteScopeBase):
    """正向对照 —— 改自己的记录必须**照常放行**（守卫不能做成「一律禁止」）。"""

    def test_edit_info_own_pet_ok(self):
        resp = self.post_as(self.me, f'/api/business/adoptions/{self.o_pet.id}/edit-info/',
                            {'intro': '本院文案', 'is_active': True})
        self.assert_ok(resp, 'edit-info 本院')
        listing = AdoptionHallListing.objects.get(pet=self.o_pet)
        self.assertEqual(listing.intro, '本院文案')
        self.assertTrue(listing.is_active)
        self.assertEqual(listing.hospital_id, self.hospital_a.id,
                         '上架记录的归属必须是宠物当前的医院')

    def test_confirm_claim_own_ok(self):
        resp = self.post_as(self.me,
                            f'/api/business/adoptions/{self.o_adoption.id}/confirm-claim/')
        self.assert_ok(resp, 'confirm-claim 本院')

    def test_application_review_own_ok(self):
        resp = self.post_as(self.me,
                            f'/api/business/adoptions/applications/{self.o_app.id}/review/',
                            {'action': 'approve'})
        self.assert_ok(resp, 'application review 本院')

    def test_material_receive_own_ok(self):
        resp = self.post_as(self.me, f'/api/business/materials/{self.o_txn.id}/receive/')
        self.assert_ok(resp, 'material receive 本院')

    def test_transfer_receive_own_ok(self):
        resp = self.post_as(self.me, f'/api/business/transfers/{self.o_transfer.id}/receive/')
        self.assert_ok(resp, 'transfer receive 本院')

    def test_gov_district_can_still_edit_in_district(self):
        """政府端的口径**不变**：区级仍可按区县编辑（不能为了修医院把区级一起收紧）。"""
        resp = self.post_as(self.gov_a,
                            f'/api/business/adoptions/{self.v_pet.id}/edit-info/',
                            {'intro': '区级可改'})
        self.assert_ok(resp, '区级 edit-info')
        self.assertEqual(AdoptionHallListing.objects.get(pet=self.v_pet).intro, '区级可改')

    def test_gov_city_can_still_edit_anywhere(self):
        resp = self.post_as(self.gov_city,
                            f'/api/business/adoptions/{self.v_pet.id}/edit-info/',
                            {'intro': '市级可改'})
        self.assert_ok(resp, '市级 edit-info')


class HospitalWriteScopeContractTest(BusinessTestBase):
    """源码级契约：**医院可达的写接口必须出现机构判据**。

    这一条是给「以后新增接口」用的 —— 本轮那个洞就是「同类接口都写了、
    只有这一个没写」，靠逐个接口人工比对很容易漏。

    **已知局限（第二十一轮变异 M5 实测）**：判据是**文本级**的（查字面量
    `institution_id`），所以把判据改写成**等价但不同字面量**的形式
    （如 `user.institution.pk != transfer.to_hospital.pk`）会被**误报**为「缺失」。
    这是刻意的保守取向：宁可让重构的人来显式确认一次，也不要放过真正的漏写。
    真被误报时，正确做法是在接口里保留 `_id` 比较（也更快，少一次关联查询），
    而不是放宽这条扫描。
    """

    @staticmethod
    def _roles_of(cb):
        f = cb
        for _ in range(6):
            for cell in getattr(f, '__closure__', None) or []:
                try:
                    v = cell.cell_contents
                except Exception:
                    continue
                if isinstance(v, (tuple, list)) and v and all(
                        isinstance(x, str) and x in ROLES for x in v):
                    return set(v)
            f = getattr(f, '__wrapped__', None)
            if f is None:
                break
        return None

    def _hospital_write_views(self):
        """全部「带 id 参数 + 允许医院 + 名字像写操作」的视图。"""
        pairs = []

        def walk(r, prefix=''):
            for p in r.url_patterns:
                if hasattr(p, 'url_patterns'):
                    walk(p, prefix + str(p.pattern))
                else:
                    pairs.append(('/' + (prefix + str(p.pattern)).lstrip('/'), p.callback))

        walk(get_resolver())
        out = []
        for pat, cb in pairs:
            if '/api/' not in pat:
                continue
            if not re.search(r'<(?:int:)?(?:pk|id|pet_id|txn_id)>', pat):
                continue
            roles = self._roles_of(cb)
            if not roles or 'hospital' not in roles:
                continue
            fn = getattr(cb, '__name__', '')
            # 只看写操作（detail / lifecycle 这类纯读接口另有读侧判据）
            if not re.search(r'(edit|receive|reject|review|confirm|update|delete|create|'
                             r'toggle|withdraw|reclaim|apply)', fn):
                continue
            out.append((pat, fn, cb))
        return out

    def test_every_hospital_write_view_has_institution_check(self):
        views = self._hospital_write_views()
        self.assertTrue(views, '没扫到任何医院写接口 —— 扫描逻辑失效了，不是「全部通过」')
        missing = []
        for pat, fn, cb in views:
            try:
                src = inspect.getsource(cb)
            except Exception:
                continue
            if 'institution_id' not in src:
                missing.append(f'{fn}  ({pat})')
        self.assertEqual(missing, [], f'以下医院写接口缺少机构判据：{missing}')

    def test_edit_info_checks_before_any_write(self):
        """判据必须**在落库之前** —— 否则会「报错但已写入」。

        `adoption_info_edit` 历史上就栽过一次同类问题（照片校验放在 `listing.save()`
        之后），所以这里用位置断言锁住顺序。
        """
        from business import views_adoption
        src = inspect.getsource(views_adoption.adoption_info_edit)
        m = re.search(r'pet\.hospital_id\s*!=\s*user\.institution_id', src)
        self.assertIsNotNone(m, '机构判据不见了（或被改成别的比较）')
        i_write = src.index('listing.save()')
        self.assertLess(m.start(), i_write, '机构判据必须早于 listing.save()')
