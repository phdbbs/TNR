"""
Task 5: 转运拆分下发
- 转运列表/创建/签收/驳回
- 支持拆分至多家医院
"""
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Transfer, Pet, Capture
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    generate_ledger_no, get_district_filtered_queryset,
    resolve_district_scope, recalc_capture_status, get_scoped_object,
    busy_transfer_codes,
)
from core.models import Institution


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district', 'hospital')
@login_required
def transfer_list(request):
    """转运列表（捕捉点看发出，医院看接收）"""
    user = request.user

    if user.role == 'hospital':
        # 医院只看发给自己的（不按区县过滤，因为转运可能跨区县）
        if user.institution_id:
            qs = Transfer.objects.filter(to_hospital_id=user.institution_id)
        else:
            qs = Transfer.objects.none()
    else:
        qs = get_district_filtered_queryset(Transfer, user)
        if user.role == 'shelter':
            # 捕捉点只看自己发出的
            if user.institution_id:
                qs = qs.filter(from_shelter_id=user.institution_id)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    data = [serialize_instance(t) for t in qs]
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def transfer_create(request):
    """创建转运记录（支持拆分至多家医院 / 简单单医院两种格式）

    格式一（拆分多家医院）:
    {
        "capture_id": 1,
        "items": [
            {"hospital_id": 3, "pet_ids": [1, 2]},
            {"hospital_id": 4, "pet_ids": [3]}
        ]
    }

    格式二（单医院 + 编号数组，前端默认使用此格式）:
    {
        "from_shelter_id": 11,
        "to_hospital_id": 13,
        "pet_codes": ["TNR2501001", "TNR2501002"],
        "pet_count": 2,
        "note": "备注"
    }
    """
    data = parse_json_body(request)
    user = request.user

    # 获取来源捕捉点
    shelter_id = data.get('from_shelter_id') or getattr(user, 'institution_id', None)
    if not shelter_id:
        return json_fail('缺少捕捉点信息')

    try:
        shelter = Institution.objects.get(id=shelter_id, type='shelter')
    except Institution.DoesNotExist:
        return json_fail('捕捉点不存在')

    # 归属区县以**发出捕捉点**为准，不能取操作员所属区县：
    # 现场两个捕捉点操作员都挂在「全市（市级）」下，而前端并不提交 district_id，
    # 取操作员区县会让转运单全部落到市级 —— 本区县政府在区县隔离下看不到本区
    # 转运单（捕捉单当初就是这么错的）。
    district, err = resolve_district_scope(user, shelter, data.get('district_id'))
    if err:
        return json_fail(err)
    district_id = district.id

    capture_id = data.get('capture_id')
    capture = Capture.objects.filter(id=capture_id).first() if capture_id else None

    # 统一构造 items 列表：支持两种格式
    items = data.get('items')
    if not items:
        # 格式二：单医院 + pet_codes（字符串编号）
        to_hospital_id = data.get('to_hospital_id')
        pet_codes_raw = data.get('pet_codes', [])
        if isinstance(pet_codes_raw, str):
            pet_codes_raw = [c.strip() for c in pet_codes_raw.split(',') if c.strip()]
        if not to_hospital_id or not pet_codes_raw:
            return json_fail('缺少转运明细（items 或 to_hospital_id+pet_codes）')
        items = [{'hospital_id': to_hospital_id, 'pet_codes': pet_codes_raw}]

    # 预校验（两段式：先全部校验完，再落库）：
    # 1) 已在**未结**转运单里的动物不能再下发 —— 否则同一只动物会同时挂在多张
    #    未结单据上（医院收到重复单、捕捉单状态推导跟着乱）。旧版「重新下发」
    #    没有这层校验，同一张被驳回的单可以反复下发，就是这么乱起来的。
    #    rejected / void 不占位，所以被驳回的动物仍能重新被选进来安排转运。
    # 2) 只能转运本区县的动物（原来裸查 code__in，猜到编号就能跨区县下发）。
    requested_codes = set()
    for item in items:
        requested_codes |= {c for c in (item.get('pet_codes') or []) if c}

    if requested_codes:
        busy = busy_transfer_codes(requested_codes, district_id)
        if busy:
            return json_fail(
                '以下动物已在未结的转运单中，不能重复下发：' + '、'.join(sorted(busy))
                + '。如需改派，请先在「转运明细」里撤回原单。')

        foreign = sorted(Pet.objects.filter(
            code__in=requested_codes, is_deleted=False
        ).exclude(district_id=district_id).values_list('code', flat=True))
        if foreign:
            return json_fail('以下动物不属于本区县，无法转运：' + '、'.join(foreign))

    created = []
    assigned_pet_ids = set()  # 防止同一宠物被拆分进多家医院
    for item in items:
        hospital_id = item.get('hospital_id')
        if not hospital_id:
            continue

        try:
            hospital = Institution.objects.get(id=hospital_id, type='hospital')
        except Institution.DoesNotExist:
            continue

        # 优先使用 pet_ids（数字ID），否则用 pet_codes（字符串编号）
        # is_deleted=False：已作废（逻辑删除）的宠物不参与转运
        pet_ids = [pid for pid in item.get('pet_ids', []) if pid not in assigned_pet_ids]
        pet_codes = item.get('pet_codes', [])
        if pet_ids:
            pets = list(Pet.objects.filter(id__in=pet_ids, status='in_transit', is_deleted=False))
        elif pet_codes:
            pets = list(Pet.objects.filter(code__in=pet_codes, status='in_transit', is_deleted=False))
        else:
            continue

        # 拆分场景：剔除已分配给前一家医院的宠物
        pets = [p for p in pets if p.id not in assigned_pet_ids]
        if not pets:
            continue
        assigned_pet_ids.update(p.id for p in pets)

        pet_code_list = [p.code for p in pets]
        # 未显式指定捕捉单时，按宠物归属回填，保证捕捉单状态可被准确推导
        if capture is None:
            capture = next((p.capture for p in pets if p.capture_id), None)

        transfer = Transfer.objects.create(
            capture=capture,
            from_shelter=shelter,
            from_shelter_name=shelter.name,
            to_hospital=hospital,
            to_hospital_name=hospital.name,
            pet_codes=','.join(pet_code_list),
            pet_count=len(pet_code_list),
            status='pending',
            # 备注：前端表单一直提交 note（接口文档也声明接受），此前模型没有这一列，
            # 用户填的内容被静默丢弃。拆分场景允许每个 item 各自带备注。
            note=item.get('note') or data.get('note', '') or '',
            operator=user,
            operator_name=user.get_full_name() or user.username,
            ledger_no=generate_ledger_no('TRF'),
            district_id=district_id,
        )

        # 更新宠物归属医院（状态保持 in_transit 直到签收）
        for pet in pets:
            pet.hospital = hospital
            pet.save(update_fields=['hospital'])

        created.append(serialize_instance(transfer))

    # 转运单提交后重算相关捕捉单状态（待转运 / 部分转运 / 已完成）
    for cap in {t['capture'] for t in created if t.get('capture')}:
        capture_obj = Capture.objects.filter(id=cap).first()
        if capture_obj:
            recalc_capture_status(capture_obj)

    return json_ok(created, message=f'创建 {len(created)} 条转运记录')


@csrf_exempt
@role_required('hospital')
@login_required
def transfer_receive(request, pk):
    """医院签收转运"""
    try:
        transfer = Transfer.objects.get(id=pk)
    except Transfer.DoesNotExist:
        return json_fail('转运记录不存在', status=404)

    if transfer.status != 'pending':
        return json_fail(f'当前状态({transfer.status})不可签收')

    user = request.user
    if user.institution_id != transfer.to_hospital_id:
        return json_fail('无权签收此转运记录')

    transfer.status = 'received'
    transfer.received_at = timezone.localdate()
    transfer.save(update_fields=['status', 'received_at'])

    # 更新宠物状态为待诊疗
    pet_codes = [c.strip() for c in transfer.pet_codes.split(',') if c.strip()]
    Pet.objects.filter(code__in=pet_codes).update(status='in_treatment')

    if transfer.capture_id:
        recalc_capture_status(transfer.capture)

    return json_ok(serialize_instance(transfer), message='签收成功')


@csrf_exempt
@role_required('hospital')
@login_required
def transfer_reject(request, pk):
    """医院驳回转运"""
    data = parse_json_body(request)

    try:
        transfer = Transfer.objects.get(id=pk)
    except Transfer.DoesNotExist:
        return json_fail('转运记录不存在', status=404)

    if transfer.status != 'pending':
        return json_fail(f'当前状态({transfer.status})不可驳回')

    user = request.user
    if user.institution_id != transfer.to_hospital_id:
        return json_fail('无权驳回此转运记录')

    transfer.status = 'rejected'
    transfer.reject_reason = data.get('reason', '')
    transfer.save(update_fields=['status', 'reject_reason'])

    # 医院退回：宠物回退为在途，并解除医院归属，
    # 使其真正回到「未转运」状态（可被领回、可编辑/删除捕捉单）
    pet_codes = [c.strip() for c in transfer.pet_codes.split(',') if c.strip()]
    Pet.objects.filter(code__in=pet_codes).update(status='in_transit', hospital=None)

    # 若该捕捉单还存在其它未退回的转运单，则保持已转运，不重复回退
    if transfer.capture_id:
        recalc_capture_status(transfer.capture)

    return json_ok(serialize_instance(transfer), message='已驳回')


# 说明：「重新下发被驳回的转运单」（transfer_resend）已**移除**。
# 原因：原实现把被驳回的单据再复制成一张新的 pending 单，而原单永远停留在
# rejected —— 于是同一张被驳回的单可以反复下发，同一只动物会同时挂在多张
# 未结单据上（医院收到重复单、捕捉单状态推导跟着错乱）。
# 现在改为：医院驳回时宠物已经回退为 in_transit + 解除医院归属（见
# transfer_reject），这些动物**自动回到「待转运」的备选框**，
# 由操作员重新勾选、按需改派医院，再下发一张新单即可。
# 「同一动物不能挂在多张未结单上」由 transfer_create 的预校验兜底。


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def transfer_withdraw(request, pk):
    """捕捉点撤回转运单（医院未签收时才可撤回）。

    撤回后：
    - 转运单状态变为 ``void``（撤回），不再占位
    - 宠物状态回退为 ``in_transit``、解除医院归属
    - 捕捉单状态重算（可能从「部分转运」回退为「待转运」）
    - 宠物可被选到新的转运单中
    """
    user = request.user

    # 先按区县范围取单（禁止裸查：裸查不做范围校验，任何登录用户猜到主键
    # 就能操作其他区县的转运单）。区县过滤之后再叠加更严的「发出捕捉点」校验。
    transfer = get_scoped_object(Transfer, pk, user)
    if transfer is None:
        return json_fail('转运记录不存在', status=404)

    if transfer.status != 'pending':
        return json_fail(f'当前状态({transfer.get_status_display()})不可撤回，仅待签收的转运单可撤回')

    if user.role == 'shelter' and user.institution_id and user.institution_id != transfer.from_shelter_id:
        return json_fail('无权撤回此转运记录')

    transfer.status = 'void'
    transfer.save(update_fields=['status'])

    pet_codes = [c.strip() for c in transfer.pet_codes.split(',') if c.strip()]
    # 宠物编号全局唯一，按编号回退状态；同时限定本单区县以防编号被伪造
    Pet.objects.filter(code__in=pet_codes, district_id=transfer.district_id).update(
        status='in_transit', hospital=None)

    if transfer.capture_id:
        recalc_capture_status(transfer.capture)

    return json_ok(serialize_instance(transfer), message='转运单已撤回，宠物回退为待转运')
