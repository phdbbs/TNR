from django.utils.deprecation import MiddlewareMixin

# ⚠ 判据在 core.scope，不要在这里重写一份：
#   早先这里认 gov_city、而 services.get_district_filtered_queryset 也自己判一遍，
#   两边对「账号没挂区县」的解释相反 —— 同一个账号既「列表全空」又「写接口全放行」。
from core.scope import resolve_user_district_scope


class DistrictScopeMiddleware(MiddlewareMixin):
    """
    区县数据隔离中间件。
    - 全局角色（platform_admin / gov_city）或所属区县为市级 (is_city=True):
      user_district_scope = None, 可见全部。
    - 其他已登录用户: user_district_scope = user.district_id, 仅可见所属区县数据。
    - 已登录但**没挂区县**的用户: user_district_scope = EMPTY_DISTRICT_SCOPE,
      什么都看不到。⚠ 早先这里写的是 `None`，与「可见全部」撞了同一个值 ——
      于是这类账号列表全空、写接口却全部放行。
    - 未登录用户: 不设置该属性。
    """

    def process_request(self, request):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        request.user_district_scope = resolve_user_district_scope(user)
        return None
