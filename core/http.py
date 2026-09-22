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
    """安全地把 `request.body` 解析出来 —— **任何情况都返回 `dict`**。

    ⚠ `multipart/form-data` 一律**不读** `request.body`：这类请求的有用数据在
    `request.POST` / `request.FILES`，它们**流式**解析、大文件落临时文件，且
    `DATA_UPLOAD_MAX_MEMORY_SIZE` 按官方定义「不含文件上传部分」计算 ——
    所以大图不会触发该限制。视图拿到 `{}` 后应回退到 `request.POST`。

    ⚠⚠ **顶层非 dict 的合法 JSON 一律归一成 `{}`（第三十五轮收口）。**

    此前这里原样返回 `json.loads()` 的结果，docstring 写的是「返回值可能是
    `list` 等非 dict（合法 JSON），**调用方自行判断**」。实测这条约定**没有
    一个调用方遵守** —— 约 30 处调用点清一色是

        data = parse_json_body(request)      # 或 read_json_body(request)
        xxx = data.get('xxx')                # ← 直接当 dict 用

    于是 `[1,2,3]` / `"str"` / `null` 这种顶层非 dict 的 body 会让
    `.get()` 炸 `AttributeError` → **HTTP 500**。枚举实测（第三十五轮）：

        17 个接口 × 3 种非 dict body ≈ 51 处 500
        异常形态：'list' / 'str' / 'NoneType' object has no attribute 'get'
                  （还有 'items' 变体）

    更糟的是 `system_config` 这类**边解析边写库**的接口：`for k, v in data.items()`
    在 list 上直接抛错，而在 dict 上会把**任意 key** 写进 `SystemConfig` ——
    同一族问题的两个面。

    **为什么不改调用方而要在这里归一**：`request.body` 的容错逻辑（
    `RequestDataTooBig` / 坏 JSON / multipart）**只应该有一份**，第二十六轮
    就是因为两份实现漂移才漏掉 `RequestDataTooBig`。归一化放在这里，所有
    调用方（含未来新增的）自动受益；散到 30 处去加 `isinstance` 必然再次漂移。

    **语义安全性**：现有调用点全是 `data.get(...)` 的 dict 用法，没有任何一处
    依赖「顶层数组」这种形状 —— 合法请求体本来就是 JSON 对象。
    """
    content_type = (request.content_type or '').lower()
    if content_type.startswith('multipart/form-data'):
        return {}
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    except RequestDataTooBig:
        return {}
    return data if isinstance(data, dict) else {}


def body_str(data, key, default=''):
    """从**请求体** dict 里安全地取一个字符串。

    ⚠ 为什么不能写 `(data.get('name') or '').strip()`（第三十五轮）：
    `or ''` 只挡 **falsy** 值（`None` / `''` / `0` / `[]` / `{}`）。
    `{"name": [1, 2, 3]}` / `{"name": {"$ne": null}}` / `{"name": 123}`
    全都是 **truthy** → 直接 `.strip()` → `AttributeError: 'list' object has
    no attribute 'strip'` → **HTTP 500**。枚举实测命中 4 个接口。

    ⚠ 也**不能**用 `str(value)` 强转来「修」：`str({'$ne': None})` 得到字面量
    `"{'$ne': None}"`，会被**当成真实业务数据写进库**（区县名、用户名……）。
    「静默写入垃圾」比 500 更难查 —— 500 至少留下 Traceback。

    非字符串一律返回 `default`（= 视为未提供），由调用方的必填校验给出
    可读的 400。**不 strip** —— 要不要 strip 由调用方决定，保持与原写法等价。

    放在 `core.http` 而不是 `business.services`：它是**纯类型守卫**，
    不依赖任何业务逻辑，`accounts` 侧也要用，下沉可避免无谓的跨 app 依赖。
    """
    value = data.get(key)
    return value if isinstance(value, str) else default


def body_dict(data, key):
    """从**请求体** dict 里安全地取一个**子 dict**。

    ⚠ `data.get('sterilization', {}) or {}` 挡不住非 dict：`{"sterilization":
    "abc"}` → `"abc"` 是 truthy → 原样返回 → 下游 `ster.get('xxx')` 炸
    `AttributeError: 'str' object has no attribute 'get'` → **500**。
    （`or {}` 只对 **falsy** 值生效。）

    非 dict 一律返回 `{}`，下游按「未提供」处理。
    """
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def body_list(data, key):
    """从**请求体** dict 里安全地取一个**列表**。

    ⚠ 同 `body_dict`：`{"items": {"a": 1}}` 会让 `for item in items` 去遍历
    **字典的键**，随后 `item.get(...)` 炸 `AttributeError: 'str' object has
    no attribute 'get'` —— 报错点在**循环体内**，比在参数读取处更难定位。

    非 list 一律返回 `[]`，下游按「未提供」处理。
    """
    value = data.get(key)
    return value if isinstance(value, list) else []
