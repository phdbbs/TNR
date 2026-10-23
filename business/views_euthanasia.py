"""
Task 11: 安乐死处置
- 安乐死列表/创建
- 遗体领取
"""
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Euthanasia
from business.services_discharge import discharge_candidate_error
from business.services_treatment import dispose_for_discharge
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_str, body_int,
    generate_doc_no, get_district_filtered_queryset,
    get_active_pet, get_hospital_scoped_pet, get_scoped_object,
    hospital_pet_write_error, pet_has_approved_application,
    pet_has_pending_return, validate_image_upload,
)


@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def euthanasia_list(request):
    """安乐死 / 死亡记录列表。

    医院端按**冗余 `institution`** 收口，且**不套区县口径**：
    跨区县送医时 `Euthanasia.district` 是**动物**的区县（与本院不同），
    先按区县过滤再按机构过滤，本院刚登记的单据会立刻查不到 ——
    出院页那一行就永远不出现，而接口返回 200。
    """
    user = request.user
    if user.role == 'hospital':
        if user.institution_id:
            qs = Euthanasia.objects.filter(
                Q(institution_id=user.institution_id)
                | Q(institution_id__isnull=True,
                    pet__hospital_id=user.institution_id)
            )
        else:
            qs = Euthanasia.objects.none()
    else:
        qs = get_district_filtered_queryset(Euthanasia, user)

    data = [serialize_instance(e) for e in qs]
    return json_ok(data)


@csrf_exempt
@role_required('hospital')
@login_required
def euthanasia_create(request):
    """登记死亡 / 安乐死记录（第四十八轮：合并为「死亡（含安乐死）」）。

    请求体示例:
    {
        "pet_id": 1,
        "kind": "euthanasia",              // euthanasia（默认）/ dead
        "reason": "严重外伤感染，无法救治",
        "condition": "后腿骨折感染，多处伤口化脓",
        "euthanized_at": "2025-01-20"
    }
    """
    data = parse_json_body(request)
    user = request.user

    pet_id = body_int(data, 'pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    # 处置方式：默认安乐死（向后兼容只传 pet_id + reason 的旧调用）
    kind = body_str(data, 'kind').strip() or 'euthanasia'
    if kind not in dict(Euthanasia.KIND_CHOICES):
        return json_fail(f'处置方式「{kind}」不合法')
    kind_label = dict(Euthanasia.KIND_CHOICES)[kind]

    # 取单口径按角色分叉：医院走「本院在治 ∪ 本院诊疗过」（跨区县送医也能登记），
    # 其余角色仍按区县。写单据前再校一次「动物当前是否挂在本院」。
    if user.role == 'hospital':
        pet = get_hospital_scoped_pet(pet_id, user)
    else:
        pet = get_active_pet(pet_id, user)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

    err = hospital_pet_write_error(pet, user)
    if err:
        return json_fail(err)

    # 先于状态校验：运回单上的动物状态已是 `return_pending`，
    # 先说「状态不可登记」会让操作员漏掉「去撤回运回单」这个动作。
    if pet_has_pending_return(pet):
        return json_fail('该宠物已在运回捕捉点的单据上，请先撤回或等待捕捉点处理')

    # 仅诊疗中/待领养的在院宠物可登记，防止误操作已领养、已放归等终态宠物
    if pet.status not in ('in_treatment', 'pending_adopt'):
        return json_fail(f'宠物当前状态({pet.get_status_display()})不可{kind_label}，'
                         '仅诊疗中/待领养宠物可登记')
    # 建议去向必须是「安乐死 / 死亡」（第五十七轮）：建议领养的动物
    # 不能从这里被登记成死亡，去向分流判据见 `services_discharge`。
    err = discharge_candidate_error(pet, 'death')
    if err:
        return json_fail(err)
    # 已批准的线上申请不写 Pet.status，状态校验拦不住这类预留
    if pet_has_approved_application(pet):
        return json_fail('该宠物已有已批准的领养申请（待出库），'
                         f'请先处理后再登记{kind_label}')

    reason = body_str(data, 'reason').strip()
    if not reason:
        return json_fail(f'{kind_label}原因不能为空')

    hospital = pet.hospital or user.institution
    if not hospital:
        return json_fail('缺少医院信息')

    district_id = pet.district_id or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    # 处置日期**必填**（第五十二轮电脑端巡检发现）。
    # 表单上标了「处置日期\*」，接口却把空值/解析失败一律落成 `None`：
    # 一张没有日期的不可逆处置单据，台账按日期圈选时靠 `created_at` 兜底才没消失，
    # 打印件（`views_print` 的「安乐死日期」）与政府端监管台账则是直接空着。
    # `Euthanasia.euthanized_at` 字段本身仍允许为空，因为历史数据里有这种行；
    # 收紧的是**新单据**的入口。
    date_str = data.get('euthanized_at')
    if not date_str:
        return json_fail(f'{kind_label}日期不能为空')
    try:
        euthanized_at = datetime.strptime(str(date_str)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return json_fail(f'{kind_label}日期「{date_str}」格式不正确，应为 YYYY-MM-DD')

    record = Euthanasia.objects.create(
        pet=pet,
        pet_code=pet.code,
        kind=kind,
        hospital=hospital,
        hospital_name=hospital.name,
        # 归属机构 = 执行医院（业务对象推导）
        institution=hospital,
        reason=reason,
        condition=body_str(data, 'condition'),
        euthanized_at=euthanized_at,
        body_received=False,
        operator=user,
        operator_name=user.get_full_name() or user.username,
        ledger_no=generate_doc_no('EUT'),
        district_id=district_id,
    )

    # 更新宠物状态 —— `euthanized` 与 `dead` 是**两个**终态，
    # 后续单据与统计口径不同（安乐死单 / 无害化处理），不能都写成 euthanized。
    pet.status = 'euthanized' if kind == 'euthanasia' else 'dead'
    pet.save(update_fields=['status'])

    return json_ok(serialize_instance(record), message=f'{kind_label}登记成功')


@csrf_exempt
@role_required('hospital')
@login_required
def euthanasia_outbound(request, pk):
    """死亡出库单：医院把遗体交给无害化处理厂（第五十四轮）。

    登记死亡 / 安乐死只是临床处置；遗体离院还需要一张出库凭证，
    记录交接人、接收机构（无害化处理厂）、接收人、接收单位、日期、签字，
    照片为非必需。此前只有捕捉点的「领取遗体」，医院侧没有出口，
    纸面上无法证明遗体由谁、何时、交给哪家单位。
    """
    data = parse_json_body(request)
    if request.POST:
        # multipart/form-data（含可选照片）走这里；JSON 与表单字段同名。
        data = request.POST.dict()

    user = request.user
    qs = Euthanasia.objects.filter(pk=pk)
    if user.institution_id:
        qs = qs.filter(
            Q(institution_id=user.institution_id)
            | Q(institution_id__isnull=True,
                pet__hospital_id=user.institution_id))
    else:
        qs = qs.none()
    record = qs.first()
    if record is None:
        return json_fail('死亡记录不存在或无权访问', status=404)

    if record.outbound_at:
        return json_fail('该记录已完成死亡出库，请勿重复操作')

    if record.pet_id and record.pet.is_deleted:
        return json_fail('该宠物档案已作废，无法办理出库')

    handover_name = body_str(data, 'handover_name').strip()
    receiving_org = body_str(data, 'receiving_org').strip()
    receiver_name = body_str(data, 'receiver_name').strip()
    signature = body_str(data, 'signature')
    reason = body_str(data, 'reason').strip() or (record.reason or '').strip()

    if not handover_name:
        return json_fail('请填写交接人')
    if not receiving_org:
        return json_fail('请填写接收机构（无害化处理厂）')
    if not receiver_name:
        return json_fail('请填写接收人')
    if not signature:
        return json_fail('请填写接收人签字')
    if not reason:
        return json_fail('请填写死亡 / 安乐死原因')

    date_str = data.get('outbound_at')
    if not date_str:
        return json_fail('请选择出库日期')
    try:
        outbound_at = datetime.strptime(str(date_str)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return json_fail(f'出库日期「{date_str}」格式不正确，应为 YYYY-MM-DD')

    photo_err = validate_image_upload(request.FILES.get('photo'), '出库照片')
    if photo_err:
        return json_fail(photo_err)

    with transaction.atomic():
        record.reason = reason
        record.outbound_at = outbound_at
        record.handover_name = handover_name
        record.receiving_org = receiving_org
        record.receiver_name = receiver_name
        record.signature = signature
        # 出库即「遗体已交接到接收单位」，旧字段同步落值，监管台账不必再分叉。
        record.body_received = True
        record.body_received_at = outbound_at
        record.body_received_by = user
        record.body_received_by_name = receiver_name
        if request.FILES.get('photo'):
            record.photo = request.FILES['photo']
        record.save(update_fields=[
            'reason', 'outbound_at', 'handover_name', 'receiving_org',
            'receiver_name', 'signature', 'body_received',
            'body_received_at', 'body_received_by', 'body_received_by_name',
            'photo',
        ])
        # 遗体交出 = 出院终态，诊疗单此刻才算办结（诊疗页只记了建议去向）。
        dispose_for_discharge(
            record.pet,
            'euthanized' if record.kind == 'euthanasia' else 'dead',
            user)

    return json_ok(serialize_instance(record), message='死亡出库登记成功')


@csrf_exempt
@role_required('shelter')
@login_required
def body_receive(request, pk):
    """捕捉点领取遗体

    请求体示例:
    {
        "receiver_name": "朝阳捕捉点操作员"
    }
    """
    data = parse_json_body(request)

    record = get_scoped_object(Euthanasia, pk, request.user)
    if record is None:
        return json_fail('安乐死记录不存在或无权访问', status=404)

    if record.body_received:
        return json_fail('遗体已被领取')

    record.body_received = True
    record.body_received_at = timezone.localdate()
    record.body_received_by = request.user
    record.body_received_by_name = body_str(
        data, 'receiver_name', request.user.get_full_name() or request.user.username)
    record.save(update_fields=[
        'body_received', 'body_received_at', 'body_received_by', 'body_received_by_name',
    ])

    return json_ok(serialize_instance(record), message='遗体领取登记成功')
