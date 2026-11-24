"""平台端「入院单据维护」。

与 `doc_maintenance.py`（出院单据维护）对称，但入院侧的纠错语义各不相同，
不能照搬出院的「状态回退」一套：

* **捕捉单 / 现场领回单** —— 两级：`生效 → 草稿 → 改错 → 重新提交`。
  草稿不是终态：单据本身、照片、编号、主人信息全部保留，只是**业务效力暂停**
  （捕捉单宠物逻辑失效、领回单宠物回领回前状态），改完可再次提交生效。
* **转运单** —— 没有草稿。只有「尚未被医院签收」的单可退回（宠物回
  `in_transit`，重新选宠物/医院再转）；一旦医院已签收、动物已入院，就不允许退。
* **诊疗单** —— 只在列表展示，**不提供任何回退动作**：诊疗完成意味着手术、
  处方已执行并扣过库存，回退会破坏医疗记录与库存台账（见 `TRANSITIONS` 注释）。

⚠ 所有函数都**必须在调用方的 `transaction.atomic()` 里**执行。
"""
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Capture, OwnerReturn, Pet, Transfer, Treatment
from business.services import (
    json_fail, json_ok, parse_json_body, recalc_capture_status,
    recalc_captures_for_transfer,
)
from business.services_treatment import dispose_for_discharge, undo_discharge
from core.http import body_str

#: 单据类型 → 展示名（页面与审计摘要共用）。
DOC_LABELS = {
    'capture': '捕捉单',
    'transfer': '转运单',
    'owner_return': '现场领回单',
    'treatment': '诊疗单',
}

#: 允许的状态迁移白名单：`{类型: {当前状态: (可变成的状态, ...)}}`。
#:
#: 捕捉单/领回单的 `draft → effective` 是**重新提交**，与 `effective → draft`
#: 的退回合起来构成纠错闭环；转运单的 `pending/rejected/void → void` 是「退回
#: 重转」（宠物回在途）。诊疗单**故意是空表** —— 它不是没有状态可退，而是
#: 回退它会连带撕毁手术/处方执行记录与库存流水，比填错单更严重。
TRANSITIONS = {
    'capture': {
        'pending': ('draft',),
        'partial': ('draft',),
        'completed': ('draft',),
        'draft': ('effective',),
        'void': (),
    },
    'transfer': {
        'pending': ('void',),
        'rejected': ('void',),
        # 已签收/部分签收/已撤回的都不再提供退回：签收入院的动物不能倒回在途。
        'received': (),
        'partial': (),
        'void': (),
    },
    'owner_return': {
        'effective': ('draft',),
        'draft': ('effective',),
    },
    # 诊疗单只读：任何状态都没有可迁移项，前端因此渲染「无需维护」。
    'treatment': {},
}
#: 「重新提交」目标（与「退回」目标区分开，便于前端选按钮样式/文案）。
RESUBMIT_TARGET = 'effective'
REASON_MAX = 200

_MODELS = {
    'capture': Capture,
    'transfer': Transfer,
    'owner_return': OwnerReturn,
    'treatment': Treatment,
}


class MaintenanceError(Exception):
    """校验失败 —— 视图折成 `json_fail`，不让它冒成 500。"""


# ============================================
# 状态口径
# ============================================
def status_labels(doc_type):
    """该类型所有可展示状态 key → 中文（列表筛选/行数据/错误提示共用）。"""
    if doc_type == 'capture':
        # `pending/partial/completed/void` 的含义在入院侧与捕捉点列表一致，
        # `draft` 是本模块新增；这里显式给全，避免漏 network 的字典读者。
        labels = dict(Capture.STATUS_CHOICES)
        return labels
    if doc_type == 'treatment':
        return dict(Treatment.STATUS_CHOICES)
    return dict(_MODELS[doc_type].STATUS_CHOICES)


def current_status(doc, doc_type):
    """单据当前状态 key。捕捉单读派生值（草稿单 status 缓存已写 draft）。"""
    if doc_type == 'capture':
        return doc.status or 'pending'
    if doc_type == 'treatment':
        return doc.status or 'in_progress'
    if doc_type == 'owner_return':
        return doc.status or 'effective'   # 存量行 status 缺省 = 已生效
    return doc.status


def status_text(doc, doc_type):
    key = current_status(doc, doc_type)
    return status_labels(doc_type).get(key, key)


def allowed_targets(doc, doc_type):
    key = current_status(doc, doc_type)
    # 捕捉单的派生状态可能因为转运变化而与缓存列不同（缓存滞后），维护页
    # 一律以传入的 doc 为准，这里不再重算，避免列表显示与点击后校验打架。
    return TRANSITIONS.get(doc_type, {}).get(key, ())


# ============================================
# 回退 / 重新提交执行
# ============================================
def _codes(text):
    return [c.strip() for c in (text or '').split(',') if c.strip()]


def _pets_by_code(codes, district_id=None):
    """按编号取宠物（编号全局唯一），可选限定区县。"""
    if not codes:
        return []
    qs = Pet.objects.filter(code__in=codes)
    if district_id:
        qs = qs.filter(district_id=district_id)
    return list(qs)


def _capture_to_draft(doc, user):
    """捕捉单退回草稿：单据保留、宠物逻辑失效（不在待转运列表）。

    ⚠ 只失效**本单仍未转运**的宠物（`in_transit`）。已被转运/签收的动物由
    别的单据负责，把它们标成 is_deleted 会让一宠一档里这段历史凭空消失。
    「图片和编号保留」= 不改 Pet 的任何照片/编号字段，只打删除标记。
    """
    if doc.is_deleted:
        raise MaintenanceError('该捕捉单已作废，无法退回草稿')
    codes = _codes(doc.pet_codes)
    if not codes:
        raise MaintenanceError('该捕捉单没有动物编号，无法退回草稿')
    # ⚠ 不能只看 pet.status != 'in_transit'：转运单「待签收」时宠物仍是
    # in_transit（签收才改状态），但动物已经被转运单占用。若只按状态判，
    # 一张挂着待签收转运单的捕捉单也能退回草稿，草稿重提时动物却又被占用 ——
    # 下游链被撕断。必须按「是否被未结转运单占用」这一真源判。
    busy = set(_busy_codes(codes))
    if busy:
        raise MaintenanceError(
            f'本单有 {len(busy)} 只动物已被转运单占用，'
            '请先退回对应转运单，再退回捕捉单草稿')
    pets = _pets_by_code(codes, doc.district_id)
    moved = [p for p in pets if p.status != 'in_transit']
    if moved:
        raise MaintenanceError(
            f'本单有 {len(moved)} 只动物已进入后续流程，'
            '请先处理对应单据，再退回捕捉单草稿')
    doc.status = 'draft'
    doc.save(update_fields=['status', 'updated_at'])
    for pet in pets:
        if pet.is_deleted:
            continue
        pet.is_deleted = True
        pet.save(update_fields=['is_deleted'])
    return f'{doc.ledger_no or doc.id} 已退回草稿，{len(pets)} 只动物暂时失效，照片与编号保留'


def _capture_resubmit(doc, user):
    """草稿重新提交：宠物恢复，单据回「待转运」，交给捕捉点继续办理。"""
    codes = _codes(doc.pet_codes)
    pets = _pets_by_code(codes, doc.district_id)
    missing = len(codes) - len(pets)
    if missing:
        raise MaintenanceError(f'有 {missing} 只动物档案缺失，无法重新提交')
    freed = set(_busy_codes(codes))
    conflict = [p.code for p in pets if p.code in freed and not p.is_deleted]
    if conflict:
        raise MaintenanceError(
            f'{conflict[0]} 已被其它转运单占用，无法随本单重新提交')
    doc.status = 'pending'
    doc.save(update_fields=['status', 'updated_at'])
    restored = 0
    for pet in pets:
        if pet.is_deleted:
            pet.is_deleted = False
            pet.save(update_fields=['is_deleted'])
            restored += 1
        if pet.status != 'in_transit':
            pet.status = 'in_transit'
            pet.save(update_fields=['status'])
    recalc_capture_status(doc)
    return f'{doc.ledger_no or doc.id} 已重新提交，{restored} 只动物恢复待转运'


def _busy_codes(codes):
    """这些编号当前是否被未结转运单占用（复用真源，不另写状态清单）。"""
    from business.services import busy_transfer_codes
    return busy_transfer_codes(codes)


def _transfer_return(doc, user):
    """转运单退回重转：仅未签收可退；宠物回 in_transit、解除医院归属。"""
    if doc.status not in ('pending', 'rejected', 'void'):
        raise MaintenanceError(
            f'{doc.get_status_display()}的转运单不可退回；医院已签收的动物不能倒回在途')
    codes = _codes(doc.pet_codes)
    if not codes:
        raise MaintenanceError('该转运单没有动物编号，无法退回')
    doc.status = 'void'
    doc.save(update_fields=['status'])
    # 终态动物不回退（与 transfer_withdraw 同口径）：不能把已放归/已领养的
    # 动物因为一张旧转运单退回而从归档里拽回在途。
    changed = 0
    for pet in _pets_by_code(codes, doc.district_id):
        if pet.status in Pet.TERMINAL_STATUSES or pet.status == 'in_treatment':
            continue
        if pet.status == 'in_transit' and pet.hospital_id is None:
            continue
        pet.status = 'in_transit'
        pet.hospital = None
        pet.save(update_fields=['status', 'hospital'])
        changed += 1
    recalc_captures_for_transfer(doc)
    return f'{doc.ledger_no or doc.id} 已退回，{changed} 只动物回待转运，可重新选择宠物与医院'


def _owner_return_to_draft(doc, user):
    """领回单退回草稿：宠物回「领回前状态」，诊疗单同步退回待出院。"""
    pet = doc.pet
    if pet is None:
        raise MaintenanceError('该领回单没有关联动物，无法退回草稿')
    if pet.is_deleted:
        raise MaintenanceError(f'{pet.code} 的档案已作废，请先恢复档案')
    if pet.status != 'owner_returned':
        raise MaintenanceError(
            f'{pet.code} 当前状态为「{pet.get_status_display()}」，不是已领回，不能退回草稿')
    before = doc.pet_status_before or 'in_transit'
    if before not in dict(Pet.STATUS_CHOICES):
        before = 'in_transit'
    doc.status = 'draft'
    doc.save(update_fields=['status'])
    pet.status = before
    pet.save(update_fields=['status'])
    # 领回是出院终态：撤销时诊疗单的 disposed_at 必须一起清掉，否则看板
    # 仍把它归进「已出院」，动物既不在院也没出院，成为孤儿。
    undo_discharge(pet)
    if pet.capture_id:
        recalc_capture_status(pet.capture)
    return f'{pet.code} 的领回单已退回草稿，动物恢复「{dict(Pet.STATUS_CHOICES)[before]}」'


def _owner_return_resubmit(doc, user):
    """草稿重新提交：宠物回 owner_returned，诊疗单补办结。"""
    pet = doc.pet
    if pet is None:
        raise MaintenanceError('该领回单没有关联动物，无法重新提交')
    if pet.is_deleted:
        raise MaintenanceError(f'{pet.code} 的档案已作废，无法重新提交')
    if pet.status == 'owner_returned':
        raise MaintenanceError(f'{pet.code} 已是已领回状态，无需重复提交')
    if pet.status in Pet.TERMINAL_STATUSES:
        raise MaintenanceError(
            f'{pet.code} 已进入其它终态「{pet.get_status_display()}」，不能按本单重新领回')
    # 首次提交时记下领回前状态，供再次退回精确恢复（存量草稿可能为空）。
    fields = ['status']
    if not doc.pet_status_before:
        doc.pet_status_before = pet.status
        fields.append('pet_status_before')
    doc.status = 'effective'
    doc.save(update_fields=fields)
    pet.status = 'owner_returned'
    pet.save(update_fields=['status'])
    dispose_for_discharge(pet, 'owner_returned', user)
    return f'{pet.code} 的领回单已重新提交生效'


_APPLIERS = {
    ('capture', 'draft'): _capture_to_draft,
    ('capture', 'effective'): _capture_resubmit,
    ('transfer', 'void'): _transfer_return,
    ('owner_return', 'draft'): _owner_return_to_draft,
    ('owner_return', 'effective'): _owner_return_resubmit,
}


def apply_transition(doc, doc_type, target, user):
    """执行一次迁移（调用方必须在 `transaction.atomic()` 里）。返回摘要文案。"""
    if doc_type not in _MODELS:
        raise MaintenanceError('单据类型不合法')
    if target not in allowed_targets(doc, doc_type):
        raise MaintenanceError(
            f'{DOC_LABELS[doc_type]}当前为「{status_text(doc, doc_type)}」，'
            f'不能改到「{status_labels(doc_type).get(target, target)}」')
    applier = _APPLIERS.get((doc_type, target))
    if applier is None:
        raise MaintenanceError('该单据类型暂不支持此操作')
    return applier(doc, user)


def get_doc(doc_type, doc_id):
    """按类型取单并加行锁（platform_admin 是全局角色，不按区县收敛）。"""
    model = _MODELS.get(doc_type)
    if model is None:
        raise MaintenanceError('单据类型不合法')
    doc = model.objects.select_for_update().filter(pk=doc_id).first()
    if doc is None:
        raise MaintenanceError(f'{DOC_LABELS[doc_type]}不存在')
    return doc


# ============================================
# 列表取数
# ============================================
def _pet_id(doc, doc_type):
    if doc_type == 'transfer':
        return None
    return getattr(doc, 'pet_id', None)


def _doc_rows(doc_type, qs):
    rows = []
    labels = status_labels(doc_type)
    for doc in qs:
        key = current_status(doc, doc_type)
        targets = list(allowed_targets(doc, doc_type))
        pet = getattr(doc, 'pet', None)
        pet_code = ''
        if doc_type == 'transfer':
            pet_code = doc.pet_codes or ''
        elif doc_type == 'capture':
            pet_code = doc.pet_codes or ''
        else:
            pet_code = getattr(doc, 'pet_code', '') or ''
        rows.append({
            'doc_type': doc_type,
            'doc_label': DOC_LABELS[doc_type],
            'id': doc.id,
            'ledger_no': doc.ledger_no or '',
            'pet_code': pet_code,
            'pet_status': pet.status if pet else '',
            'pet_status_display': pet.get_status_display() if pet else '',
            'status': key,
            'status_display': labels.get(key, key),
            'institution_name': (
                getattr(doc, 'hospital_name', '')
                or getattr(doc, 'from_shelter_name', '')
                or getattr(doc, 'shelter_name', '')
                or getattr(doc, 'community_name', '')),
            'district_name': doc.district.name if doc.district_id else '',
            'created_at': doc.created_at.strftime('%Y-%m-%d %H:%M') if doc.created_at else '',
            'targets': targets,
            'target_labels': [labels.get(t, t) for t in targets],
        })
    return rows


def _type_qs(doc_type, keyword='', status=''):
    """一种单据类型的检索 QuerySet；状态对该类型无意义时返回 None（跳过）。"""
    qs = _MODELS[doc_type].objects.select_related('district').order_by('-id')
    # 只有领回单/诊疗单是「一单一宠」、有 pet 外键；捕捉单与转运单都是
    # 多宠一单（pet_codes 文本），select_related('pet') 会直接 FieldError。
    if doc_type in ('owner_return', 'treatment'):
        qs = qs.select_related('pet')
    if keyword:
        if doc_type == 'capture':
            cond = (Q(ledger_no__icontains=keyword) | Q(pet_codes__icontains=keyword)
                    | Q(shelter_name__icontains=keyword)
                    | Q(property_name__icontains=keyword))
        elif doc_type == 'transfer':
            cond = (Q(ledger_no__icontains=keyword) | Q(pet_codes__icontains=keyword)
                    | Q(to_hospital_name__icontains=keyword)
                    | Q(from_shelter_name__icontains=keyword))
        elif doc_type == 'owner_return':
            cond = (Q(ledger_no__icontains=keyword) | Q(pet_code__icontains=keyword)
                    | Q(owner_name__icontains=keyword))
        else:
            cond = (Q(ledger_no__icontains=keyword) | Q(pet_code__icontains=keyword)
                    | Q(hospital_name__icontains=keyword))
        qs = qs.filter(cond)
    if status:
        if status not in status_labels(doc_type):
            return None
        qs = qs.filter(status=status)
    return qs


# ============================================
# 视图
# ============================================
@csrf_exempt
@role_required('platform_admin')
@login_required
def admission_doc_list(request):
    """入院单据检索（平台端维护页取数）。

    查询参数：`doc_type`（缺省 = 四类全查）、`q`（动物编号 / 单据编号）、
    `status`、`actionable=1`（只看当前可操作的）、`limit`（每类上限，默认 100）。
    """
    doc_type = (request.GET.get('doc_type') or '').strip()
    if doc_type and doc_type not in _MODELS:
        return json_fail('单据类型不合法')
    keyword = (request.GET.get('q') or request.GET.get('keyword') or '').strip()
    status = (request.GET.get('status') or '').strip()
    actionable = (request.GET.get('actionable') or '').strip() == '1'
    try:
        limit = max(1, min(int(request.GET.get('limit') or 100), 500))
    except (TypeError, ValueError):
        limit = 100

    rows = []
    for t in (doc_type,) if doc_type else tuple(_MODELS):
        qs = _type_qs(t, keyword, status)
        if qs is None:
            continue
        rows.extend(_doc_rows(t, qs[:limit]))
    if actionable:
        rows = [r for r in rows if r['targets']]
    rows.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
    return json_ok(rows, message='查询成功')


@csrf_exempt
@role_required('platform_admin')
@login_required
def admission_doc_status(request):
    """入院单据状态操作：退回草稿 / 重新提交 / 转运单退回重转。

    请求体：`{doc_type, doc_id, target, reason}`。`reason` 必填并写入审计
    `detail`。诊疗单类型在这里被直接拒绝（无白名单），杜绝绕过前端改数据。
    """
    if request.method != 'POST':
        return json_fail('该接口仅支持 POST', status=405)
    data = parse_json_body(request)

    def field(*keys):
        for key in keys:
            value = body_str(data, key)
            if value:
                return value.strip()
        return ''

    doc_type = field('doc_type', 'docType')
    target = field('target')
    reason = field('reason')
    raw_id = data.get('doc_id') or data.get('docId') or data.get('id')
    if doc_type not in _MODELS:
        return json_fail('单据类型不合法')
    try:
        doc_id = int(raw_id)
    except (TypeError, ValueError):
        return json_fail('单据主键不合法')
    if not target:
        return json_fail('请选择要变更的状态')
    if not reason:
        return json_fail('请填写维护原因')
    if len(reason) > REASON_MAX:
        return json_fail(f'维护原因不能超过 {REASON_MAX} 字')

    user = request.user
    try:
        with transaction.atomic():
            doc = get_doc(doc_type, doc_id)
            old_text = status_text(doc, doc_type)
            summary = apply_transition(doc, doc_type, target, user)
            request.audit_object = doc
            request.audit_district = doc.district
            request.audit_object_repr = (
                f'{DOC_LABELS[doc_type]} {doc.ledger_no or doc.id}')
            request.audit_detail = (
                f'{old_text} → {status_text(doc, doc_type)}'
                f'｜原因：{reason}｜{summary}')
    except MaintenanceError as exc:
        return json_fail(str(exc))

    return json_ok({
        'doc_type': doc_type,
        'id': doc.id,
        'status': current_status(doc, doc_type),
        'status_display': status_text(doc, doc_type),
        'targets': list(allowed_targets(doc, doc_type)),
    }, message=summary)
