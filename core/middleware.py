"""操作审计中间件：把每一次业务写操作自动落成一条 AuditLog。

为什么用中间件而不是在每个视图里手写一行 `log_action(...)`：
业务写接口有近 40 个，逐个手写必然漏（漏掉的接口在日志页上表现为「没发生过」，
比没有日志更危险）。中间件保证**只要经过 `/api/` 的写请求就一定有记录**；
视图只需在「响应体里读不出归属区县」这类特殊场景用 `request.audit_*` 补充上下文。

判定「成功」不能只看状态码：本项目的 `json_fail()` 返回 400 + `{"success": false}`，
但也有视图用 200 返回失败。统一以响应体的 `success` 字段为准。
"""
import json
import logging

from core import audit

logger = logging.getLogger(__name__)

WRITE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')

# POST 但只读的接口（查询/预览/校验），不产生审计记录。
# 用路径后缀而非 URL name，避免中间件依赖 URL 反向解析。
READ_ONLY_POST_SUFFIXES = (
    '/blacklist/check/',
    '/captures/codes-preview/',
    '/geocode/reverse/',
    '/geocode/ip/',
)

# 不参与审计的路径前缀
IGNORED_PREFIXES = ('/admin/', '/static/', '/media/', '/__debug__/')


class AuditLogMiddleware:
    """记录 `/api/` 下所有写操作。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not self._should_log(request):
            return self.get_response(request)
        try:
            response = self.get_response(request)
        except Exception:
            # 视图抛异常时 process_response 不会再执行，这里补记一条失败日志后原样上抛
            self._record(request, None)
            raise
        self._record(request, response)
        return response

    # --------------------------------------------
    # 判定
    # --------------------------------------------
    @staticmethod
    def _should_log(request):
        if request.method not in WRITE_METHODS:
            return False
        path = request.path or ''
        if not path.startswith('/api/'):
            return False
        if path.startswith(IGNORED_PREFIXES):
            return False
        if path.endswith(READ_ONLY_POST_SUFFIXES):
            return False
        if getattr(request, 'audit_skip', False):
            return False
        user = getattr(request, 'user', None)
        return bool(user is not None and getattr(user, 'is_authenticated', False))

    @staticmethod
    def _payload(response):
        """读取 JSON 响应体；非 JSON / 流式响应返回 None。"""
        if response is None or not hasattr(response, 'content'):
            return None
        try:
            return json.loads(response.content.decode('utf-8'))
        except (ValueError, UnicodeDecodeError, AttributeError):
            return None

    @staticmethod
    def _success(response, payload):
        if response is None:
            return False
        if isinstance(payload, dict) and 'success' in payload:
            return bool(payload['success'])
        return 200 <= response.status_code < 400

    # --------------------------------------------
    # 记录
    # --------------------------------------------
    def _record(self, request, response):
        # `audit_skip` 必须在**视图执行后**再判一次：视图是在自己内部才知道
        # 「这次写不值得留痕」（例如消息已读回执）。只在 `_should_log` 里判，
        # 视图里设的 `request.audit_skip` 永远不会生效（请求进来时还没有它）。
        if getattr(request, 'audit_skip', False):
            return
        try:
            payload = self._payload(response)
            response_data = payload.get('data') if isinstance(payload, dict) else None
            request_data = getattr(request, 'audit_payload', None)
            audit.record(
                request,
                success=self._success(response, payload),
                response_data=response_data,
                request_data=request_data,
                obj=getattr(request, 'audit_object', None),
                action_flag=getattr(request, 'audit_action_flag', None),
                module=getattr(request, 'audit_module', None),
                object_type=getattr(request, 'audit_object_type', None),
                object_id=getattr(request, 'audit_object_id', None),
                object_repr=getattr(request, 'audit_object_repr', None),
                summary=getattr(request, 'audit_summary', None),
                detail=getattr(request, 'audit_detail', ''),
                district=getattr(request, 'audit_district', None),
            )
        except Exception:  # noqa: BLE001 —— 审计是旁路，绝不外抛
            logger.exception('审计中间件记录失败：%s', getattr(request, 'path', ''))
