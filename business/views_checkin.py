"""
Task 10: 回访打卡与黑名单
- 回访打卡列表/创建/审核
- 黑名单列表/创建/检查
"""
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import CheckIn, Blacklist, Pet
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    get_district_filtered_queryset, check_blacklist,
)


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
        pet = Pet.objects.get(id=pet_id)
    except Pet.DoesNotExist:
        return json_fail('宠物不存在')

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

    try:
        checkin = CheckIn.objects.get(id=pk)
    except CheckIn.DoesNotExist:
        return json_fail('打卡记录不存在', status=404)

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
    """黑名单列表"""
    qs = get_district_filtered_queryset(Blacklist, request.user)

    keyword = request.GET.get('keyword', '').strip()
    if keyword:
        qs = qs.filter(name__icontains=keyword) | qs.filter(phone__icontains=keyword) | qs.filter(id_card__icontains=keyword)

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

    # 解析违规日期
    violation_date = None
    date_str = data.get('violation_date')
    if date_str:
        from datetime import datetime
        try:
            violation_date = datetime.strptime(str(date_str)[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            violation_date = None

    bl = Blacklist.objects.create(
        name=name,
        id_card=data.get('id_card', ''),
        phone=data.get('phone', ''),
        reason=reason,
        violation_date=violation_date,
        operator=user,
        operator_name=user.get_full_name() or user.username,
        district_id=district_id,
    )

    return json_ok(serialize_instance(bl), message='已添加至黑名单')


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
