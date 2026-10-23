"""平台端「出院单据状态维护」（第五十七轮）。

为什么需要它
------------
出院的四类去向一旦办完，动物档案与诊疗单就同时落到终态；此前**没有任何
合法出口**能把一张填错、重开、或去向搞反的出院单据退回去。后果是：

* 医院只能把错单留着，动物既不在院、也没正确出院；
* 建议去向被 `advice_edit_error()` 锁死（动物已是终态），业务方要求的
  「已出院的动物要能纠正去向」根本走不通。

本模块给平台管理员一个**白名单驱动**的回退入口：只允许 `TRANSITIONS` 列出
的状态迁移，必须填原因，「单据状态 + 动物档案 + 诊疗单办结」三半在同一事务
里落地，全过程写审计台账（原因只落审计 `detail`：四类单据各有各的字段语义，
没有可复用的「维护原因」列，硬塞进 `reject_reason` 之类会污染业务字段）。

为什么是三半一起改
------------------
只回退单据不改 `Pet.status` → 动物仍是「已领养」，出院管理选不到它；
只改动物不回退诊疗单 `disposed_at` → 看板把它归进「已出院」，同样选不到；
只回退诊疗单不改单据 → 一张「已完成」的领养单挂着一只待出院的动物，
台账自相矛盾。

`undo_discharge()` 只在**动物真的回到医院**时调用（领养撤销出库 / 运回单
重新打开）。放归单退回与死亡出库凭证退回不调：前者动物人在捕捉点、后者
动物已死，把诊疗单打回「待出院」等于凭空造出一只「院里还等着办出院」的
动物。判据见各 `_apply_*` 的注释。

⚠ 本模块所有函数都**必须在调用方的 `transaction.atomic()` 里**执行。
"""
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import (
    Adoption, AdoptionHallListing, Euthanasia, Pet, Release, ReturnOrder,
    Treatment,
)
from business.services import (
    json_fail, json_ok, parse_json_body, pet_has_pending_release,
)
from business.services_discharge import disposal_label
from business.services_treatment import undo_discharge
from core.http import body_str

#: 单据类型 → 展示名（页面与审计摘要共用一份）。
DOC_LABELS = {
    'adoption': '领养单',
    'euthanasia': '死亡出库单',
    'return_order': '运回单',
    'release': '放归单',
}

#: 允许的状态迁移白名单：`{单据类型: {当前状态: (可变成的状态, ...)}}`。
#: 死亡记录没有 `status` 列，出库与否由 `outbound_at` 派生，这里用同样
#: 两个 key 表达，避免为一次维护操作专门加一列。
#:
#: 只列**回退方向**：往终点推进（`pending → released`）是业务角色自己的
#: 流程，平台端不该代办；平台端唯一的职责是纠错，所以这里不需要
#: 「前进」白名单，也不需要给单据加作废态。
TRANSITIONS = {
    'adoption': {
        # 出库办错了 → 退回「已预约」重新交接；整单作废 → 动物回待领养
        'completed': ('pending_claim', 'cancelled'),
        'pending_claim': ('cancelled',),
    },
    # 遗体移交凭证录错（处理厂 / 日期 / 签字）→ 退回待出库重新登记。
    # 动物的「已死亡 / 已安乐死」是临床事实，不随凭证回退。
    'euthanasia': {
        'outbound': ('registered',),
    },
    'return_order': {
        'received': ('pending',),
        'rejected': ('pending',),
        'withdrawn': ('pending',),
    },
    'release': {
        'released': ('transferred', 'pending'),
        'transferred': ('pending',),
    },
}

#: 维护原因长度上限。原因会进审计 `detail`，不设上限等于允许往台账里
#: 塞任意大的文本（`SystemConfig` 的取值上限同理）。
REASON_MAX = 200


class MaintenanceError(Exception):
    """校验失败 —— 由视图折成 `json_fail`，不让它冒成 500。"""


# ============================================
# 状态口径
# ============================================
_MODELS = {
    'adoption': Adoption,
    'euthanasia': Euthanasia,
    'return_order': ReturnOrder,
    'release': Release,
}


def status_labels(doc_type):
    """该类型所有可展示的状态 key → 中文。

    派生态（死亡记录）自己给文案，其余直接取模型的 `choices` —— 列表筛选、
    行数据、错误提示三处都读这一份，不再各写一遍字面量。
    """
    if doc_type == 'euthanasia':
        return {'registered': '已登记 · 待出库', 'outbound': '已出库（遗体已移交）'}
    return dict(_MODELS[doc_type].STATUS_CHOICES)


def current_status(doc, doc_type):
    """单据当前状态 key（死亡记录由 `outbound_at` 派生）。"""
    if doc_type == 'euthanasia':
        return 'outbound' if doc.outbound_at else 'registered'
    return doc.status


def status_text(doc, doc_type):
    return status_labels(doc_type).get(
        current_status(doc, doc_type), current_status(doc, doc_type))


def allowed_targets(doc, doc_type):
    """该单据当前能退回哪些状态。"""
    return TRANSITIONS.get(doc_type, {}).get(current_status(doc, doc_type), ())


# ============================================
# 回退执行
# ============================================
def _pet_of(doc):
    pet = getattr(doc, 'pet', None)
    if pet is None:
        raise MaintenanceError('该单据没有关联的动物档案，无法回退')
    if pet.is_deleted:
        raise MaintenanceError(f'{pet.code} 的档案已作废，请先恢复档案再维护单据')
    return pet


def _require_pet_status(pet, allowed, action):
    """动物已被**别的**流程接管时拒绝回退（终态排除的既有安全先例）。

    `Pet.TERMINAL_STATUSES` 的先例来自转运单驳回 / 撤回：一张作废的单据
    不能把「已放归」的动物打回在办流程。这里同理 —— 已经走向别的归宿的
    动物，要纠正得先处理那张单据。
    """
    if pet.status not in allowed:
        raise MaintenanceError(
            f'{pet.code} 当前状态为「{pet.get_status_display()}」，'
            f'不属于本单据可回退的状态，不能{action}')


def _apply_adoption(doc, target, user):
    pet = _pet_of(doc)
    if target == 'pending_claim':
        # 撤销出库：交接凭证与出库日期一并撤回，否则「待领出」的单上还留着
        # 签字和日期，重新出库时会被当成「早已办过」。
        doc.status = 'pending_claim'
        doc.outbound_at = None
        doc.adopted_at = None
        doc.handover_name = ''
        doc.signature = ''
        doc.handover_photo = None
        doc.save(update_fields=[
            'status', 'outbound_at', 'adopted_at', 'handover_name',
            'signature', 'handover_photo'])
        _require_pet_status(pet, ('adopted',), '撤销领养出库')
        pet.status = 'pending_claim'
        pet.save(update_fields=['status'])
        # 动物回到院内：诊疗单的办结必须一起退回，否则看板仍按 `disposed_at`
        # 把它归进「已出院」，出院管理再也选不到它。
        undo_discharge(pet)
        return f'{pet.code} 已退回「待领出」，可重新办理领养出库'

    # 整单作废：动物回「待领养」并重新上架大厅（与 `adoption_reclaim` 同口径）
    doc.status = 'cancelled'
    doc.save(update_fields=['status'])
    _require_pet_status(pet, ('adopted', 'pending_claim'), '作废领养单')
    pet.status = 'pending_adopt'
    pet.save(update_fields=['status'])
    undo_discharge(pet)
    listing = AdoptionHallListing.objects.filter(pet=pet).first()
    if listing is None:
        AdoptionHallListing.objects.create(
            pet=pet, hospital=pet.hospital,
            hospital_name=pet.hospital.name if pet.hospital else '',
            is_active=True)
    else:
        # 已有记录时**显式置回 is_active**：`update(is_active=True)` 对零行
        # 不报错，但建档时没上架过的动物会一条都不命中，动物就永远回不了大厅。
        listing.is_active = True
        listing.save(update_fields=['is_active'])
    return f'{pet.code} 领养单作废，动物回「待领养」并重新上架大厅'


def _apply_euthanasia(doc, target, user):
    pet = _pet_of(doc)
    if target != 'registered':
        raise MaintenanceError('死亡出库单只能退回「已登记 · 待出库」')
    # 与 `euthanasia_outbound` 写入的字段一一对冲（reason 属于临床登记，不动）。
    doc.outbound_at = None
    doc.handover_name = ''
    doc.receiving_org = ''
    doc.receiver_name = ''
    doc.receiver_unit = ''
    doc.signature = ''
    doc.photo = None
    # 出库时「遗体已领取」是同步落值的，不回退会留下
    # 「待出库的单 + 遗体已被领取」这种自相矛盾的台账。
    doc.body_received = False
    doc.body_received_at = None
    doc.body_received_by = None
    doc.body_received_by_name = ''
    doc.save(update_fields=[
        'outbound_at', 'handover_name', 'receiving_org', 'receiver_name',
        'receiver_unit', 'signature', 'photo', 'body_received',
        'body_received_at', 'body_received_by', 'body_received_by_name',
    ])
    # 不调 `undo_discharge()`：动物已死亡 / 已安乐死是临床事实，本次维护的
    # 只是遗体移交凭证。打回「待出院」会让一只死掉的动物出现在出院候选里。
    return f'{pet.code} 死亡出库单已撤回，遗体待重新移交无害化处理厂'


def _apply_return_order(doc, target, user):
    codes = [c.strip() for c in (doc.pet_codes or '').split(',') if c.strip()]
    if not codes:
        raise MaintenanceError('该运回单没有动物编号，无法回退')
    pets = list(Pet.objects.select_for_update().filter(code__in=codes))
    if len(pets) != len(codes):
        raise MaintenanceError('运回单中的动物档案缺失，无法回退')
    for pet in pets:
        if pet.is_deleted:
            raise MaintenanceError(f'{pet.code} 的档案已作废，请先恢复档案再维护单据')
        # 签收后动物在捕捉点（pending_release），驳回 / 撤回后已退回医院
        # （pending_adopt / in_treatment / return_pending）。除此之外都说明
        # 动物已被别的出院流程接走。
        _require_pet_status(
            pet, ('pending_release', 'pending_adopt', 'in_treatment', 'return_pending'),
            '重新打开运回单')
        # 动物已在另一张未办结的放归单上时，重新打开运回单等于让「回捕捉点」
        # 与「放归小区」两张单同时承诺同一只动物。
        if pet.status == 'pending_release' and pet_has_pending_release(pet):
            raise MaintenanceError(
                f'{pet.code} 已有未办结的放归单，请先退回那张放归单再维护运回单')
    doc.status = 'pending'
    doc.received_at = None
    doc.receiver_name = ''
    doc.receiver_phone = ''
    doc.signature = ''
    doc.reject_reason = ''
    doc.save(update_fields=[
        'status', 'received_at', 'receiver_name', 'receiver_phone',
        'signature', 'reject_reason'])
    for pet in pets:
        # 回到 `return_order_create` 开单时的占用态：待运回捕捉点
        pet.status = 'return_pending'
        fields = ['status']
        if doc.hospital_id and pet.hospital_id is None:
            # 签收时 `hospital` 被清空（动物已离院），重新占用要挂回发出医院
            pet.hospital_id = doc.hospital_id
            fields.append('hospital_id')
        pet.save(update_fields=fields)
        undo_discharge(pet)
    return f'运回单重新打开，{len(pets)} 只动物回到「待运回捕捉点」'


def _apply_release(doc, target, user):
    pet = _pet_of(doc)
    doc.status = target
    doc.released_at = None
    doc.signature = ''
    if target == 'pending':
        doc.transferred_at = None
    doc.save(update_fields=['status', 'released_at', 'signature', 'transferred_at'])
    if pet.status == 'released':
        # 撤销放归 = 动物还在小区 / 捕捉点等签字，回到「待放归」。
        # 状态**不是** released 时不动档案：可能是别的单据（领养 / 死亡）
        # 接走了它，那张单据才是档案状态的当前来源。
        pet.status = 'pending_release'
        pet.save(update_fields=['status'])
    # 不调 `undo_discharge()`：动物人已离院（在捕捉点或小区），放归单退回
    # 只是让「物业签字」这一步重来；诊疗单在运回签收时就已办结，打回
    # 「待出院」会让出院候选里凭空多一只不在院的动物。
    return f'{pet.code} 放归单退回「{status_labels("release")[target]}」'


_APPLIERS = {
    'adoption': _apply_adoption,
    'euthanasia': _apply_euthanasia,
    'return_order': _apply_return_order,
    'release': _apply_release,
}


def apply_transition(doc, doc_type, target, user):
    """执行一次状态回退（调用方必须在 `transaction.atomic()` 里）。

    返回给界面与审计用的摘要文案。
    """
    if doc_type not in _MODELS:
        raise MaintenanceError('单据类型不合法')
    if target not in allowed_targets(doc, doc_type):
        raise MaintenanceError(
            f'{DOC_LABELS[doc_type]}当前为「{status_text(doc, doc_type)}」，'
            f'不能退回「{status_labels(doc_type).get(target, target or "空")}」')
    return _APPLIERS[doc_type](doc, target, user)


def get_doc(doc_type, doc_id):
    """按类型取单并加行锁（platform_admin 是全局角色，不按区县收敛）。"""
    model = _MODELS.get(doc_type)
    if model is None:
        raise MaintenanceError('单据类型不合法')
    qs = model.objects.select_for_update().filter(pk=doc_id)
    if doc_type != 'return_order':
        qs = qs.select_related('pet')
    doc = qs.first()
    if doc is None:
        raise MaintenanceError(f'{DOC_LABELS[doc_type]}不存在')
    return doc


# ============================================
# 视图
# ============================================
def _pet_id(doc):
    """单宠单据取动物外键；运回单是「多宠一单」，没有 `pet_id` 属性。

    ⚠ 必须用 `getattr`：`doc.pet_id` 在运回单上抛 `AttributeError`，
    默认「全部单据」的检索会整页 500，四类单据一起看不见。
    """
    return getattr(doc, 'pet_id', None)


def _advice_by_pet(docs):
    """批量取这些动物「待出院诊疗单」的建议去向（一页一次查询）。

    升序遍历、后写覆盖前写 = 与 `services_discharge.pending_treatment()`
    的 `-id` 口径一致（取最后一张）。
    """
    ids = [_pet_id(d) for d in docs]
    ids = [pk for pk in ids if pk]
    if not ids:
        return {}
    out = {}
    for treatment in Treatment.objects.filter(
            pet_id__in=ids, done_at__isnull=False,
            disposed_at__isnull=True).order_by('id'):
        out[treatment.pet_id] = treatment.disposal_advised or ''
    return out


def _doc_rows(doc_type, qs, advice=True):
    rows = []
    docs = list(qs)
    advice_map = _advice_by_pet(docs) if advice else {}
    for doc in docs:
        pet = getattr(doc, 'pet', None)
        pet_id = _pet_id(doc)
        advised = advice_map.get(pet_id, '') if pet_id else ''
        targets = list(allowed_targets(doc, doc_type))
        labels = status_labels(doc_type)
        rows.append({
            'doc_type': doc_type,
            'doc_label': DOC_LABELS[doc_type],
            'id': doc.id,
            'ledger_no': doc.ledger_no or '',
            'pet_code': getattr(doc, 'pet_code', '')
            or getattr(doc, 'pet_codes', ''),
            'pet_status': pet.status if pet else '',
            'pet_status_display': pet.get_status_display() if pet else '',
            'advised': advised,
            'advised_display': disposal_label(advised),
            'status': current_status(doc, doc_type),
            'status_display': labels.get(
                current_status(doc, doc_type), current_status(doc, doc_type)),
            'institution_name': (getattr(doc, 'hospital_name', '')
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
    model = _MODELS[doc_type]
    qs = model.objects.select_related('district').order_by('-id')
    if doc_type != 'return_order':
        qs = qs.select_related('pet')
    else:
        qs = qs.select_related('hospital', 'shelter')
    if keyword:
        field = 'pet_codes' if doc_type == 'return_order' else 'pet_code'
        qs = qs.filter(Q(**{f'{field}__icontains': keyword})
                       | Q(ledger_no__icontains=keyword))
    if status:
        if status not in status_labels(doc_type):
            return None
        if doc_type == 'euthanasia':
            qs = qs.filter(outbound_at__isnull=(status == 'registered'))
        else:
            qs = qs.filter(status=status)
    return qs


@csrf_exempt
@role_required('platform_admin')
@login_required
def discharge_doc_list(request):
    """出院单据检索（平台端维护页的取数接口）。

    查询参数：`doc_type`（缺省 = 四类全查）、`q`（动物编号 / 台账编号）、
    `status`、`reversible=1`（只看当前可退回的）、`limit`（每类上限，默认 100）。
    """
    doc_type = (request.GET.get('doc_type') or '').strip()
    if doc_type and doc_type not in _MODELS:
        return json_fail('单据类型不合法')
    keyword = (request.GET.get('q') or request.GET.get('keyword') or '').strip()
    status = (request.GET.get('status') or '').strip()
    reversible = (request.GET.get('reversible') or '').strip() == '1'
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
    if reversible:
        rows = [r for r in rows if r['targets']]
    rows.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
    return json_ok(rows, message='查询成功')


@csrf_exempt
@role_required('platform_admin')
@login_required
def discharge_doc_status(request):
    """把一张出院单据退回白名单内的上一个状态。

    请求体：`{doc_type, doc_id, target, reason}`。`reason` **必填**并写入
    审计 `detail`（旧状态 → 新状态｜原因）——这类操作直接改动监管台账里
    的终态记录，无理由不可执行。
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
        return json_fail('请选择要退回的状态')
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
            # 审计上下文：区县取**单据**的归属区县（不是操作人），
            # 摘要里带上旧→新与原因，监管侧看日志即可复原这次维护。
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
