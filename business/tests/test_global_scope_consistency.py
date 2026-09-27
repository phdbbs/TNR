"""「谁能看到全部区县」必须是**一个**判据（第四十二轮）。

**缺陷形态**：这个判断历史上在三处各写了一遍 ——
`accounts.middleware.DistrictScopeMiddleware`、`business.services.get_district_scope()`、
`business.services.get_district_filtered_queryset()`。其中两处对「账号没有挂区县」
给出了**相反**的解释：

- 中间件把 `user.district_id`（`None`）写进 `user_district_scope`
  → `get_district_scope()` 返回 `None`
  → 下游 `scope is not None` 为假 → 解释为「**可见全部**」；
- `get_district_filtered_queryset()` 看见 `district_id` 为空
  → `objects.none()` → 解释为「**什么都看不到**」。

同一账号因此同时处于两种状态：**列表是空的，写接口却全部放行**。
没有报错、没有 500，只是「看不到」和「能改」同时成立 ——
这是最难查的一类缺陷，也是新增 `platform_admin` 角色时最容易踩的雷。

本文件把「两个函数必须给出同一个结论」钉死，对**每一个**角色都验一遍，
而不是只验新增的那个（只验新增角色，会在别的角色上留下同样的洞）。
"""
from django.test import RequestFactory

from accounts.middleware import DistrictScopeMiddleware
from accounts.models import User
from business.models import Pet
from business.services import get_district_filtered_queryset, get_district_scope
from business.tests.base import BusinessTestBase, make_pet, make_user
from core.models import District
from core.scope import (EMPTY_DISTRICT_SCOPE, GLOBAL_SCOPE_ROLES,
                        has_global_district_scope, is_empty_scope)


class GlobalScopeRoleDeclarationTest(BusinessTestBase):
    """`GLOBAL_SCOPE_ROLES` 里的值必须是真实存在的角色。"""

    def test_every_global_role_exists_in_role_choices(self):
        declared = {value for value, _label in User.ROLE_CHOICES}
        for role in GLOBAL_SCOPE_ROLES:
            with self.subTest(role=role):
                self.assertIn(role, declared,
                              f'GLOBAL_SCOPE_ROLES 里的 {role} 不在 User.ROLE_CHOICES 中'
                              ' —— 拼错的话该角色会被静默当成「只有本区县」')

    def test_city_district_is_treated_as_global(self):
        """挂在「全市（市级）」区县下的账号同样全局可见（历史语义，不能改坏）。"""
        user = make_user(role='shelter', district=self.city)
        self.assertTrue(has_global_district_scope(user))

    def test_district_user_is_not_global(self):
        user = make_user(role='hospital', district=self.district_a)
        self.assertFalse(has_global_district_scope(user))

    def test_anonymous_is_not_global(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(has_global_district_scope(AnonymousUser()))
        self.assertFalse(has_global_district_scope(None))


class ScopeVerdictConsistencyTest(BusinessTestBase):
    """两个隔离函数对**同一个账号**必须给出一致的结论。"""

    def _verdicts(self, user):
        """返回 (列表是否可见全部, get_district_scope 是否表示全部)。"""
        request = RequestFactory().get('/x/')
        request.user = user
        # MiddlewareMixin 要求传 get_response；这里只用它的 process_request
        DistrictScopeMiddleware(lambda r: None).process_request(request)
        scope = get_district_scope(request)
        qs = get_district_filtered_queryset(Pet, user)
        # 用「造两条跨区县的宠物，看列表能取到几条」来判断是不是真的全局，
        # 而不是去猜 qs.query —— 后者会把「过滤到空」和「全局」都写成 SQL。
        return qs, scope

    def setUp(self):
        super().setUp()
        self.pet_a = make_pet(district=self.district_a, status='captured')
        self.pet_b = make_pet(district=self.district_b, status='captured')

    def _roles_and_districts(self):
        """覆盖全部角色 × 「挂区县 / 不挂区县 / 挂市级」三种形态。"""
        for role in ('platform_admin', 'gov_city', 'gov_district',
                     'shelter', 'hospital', 'adopter'):
            yield role, self.district_a
            yield role, None
            yield role, self.city

    def test_list_and_scope_agree_for_every_role(self):
        """列表能看到几条，与 scope 是不是「全局」，必须一致。"""
        for role, district in self._roles_and_districts():
            with self.subTest(role=role, district=getattr(district, 'code', None)):
                user = make_user(role=role, district=district)
                qs, scope = self._verdicts(user)
                seen = qs.count()
                scope_is_global = scope is None
                if scope_is_global:
                    self.assertEqual(seen, 2,
                                     f'{role}（区县={district}）: scope 说可见全部，'
                                     f'列表却只给了 {seen} 条')
                else:
                    self.assertLessEqual(seen, 1,
                                         f'{role}（区县={district}）: scope 说只可见本区，'
                                         f'列表却给了 {seen} 条')

    def test_platform_admin_sees_every_district(self):
        """平台管理员不挂区县，也必须能看到全部区县（这是它的定义）。"""
        user = make_user(role='platform_admin', district=None)
        qs, scope = self._verdicts(user)
        self.assertIsNone(scope, '平台管理员的 scope 必须是 None（= 全局）')
        self.assertEqual(qs.count(), 2)

    def test_role_without_district_gets_empty_scope_not_global(self):
        """「非全局角色 + 没挂区县」必须是**空范围**，不能被当成可见全部。

        这是本轮修掉的核心缺陷：早先该账号的 `scope` 是 `None`，而下游把
        `None` 解释成「可见全部」——于是**列表全空、写接口却全部放行**。
        """
        for role in ('shelter', 'hospital', 'adopter', 'gov_district'):
            with self.subTest(role=role):
                user = make_user(role=role, district=None)
                qs, scope = self._verdicts(user)
                self.assertEqual(scope, EMPTY_DISTRICT_SCOPE,
                                 f'{role} 没挂区县时 scope 必须是空哨兵，不是 None')
                self.assertEqual(qs.count(), 0)

    def test_empty_scope_rejects_every_target_district(self):
        """空范围账号：任何目标区县都算越权（不能被当成市级放行）。"""
        from django.test import RequestFactory as RF

        from supervision.views import _district_out_of_scope
        user = make_user(role='shelter', district=None)
        req = RF().get('/x/')
        req.user = user
        DistrictScopeMiddleware(lambda r: None).process_request(req)
        for target in (self.district_a.id, self.district_b.id, self.city.id):
            with self.subTest(target=target):
                self.assertTrue(_district_out_of_scope(req, target),
                                '没挂区县的账号不能被当成「可见全部」而放行')
