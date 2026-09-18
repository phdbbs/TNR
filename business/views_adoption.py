"""
Task 9: 领养业务
- 领养大厅列表/详情（公开）
- 领养信息编辑
- 线下领养登记（含创建领养人账号）
- 在线领养申请（领养人提交 / 机构审核）
- 领养记录列表
"""
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from accounts.models import User
from business.models import (
    Adoption, AdoptionApplication, Pet, AdoptionHallListing, Message, Blacklist,
)
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    generate_ledger_no, get_district_filtered_queryset,
    check_blacklist, get_active_pet, get_scoped_object,
    pet_has_pending_release, pet_has_active_adoption,
    validate_uploaded_images,
)


# ============================================
# 领养大厅（公开接口，无需登录）
# ============================================
def adoption_hall_list(request):
    """领养大厅 - 公开列表（待领养宠物）

    只展示「上架中 + 宠物未逻辑删除 + 宠物仍处于可领养状态」的记录，
    避免已领养/已作废的动物继续挂在大厅里。
    """
    qs = AdoptionHallListing.objects.filter(
        is_active=True,
        pet__is_deleted=False,
        pet__status__in=('pending_adopt', 'in_treatment'),
    ).select_related('pet')

    data = []
    for listing in qs:
        item = serialize_instance(listing)
        item['pet'] = serialize_instance(listing.pet)
        data.append(item)
    return json_ok(data)


def adoption_hall_detail(request, pk):
    """领养大厅 - 公开详情"""
    try:
        listing = AdoptionHallListing.objects.select_related('pet').get(
            id=pk, is_active=True, pet__is_deleted=False)
    except AdoptionHallListing.DoesNotExist:
        return json_fail('领养信息不存在', status=404)

    data = serialize_instance(listing)
    data['pet'] = serialize_instance(listing.pet)
    return json_ok(data)


# ============================================
# 领养信息编辑（医院）
# ============================================
@csrf_exempt
@role_required('hospital', 'gov_city', 'gov_district')
@login_required
def adoption_info_edit(request, pk):
    """编辑领养上架信息

    请求体示例:
    {
        "intro": "性格亲人，已绝育免疫",
        "personality": "活泼亲人",
        "body_condition": "健康良好",
        "flow_doc": "领养流程...",
        "is_active": true
    }
    """
    data = parse_json_body(request)

    if not data and request.POST:
        # 兼容 multipart/form-data 提交（照片上传）
        data = request.POST.dict()

    user = request.user
    # 按区县范围取宠物，并排除已逻辑删除的档案
    pet = get_active_pet(pk, user)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

    hospital = pet.hospital or user.institution
    if not hospital:
        return json_fail('缺少医院信息')

    # 照片校验必须在**任何落库之前**。
    # 原实现是先 `listing.save()` 再校验照片，于是上传一张假图片（.txt 改名 .png）时：
    # 接口返回「不是有效的图片文件」→ 界面弹红色失败提示，但 listing 已经写进库了，
    # 简介/性格/健康状况其实**已经改掉**。用户看到「失败」又去重试，
    # 每次重试都在改数据 —— 界面说的和实际发生的不是一回事。
    photo_err = validate_uploaded_images(request.FILES, {
        'photo_group': '整体合照',
        'photo_before': '捕捉前照片',
        'photo_after': '捕捉后照片',
        'photo_treatment': '诊疗后照片',
    })
    if photo_err:
        return json_fail(photo_err)

    with transaction.atomic():
        listing, created = AdoptionHallListing.objects.get_or_create(
            pet=pet,
            defaults={
                'hospital': hospital,
                'hospital_name': hospital.name,
            }
        )

        if 'intro' in data:
            listing.intro = data['intro']
        if 'personality' in data:
            listing.personality = data['personality']
        if 'body_condition' in data:
            listing.body_condition = data['body_condition']
        if 'flow_doc' in data:
            listing.flow_doc = data['flow_doc']
        if 'is_active' in data:
            listing.is_active = bool(data['is_active'])

        if created or not listing.published_at:
            listing.published_at = timezone.localdate()

        listing.save()

        for photo_field in ('photo_group', 'photo_before', 'photo_after',
                            'photo_treatment'):
            if request.FILES.get(photo_field):
                setattr(pet, photo_field, request.FILES[photo_field])
        pet.save()

    # 审计用上下文：`AdoptionHallListing` 没有 district 字段（沿 pet 派生），
    # 响应体里读不出归属区县。
    request.audit_district = pet.district

    return json_ok(serialize_instance(listing), message='领养信息已更新')


# ============================================
# 线下领养登记（捕捉点）
# ============================================
@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def adoption_register(request):
    """线下领养登记（创建领养人账号 + 领养记录）

    请求体示例:
    {
        "pet_id": 1,
        "adopter_name": "王领养",
        "adopter_phone": "13800000009",
        "adopter_id_card": "110105199001011234",
        "adopter_address": "朝阳区某某小区",
        "qualification": "有稳定住所",
        "commitment_letter": "已签署",
        "adoption_agreement": "线下已签",
        "adopted_at": "2025-02-01"
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

    if pet.status not in ('pending_adopt', 'in_treatment'):
        return json_fail(f'宠物当前状态({pet.get_status_display()})不可领养')

    # 互斥校验：同一只动物不能被放养流程与领养流程同时占用
    if pet_has_pending_release(pet):
        return json_fail('该宠物已有待放养记录，请先完成或取消放养后再办理领养')
    if pet_has_active_adoption(pet):
        return json_fail('该宠物已有未完结的领养记录，请勿重复登记')

    adopter_name = data.get('adopter_name', '').strip()
    adopter_phone = data.get('adopter_phone', '').strip()
    adopter_id_card = data.get('adopter_id_card', '')

    if not adopter_name:
        return json_fail('领养人姓名不能为空')
    if not adopter_phone:
        return json_fail('领养人电话不能为空')

    # 黑名单检查
    bl = check_blacklist(adopter_id_card, adopter_phone)
    if bl:
        return json_fail(f'该领养人已在黑名单中：{bl.reason}')

    district_id = pet.district_id or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    # 创建或获取领养人账号
    adopter = _get_or_create_adopter(
        name=adopter_name,
        phone=adopter_phone,
        id_card=adopter_id_card,
    )

    # 解析领养日期
    adopted_at = None
    adopted_at_str = data.get('adopted_at')
    if adopted_at_str:
        from datetime import datetime
        try:
            adopted_at = datetime.strptime(str(adopted_at_str)[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            adopted_at = None

    hospital = pet.hospital

    adoption = Adoption.objects.create(
        pet=pet,
        pet_code=pet.code,
        adopter=adopter,
        adopter_name=adopter_name,
        adopter_phone=adopter_phone,
        adopter_id_card=adopter_id_card,
        adopter_address=data.get('adopter_address', ''),
        qualification=data.get('qualification', ''),
        commitment_letter=data.get('commitment_letter', ''),
        adoption_agreement=data.get('adoption_agreement', ''),
        hospital=hospital,
        hospital_name=hospital.name if hospital else '',
        status='pending_claim',
        adopted_at=adopted_at,
        operator=user,
        operator_name=user.get_full_name() or user.username,
        ledger_no=generate_ledger_no('ADP'),
        district_id=district_id,
    )

    # 更新宠物状态为待领出（等待医院确认领出）
    pet.status = 'pending_claim'
    pet.save(update_fields=['status'])

    # 下架领养大厅
    AdoptionHallListing.objects.filter(pet=pet).update(is_active=False)

    # 发送通知给领养人
    if adopter:
        Message.objects.create(
            user=adopter,
            type='approval',
            title='领养审核通过',
            content=f'您的领养申请已通过审核，{pet.code} 已登记。请前往医院领取动物，医院确认领出后领养完成。',
        )

    return json_ok(serialize_instance(adoption), message='领养登记成功，待医院确认领出')


@csrf_exempt
@role_required('hospital')
@login_required
def adoption_confirm_claim(request, pk):
    """医院确认领出动物

    领养人在捕捉点办理好手续后，到医院领取动物。
    医院确认领出后，宠物状态变为已领养，领养记录状态变为已完成。

    请求体示例:
    {
        "note": "动物已领出"
    }
    """
    data = parse_json_body(request)
    user = request.user

    adoption = get_scoped_object(Adoption, pk, user)
    if adoption is None:
        return json_fail('领养记录不存在或无权访问', status=404)

    if adoption.status != 'pending_claim':
        return json_fail(f'当前领养状态({adoption.get_status_display()})不可确认领出')

    # 医院仅能确认本机构受理的领养记录
    if user.role == 'hospital' and adoption.hospital_id != user.institution_id:
        return json_fail('无权确认此领养记录')

    if adoption.pet_id and adoption.pet.is_deleted:
        return json_fail('该宠物档案已作废，无法确认领出')

    # 确认领出
    adoption.status = 'completed'
    if not adoption.adopted_at:
        adoption.adopted_at = timezone.localdate()
    adoption.save(update_fields=['status', 'adopted_at'])

    # 更新宠物状态为已领养
    pet = adoption.pet
    pet.status = 'adopted'
    pet.save(update_fields=['status'])

    # 通知领养人
    if adoption.adopter:
        Message.objects.create(
            user=adoption.adopter,
            type='approval',
            title='领养完成',
            content=f'医院已确认领出，{pet.code} 领养流程完成。',
        )

    return json_ok(serialize_instance(adoption), message='领出确认成功，领养流程已完成')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def adoption_reclaim(request, pk):
    """违规收回：撤销领养 + 收回动物 + 记入黑名单（一个事务内完成）。

    捕捉点/监管部门回访发现领养人违规（弃养、虐待等）时使用。
    此前前端只写入了黑名单，动物状态与领养记录都没变，导致动物在系统里
    仍归领养人所有、领养记录仍是「已完成」，既无法重新上架也无法再次领养。

    请求体示例:
    {
        "reason": "回访发现弃养",
        "relist": true          // 是否重新上架领养大厅，默认 true
    }
    """
    data = parse_json_body(request)
    user = request.user

    adoption = get_scoped_object(Adoption, pk, user)
    if adoption is None:
        return json_fail('领养记录不存在或无权访问', status=404)

    if adoption.status == 'cancelled':
        return json_fail('该领养记录已撤销，请勿重复操作')

    reason = (data.get('reason') or '').strip()
    if not reason:
        return json_fail('请填写收回原因')

    pet = adoption.pet
    if pet is None:
        return json_fail('领养记录缺少宠物档案')

    with transaction.atomic():
        # 1. 撤销领养记录
        adoption.status = 'cancelled'
        adoption.save(update_fields=['status'])

        # 2. 收回动物：回到「待领养」
        pet.status = 'pending_adopt'
        pet.save(update_fields=['status'])

        # 3. 重新上架领养大厅（原记录已下架时需显式置回 is_active）
        if data.get('relist', True) and not pet.is_deleted:
            listing = AdoptionHallListing.objects.filter(pet=pet).first()
            if listing:
                listing.is_active = True
                listing.save(update_fields=['is_active'])
            else:
                AdoptionHallListing.objects.create(
                    pet=pet,
                    hospital=pet.hospital,
                    hospital_name=pet.hospital.name if pet.hospital else '',
                    is_active=True,
                    published_at=timezone.localdate(),
                )

        # 4. 记入黑名单（已存在则不重复插入，避免同一条违规产生多条记录）
        if not check_blacklist(adoption.adopter_id_card, adoption.adopter_phone):
            Blacklist.objects.create(
                name=adoption.adopter_name or '未知领养人',
                id_card=adoption.adopter_id_card,
                phone=adoption.adopter_phone,
                reason=reason,
                operator=user,
                operator_name=user.get_full_name() or user.username,
                district_id=adoption.district_id,
            )

    if adoption.adopter:
        Message.objects.create(
            user=adoption.adopter,
            type='system',
            title='领养关系已撤销',
            content=f'因「{reason}」，{adoption.pet_code} 的领养关系已被撤销，该动物已收回。',
        )

    return json_ok(
        serialize_instance(adoption),
        message='已收回动物、撤销领养并记入黑名单',
    )


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def adoption_list(request):
    """领养记录列表"""
    qs = get_district_filtered_queryset(Adoption, request.user)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    keyword = request.GET.get('keyword', '').strip()
    if keyword:
        # 单条 Q 组合，避免链式 | 产生重复行
        qs = qs.filter(
            Q(adopter_name__icontains=keyword)
            | Q(adopter_phone__icontains=keyword)
            | Q(pet_code__icontains=keyword)
            | Q(ledger_no__icontains=keyword)
        )

    data = [serialize_instance(a) for a in qs]
    return json_ok(data)


# ============================================
# 在线领养申请（领养人提交 / 机构审核）
# ============================================
@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district')
@login_required
def adoption_apply(request):
    """领养人在线提交领养申请。

    请求体示例:
    {
        "pet_id": 1,
        "applicant_name": "王领养",
        "applicant_phone": "13800000009",
        "applicant_id_card": "110105199001011234",
        "applicant_address": "朝阳区某某小区",
        "qualification": "有稳定住所与稳定收入",
        "reason": "非常喜欢这只猫，希望给它一个温暖的家"
    }
    """
    data = parse_json_body(request)

    pet_id = data.get('pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    # 领养人无区县归属，这里按「未作废」取档案即可（大厅本身是公开数据）
    pet = Pet.objects.filter(id=pet_id, is_deleted=False).first()
    if pet is None:
        return json_fail('宠物不存在', status=404)

    # 仅待领养 / 待诊疗（已绝育可领养）宠物可申请
    if pet.status not in ('pending_adopt', 'in_treatment'):
        return json_fail(f'该宠物当前状态({pet.get_status_display()})不可申请领养')

    # 必须仍在大厅上架，避免对已下架/未上架的动物提交申请
    if not AdoptionHallListing.objects.filter(pet=pet, is_active=True).exists():
        return json_fail('该宠物当前未在领养大厅上架，无法申请')

    # 互斥校验：已有待放养记录的动物不接受领养申请
    if pet_has_pending_release(pet):
        return json_fail('该宠物已有待放养记录，暂不可申请领养')

    # 黑名单检查
    bl = check_blacklist(data.get('applicant_id_card', ''), data.get('applicant_phone', ''))
    if bl:
        return json_fail(f'您已被列入领养黑名单，无法申请：{bl.reason}')

    # 检查是否已对该宠物提交处理中的申请
    if AdoptionApplication.objects.filter(
        applicant=request.user, pet=pet,
    ).exclude(status='rejected').exists():
        return json_fail('您已提交过该宠物的领养申请，请勿重复提交')

    applicant_name = data.get('applicant_name', '').strip()
    applicant_phone = data.get('applicant_phone', '').strip()
    if not applicant_name:
        return json_fail('请填写姓名')
    if not applicant_phone:
        return json_fail('请填写联系电话')

    hospital = pet.hospital
    application = AdoptionApplication.objects.create(
        pet=pet,
        pet_code=pet.code,
        applicant=request.user,
        applicant_name=applicant_name,
        applicant_phone=applicant_phone,
        applicant_id_card=data.get('applicant_id_card', ''),
        applicant_address=data.get('applicant_address', ''),
        qualification=data.get('qualification', ''),
        reason=data.get('reason', ''),
        status='pending',
        hospital=hospital,
        hospital_name=hospital.name if hospital else '',
    )

    # 审计用上下文：`AdoptionApplication` 没有 district 字段（沿 pet 派生）。
    request.audit_district = pet.district

    return json_ok(serialize_instance(application), message='领养申请已提交，请等待机构审核')


@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district')
@login_required
def my_applications(request):
    """领养人 - 我的领养申请列表。"""
    qs = AdoptionApplication.objects.filter(applicant=request.user).order_by('-id')
    data = []
    for a in qs:
        item = serialize_instance(a)
        if a.pet_id:
            item['pet'] = serialize_instance(a.pet)
        data.append(item)
    return json_ok(data)


@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def adoption_application_list(request):
    """机构 - 领养申请列表（可按状态筛选）。"""
    user = request.user
    qs = AdoptionApplication.objects.select_related('pet').order_by('-id')

    # 机构数据权限：医院只看到本机构；区级看本区；市级看全部
    if user.role == 'hospital':
        if user.institution_id:
            qs = qs.filter(hospital_id=user.institution_id)
        else:
            qs = qs.none()
    elif user.role == 'gov_district':
        qs = qs.filter(pet__district_id=user.district_id)
    elif user.role == 'shelter':
        qs = qs.filter(pet__shelter_id=user.institution_id)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    data = []
    for a in qs:
        item = serialize_instance(a)
        if a.pet_id:
            item['pet'] = serialize_instance(a.pet)
        data.append(item)
    return json_ok(data)


@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def adoption_application_review(request, pk):
    """机构审核领养申请（通过 / 拒绝）。

    请求体示例:
    {
        "action": "approve",   // approve | reject
        "review_note": "资质符合，同意领养"
    }
    """
    data = parse_json_body(request)
    user = request.user

    # 按角色/区县收敛可见范围（申请单本身无 district 字段，故按机构/宠物归属过滤）
    qs = AdoptionApplication.objects.all()
    if user.role == 'hospital':
        qs = qs.filter(hospital_id=user.institution_id) if user.institution_id else qs.none()
    elif user.role == 'gov_district':
        qs = qs.filter(pet__district_id=user.district_id)
    elif user.role == 'shelter':
        qs = qs.filter(pet__shelter_id=user.institution_id)

    application = qs.filter(id=pk).first()
    if application is None:
        return json_fail('申请不存在或无权访问', status=404)

    if application.status != 'pending':
        return json_fail(f'该申请已处理（{application.get_status_display()}）')

    action = data.get('action', '')
    if action not in ('approve', 'reject'):
        return json_fail('无效的审核操作')

    # 通过前复核宠物是否仍可领养，避免同一只动物被重复批准
    if action == 'approve':
        pet = application.pet
        if pet is None or pet.is_deleted:
            return json_fail('该宠物档案已作废，无法通过申请')
        if pet.status not in ('pending_adopt', 'in_treatment'):
            return json_fail(f'该宠物当前状态({pet.get_status_display()})已不可领养，无法通过申请')
        if pet_has_active_adoption(pet):
            return json_fail('该宠物已有未完结的领养记录，无法重复通过申请')
        if pet_has_pending_release(pet):
            return json_fail('该宠物已有待放养记录，无法通过领养申请')

    application.status = 'approved' if action == 'approve' else 'rejected'
    application.review_note = data.get('review_note', '')
    application.reviewed_by = user
    application.reviewed_at = timezone.now()
    application.save(update_fields=['status', 'review_note', 'reviewed_by', 'reviewed_at'])

    # 通知申请人
    if application.applicant:
        Message.objects.create(
            user=application.applicant,
            type='approval',
            title='领养申请审核通知',
            content='您的领养申请已通过，请前往医院办理领养手续。' if action == 'approve'
            else f'您的领养申请未通过：{application.review_note or "资质不符合要求"}',
        )

    # 审计用上下文：`AdoptionApplication` 没有 district 字段（沿 pet 派生）。
    request.audit_district = application.pet.district if application.pet_id else None

    return json_ok(serialize_instance(application), message='审核完成')


# ============================================
# 辅助函数
# ============================================
def _get_or_create_adopter(name, phone, id_card):
    """创建或获取领养人账号

    - 优先按电话查找已有用户
    - 不存在则创建新账号（role='adopter'）
    """
    # 按电话查找
    existing = User.objects.filter(phone=phone, role='adopter').first()
    if existing:
        return existing

    # 生成唯一用户名
    base = phone or f'adopter_{name}'
    username = base
    n = 1
    while User.objects.filter(username=username).exists():
        username = f'{base}_{n}'
        n += 1

    adopter = User.objects.create_user(
        username=username,
        password='123456',  # 默认密码，领养人可后续修改
        role='adopter',
        phone=phone,
        first_name=name[:30],
    )
    return adopter
