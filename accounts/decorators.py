from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect

from core.http import is_api_request


def api_unauthorized(message='请先登录'):
    """未登录时给 `/api/` 的响应（401 JSON）。

    提成共用函数：`role_required` 与 `api_login_required` **必须回同一个形状**，
    否则同一个前端（`res.success` + `res.message`）会在两条路径上读到不同的东西。
    """
    return JsonResponse({'success': False, 'message': message}, status=401)


def api_forbidden(message='无权访问该接口'):
    """角色不符时给 `/api/` 的响应（403 JSON）。同上，形状必须唯一。"""
    return JsonResponse({'success': False, 'message': message}, status=403)


def role_required(*roles):
    """角色校验装饰器: 仅允许指定角色访问。

    对 API 请求（路径以 /api/ 开头）返回 JSON 错误，对页面请求重定向到登录页。

    ⚠ 本装饰器**已自带未登录判断**（回 401 JSON）。所以它与 `@login_required`
    叠加时，**必须写在外层**：

        @role_required('shelter')      # ← 外层：未登录在这里就被拦成 JSON
        @login_required                # ← 内层：永远走不到（被外层短路）
        def view(request): ...

    写反了（`@login_required` 在外）就会先 302 到登录页 —— 见
    `api_login_required` 的说明。
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                if is_api_request(request):
                    return api_unauthorized()
                return redirect(settings.LOGIN_URL)
            if request.user.role not in roles:
                if is_api_request(request):
                    return api_forbidden()
                messages.error(request, '无权访问该页面')
                return redirect('/')
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def api_login_required(view_func):
    """「只要求登录、不限角色」的接口专用装饰器：`/api/` 未登录回 **401 JSON**。

    ⚠ 为什么不能直接用 Django 自带的 `login_required`（第三十三轮实测）：

    它未登录时走 `redirect_to_login()` → **302**。而前端三个封装
    （`TNR_API._get` / `_post` / `_postForm`）都是 `await res.json()`，
    且 `fetch` 的默认 `redirect` 是 **`'follow'`** —— 于是：

        302 → fetch 自动跟随到 /login/ → 最终 status **200**、body 是 **HTML 登录页**
            → `res.json()` 抛 SyntaxError

    ⚠ 关键点：**这不是「非 2xx 返回 []」那条路径**（跟随后是 200）。
    所以 `_get` 也会抛错，不只是 `_post`。后果：

    - `_get` 的调用点（下拉数据源）→ 渲染函数中断，**页面空白**；
    - `_post` / `_postForm` 的调用点（提交）→ **「点了没反应」**。

    实测的三处（第三十三轮）：
      `/api/me/password/`、`/api/supervision/institutions/`、
      `/api/supervision/districts/` —— 都是「所有登录用户可用」，
      所以当时只写了 `@login_required`，漏掉了 `/api/` 的 JSON 口径。

    用法与 `role_required` 保持一致（写在最外层）：
        @csrf_exempt
        @api_login_required
        def view(request): ...
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if is_api_request(request) and not request.user.is_authenticated:
            return api_unauthorized()
        return login_required(view_func)(request, *args, **kwargs)
    return _wrapped
