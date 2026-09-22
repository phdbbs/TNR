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
    body_str, body_int,
    generate_ledger_no, get_district_filtered_queryset,
    get_active_pet, get_scoped_object, pet_has_active_adoption,
    resolve_community,
)


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
    """创建放养记录（放回原捕捉小区）

    请求体示例:
    {
        "pet_id": 1,
        "community_name": "阳光花园小区",   // 可选，不传则取捕捉单登记的小区名
        "community_id": 5,                 // 可选，显式指定小区机构（同区县）
        "receiver_name": "张物业",          // 可选，确认放养时再填
        "receiver_phone": "13800003001"
    }
    """
    data = parse_json_body(request)
    user = request.user

    pet_id = body_int(data, 'pet_id')
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
    #
    # 「小区」在本项目里是**手填文本**（政府端已取消小区管理页签，捕捉登记也是
    # 输入框 + 模糊搜索，不做枚举），所以匹配不到机构档案是**正常情况**，
    # 不能因此拒绝放养。原实现只认 `Capture.community` 外键：
    #   1. 捕捉登记从不回填该外键 → 历史数据全是 None；
    #   2. 界面上也没有指定小区的入口。
    # 于是「发起放养」永远返回「无法匹配原小区，请指定 community_id」，
    # 整个放养流程在界面上不可达，而且不报错，页面看起来只是「还没有数据」。
    #
    # 现在：小区名以**捕捉单登记的文本**为准（可显式覆盖），
    # 能匹配到机构档案就顺带回填外键（`Release.community` 本身 nullable）。
    capture = pet.capture
    community_name = body_str(data, 'community_name').strip()
    if not community_name and capture is not None:
        community_name = (capture.community_name or '').strip()

    community = resolve_community(
        pet.district,
        body_int(data, 'community_id') or (capture.community_id if capture else None),
        community_name,
    )
    if community and not community_name:
        community_name = community.name

    if not community_name:
        return json_fail('无法匹配原小区，请在捕捉单中补充小区名称')

    district_id = pet.district_id or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    release = Release.objects.create(
        pet=pet,
        pet_code=pet.code,
        community=community,
        community_name=community_name,
        receiver_name=body_str(data, 'receiver_name'),
        receiver_phone=body_str(data, 'receiver_phone'),
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

    release.receiver_name = body_str(data, 'receiver_name', release.receiver_name)
    release.receiver_phone = body_str(data, 'receiver_phone', release.receiver_phone)
    release.signature = body_str(data, 'signature')
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
