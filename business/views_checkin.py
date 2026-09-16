"""
Task 10: 回访打卡与黑名单
- 回访打卡列表/创建/审核
- 黑名单列表/创建/检查
"""
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import CheckIn, Blacklist, Pet
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    get_district_filtered_queryset, check_blacklist,
)


def _parse_date(value):
    """宽松解析 YYYY-MM-DD，非法值返回 None。"""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


# ============================================
# 回访打卡
# ============================================
@csrf_exempt
@role_required('adopter', 'shelter', 'gov_city', 'gov_district')
@login_required
def checkin_list(request):
    """回访打卡列表（领养人看自己，捕捉点看全区）"""
    user = request.user

    if user.role == 'adopter':
        qs = CheckIn.objects.filter(adopter=user)
    else:
        qs = get_district_filtered_queryset(CheckIn, user)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    data = [serialize_instance(c) for c in qs]
    return json_ok(data)


@csrf_exempt
@role_required('adopter')
@login_required
def checkin_create(request):
    """领养人提交月度回访打卡

    请求体示例 (form-data):
    {
        "pet_id": 1,
        "month": "2025-02",
        "note": "猫咪适应良好"
    }
    + photo file
    """
    data = parse_json_body(request)
    # 兼容 form-data 提交
    if not data:
        data = request.POST.dict()

    user = request.user

    pet_id = data.get('pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    try:
        pet = Pet.objects.get(id=pet_id, is_deleted=False)
    except Pet.DoesNotExist:
        return json_fail('宠物不存在或档案已作废')

    # 验证该宠物属于当前领养人
    if pet.adoptions.filter(adopter=user).exists() is False:
        return json_fail('无权为该宠物打卡')

    month = data.get('month', '').strip()
    if not month:
        from django.utils import timezone
        month = timezone.now().strftime('%Y-%m')

    # 检查是否已打卡
    exists = CheckIn.objects.filter(pet=pet, month=month, adopter=user).exists()
    if exists:
        return json_fail(f'{month} 已打卡，请勿重复提交')

    checkin = CheckIn.objects.create(
        pet=pet,
        pet_code=pet.code,
        adopter=user,
        adopter_name=user.get_full_name() or user.username,
        month=month,
        note=data.get('note', ''),
        status='pending',
    )

    # 处理照片上传
    if request.FILES.get('photo'):
        checkin.photo = request.FILES['photo']
        checkin.save(update_fields=['photo'])

    return json_ok(serialize_instance(checkin), message='打卡提交成功，待审核')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def checkin_review(request, pk):
    """审核回访打卡

    请求体示例:
    {
        "status": "approved",  // 或 "rejected"
        "note": "审核通过"
    }
    """
    data = parse_json_body(request)
    user = request.user

    # 打卡记录本身无 district 字段，按所属宠物的区县收敛可见范围，
    # 避免只凭主键即可跨区县审核他人的回访打卡。
    qs = CheckIn.objects.select_related('pet')
    if user.role == 'gov_city':
        pass
    elif getattr(getattr(user, 'district', None), 'is_city', False):
        pass
    elif user.district_id:
        qs = qs.filter(pet__district_id=user.district_id)
    else:
        qs = qs.none()

    checkin = qs.filter(id=pk).first()
    if checkin is None:
        return json_fail('打卡记录不存在或无权访问', status=404)

    if checkin.status != 'pending':
        return json_fail(f'该打卡已审核（{checkin.get_status_display()}），不可重复审核')

    new_status = data.get('status')
    if new_status not in ('approved', 'rejected'):
        return json_fail('status 必须为 approved 或 rejected')

    checkin.status = new_status
    checkin.operator = request.user
    checkin.save(update_fields=['status', 'operator'])

    return json_ok(serialize_instance(checkin), message='审核完成')


# ============================================
# 黑名单
# ============================================
@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def blacklist_list(request):
    """黑名单列表（默认不含已移出记录）"""
    qs = get_district_filtered_queryset(Blacklist, request.user)

    if request.GET.get('include_deleted') not in ('1', 'true', 'True'):
        qs = qs.filter(is_deleted=False)

    keyword = request.GET.get('keyword', '').strip()
    if keyword:
        # 用 Q 组合，避免多次 filter 相或产生重复行
        qs = qs.filter(
            Q(name__icontains=keyword)
            | Q(phone__icontains=keyword)
            | Q(id_card__icontains=keyword)
            | Q(reason__icontains=keyword)
        )

    data = [serialize_instance(b) for b in qs]
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def blacklist_create(request):
    """添加黑名单

    请求体示例:
    {
        "name": "李某某",
        "id_card": "110102****5678",
        "phone": "13900000001",
        "reason": "弃养领养宠物",
        "violation_date": "2024-12-15"
    }
    """
    data = parse_json_body(request)
    user = request.user

    name = data.get('name', '').strip()
    if not name:
        return json_fail('姓名不能为空')

    reason = data.get('reason', '').strip()
    if not reason:
        return json_fail('拉黑原因不能为空')

    district_id = data.get('district_id') or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    bl = Blacklist.objects.create(
        name=name,
        id_card=data.get('id_card', ''),
        phone=data.get('phone', ''),
        reason=reason,
        violation_date=_parse_date(data.get('violation_date')),
        operator=user,
        operator_name=user.get_full_name() or user.username,
        district_id=district_id,
    )

    return json_ok(serialize_instance(bl), message='已添加至黑名单')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def blacklist_update(request, pk):
    """编辑黑名单记录。"""
    if request.method != 'POST':
        return json_fail('仅支持 POST 请求', status=405)

    bl = get_district_filtered_queryset(Blacklist, request.user).filter(id=pk).first()
    if bl is None:
        return json_fail('黑名单记录不存在', status=404)
    if bl.is_deleted:
        return json_fail('该记录已移出黑名单，不可编辑')

    data = parse_json_body(request)
    if not data and request.POST:
        data = request.POST.dict()

    for field, label in (('name', '姓名'), ('phone', '电话'), ('reason', '拉黑原因')):
        if field in data and not (data.get(field) or '').strip():
            return json_fail(f'{label}不能为空')

    changed = []
    for field in ('name', 'phone', 'id_card', 'reason'):
        if field in data:
            setattr(bl, field, (data.get(field) or '').strip())
            changed.append(field)
    if 'violation_date' in data:
        bl.violation_date = _parse_date(data.get('violation_date'))
        changed.append('violation_date')

    if changed:
        bl.save(update_fields=changed)

    return json_ok(serialize_instance(bl), message='黑名单记录已更新')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def blacklist_delete(request, pk):
    """移出黑名单（逻辑删除，数据库记录保留）。"""
    if request.method != 'POST':
        return json_fail('仅支持 POST 请求', status=405)

    bl = get_district_filtered_queryset(Blacklist, request.user).filter(id=pk).first()
    if bl is None:
        return json_fail('黑名单记录不存在', status=404)
    if bl.is_deleted:
        return json_fail('该记录已移出黑名单，请勿重复操作')

    bl.is_deleted = True
    bl.deleted_at = timezone.now()
    bl.save(update_fields=['is_deleted', 'deleted_at'])

    return json_ok(serialize_instance(bl), message='已移出黑名单')


@csrf_exempt
@role_required('shelter', 'hospital', 'adopter', 'gov_city', 'gov_district')
@login_required
def blacklist_check(request):
    """检查身份证/电话是否在黑名单中（前端拦截用）

    GET 参数: ?id_card=xxx&phone=xxx
    """
    id_card = request.GET.get('id_card', '').strip()
    phone = request.GET.get('phone', '').strip()

    bl = check_blacklist(id_card, phone)
    if bl:
        return json_ok({
            'in_blacklist': True,
            'name': bl.name,
            'reason': bl.reason,
            'phone': bl.phone,
        })
    return json_ok({'in_blacklist': False})
