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
from django.http import JsonResponse


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


# ============================================
# 响应体：统一信封 + 键命名补齐（snake_case ↔ camelCase）
# ============================================
#: `/api/` 响应的统一信封：`{'success': bool, 'data': ..., 'message': str}`。
#: 前端 `TNR_API` 三个封装都按这个形状解析，所以**只有一份实现**。
def json_ok(data=None, message='操作成功'):
    """成功 JSON 响应。

    ⚠⚠ **`data` 一律过 `with_camel_keys()`（第三十六轮收口）。**
    本项目约定「返回给前端的每个记录字典同时含 snake_case 与 camelCase
    两套键」—— 模型序列化走 `serialize_instance` 会自动补，但**手工聚合的
    接口**（dashboard / ledger / users / institutions / materials / me …）
    全靠人手写，实测 78 条路由 × 5 角色里漏了 48 处，且**全在嵌套层**。

    把补齐放在这里而不是逐个接口去改，理由与 `read_json_body` 的归一化同源：
    **归一化点只能有一个**。分散到十几个接口去写，必然出现「有的接口补了、
    有的没补」这种最难查的形态 —— 而且新加接口时**没人会记得补**。

    ⚠ 直连 `JsonResponse` 的接口会**绕过**这里。`/api/` 下必须用本函数
    （`accounts.api_me` 曾经就是裸 `JsonResponse`，因此 `/api/me/` 的
    `districtId` / `districtName` 一直是 undefined）。
    """
    return JsonResponse({
        'success': True, 'data': with_camel_keys(data), 'message': message})


def json_fail(message='操作失败', data=None, status=400):
    """失败 JSON 响应（`data` 同样过 `with_camel_keys()`，见 `json_ok`）。"""
    return JsonResponse({
        'success': False, 'data': with_camel_keys(data), 'message': message},
        status=status)


def to_camel_key(snake):
    """`snake_case` → `camelCase`。

    ⚠ **全项目唯一实现**。`serialize_instance` 里原本另有一份同名内部函数
    `_to_camel`，注释写着「与 `with_camel_keys` 同一套规则」—— 但那只是
    **一句注释**，两处代码各写各的。规则一旦要改（例如处理连续下划线、
    数字前缀），必然只改一处 → 同一个字段在模型序列化与手工聚合两条路径上
    得到**不同的驼峰名**，前端按其中一种读，另一条路径就是 undefined。
    第三十六轮把它们合并到这里。
    """
    parts = str(snake).split('_')
    if len(parts) == 1:
        return parts[0]
    return parts[0] + ''.join(p.title() for p in parts[1:])


def with_camel_keys(data):
    """**递归地**给字典/列表补一份 camelCase 别名，让两种命名都能取到值。

    本项目约定「返回给前端的每个记录字典同时含 snake_case 与 camelCase
    两套键」。模型序列化走 `serialize_instance` 会自动补；**手工聚合的接口**
    必须显式补齐 —— 政府端读 snake_case、捕捉端读 camelCase，缺哪一套哪一端
    就静默出问题：前端读 `r.ledgerNo` 拿到 undefined 时，**编号列整列空白、
    编号点不开档案，且不报任何错**。

    ⚠⚠ **必须递归（第三十六轮）**。原实现只补**顶层**键，嵌套的值原样透传，
    于是所有嵌套结构里的 snake 键都没有孪生。实测枚举 78 条路由 × 5 角色，
    命中 48 处「前端读 camel、接口只给 snake」，**全部落在嵌套层**：

    - `pet_brief()` 返回的 snake-only 字典被塞进 `detail.pet` /
      `data.pet_brief` → `photoCapture` / `districtName` / `shelterName` /
      `hospitalName` / `districtId` 全缺；
    - `pet_archive_records` 的 `records[].detail` 里的 `contactPerson` /
      `geoAddress` / `propertyName` / `groupPhoto` / `petCodes` 全缺；
    - 更刺眼的是**同一响应里两套口径**：`/api/business/captures/` 的外层记录
      给的是 camel（`canDelete`），而它的嵌套 `transferState` 给的是 snake
      （`can_delete`）—— 手工拼装没有统一口径。

    ⚠ **不改入参**：返回的是新建的 dict/list，不会污染调用方传进来的对象
    （`pet_brief()` 的返回值被多处共享，就地改写会串味）。

    ⚠ **副作用提醒**：若某个响应字典是「枚举值 → 数字」的**聚合映射**
    （如 `pet_status_distribution`），补齐后它会同时含 `in_transit` 与
    `inTransit`。**按 key 直接取值不受影响**；但**遍历它的键**会拿到两份
    （图表会翻倍）。当前没有任何前端代码遍历这类映射；若将来要遍历，
    请在那一处显式只取一种命名，而**不要**为了它把这里改回非递归。

    ⚠ 非字符串键（如 `{1: 'a'}`）跳过：JSON 里键必是字符串，但服务层可能
    在序列化前传入 int 键的字典。
    """
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            out[key] = with_camel_keys(value)
        for key, value in list(out.items()):
            if not isinstance(key, str):
                continue
            camel = to_camel_key(key)
            if camel != key and camel not in out:
                out[camel] = value
        return out
    if isinstance(data, (list, tuple)):
        return [with_camel_keys(v) for v in data]
    return data
