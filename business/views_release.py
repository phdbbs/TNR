"""
Task 8: 放养闭环
- 放养列表/创建/确认
- 匹配原捕捉小区
"""
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Release, Pet, Capture
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    generate_ledger_no, get_district_filtered_queryset,
    get_active_pet, get_scoped_object, pet_has_active_adoption,
)
from core.models import Institution


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def release_list(request):
    """放养列表（待放养/已放养）"""
    qs = get_district_filtered_queryset(Release, request.user)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    data = [serialize_instance(r) for r in qs]
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def release_create(request):
    """创建放养记录（从医院回收，匹配原小区）

    请求体示例:
    {
        "pet_id": 1,
        "community_id": 5,       // 可选，不传则从捕捉记录匹配
        "receiver_name": "张物业",
        "receiver_phone": "13800003001"
    }
    """
    data = parse_json_body(request)
    user = request.user

    pet_id = data.get('pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    # 按区县范围取宠物，并排除已逻辑删除的档案
    pet = get_active_pet(pet_id, user)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

    if pet.status not in ('in_treatment', 'pending_adopt'):
        return json_fail(f'宠物当前状态({pet.get_status_display()})不可放养')

    # 互斥校验：不能与未完结的领养流程同时占用同一只动物
    if pet_has_active_adoption(pet):
        return json_fail('该宠物已有未完结的领养记录，请先撤销领养后再办理放养')

    # 防止重复创建待放养记录
    if Release.objects.filter(pet=pet, status='pending').exists():
        return json_fail('该宠物已有待放养记录，请勿重复创建')

    # 匹配原小区
    community_id = data.get('community_id')
    community = None
    if community_id:
        community = Institution.objects.filter(id=community_id, type='community').first()

    if not community and pet.capture:
        # 从捕捉记录匹配原小区
        community = pet.capture.community

    if not community:
        return json_fail('无法匹配原小区，请指定 community_id')

    district_id = pet.district_id or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    release = Release.objects.create(
        pet=pet,
        pet_code=pet.code,
        community=community,
        community_name=community.name,
        receiver_name=data.get('receiver_name', ''),
        receiver_phone=data.get('receiver_phone', ''),
        status='pending',
        operator=user,
        operator_name=user.get_full_name() or user.username,
        ledger_no=generate_ledger_no('REL'),
        district_id=district_id,
    )

    return json_ok(serialize_instance(release), message='放养记录创建成功，待小区确认')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def release_confirm(request, pk):
    """小区确认放养（接收人签字确认）

    请求体示例:
    {
        "receiver_name": "张物业",
        "receiver_phone": "13800003001",
        "signature": "base64..."
    }
    """
    data = parse_json_body(request)
    user = request.user

    release = get_scoped_object(Release, pk, user)
    if release is None:
        return json_fail('放养记录不存在或无权访问', status=404)

    if release.status != 'pending':
        return json_fail(f'当前状态({release.get_status_display()})不可确认')

    if release.pet_id and release.pet.is_deleted:
        return json_fail('该宠物档案已作废，无法确认放养')

    release.receiver_name = data.get('receiver_name', release.receiver_name)
    release.receiver_phone = data.get('receiver_phone', release.receiver_phone)
    release.signature = data.get('signature', '')
    release.status = 'released'
    release.released_at = timezone.localdate()
    release.save(update_fields=[
        'receiver_name', 'receiver_phone', 'signature', 'status', 'released_at',
    ])

    # 更新宠物状态
    pet = release.pet
    pet.status = 'released'
    pet.save(update_fields=['status'])

    return json_ok(serialize_instance(release), message='放养确认成功')
