"""
URL configuration for tnr_system project.
"""
from django.contrib import admin
from django.core.exceptions import (
    RequestDataTooBig, TooManyFieldsSent, TooManyFilesSent,
)
from django.http import JsonResponse
from django.http.multipartparser import MultiPartParserError
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.csrf import csrf_failure
from django.views.defaults import bad_request
from django.views.generic import TemplateView

from business.views_portal import (
    adopter_portal, adoption_hall_public, hospital_portal, shelter_portal,
    gov_portal,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('accounts.urls')),
    path('portal/', TemplateView.as_view(template_name='portal/index.html'), name='portal'),
    path('shelter/', shelter_portal, name='shelter_home'),
    path('hospital/', hospital_portal, name='hospital_home'),
    path('adopter/', adopter_portal, name='adopter_home'),
    path('adopter/hall/', adoption_hall_public, name='adoption_hall_public'),
    path('gov/', gov_portal, name='gov_home'),
    path('api/business/', include('business.urls')),
    path('api/supervision/', include('supervision.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)


# ---------------------------------------------------------------------------
# `/api/` 下的 400 一律回**可读 JSON**（第三十一轮）
# ---------------------------------------------------------------------------
# `DEBUG=False` 时，`SuspiciousOperation` 与 `MultiPartParserError` 都经
# `django.core.handlers.exception.get_exception_response(..., 400, exc)` →
# `resolver.resolve_error_handler(400)` 落到这里。
#
# 默认的 `django.views.defaults.bad_request` 渲染的是 **HTML**（`400.html`），
# 而前端 `TNR_API._postForm` 内部是 `await res.json()` —— 拿到 HTML 就抛错，
# 调用方没 `try/catch` 时表现为「**点提交没反应**」，比 500 更难排查。
# 这一族包括：
#   `RequestDataTooBig`   请求体超 `DATA_UPLOAD_MAX_MEMORY_SIZE`
#   `TooManyFilesSent`    文件数超 `DATA_UPLOAD_MAX_NUMBER_FILES`
#   `TooManyFieldsSent`   字段数超 `DATA_UPLOAD_MAX_NUMBER_FIELDS`
#   `MultiPartParserError` multipart 体本身损坏
#
# ⚠ **不要把 `str(exc)` 直接回给用户** —— 那是 Django 的英文内部措辞
#   （`Request body exceeded settings.DATA_UPLOAD_MAX_MEMORY_SIZE.`），
#   既不可读、又把框架实现细节暴露给客户端（已有测试钉住不许出现
#   `exceeded` / `DATA_UPLOAD_MAX_MEMORY_SIZE`）。所以按类型映射成中文。
_API_BAD_REQUEST_MESSAGES = (
    (RequestDataTooBig, '提交的数据过大，请减少照片数量或压缩后重试'),
    (TooManyFilesSent, '提交的照片数量过多，请减少数量后分批提交'),
    (TooManyFieldsSent, '提交的表单字段过多，请减少数量后分批提交'),
    (MultiPartParserError, '提交的表单数据无法解析，请重试'),
)


def api_aware_bad_request(request, exception=None, template_name='400.html'):
    """`/api/` 前缀 → JSON；其余路径 → 保持 Django 默认的 HTML 400 页。

    **只按路径前缀分流**：页面导航拿到的仍是可读的 HTML 错误页，
    接口调用拿到的是能 `res.json()` 的 JSON 信封。
    """
    if not (getattr(request, 'path', '') or '').startswith('/api/'):
        return bad_request(request, exception, template_name)

    message = '请求被拒绝'
    for exc_type, text in _API_BAD_REQUEST_MESSAGES:
        if isinstance(exception, exc_type):
            message = text
            break

    return JsonResponse({'success': False, 'data': None, 'message': message},
                        status=400)


handler400 = api_aware_bad_request


# ---------------------------------------------------------------------------
# `/api/` 下的 CSRF 失败也回**可读 JSON**（第三十二轮）
# ---------------------------------------------------------------------------
# ⚠ **不能用 `handler403`**：`CsrfViewMiddleware.process_view()` 是**直接返回**
#   `HttpResponseForbidden`（`self._reject(...)`），**不抛异常** ——
#   所以它既不经 `handler400` 也不经 `handler403`。唯一可用的钩子是
#   `settings.CSRF_FAILURE_VIEW`。
#
# 当前前端**够不到**这条路径（实测：所有 `/api/` 路由里只有 3 个没 `@csrf_exempt`
# —— `/api/me/`、`/api/business/adoptions/hall/`、`.../hall/<pk>/` ——
# 而前端对它们**只发 GET**，CSRF 只校验非安全方法；模板里也没有裸 `fetch` POST）。
#
# 但这是**潜在**风险面，且 `HTTPS=on` 之后会变活：届时 `request.is_secure()`
# 为真，CSRF 的 **Referer/Origin 校验首次生效**，而 `CSRF_TRUSTED_ORIGINS`
# 来自 env（可能为空）—— 一旦域名/端口对不上，POST 就会被 403 掉，
# 而默认的 403 是 **HTML**（`403_csrf.html`）→ 前端 `res.json()` 抛错 →
# 又是「点提交没反应」。所以提前收口。
def api_aware_csrf_failure(request, reason=''):
    """`/api/` 前缀 → JSON；其余路径 → 保持 Django 默认的 HTML 403 页。

    ⚠ 不回 `reason`：它是 Django 的内部判定原因（如
    `CSRF cookie not set.` / `Origin checking failed`），属实现细节。
    """
    if not (getattr(request, 'path', '') or '').startswith('/api/'):
        return csrf_failure(request, reason=reason)

    return JsonResponse(
        {'success': False, 'data': None,
         'message': '安全校验未通过，请刷新页面后重试'},
        status=403)
