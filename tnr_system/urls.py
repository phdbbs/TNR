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
