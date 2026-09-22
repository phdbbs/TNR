"""请求体解析的安全入口。

**为什么必须收口**：`request.body` 会把整个请求体读进内存，并按
`CONTENT_LENGTH` 校验 `settings.DATA_UPLOAD_MAX_MEMORY_SIZE`（Django 默认 2.5MB），
超限抛 `RequestDataTooBig`。

它是 `SuspiciousOperation` 的子类 —— **不是** `ValueError` / `TypeError` 的子类，
所以「随手包一层 `except (ValueError, TypeError)`」是**假修**：异常会一路冒泡成
HTTP 400 的 **HTML 错误页**（`DEBUG` 下还带 Traceback），前端拿到非 JSON 响应体
→ `res.json()` 抛错 → **静默中断**，用户看到的就是「点了提交没反应」。

线上现场（第二十六轮）：微信内置浏览器反复
`POST /api/business/captures/create/ → 400（143 字节）`，gunicorn 日志为
`RequestDataTooBig: Request body exceeded settings.DATA_UPLOAD_MAX_MEMORY_SIZE`。
手机端上传的是 `capture="environment"` 拍的**原图**，单张 3~5MB 很常见，必然超限。

第二十九轮续查：`accounts/views.py::api_change_password` 有**同一个**缺陷
（`except (ValueError, TypeError)`）。实测 3MB 请求体返回 `400 text/html`、
标题 `RequestDataTooBig at /api/me/password/`、响应体含 Traceback ——
而同一体积下 `business` 侧已修的接口返回可读 JSON。故把能力下沉到这里，两处共用。
"""
import json

from django.core.exceptions import RequestDataTooBig


def is_api_request(request):
    """`/api/` 前缀判定 —— **全项目唯一口径**。

    前端 `TNR_API` 的三个封装（`_get` / `_post` / `_postForm`）内部都是
    `await res.json()`。所以**任何** `/api/` 响应只要不是 JSON，前端就会
    抛 `SyntaxError` —— 表现为「页面空白」或「点了没反应」，而服务端日志里
    只有一个 4xx/5xx，看起来完全正常。

    因此所有「错误响应形状」的收口点都必须按**同一个口径**分流：

        `handler400` / `handler404` / `handler500`
        `settings.CSRF_FAILURE_VIEW`
        `accounts.decorators.role_required` / `api_login_required`

    任何一处写成 `request.path_info`、`startswith('api/')`（漏斜杠）、
    `'/api' in path`（会误命中 `/api-docs/`），就会留下「有的接口回 JSON、
    有的回 HTML」的裂缝 —— 而且**测试很难发现**，因为裂缝只在特定路径上出现。

    ⚠ 用 `getattr` 兜底：`handler500` 拿到的 request 可能是残缺对象。
    """
    return (getattr(request, 'path', '') or '').startswith('/api/')


def read_json_body(request):
    """安全地把 `request.body` 解析出来 —— **任何失败都返回 `{}`**。

    ⚠ `multipart/form-data` 一律**不读** `request.body`：这类请求的有用数据在
    `request.POST` / `request.FILES`，它们**流式**解析、大文件落临时文件，且
    `DATA_UPLOAD_MAX_MEMORY_SIZE` 按官方定义「不含文件上传部分」计算 ——
    所以大图不会触发该限制。视图拿到 `{}` 后应回退到 `request.POST`。

    返回值可能是 `list` 等非 dict（合法 JSON），调用方自行判断；
    失败时**一定**是 `{}`。
    """
    content_type = (request.content_type or '').lower()
    if content_type.startswith('multipart/form-data'):
        return {}
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    except RequestDataTooBig:
        return {}
