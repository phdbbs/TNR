"""一宠一档（lifecycle）数据构建：**接口与打印页共用一份实现**。

⚠ 为什么必须抽出来而不是让打印页自己查一遍：
  1. **范围收敛**（按角色）与 **PII 脱敏**（领养人清空 / 监管端掩码）是
     同一条业务规则的两半。打印页若另写一套，两条路径迟早漂移 —— 一边
     收紧了另一边没收紧，打印页就成了「猜主键 + 打开打印页」的越权读取
     与 PII 泄漏面。而这类漂移**不会报错**，只会在某次对账时被发现。
  2. 事件字段名（`date` / `ledger_no` / `owner_phone` …）是前端时间线与
     `/api/business/pets/<id>/lifecycle/` 的契约。两处各拼一遍，键名漏一个
     就是「档案打印页比屏幕上少一段」。

调用方：
  - `business.views_portal.pet_lifecycle`（JSON 接口）
  - `business.views_print.print_pet_archive`（A4 打印页 / 另存 PDF）
"""
from django.db.models import Q
from django.utils import timezone

from business.models import (
    Adoption, CheckIn, Euthanasia, OwnerReturn, Pet, Release, ReturnOrder,
    Transfer, Treatment, TreatmentItem, TreatmentRx,
)
from business.services import (
    get_scoped_object, hospital_pet_scope, pet_brief, pet_photo_list,
    serialize_instance,
)
from core.privacy import mask_id_card, mask_phone


def _stage_photos(pet, *fields):
    """取动物的指定阶段照片（只返回确实有图的），供时间线内嵌展示。"""
    wanted = set(fields)
    return [p for p in pet_photo_list(pet) if p['field'] in wanted]


def _local_time(value):
    """完整到本地分钟的 `YYYY-MM-DD HH:MM`。

    `date` 字段契约是**纯日期**（前端 `.slice(0, 10)`、`inDateRange` 都按它来），
    不能往里塞时间串；PDF 要的「时间」另放 `time` 键。两个字段各司其职 ——
    混在一起会让时间线排序和筛选一起错。
    """
    if not value:
        return ''
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M')


def _image_url(image):
    """ImageField → url；空字段返回 ''（模板里据此不渲染 <img>）。"""
    try:
        return image.url if image else ''
    except ValueError:
        return ''


def _return_order_pet_q(code):
    """在逗号分隔的 `ReturnOrder.pet_codes` 里精确匹配一个编号。

    直接 `pet_codes__contains=code` 会把 `TNR26101800` 当成
    `TNR261018002` 的前缀命中 —— 一张单子会让另一只动物也显示「待运回」。
    """
    return (Q(pet_codes=code)
            | Q(pet_codes__startswith=code + ',')
            | Q(pet_codes__endswith=',' + code)
            | Q(pet_codes__contains=',' + code + ','))


def pet_stage(pet):
    """一宠一档的「当前状态」：按业务单据派生，不只看 `Pet.status`。

    `Pet.status` 只在几个节点被改写，放归单创建 / 转运回小区两段都
    不改它（`release_create`、`release_transfer`），于是动物已经回到小区
    等签字、档案却还写着「待诊疗」/「待领养」——TNR261018002 就是这样：
    诊疗早已办结、`Release` 停在 transferred，档案显示的却是 in_treatment。

    派生顺序按「最具体的未办结单据」优先：
      未办结运回单 → 待运回捕捉点
      未办结放归单（pending / transferred）→ 待放归
      诊疗已完成但未办结出院 → 待出院
      其余仍以 `Pet.status` 为准（含全部终态）。
    """
    if pet.status in Pet.TERMINAL_STATUSES:
        return pet.status, pet.get_status_display()

    if ReturnOrder.objects.filter(status='pending').filter(
            _return_order_pet_q(pet.code)).exists():
        return 'return_pending', dict(Pet.STATUS_CHOICES)['return_pending']

    if Release.objects.filter(pet=pet, status__in=Release.OPEN_STATUSES).exists():
        return 'pending_release', dict(Pet.STATUS_CHOICES)['pending_release']

    if Treatment.objects.filter(
            pet=pet, done_at__isnull=False, disposed_at__isnull=True).exists():
        return 'pending_discharge', '待出院'

    return pet.status, pet.get_status_display()


def pet_for_lifecycle(user, pet_id):
    """按角色收敛可见范围；**不存在与越权一律返回 None**（不可区分）。

    领养人仅限自己领养/申请过的动物；**医院限本院在治或本院经手过（诊疗）
    的动物**；其余角色按所属区县过滤，避免只凭主键即可遍历全量动物档案。

    医院这一支**不能按区县收敛**（第二十轮）：同一区县可能有多家医院
    （襄城区有 2 家），按区县会让 A 院读到 B 院名下动物的完整溯源记录 ——
    含主人姓名与电话。判据与 ``pet_archive`` 共用 ``hospital_pet_scope()``。
    """
    if user.role == 'adopter':
        return Pet.objects.filter(id=pet_id).filter(
            Q(adoptions__adopter=user) | Q(adoption_applications__applicant=user)
        ).distinct().first()
    if user.role == 'hospital':
        return Pet.objects.filter(id=pet_id).filter(
            hospital_pet_scope(user)).distinct().first()
    return get_scoped_object(Pet, pet_id, user)


def owner_contact_fields(user, record):
    """主人/领回人字段的最小化下发（第五十七轮）。

    同一份时间线，不同角色看到的主人字段不同。领养人只需要「这只动物
    经历过什么」，不需要前任主人的电话、住址、身份证与签名；监管端保留
    脱敏号用于比对；捕捉点/医院要执行领回与回访，保留完整联系方式。
    """
    if user.role == 'adopter':
        return {'owner_phone': '', 'owner_id_card': '',
                'owner_address': '', 'signature': ''}
    if user.role in ('gov_city', 'gov_district'):
        return {
            'owner_phone': mask_phone(record.owner_phone),
            'owner_id_card': mask_id_card(record.owner_id_card),
            'owner_address': record.owner_address,
            'signature': record.signature or '',
        }
    return {
        'owner_phone': record.owner_phone,
        'owner_id_card': record.owner_id_card,
        'owner_address': record.owner_address,
        'signature': record.signature or '',
    }


def build_pet_lifecycle(pet, user):
    """返回 `{'pet', 'pet_brief', 'photos', 'events'}`（字段名即接口契约）。"""
    is_adopter = user.role == 'adopter'
    events = []

    # 捕捉记录
    if pet.capture:
        cap = pet.capture
        capture_photos = _stage_photos(pet, 'photo_capture')
        if cap.group_photo:
            capture_photos.append(
                {'field': 'group_photo', 'label': '整体合影', 'url': cap.group_photo.url})
        events.append({
            'type': 'capture',
            'type_display': '捕捉登记',
            'content': (f'{cap.shelter_name or "捕捉点"}在'
                        f'{cap.community_name or cap.address or "捕捉地"}捕捉登记，'
                        f'编号 {cap.ledger_no or "—"}'),
            'time': _local_time(cap.created_at),
            'date': timezone.localdate(cap.created_at).isoformat() if cap.created_at else '',
            'ledger_no': cap.ledger_no,
            'shelter_name': cap.shelter_name,
            'community_name': cap.community_name,
            'address': cap.address,
            'property_name': cap.property_name,
            # 物业交接人是捕捉点/监管端的操作联系人；领养人不需要。
            'contact_person': '' if is_adopter else cap.contact_person,
            'contact_phone': '' if is_adopter else cap.contact_phone,
            'operator_name': cap.operator_name,
            'photos': capture_photos,
        })

    # 转运记录
    transfers = Transfer.objects.filter(pet_codes__contains=pet.code).order_by('created_at')
    for t in transfers:
        events.append({
            'type': 'transfer',
            'type_display': '转运交接',
            'content': f'由{t.from_shelter_name or "捕捉点"}转运至'
                       f'{t.to_hospital_name or "接收医院"}，'
                       f'单号 {t.ledger_no or "—"}',
            'time': _local_time(t.created_at),
            'date': timezone.localdate(t.created_at).isoformat() if t.created_at else '',
            'ledger_no': t.ledger_no,
            'from_shelter_name': t.from_shelter_name,
            'to_hospital_name': t.to_hospital_name,
            'status': t.status,
            'received_at': t.received_at.isoformat() if t.received_at else '',
            'operator_name': t.operator_name,
        })

    # 诊疗记录 + 手术 / 处方 / 出院（用户要求的全过程四段）
    # 子表优先、旧布尔与旧明细列兜底：`Treatment` 两代字段并存是刻意保留的，
    # 只认子表会漏掉迁移前的历史单，只认旧字段会漏掉新流程的明细。
    treatments = Treatment.objects.filter(pet=pet).prefetch_related(
        'items__rx_rows').order_by('created_at')
    for t in treatments:
        txn_items = list(t.items.all())
        items = []
        if t.items_sterilization: items.append('绝育')
        if t.items_vaccine: items.append('疫苗')
        if t.items_deworming: items.append('驱虫')
        if t.items_chip: items.append('芯片')
        for it in txn_items:
            label = it.item_name or it.get_item_key_display()
            if label not in items:
                items.append(label)
        rx_rows = [(rx, it) for it in txn_items for rx in it.rx_rows.all()]

        events.append({
            'type': 'treatment',
            'type_display': '诊疗记录',
            'content': f'{t.hospital_name or "医院"}完成诊疗'
                       f'（{"、".join(items) if items else "常规检查"}）'
                       f'，单号 {t.ledger_no or "—"}',
            'time': _local_time(t.created_at),
            'date': timezone.localdate(t.created_at).isoformat() if t.created_at else '',
            'ledger_no': t.ledger_no,
            'hospital_name': t.hospital_name,
            'items': items,
            'rx_summary': '、'.join(
                f'{rx.material_name or getattr(rx.material, "name", "")}×{rx.quantity}{rx.unit}'
                for rx, _it in rx_rows),
            'advice': t.advice,
            'chip_no': t.chip_no,
            'status': t.status,
            'operator_name': t.operator_name,
            'photos': _stage_photos(pet, 'photo_before', 'photo_after', 'photo_treatment'),
        })

        # 手术记录：新流程看 TreatmentItem(sterilization)，旧单看六个 sterilization_* 列。
        surgery = next((it for it in txn_items if it.item_key == 'sterilization'), None)
        legacy = [t.sterilization_surgery_date, t.sterilization_surgeon,
                  t.sterilization_anesthesia, t.sterilization_procedure]
        if surgery or any(legacy):
            surgery_date = (t.sterilization_surgery_date.isoformat()
                            if t.sterilization_surgery_date else '')
            if not surgery_date and surgery and surgery.done_at:
                surgery_date = timezone.localdate(surgery.done_at).isoformat()
            if not surgery_date and t.created_at:
                surgery_date = timezone.localdate(t.created_at).isoformat()
            events.append({
                'type': 'surgery',
                'type_display': '手术记录',
                'content': f'{t.hospital_name or "医院"}实施绝育手术，'
                           f'主刀 {t.sterilization_surgeon or (surgery.vet_name if surgery else "") or "—"}',
                # 旧单只有手术**日期**没有执行时刻，退到诊疗完成时间；
                # 日期那一行已如实给出，这里不再另写「未记录」。
                'time': _local_time(
                    (surgery.done_at if surgery and surgery.done_at else None)
                    or t.done_at or t.created_at),
                'date': surgery_date,
                'ledger_no': t.ledger_no,
                'hospital_name': t.hospital_name,
                'surgery_date': surgery_date,
                'surgeon': t.sterilization_surgeon or (surgery.vet_name if surgery else ''),
                'anesthesia': t.sterilization_anesthesia,
                'procedure': t.sterilization_procedure,
                'notes': (surgery.notes if surgery else ''),
                'operator_name': t.operator_name,
            })

        # 处方：一条一行（药名 / 数量 / 单位 / 项目 / 操作人），不合并成一段字符串 ——
        # 合并后纸上只剩一句话，查「哪次用了哪批药」还得回系统里翻。
        for rx, it in rx_rows:
            drug = rx.material_name or getattr(rx.material, 'name', '') or '药品'
            events.append({
                'type': 'prescription',
                'type_display': '处方用药',
                'content': f'开具处方：{drug} {rx.quantity}{rx.unit}'
                           f'（{it.item_name or it.get_item_key_display()}）',
                'time': _local_time(rx.created_at),
                'date': timezone.localdate(rx.created_at).isoformat() if rx.created_at else '',
                'ledger_no': t.ledger_no,
                'hospital_name': t.hospital_name,
                'drug_name': drug,
                'quantity': f'{rx.quantity}{rx.unit}',
                'item_name': it.item_name or it.get_item_key_display(),
                'vet_name': it.vet_name,
                'operator_name': it.vet_name or t.operator_name,
                'manual_rx': it.rx_other,
            })

        # 出院：诊疗单办结那一刻（disposed_at）。它是诊疗流程的收口节点，
        # 与下面的放归/领回/安乐死等**业务单据**是两件事，不能只靠后者代替 ——
        # 单据未开但已办结时，档案里也必须看得到「什么时候以什么方式出院」。
        if t.disposed_at:
            disposal_label = dict(Treatment.DISPOSAL_CHOICES).get(t.disposal, '')
            events.append({
                'type': 'discharge',
                'type_display': '出院办理',
                'content': f'办结出院，去向：{disposal_label or t.disposal or "—"}',
                'time': _local_time(t.disposed_at),
                'date': timezone.localdate(t.disposed_at).isoformat(),
                'ledger_no': t.ledger_no,
                'hospital_name': t.hospital_name,
                'disposal_label': disposal_label,
                'disposal': t.disposal,
                'advised_label': dict(Treatment.DISPOSAL_CHOICES).get(
                    t.disposal_advised, ''),
                # Treatment 没有办结操作人快照，`operator_name` 是开单兽医。
                # 不假装有第二个人 —— 纸上如实写「诊疗单操作员」。
                'operator_name': t.operator_name,
            })

    # 放归记录
    releases = Release.objects.filter(pet=pet).order_by('created_at')
    for r in releases:
        events.append({
            'type': 'release',
            'type_display': '放归记录',
            'content': f'放归至{r.community_name or "原小区"}，'
                       f'接收人 {r.receiver_name or "—"}',
            'time': _local_time(r.created_at),
            'date': r.released_at.isoformat() if r.released_at else (timezone.localdate(r.created_at).isoformat() if r.created_at else ''),
            'ledger_no': r.ledger_no,
            'community_name': r.community_name,
            'receiver_name': r.receiver_name,
            'status': r.status,
            'operator_name': r.operator_name,
        })

    # 领养记录
    adoptions = Adoption.objects.filter(pet=pet).order_by('created_at')
    for a in adoptions:
        events.append({
            'type': 'adoption',
            'type_display': '领养记录',
            'content': f'由{a.adopter_name or "领养人"}领养，'
                       f'单号 {a.ledger_no or "—"}',
            'time': _local_time(a.created_at),
            'date': a.adopted_at.isoformat() if a.adopted_at else (timezone.localdate(a.created_at).isoformat() if a.created_at else ''),
            'ledger_no': a.ledger_no,
            'adopter_name': a.adopter_name,
            'hospital_name': a.hospital_name,
            'status': a.status,
            'operator_name': a.operator_name,
        })

    # 安乐死记录
    euthanasias = Euthanasia.objects.filter(pet=pet).order_by('created_at')
    for e in euthanasias:
        events.append({
            'type': 'euthanasia',
            'type_display': '安乐死记录',
            'content': f'{e.hospital_name or "医院"}处置：{e.reason or "—"}',
            'time': _local_time(e.created_at),
            'date': e.euthanized_at.isoformat() if e.euthanized_at else (timezone.localdate(e.created_at).isoformat() if e.created_at else ''),
            'ledger_no': e.ledger_no,
            'hospital_name': e.hospital_name,
            'reason': e.reason,
            'operator_name': e.operator_name,
        })

    # 主人领回记录
    # 每只动物只可能有一条 OwnerReturn（owner_returned 状态互斥），但保留循环以兼容历史数据
    owner_returns = OwnerReturn.objects.filter(pet=pet).order_by('created_at')
    for r in owner_returns:
        events.append({
            'type': 'owner_return',
            'type_display': '领回登记',
            'content': f'由{r.owner_name or "主人"}领回，'
                       f'原因 {r.reason or "—"}',
            'time': _local_time(r.return_time or r.created_at),
            # ⚠ `date` 字段一律是**本地日期**（前端当 `YYYY-MM-DD` 用）。
            # 本行原来写的是 `r.return_time.isoformat()` —— `return_time` 是
            # DateTimeField，`.isoformat()` 给的是 **UTC 带偏移**的时间串，于是：
            #   ① `date` 字段语义不一致（同文件其它 6 处都是纯日期）；
            #   ② 前端 `.slice(0, 10)` / `formatDate` 拿到 **UTC 日期**，
            #      北京时间 00:00–08:00 的回收记录会显示成**前一天**。
            # 与 `created_at` 分支、以及 `views_capture` 里
            # `return_time__date__gte`（Django 转本地时区）的口径对齐。
            'date': timezone.localdate(r.return_time).isoformat() if r.return_time else (timezone.localdate(r.created_at).isoformat() if r.created_at else ''),
            'ledger_no': r.ledger_no,
            'owner_name': r.owner_name,
            # 签字是 base64 data URL；领养人拿到空串，前端据此不渲染。
            **owner_contact_fields(user, r),
            'reason': r.reason,
            'operator_name': r.operator_name,
            'created_at': r.created_at.isoformat() if r.created_at else '',
        })

    # 领养后回访打卡 —— 领养完成**之后**的全过程，之前完全没进档案。
    # 打卡的 photo 不是 Pet 上的阶段照片，单独塞进事件的 photos 里，
    # 这样 JSON 时间线与打印页的照片墙共用同一条数据。
    checkins = CheckIn.objects.filter(pet=pet).select_related('operator').order_by('created_at', 'id')
    for ci in checkins:
        photo_url = _image_url(ci.photo)
        reviewer = ci.operator
        reviewer_name = ''
        if reviewer is not None:
            reviewer_name = reviewer.get_full_name() or reviewer.username
        events.append({
            'type': 'checkin',
            'type_display': '领养后打卡',
            'content': f'{ci.month or "回访"} 回访打卡：{ci.note or "（未填近况）"}',
            'time': _local_time(ci.created_at),
            # 打卡没有业务发生时间字段，用提交时间（与接口列表口径一致）
            'date': timezone.localdate(ci.created_at).isoformat() if ci.created_at else '',
            'month': ci.month,
            'adopter_name': ci.adopter_name,
            'note': ci.note,
            'status': ci.status,
            'status_label': dict(CheckIn.STATUS_CHOICES).get(ci.status, ci.status),
            'review_note': ci.review_note,
            'reviewer_name': reviewer_name,
            'operator_name': ci.adopter_name,
            'photos': ([{'field': 'checkin', 'label': f'回访打卡 · {ci.month}',
                         'url': photo_url}] if photo_url else []),
        })

    # 按日期排序（空日期排最后）
    events.sort(key=lambda x: x.get('date') or '', reverse=False)

    # `stage` 是派生的**当前阶段**（见 `pet_stage`）：放归单已开到一半、
    # 诊疗完成待出院这类中间态，`Pet.status` 不会改，档案必须按单据算。
    # 保留原始 `status` 供调用方核对，展示一律用 stage。
    brief = pet_brief(pet)
    stage, stage_display = pet_stage(pet)
    brief['stage'] = stage
    brief['stage_display'] = stage_display

    return {
        'pet': serialize_instance(pet),
        # pet_brief / photos 为「一宠一档」详情抽屉补充：统一的中文字段标签 +
        # 按阶段排好序的照片列表（前端据此渲染照片墙并点击放大）。
        # 保留原有 pet/events 结构，领养人端的时间线不受影响。
        'pet_brief': brief,
        'photos': pet_photo_list(pet),
        'events': events,
    }


# ---------------------------------------------------------------------------
# 打印页专用的「事件 → 中文字段行」转换
# ---------------------------------------------------------------------------
# ⚠ 放在这里而不是模板里：模板里写 `{% if ev.type == 'capture' %}` 一张
#   十行的表，等于把事件字段名再抄一遍 —— 接口加了字段，打印页没人会去
#   同步模板，纸上就少一行，而且**不报错**。
_STATUS_LABELS = {
    'transfer': dict(Transfer.STATUS_CHOICES),
    'treatment': dict(Treatment.STATUS_CHOICES),
    'release': dict(Release.STATUS_CHOICES),
    'adoption': dict(Adoption.STATUS_CHOICES),
}


def event_print_rows(ev):
    """把一条 lifecycle 事件转成 `[(标签, 值)]`，空值不印（避免整片空白栏）。

    ⚠ 四要素里「时间 / 操作内容 / 操作人」是**每条事件都要有**的，
    所以前两项在这里统一加，不写进 type 分支 —— 新增事件类型时忘了写
    分支里的「操作内容」，纸上就会少一行，而且没有任何报错。
    「照片」由 `ev['photos']` 走模板单独的图片区渲染。
    """
    t = ev.get('type')
    raw = []
    if ev.get('content'):
        raw.append(('操作内容', ev['content']))
    if ev.get('time'):
        raw.append(('操作时间', ev['time']))
    # 「操作人」也统一在这里补：早先写在各 type 分支里，新增事件类型时
    # 漏写一行，纸上就是一片空白且没有任何报错（用户明确要求操作人要记全）。
    raw.append(('操作人', ev.get('operator_name') or '未记录'))
    if t == 'capture':
        raw.extend([
            ('捕捉单号', ev.get('ledger_no')),
            ('捕捉点', ev.get('shelter_name')),
            ('来源小区', ev.get('community_name')),
            ('地址', ev.get('address')),
            ('物业', ev.get('property_name')),
            ('物业交接人', ' '.join(x for x in (ev.get('contact_person'), ev.get('contact_phone')) if x)),
        ])
    elif t == 'transfer':
        raw.extend([
            ('转运单号', ev.get('ledger_no')),
            ('发出捕捉点', ev.get('from_shelter_name')),
            ('接收医院', ev.get('to_hospital_name')),
            ('状态', _STATUS_LABELS['transfer'].get(ev.get('status'), ev.get('status'))),
            ('签收时间', (ev.get('received_at') or '')[:19].replace('T', ' ')),
        ])
    elif t == 'treatment':
        raw.extend([
            ('诊疗单号', ev.get('ledger_no')),
            ('医院', ev.get('hospital_name')),
            ('诊疗项目', '、'.join(ev.get('items') or [])),
            ('处方用药', ev.get('rx_summary')),
            ('医嘱', ev.get('advice')),
            ('芯片号', ev.get('chip_no')),
            ('状态', _STATUS_LABELS['treatment'].get(ev.get('status'), ev.get('status'))),
        ])
    elif t == 'surgery':
        raw.extend([
            ('手术单号', ev.get('ledger_no')),
            ('手术医院', ev.get('hospital_name')),
            ('手术日期', ev.get('surgery_date')),
            ('主刀医生', ev.get('surgeon')),
            ('麻醉方式', ev.get('anesthesia')),
            ('手术过程', ev.get('procedure')),
            ('注意事项', ev.get('notes')),
        ])
    elif t == 'prescription':
        raw.extend([
            ('诊疗单号', ev.get('ledger_no')),
            ('医院', ev.get('hospital_name')),
            ('药品', ev.get('drug_name')),
            ('数量', ev.get('quantity')),
            ('所属项目', ev.get('item_name')),
            ('开方兽医', ev.get('vet_name')),
            ('手写处方', ev.get('manual_rx')),
        ])
    elif t == 'discharge':
        raw.extend([
            ('诊疗单号', ev.get('ledger_no')),
            ('出院医院', ev.get('hospital_name')),
            ('出院去向', ev.get('disposal_label') or ev.get('disposal')),
            ('原建议去向', ev.get('advised_label')),
        ])
    elif t == 'release':
        raw.extend([
            ('放归单号', ev.get('ledger_no')),
            ('放归小区', ev.get('community_name')),
            ('接收人', ev.get('receiver_name')),
            ('状态', _STATUS_LABELS['release'].get(ev.get('status'), ev.get('status'))),
        ])
    elif t == 'adoption':
        raw.extend([
            ('领养单号', ev.get('ledger_no')),
            ('领养人', ev.get('adopter_name')),
            ('所属医院', ev.get('hospital_name')),
            ('状态', _STATUS_LABELS['adoption'].get(ev.get('status'), ev.get('status'))),
        ])
    elif t == 'euthanasia':
        raw.extend([
            ('单据号', ev.get('ledger_no')),
            ('处置医院', ev.get('hospital_name')),
            ('处置原因', ev.get('reason')),
        ])
    elif t == 'owner_return':
        raw.extend([
            ('领回单号', ev.get('ledger_no')),
            ('领回人', ev.get('owner_name')),
            ('联系电话', ev.get('owner_phone')),
            ('身份证', ev.get('owner_id_card')),
            ('住址', ev.get('owner_address')),
            ('原因', ev.get('reason')),
        ])
    elif t == 'checkin':
        raw.extend([
            ('回访月份', ev.get('month')),
            ('领养人', ev.get('adopter_name')),
            ('近况备注', ev.get('note')),
            ('审核状态', ev.get('status_label') or ev.get('status')),
            ('审核意见', ev.get('review_note')),
            ('审核人', ev.get('reviewer_name')),
        ])
    else:
        raw.append(('说明', ev.get('type_display') or t))
    return [(label, value) for label, value in raw
            if value not in (None, '', [])]
