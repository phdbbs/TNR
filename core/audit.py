"""业务操作审计日志（AuditLog）的解析与写入。

两条必须守住的约定：

1. **区县口径 = 业务记录的归属区县，不是操作人的区县。**
   现场两个捕捉点操作员都挂在「全市（市级）」下，而他们登记的捕捉单/转运单
   归属的是襄城区/樊城区。若按操作人区县归属，**本区县政府在自己的日志页里
   看不到本区的操作**——这正是原实现（读 admin `LogEntry`、按 `user__district_id`
   过滤）的第二个缺陷。
2. **审计永远不能成为业务请求的故障点。**
   所有解析与写库都包在 try/except 里，失败只记 logger，绝不上抛。
"""
import ipaddress
import logging

from core.models import AuditLog, District

logger = logging.getLogger(__name__)

ACTION_ADD = AuditLog.ACTION_ADD
ACTION_CHANGE = AuditLog.ACTION_CHANGE
ACTION_DELETE = AuditLog.ACTION_DELETE

ACTION_LABELS = dict(AuditLog.ACTION_CHOICES)

# 路径前缀 → (业务模块, 对象类型)。**长前缀必须排在短前缀前面**，
# 否则 `/api/business/materials/` 会先被更短的规则抢走。
MODULE_BY_PREFIX = [
    ('/api/business/captures/', '捕捉登记', '捕捉单'),
    ('/api/business/owner-returns/', '主人领回', '领回记录'),
    ('/api/business/transfers/', '转运下发', '转运单'),
    ('/api/business/treatments/', '诊疗记录', '诊疗单'),
    ('/api/business/materials/', '物料台账', '物料记录'),
    ('/api/business/releases/', '放养闭环', '放养记录'),
    ('/api/business/adoptions/', '领养业务', '领养记录'),
    ('/api/business/checkins/', '回访打卡', '回访记录'),
    ('/api/business/blacklist/', '黑名单', '黑名单'),
    ('/api/business/euthanasia/', '安乐死处置', '处置记录'),
    ('/api/business/portal/', '门户消息', '站内消息'),
    ('/api/supervision/institutions/', '机构管理', '机构'),
    ('/api/supervision/districts/', '区县管理', '区县'),
    ('/api/supervision/users/', '用户管理', '账号'),
    ('/api/supervision/config/', '系统配置', '系统参数'),
    ('/api/accounts/', '账户安全', '账户'),
]

# URL 末段 → (操作类型, 动作词)。前端 gov 端已有 1/2/3 → 新增/修改/删除 的映射表，
# 这里沿用同一套 action_flag，避免两端各维护一份。
ACTION_BY_SEGMENT = {
    'create': (ACTION_ADD, '新增'),
    'register': (ACTION_ADD, '登记'),
    'purchase': (ACTION_ADD, '采购入库'),
    'dispatch': (ACTION_ADD, '下发'),
    'apply': (ACTION_ADD, '提交申请'),
    'update': (ACTION_CHANGE, '修改'),
    'edit': (ACTION_CHANGE, '修改'),
    'edit-info': (ACTION_CHANGE, '修改'),
    'adjustment': (ACTION_CHANGE, '库存调整'),
    'receive': (ACTION_CHANGE, '签收'),
    'confirm': (ACTION_CHANGE, '确认'),
    'confirm-claim': (ACTION_CHANGE, '确认领出'),
    'reject': (ACTION_CHANGE, '退回'),
    'withdraw': (ACTION_CHANGE, '撤回'),
    'review': (ACTION_CHANGE, '审核'),
    'reclaim': (ACTION_CHANGE, '回收'),
    'toggle': (ACTION_CHANGE, '启停'),
    'body-receive': (ACTION_CHANGE, '遗体接收'),
    'owner-return': (ACTION_CHANGE, '主人领回'),
    'read': (ACTION_CHANGE, '标记已读'),
    'delete': (ACTION_DELETE, '删除'),
}

# 生成 object_repr 时优先取用的业务标识字段（越靠前越具体）。
REPR_KEYS = ('ledger_no', 'pet_code', 'code', 'number', 'username', 'title', 'name')
# 便于在响应体里定位到「刚写的那条记录」。
DISTRICT_KEYS = ('district_id', 'districtId')

_MAX_DEPTH = 4


# ============================================
# 请求侧解析
# ============================================
def client_ip(request):
    """取来源 IP。

    ``X-Forwarded-For`` 是客户端可伪造的，只有在 ``REMOTE_ADDR`` 是回环地址
    （即请求确实来自本机 nginx 反代）时才采信它，否则一律用 ``REMOTE_ADDR``。
    """
    if request is None:
        return None
    remote = (request.META.get('REMOTE_ADDR') or '').strip()
    candidate = remote
    if _is_loopback(remote):
        forwarded = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
        candidate = forwarded or remote
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def _is_loopback(value):
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def describe(request):
    """按 URL 推导出这次操作的模块、动作与对象类型。"""
    path = getattr(request, 'path', '') or ''
    module, object_type = '', ''
    for prefix, mod_name, obj_name in MODULE_BY_PREFIX:
        if path.startswith(prefix):
            module, object_type = mod_name, obj_name
            break

    segments = [s for s in path.split('/') if s]
    tail = segments[-1] if segments else ''
    if tail.isdigit() and len(segments) >= 2:
        tail = segments[-2]
    flag, verb = ACTION_BY_SEGMENT.get(tail, (None, ''))
    if flag is None:
        # 未登记的动作词：写操作统一按「修改」记，并在动作词里保留原段名，
        # 便于日后发现漏登记（而不是静默归成「修改」）。
        flag, verb = ACTION_CHANGE, tail or '操作'

    object_id = ''
    for seg in segments:
        if seg.isdigit():
            object_id = seg
            break

    return {
        'module': module or '其他',
        'object_type': object_type,
        'action_flag': flag,
        'verb': verb,
        'object_id': object_id,
    }


# ============================================
# 载荷侧解析
# ============================================
def _walk(payload, depth=0):
    """深度优先遍历嵌套的 dict/list，产出其中的 dict 节点。"""
    if depth > _MAX_DEPTH or payload is None:
        return
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _walk(value, depth + 1)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _walk(value, depth + 1)


def sniff_district_id(*payloads):
    """在若干嵌套载荷里找第一个非空 district_id / districtId。"""
    for payload in payloads:
        for node in _walk(payload):
            for key in DISTRICT_KEYS:
                value = node.get(key)
                if isinstance(value, int) and value > 0:
                    return value
    return None


def sniff_object_repr(*payloads):
    """在若干嵌套载荷里挑一个像「业务标识」的字符串，用于日志可读性。

    优先取台账/编号类字段；取不到就返回空串（宁可为空，也不要编造内容）。
    """
    for payload in payloads:
        for node in _walk(payload):
            for key in REPR_KEYS:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if key == 'pet_code' and isinstance(value, list) and value:
                    first = value[0]
                    if isinstance(first, str) and first.strip():
                        return first.strip()
    return ''


# ============================================
# 区县解析
# ============================================
def resolve_district(request=None, obj=None, response_data=None, request_data=None):
    """解析这次操作的**业务归属区县**。

    顺序：目标对象 → 视图显式指定 → 响应体 → 请求体 → 操作员区县。
    「视图显式指定」(`request.audit_district`) 排在请求体之前，是因为请求体里的
    `district_id` 是前端可伪造的，而视图里的值经过 `resolve_district_scope()` 校验。
    响应体排在请求体之前同理：它是服务端落库后的真实值。
    """
    district = getattr(obj, 'district', None) if obj is not None else None
    if district is not None:
        return district

    explicit = getattr(request, 'audit_district', None) if request is not None else None
    if explicit is not None:
        return explicit

    district_id = sniff_district_id(response_data, request_data)
    if district_id:
        found = District.objects.filter(pk=district_id).first()
        if found is not None:
            return found

    user = getattr(request, 'user', None) if request is not None else None
    if user is not None and getattr(user, 'is_authenticated', False):
        user_district = getattr(user, 'district', None)
        # 操作员挂在「全市（市级）」时不作为归属（否则本区县政府看不到本条日志）
        if user_district is not None and not user_district.is_city:
            return user_district
    return None


# ============================================
# 写入
# ============================================
def write_log(**fields):
    """写一条审计记录；任何异常都吞掉（审计不能拖垮业务请求）。"""
    try:
        return AuditLog.objects.create(**fields)
    except Exception:  # noqa: BLE001 —— 审计是旁路，绝不外抛
        logger.exception('写入操作日志失败：%s', fields.get('summary', ''))
        return None


def record(request, *, success=True, response_data=None, request_data=None,
           obj=None, action_flag=None, module=None, object_type=None,
           object_id=None, object_repr=None, summary=None, detail='',
           district=None, **extra):
    """把一次业务写操作落成审计记录。

    未显式传入的字段按 URL 推导；视图可用 ``request.audit_*`` 覆盖（见中间件）。
    """
    user = getattr(request, 'user', None)
    if user is None or not getattr(user, 'is_authenticated', False):
        return None

    base = describe(request)
    action_flag = action_flag or base['action_flag']
    module = module or base['module']
    object_type = object_type or base['object_type']
    object_id = object_id if object_id is not None else base['object_id']
    object_repr = object_repr or sniff_object_repr(response_data, request_data)

    if district is None:
        district = resolve_district(
            request=request, obj=obj, response_data=response_data, request_data=request_data)

    if summary is None:
        summary = f"{module}·{base['verb']}"
        if object_repr:
            summary = f'{summary} {object_repr}'

    if not success:
        detail = ('失败：' + (detail or '')) if detail else '失败'
        summary = f'{summary}（失败）'

    user_name = (user.get_full_name() or '').strip() or user.username
    return write_log(
        user=user,
        user_name=user_name[:64],
        role=getattr(user, 'role', '') or '',
        district=district,
        district_name=(district.name if district is not None else '')[:50],
        module=module[:64],
        action_flag=action_flag,
        object_type=object_type[:64],
        object_id=str(object_id or '')[:64],
        object_repr=str(object_repr or '')[:200],
        summary=summary[:255],
        detail=detail or '',
        method=(getattr(request, 'method', '') or '')[:8],
        path=(getattr(request, 'path', '') or '')[:255],
        success=bool(success),
        ip=client_ip(request),
        **extra,
    )
