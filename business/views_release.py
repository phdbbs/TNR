"""
Task 8: 放归闭环
- 放归列表/创建/确认
- 匹配原捕捉小区
"""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Release, Pet, Capture
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_str, body_int,
    generate_doc_no, get_district_filtered_queryset,
    get_active_pet, get_hospital_scoped_pet, get_scoped_object,
    hospital_pet_write_error, pet_has_active_adoption,
    resolve_community,
)


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def release_list(request):
    """放归列表（待放归/已放归）。

    医院端按**冗余 `institution`** 收口，且**不套区县口径**：跨区县送医时
    `Release.district` 是**动物**的区县（与本院不同），先按区县过滤再按机构
    过滤会让本院刚发起的放归立刻查不到 —— 出院页那一行永远不出现。
    """
    user = request.user
    if user.role == 'hospital':
        if user.institution_id:
            qs = Release.objects.filter(
                Q(institution_id=user.institution_id)
                | Q(institution_id__isnull=True,
                    pet__hospital_id=user.institution_id)
            )
        else:
            qs = Release.objects.none()
    else:
        qs = get_district_filtered_queryset(Release, user)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    data = [serialize_instance(r) for r in qs]
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def release_create(request):
    """创建放归记录（放回原捕捉小区）

    请求体示例:
    {
        "pet_id": 1,
        "community_name": "阳光花园小区",   // 可选，不传则取捕捉单登记的小区名
        "community_id": 5,                 // 可选，显式指定小区机构（同区县）
        "receiver_name": "张物业",          // 可选，确认放归时再填
        "receiver_phone": "13800003001"
    }
    """
    data = parse_json_body(request)
    user = request.user

    pet_id = body_int(data, 'pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    # 取单口径按角色分叉：医院走「本院在治 ∪ 本院诊疗过」（跨区县送医也能发起放归），
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

    if pet.status not in ('in_treatment', 'pending_adopt'):
        return json_fail(f'宠物当前状态({pet.get_status_display()})不可放归')

    # 互斥校验：不能与未完结的领养流程同时占用同一只动物
    if pet_has_active_adoption(pet):
        return json_fail('该宠物已有未完结的领养记录，请先撤销领养后再办理放归')

    # 防止重复创建待放归记录
    if Release.objects.filter(pet=pet, status='pending').exists():
        return json_fail('该宠物已有待放归记录，请勿重复创建')

    # 匹配原小区
    #
    # 「小区」在本项目里是**手填文本**（政府端已取消小区管理页签，捕捉登记也是
    # 输入框 + 模糊搜索，不做枚举），所以匹配不到机构档案是**正常情况**，
    # 不能因此拒绝放归。原实现只认 `Capture.community` 外键：
    #   1. 捕捉登记从不回填该外键 → 历史数据全是 None；
    #   2. 界面上也没有指定小区的入口。
    # 于是「发起放归」永远返回「无法匹配原小区，请指定 community_id」，
    # 整个放归流程在界面上不可达，而且不报错，页面看起来只是「还没有数据」。
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
        ledger_no=generate_doc_no('REL'),
        district_id=district_id,
        # 归属机构 = 动物当时所在的机构（业务对象推导，不取操作员机构）
        institution_id=pet.hospital_id or pet.shelter_id,
    )

    return json_ok(serialize_instance(release), message='放归记录创建成功，待小区确认')


@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def release_transfer(request, pk):
    """把动物**转运回原小区**（第四十八轮：放归的第一段）。

    放归实际是两个动作：
      ① 医院 / 捕捉点把动物送回原捕捉小区  → `status='transferred'`
      ② 小区接收人签字确认「放归回捕捉地」  → `status='released'`

    原先只有一步（pending → released），等于「送回小区」与「放归」是同一
    件事：小区接收人还没签字、动物还在车上，台账上已经写「已放归」了。
    """
    user = request.user
    release = get_scoped_object(Release, pk, user)
    if release is None:
        return json_fail('放归记录不存在或无权访问', status=404)

    if release.status != 'pending':
        return json_fail(f'当前状态({release.get_status_display()})不可转运回小区')

    if release.pet_id and release.pet.is_deleted:
        return json_fail('该宠物档案已作废，无法办理')

    release.status = 'transferred'
    release.transferred_at = timezone.now()
    release.save(update_fields=['status', 'transferred_at'])
    return json_ok(serialize_instance(release), message='已登记转运回小区，等待小区确认放归')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def release_confirm(request, pk):
    """小区确认放归（接收人签字确认）

    请求体示例:
    {
        "receiver_name": "张物业",
        "receiver_phone": "13800003001",
        "signature": "base64..."
    }

    ⚠ 允许 `pending` 直接确认：旧前端 / 历史数据没有「转运回小区」这一步，
    不能因为新增了中间状态就把它们卡死。新流程走 `transferred`。
    """
    data = parse_json_body(request)
    user = request.user

    release = get_scoped_object(Release, pk, user)
    if release is None:
        return json_fail('放归记录不存在或无权访问', status=404)

    if release.status not in ('pending', 'transferred'):
        return json_fail(f'当前状态({release.get_status_display()})不可确认')

    if release.pet_id and release.pet.is_deleted:
        return json_fail('该宠物档案已作废，无法确认放归')

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

    return json_ok(serialize_instance(release), message='放归确认成功')
