from functools import wraps

from django.conf import settings
from django.shortcuts import redirect
from django.contrib import messages
from django.http import JsonResponse


def role_required(*roles):
    """角色校验装饰器: 仅允许指定角色访问。

    对 API 请求（路径以 /api/ 开头）返回 JSON 错误，对页面请求重定向到登录页。
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                if request.path.startswith('/api/'):
                    return JsonResponse({'success': False, 'message': '请先登录'}, status=401)
                return redirect(settings.LOGIN_URL)
            if request.user.role not in roles:
                if request.path.startswith('/api/'):
                    return JsonResponse({'success': False, 'message': '无权访问该接口'}, status=403)
                messages.error(request, '无权访问该页面')
                return redirect('/')
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator
