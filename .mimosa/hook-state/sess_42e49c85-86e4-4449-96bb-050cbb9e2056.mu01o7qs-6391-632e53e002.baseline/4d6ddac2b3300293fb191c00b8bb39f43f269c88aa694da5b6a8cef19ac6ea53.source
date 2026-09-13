from django.utils.deprecation import MiddlewareMixin


class DistrictScopeMiddleware(MiddlewareMixin):
    """
    区县数据隔离中间件。
    - 市级政府管理员 (gov_city) 或所属区县为市级 (is_city=True): user_district_scope = None, 可见全部。
    - 其他已登录用户: user_district_scope = user.district_id, 仅可见所属区县数据。
    - 未登录用户: 不设置该属性。
    """

    def process_request(self, request):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        if user.role == 'gov_city':
            request.user_district_scope = None
        elif user.district_id and user.district and user.district.is_city:
            # 所属区县为市级的用户（如捕捉点操作员）可见全部数据
            request.user_district_scope = None
        else:
            request.user_district_scope = user.district_id
        return None
