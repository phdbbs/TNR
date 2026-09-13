"""
Task 11: 安乐死处置
- 安乐死列表/创建
- 遗体领取
"""
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Euthanasia, Pet
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    generate_ledger_no, get_district_filtered_queryset,
)


@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def euthanasia_list(request):
    """安乐死记录列表"""
    qs = get_district_filtered_queryset(Euthanasia, request.user)

    user = request.user
    if user.role == 'hospital':
        if user.institution_id:
            qs = qs.filter(hospital_id=user.institution_id)
        else:
            qs = qs.none()

    data = [serialize_instance(e) for e in qs]
    return json_ok(data)


@csrf_exempt
@role_required('hospital', 'gov_city', 'gov_district')
@login_required
def euthanasia_create(request):
    """登记安乐死记录

    请求体示例:
    {
        "pet_id": 1,
        "reason": "严重外伤感染，无法救治",
        "condition": "后腿骨折感染，多处伤口化脓",
        "euthanized_at": "2025-01-20"
    }
    """
    data = parse_json_body(request)
    user = request.user

    pet_id = data.get('pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    try:
        pet = Pet.objects.get(id=pet_id)
    except Pet.DoesNotExist:
        return json_fail('宠物不存在')

    # 仅诊疗中/待领养的在院宠物可安乐死，防止误操作已领养、已放养等终态宠物
    if pet.status not in ('in_treatment', 'pending_adopt'):
        return json_fail(f'宠物当前状态({pet.get_status_display()})不可安乐死，仅诊疗中/待领养宠物可登记')

    reason = data.get('reason', '').strip()
    if not reason:
        return json_fail('安乐死原因不能为空')

    hospital = pet.hospital or user.institution
    if not hospital:
        return json_fail('缺少医院信息')

    district_id = pet.district_id or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    # 解析日期
    euthanized_at = None
    date_str = data.get('euthanized_at')
    if date_str:
        try:
            euthanized_at = datetime.strptime(str(date_str)[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            euthanized_at = None

    record = Euthanasia.objects.create(
        pet=pet,
        pet_code=pet.code,
        hospital=hospital,
        hospital_name=hospital.name,
        reason=reason,
        condition=data.get('condition', ''),
        euthanized_at=euthanized_at,
        body_received=False,
        operator=user,
        operator_name=user.get_full_name() or user.username,
        ledger_no=generate_ledger_no('EUT'),
        district_id=district_id,
    )

    # 更新宠物状态
    pet.status = 'euthanized'
    pet.save(update_fields=['status'])

    return json_ok(serialize_instance(record), message='安乐死登记成功')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def body_receive(request, pk):
    """捕捉点领取遗体

    请求体示例:
    {
        "receiver_name": "朝阳捕捉点操作员"
    }
    """
    data = parse_json_body(request)

    try:
        record = Euthanasia.objects.get(id=pk)
    except Euthanasia.DoesNotExist:
        return json_fail('安乐死记录不存在', status=404)

    if record.body_received:
        return json_fail('遗体已被领取')

    record.body_received = True
    record.body_received_at = timezone.localdate()
    record.body_received_by = request.user
    record.body_received_by_name = data.get('receiver_name', request.user.get_full_name() or request.user.username)
    record.save(update_fields=[
        'body_received', 'body_received_at', 'body_received_by', 'body_received_by_name',
    ])

    return json_ok(serialize_instance(record), message='遗体领取登记成功')
