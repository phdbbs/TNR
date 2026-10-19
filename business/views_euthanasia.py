"""
Task 11: 安乐死处置
- 安乐死列表/创建
- 遗体领取
"""
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Euthanasia
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_str, body_int,
    generate_doc_no, get_district_filtered_queryset,
    get_active_pet, get_hospital_scoped_pet, get_scoped_object,
    hospital_pet_write_error,
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

    # 仅诊疗中/待领养的在院宠物可登记，防止误操作已领养、已放归等终态宠物
    if pet.status not in ('in_treatment', 'pending_adopt'):
        return json_fail(f'宠物当前状态({pet.get_status_display()})不可{kind_label}，'
                         '仅诊疗中/待领养宠物可登记')

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
