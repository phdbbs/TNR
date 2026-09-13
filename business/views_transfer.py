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

    district_id = data.get('district_id') or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

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
        pet_ids = [pid for pid in item.get('pet_ids', []) if pid not in assigned_pet_ids]
        pet_codes = item.get('pet_codes', [])
        if pet_ids:
            pets = list(Pet.objects.filter(id__in=pet_ids, status='in_transit'))
        elif pet_codes:
            pets = list(Pet.objects.filter(code__in=pet_codes, status='in_transit'))
        else:
            continue

        # 拆分场景：剔除已分配给前一家医院的宠物
        pets = [p for p in pets if p.id not in assigned_pet_ids]
        if not pets:
            continue
        assigned_pet_ids.update(p.id for p in pets)

        pet_code_list = [p.code for p in pets]
        transfer = Transfer.objects.create(
            capture=capture,
            from_shelter=shelter,
            from_shelter_name=shelter.name,
            to_hospital=hospital,
            to_hospital_name=hospital.name,
            pet_codes=','.join(pet_code_list),
            pet_count=len(pet_code_list),
            status='pending',
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

    # 宠物状态回退为 in_transit
    pet_codes = [c.strip() for c in transfer.pet_codes.split(',') if c.strip()]
    Pet.objects.filter(code__in=pet_codes).update(status='in_transit')

    return json_ok(serialize_instance(transfer), message='已驳回')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def transfer_resend(request, pk):
    """重新下发被驳回的转运单。

    以原转运单的宠物/医院重建一张待签收转运单，并将宠物状态回退为在途、
    重新绑定医院，供医院再次签收或驳回。
    """
    try:
        transfer = Transfer.objects.get(id=pk)
    except Transfer.DoesNotExist:
        return json_fail('转运记录不存在', status=404)

    if transfer.status != 'rejected':
        return json_fail(f'当前状态({transfer.status})不可重新下发')

    user = request.user
    if user.role == 'shelter' and user.institution_id and user.institution_id != transfer.from_shelter_id:
        return json_fail('无权重新下发此转运记录')

    pet_codes = [c.strip() for c in transfer.pet_codes.split(',') if c.strip()]
    pets = Pet.objects.filter(code__in=pet_codes)
    if not pets.exists():
        return json_fail('原转运单关联的宠物不存在，无法重新下发')

    district_id = transfer.district_id or getattr(user, 'district_id', None)
    new_transfer = Transfer.objects.create(
        capture=transfer.capture,
        from_shelter=transfer.from_shelter,
        from_shelter_name=transfer.from_shelter_name,
        to_hospital=transfer.to_hospital,
        to_hospital_name=transfer.to_hospital_name,
        pet_codes=','.join(pet_codes),
        pet_count=len(pet_codes),
        status='pending',
        operator=user,
        operator_name=user.get_full_name() or user.username,
        ledger_no=generate_ledger_no('TRF'),
        district_id=district_id or transfer.district_id,
    )

    # 重新绑定医院，宠物状态保持/回退为在途
    for pet in pets:
        pet.hospital = transfer.to_hospital
        pet.status = 'in_transit'
        pet.save(update_fields=['hospital', 'status'])

    return json_ok(serialize_instance(new_transfer), message='重新下发成功')
