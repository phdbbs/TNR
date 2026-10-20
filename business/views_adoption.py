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

import json
from datetime import datetime

from accounts.decorators import role_required
from accounts.models import User
from core.http import to_camel_key
from business.models import (
    Adoption, AdoptionApplication, AdoptionPhoto, Pet, AdoptionHallListing,
    Message, Blacklist,
)
from business.services_treatment import dispose_for_discharge
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_str, body_int,
    generate_doc_no, get_district_filtered_queryset,
    check_blacklist, get_active_pet, get_scoped_object,
    get_hospital_scoped_pet, get_own_institution_object,
    hospital_pet_write_error,
    pet_has_pending_release, pet_has_active_adoption,
    pet_has_approved_application,
    pet_has_pending_return, validate_image_upload,
)


# ============================================
# 领养大厅（公开接口，无需登录）
# ============================================
#: 领养大厅展示的系统照片：全部由捕捉 / 诊疗环节产生，医院只决定显不显示。
HALL_SYSTEM_PHOTO_FIELDS = (
    ('photo_capture', '捕捉照片'),
    ('photo_group', '整体合影'),
    ('photo_before', '术前照片'),
    ('photo_after', '术后照片'),
    ('photo_treatment', '诊疗后照片'),
)
HALL_EXTRA_PHOTO_MAX = 6


def _photo_url(field_file):
    return field_file.url if field_file and field_file.name else ''


def serialize_hall_listing(listing, include_hidden=False):
    """大厅上架信息 + 照片清单。

    公开侧（`include_hidden=False`）只下发**显示中**的系统照片，并把隐藏
    字段在 `pet` 里一并抹空 —— 否则隐藏只作用于新加的照片清单，前端或
    爬虫直接读 `pet.photo_before` 仍能拿到不想公开的照片。

    医院侧（`include_hidden=True`）保留全部照片与 `visible` 标记，供
    「维护资料」渲染逐张显示开关。
    """
    data = serialize_instance(listing)
    pet_data = serialize_instance(listing.pet) if listing.pet_id else None

    hidden = set(listing.hidden_photos or ())
    photos = []
    for field, label in HALL_SYSTEM_PHOTO_FIELDS:
        url = _photo_url(getattr(listing.pet, field, None)) if listing.pet_id else ''
        if not url:
            continue
        photos.append({
            'field': field, 'label': label, 'url': url,
            'visible': field not in hidden,
        })
    extras = [serialize_instance(p) for p in listing.extra_photos.all()]
    for photo in extras:
        photos.append({
            'field': 'extra', 'label': '补充照片',
            'url': photo.get('image') or '', 'visible': True,
        })

    if pet_data is not None and not include_hidden:
        for field, _ in HALL_SYSTEM_PHOTO_FIELDS:
            if field in hidden:
                pet_data[field] = ''
                pet_data[to_camel_key(field)] = ''

    data['photos'] = photos if include_hidden else [p for p in photos if p['visible']]
    data['extra_photos'] = extras
    data['extraPhotos'] = extras
    if not include_hidden:
        data.pop('hidden_photos', None)
        data.pop('hiddenPhotos', None)
    data['pet'] = pet_data
    return data


def adoption_hall_list(request):
    """领养大厅 - 公开列表（待领养宠物）

    只展示「上架中 + 宠物未逻辑删除 + 宠物仍处于可领养状态」的记录，
    避免已领养/已作废的动物继续挂在大厅里。
    """
    qs = AdoptionHallListing.objects.filter(
        is_active=True,
        pet__is_deleted=False,
        pet__status__in=('pending_adopt', 'in_treatment'),
    ).select_related('pet').prefetch_related('extra_photos')

    return json_ok([serialize_hall_listing(listing) for listing in qs])


def adoption_hall_detail(request, pk):
    """领养大厅 - 公开详情"""
    try:
        listing = (AdoptionHallListing.objects
                   .select_related('pet').prefetch_related('extra_photos')
                   .get(id=pk, is_active=True, pet__is_deleted=False))
    except AdoptionHallListing.DoesNotExist:
        return json_fail('领养信息不存在', status=404)

    return json_ok(serialize_hall_listing(listing))


# ============================================
# 领养信息编辑（医院）
# ============================================
@csrf_exempt
@role_required('hospital')
@login_required
def adoption_info_edit(request, pk):
    """编辑领养上架信息

    请求体示例:
    {
        "intro": "性格亲人，已绝育免疫",
        "personality": "活泼亲人",
        "body_condition": "健康良好",
        "flow_doc": "领养流程...",
        "is_active": true,
        "hidden_photos": ["photo_before"],       // 大厅里隐藏哪些系统照片
        "delete_photo_ids": [3]                   // 删除哪些已上传的补充照片
    }
    另可 multipart 上传 `extra_photos`（多张，仅补充说明照片）。

    **照片口径**：捕捉 / 诊疗环节已经产生的照片（捕捉、合影、术前、术后、
    诊疗后）自动从动物档案带入大厅，这里只设置「显不显示」，**不再重复上传**；
    本接口只接收医院当下补拍的说明类照片。

    **数据权限（第二十一轮收口）**：`get_active_pet()` 只按**区县**收敛 ——
    对医院账号来说，同区县另一家医院名下的宠物也能取到，于是 A 院能改 B 院的
    领养上架文案、甚至把它**上架到公开领养大厅**（匿名可见）或下架。
    医院分支必须再加**本机构**判据。

    这里刻意用「**当前挂在本院名下**」（`pet.hospital_id == user.institution_id`），
    比读侧判据 `hospital_pet_scope()`（本院在治 ∪ 本院**经手过**）**更严**：
    读可以回溯历史，**写只能动当前归属自己的对象**。
    与同类医院写接口口径一致（`adoption_confirm_claim` 用
    `adoption.hospital_id != user.institution_id`；`transfer_receive` / `transfer_reject`
    用 `transfer.to_hospital_id != user.institution_id`）。
    """
    data = parse_json_body(request)

    if not data and request.POST:
        # 兼容 multipart/form-data 提交（补充照片上传）
        data = request.POST.dict()

    user = request.user
    # 与出院列表同口径：医院端按「本院在治 ∪ 本院诊疗过」取单。
    # 不能沿用 `get_active_pet`（按区县收敛）—— 跨区县送医的动物在列表里
    # 看得见，点「维护资料」却回 404「宠物不存在或无权访问」（本轮实测：
    # TNR261016004 属于樊城区，被送到襄城区的爱心宠物医院）。
    pet = get_hospital_scoped_pet(pk, user)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

    # 医院只能动**当前挂在本院名下**的宠物 —— 与同类写接口口径一致。
    # 放在任何落库动作之前：否则会先建/改 listing 再报错，留下半截写入。
    if pet.hospital_id != user.institution_id:
        return json_fail('无权编辑此动物的领养信息')

    hospital = pet.hospital or user.institution
    if not hospital:
        return json_fail('缺少医院信息')

    def _json_list(value, label):
        """form-data 里数组是 JSON 字符串，JSON 请求里已经是 list。"""
        if value is None:
            return None, None
        if isinstance(value, str):
            try:
                value = json.loads(value) if value.strip() else []
            except (TypeError, ValueError):
                return None, f'{label}格式不正确'
        if not isinstance(value, list):
            return None, f'{label}格式不正确'
        return value, None

    hidden_photos = None
    if 'hidden_photos' in data:
        hidden_photos, err = _json_list(data.get('hidden_photos'), '照片显示设置')
        if err:
            return json_fail(err)
        allowed = {field for field, _ in HALL_SYSTEM_PHOTO_FIELDS}
        if any(not isinstance(x, str) or x not in allowed for x in hidden_photos):
            return json_fail('照片显示设置包含未知照片')
        hidden_photos = sorted(set(hidden_photos))

    delete_ids = []
    if data.get('delete_photo_ids'):
        delete_ids, err = _json_list(data.get('delete_photo_ids'), '待删除照片')
        if err:
            return json_fail(err)
        if any(not isinstance(x, int) for x in delete_ids):
            return json_fail('待删除照片编号格式不正确')

    # 照片校验必须在**任何落库之前**（与捕捉 / 诊疗上传同一约定）：
    # 否则第 N 张非法时，前面的字段与照片已经写进库，界面却说「失败」。
    extra_files = request.FILES.getlist('extra_photos')
    if len(extra_files) > HALL_EXTRA_PHOTO_MAX:
        return json_fail(f'补充照片一次最多上传 {HALL_EXTRA_PHOTO_MAX} 张')
    for f in extra_files:
        err = validate_image_upload(f, '补充照片')
        if err:
            return json_fail(err)

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
            # form-data 传进来的是字符串，`bool('false')` 为 True —— 必须按字面量判
            listing.is_active = str(data['is_active']).lower() not in (
                '0', 'false', 'off', '')
        if hidden_photos is not None:
            listing.hidden_photos = hidden_photos

        if created or not listing.published_at:
            listing.published_at = timezone.localdate()

        listing.save()

        if delete_ids:
            AdoptionPhoto.objects.filter(
                listing=listing, id__in=delete_ids).delete()
        for f in extra_files:
            AdoptionPhoto.objects.create(listing=listing, image=f)

    # 审计用上下文：`AdoptionHallListing` 没有 district 字段（沿 pet 派生），
    # 响应体里读不出归属区县。
    request.audit_district = pet.district

    return json_ok(serialize_hall_listing(listing, include_hidden=True),
                   message='领养信息已更新')


# ============================================
# 线下领养登记（捕捉点）
# ============================================
@csrf_exempt
@role_required('shelter', 'hospital')
@login_required
def adoption_register(request):
    """线下领养登记（创建领养人账号 + 领养记录）

    第四十八轮：**医院端也能直接开领养单**（出院模块的「领养」子模块）。
    原先只有捕捉点 / 政府端能登记，医院端只能等捕捉点开好单、再来
    「确认领出」—— 但领养人往往就是到医院现场挑的，医院反而没有入口。

    取单口径按角色分叉：医院走 `get_hospital_scoped_pet`（本院在治 ∪ 本院
    诊疗过），其余角色仍走 `get_active_pet`（按区县）。

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

    pet_id = body_int(data, 'pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    # 按区县范围取宠物，并排除已逻辑删除的档案；
    # 医院端改按「本院」收口（跨区县送医时按区县会取不到本院动物）。
    if user.role == 'hospital':
        pet = get_hospital_scoped_pet(pet_id, user)
    else:
        pet = get_active_pet(pet_id, user)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

    # 写单据前校一次「动物当前是否挂在本院」（读口径含「本院诊疗过」，
    # 不能直接拿来做写权限 —— 否则能给别院在治的动物开领养单）。
    err = hospital_pet_write_error(pet, user)
    if err:
        return json_fail(err)

    if pet.status not in ('pending_adopt', 'in_treatment'):
        return json_fail(f'宠物当前状态({pet.get_status_display()})不可领养')

    # 互斥校验：同一只动物不能被放归流程与领养流程同时占用
    if pet_has_pending_release(pet):
        return json_fail('该宠物已有未办结的放归单（待转运回小区或待小区签字），'
                         '请先走完放归两段确认后再办理领养')
    if pet_has_pending_return(pet):
        return json_fail('该宠物已在运回捕捉点的单据上，请先撤回或等待捕捉点处理')
    if pet_has_active_adoption(pet):
        return json_fail('该宠物已有未完结的领养记录，请勿重复登记')

    adopter_name = body_str(data, 'adopter_name').strip()
    adopter_phone = body_str(data, 'adopter_phone').strip()
    adopter_id_card = body_str(data, 'adopter_id_card')

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
        adopter_address=body_str(data, 'adopter_address'),
        qualification=body_str(data, 'qualification'),
        commitment_letter=body_str(data, 'commitment_letter'),
        adoption_agreement=body_str(data, 'adoption_agreement'),
        hospital=hospital,
        hospital_name=hospital.name if hospital else '',
        # 归属机构 = 受理医院（业务对象推导）
        institution=hospital,
        status='pending_claim',
        adopted_at=adopted_at,
        operator=user,
        operator_name=user.get_full_name() or user.username,
        ledger_no=generate_doc_no('ADP'),
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
def adoption_outbound(request, pk):
    """领养出库：医院把动物交给已预约的领养人（第五十四轮）。

    已预约（`pending_claim`）只代表捕捉点批了人、动物还在院；
    真正的交接要有出库日期、医院交接人、领养人签字与可选照片。
    完成后领养单 `completed`、动物 `adopted`，之后不可再操作。
    """
    data = parse_json_body(request)
    if request.POST:
        data = request.POST.dict()

    user = request.user
    qs = Adoption.objects.filter(pk=pk)
    if user.institution_id:
        qs = qs.filter(
            Q(institution_id=user.institution_id)
            | Q(institution_id__isnull=True,
                hospital_id=user.institution_id))
    else:
        qs = qs.none()
    adoption = qs.first()
    if adoption is None:
        return json_fail('领养记录不存在或无权访问', status=404)

    if adoption.status != 'pending_claim':
        return json_fail(f'当前领养状态({adoption.get_status_display()})不可出库')

    if adoption.pet_id and adoption.pet.is_deleted:
        return json_fail('该宠物档案已作废，无法出库')

    err = _validate_outbound(request, data)
    if err:
        return json_fail(err)
    outbound_at, handover_name, signature = _outbound_values(data)

    with transaction.atomic():
        adoption.status = 'completed'
        adoption.adopted_at = adoption.adopted_at or outbound_at
        adoption.outbound_at = outbound_at
        adoption.handover_name = handover_name
        adoption.signature = signature
        if request.FILES.get('handover_photo'):
            adoption.handover_photo = request.FILES['handover_photo']
        adoption.save(update_fields=[
            'status', 'adopted_at', 'outbound_at', 'handover_name',
            'signature', 'handover_photo',
        ])

        pet = adoption.pet
        pet.status = 'adopted'
        pet.save(update_fields=['status'])
        # 领养出库 = 出院终态，诊疗单此刻办结（诊疗页只记了建议去向）。
        dispose_for_discharge(pet, 'pending_adopt', request.user)

    if adoption.adopter:
        Message.objects.create(
            user=adoption.adopter,
            type='approval',
            title='领养完成',
            content=f'医院已确认出库，{adoption.pet_code} 领养流程完成。',
        )

    return json_ok(serialize_instance(adoption), message='领养出库成功，流程已完成')


@csrf_exempt
@role_required('hospital')
@login_required
def adoption_application_outbound(request, pk):
    """已批准申请的领养出库：把「已预约」直接办成领养出库单。

    捕捉点审核通过后只写了 `AdoptionApplication.status='approved'`，
    不会自动生成 `Adoption`（捕捉点仍可走线下登记改领养人）。
    医院端「已预约」里这类行没有领养单，出库时按申请材料补建一张
    已完成的领养单，保证台账里领养人与交接签字齐全。
    """
    data = parse_json_body(request)
    if request.POST:
        data = request.POST.dict()
    user = request.user

    application = AdoptionApplication.objects.filter(
        pk=pk, hospital_id=user.institution_id).first() if user.institution_id else None
    if application is None:
        return json_fail('领养申请不存在或无权访问', status=404)
    if application.status != 'approved':
        return json_fail(f'该申请当前状态({application.get_status_display()})不可出库')

    pet = application.pet
    if pet is None or pet.is_deleted:
        return json_fail('宠物档案已作废，无法出库')
    if pet.hospital_id != user.institution_id:
        return json_fail('该动物当前不在本院，无法办理出库')
    if pet_has_active_adoption(pet):
        return json_fail('该宠物已有未完结的领养记录，请到「已预约」领养单中办理出库')
    # 申请单出库时动物仍挂在医院其他出院路径上 → 两边都办结会互相覆盖宠物状态
    if pet_has_pending_return(pet):
        return json_fail('该宠物已在运回捕捉点的单据上，请先撤回或等待捕捉点处理')
    if pet_has_pending_release(pet):
        return json_fail('该宠物已有未办结的放归单，请先处理后再出库')
    # 申请单出库后本单仍是 `approved`（它只记录审核结论），重放请求时
    # 领养记录已 `completed`，`pet_has_active_adoption` 不再拦得住 ——
    # 不查历史已完成记录就会给同一只动物再建一张完成单。
    if Adoption.objects.filter(pet=pet, status='completed').exists():
        return json_fail('该宠物已完成领养出库，请勿重复操作')

    err = _validate_outbound(request, data)
    if err:
        return json_fail(err)
    outbound_at, handover_name, signature = _outbound_values(data)

    hospital = pet.hospital or user.institution
    if hospital is None:
        return json_fail('缺少医院信息')
    if not pet.district_id:
        return json_fail('缺少区县信息')

    adopter = application.applicant
    if adopter is None:
        adopter = _get_or_create_adopter(
            name=application.applicant_name,
            phone=application.applicant_phone,
            id_card=application.applicant_id_card,
        )

    with transaction.atomic():
        adoption = Adoption.objects.create(
            pet=pet,
            pet_code=pet.code,
            adopter=adopter,
            adopter_name=application.applicant_name,
            adopter_phone=application.applicant_phone,
            adopter_id_card=application.applicant_id_card,
            adopter_address=application.applicant_address,
            qualification=application.qualification,
            hospital=hospital,
            hospital_name=hospital.name,
            institution=hospital,
            status='completed',
            adopted_at=outbound_at,
            outbound_at=outbound_at,
            handover_name=handover_name,
            signature=signature,
            operator=user,
            operator_name=user.get_full_name() or user.username,
            ledger_no=generate_doc_no('ADP'),
            district=pet.district,
        )
        if request.FILES.get('handover_photo'):
            adoption.handover_photo = request.FILES['handover_photo']
            adoption.save(update_fields=['handover_photo'])

        pet.status = 'adopted'
        pet.save(update_fields=['status'])
        # 领养出库 = 出院终态，诊疗单此刻办结（诊疗页只记了建议去向）。
        dispose_for_discharge(pet, 'pending_adopt', user)

    if adopter:
        Message.objects.create(
            user=adopter,
            type='approval',
            title='领养完成',
            content=f'医院已确认出库，{pet.code} 领养流程完成。',
        )

    return json_ok(serialize_instance(adoption), message='领养出库成功，流程已完成')


def _outbound_values(data):
    outbound_at = datetime.strptime(str(data.get('outbound_at'))[:10], '%Y-%m-%d').date()
    return (
        outbound_at,
        body_str(data, 'handover_name').strip(),
        body_str(data, 'signature'),
    )


def _validate_outbound(request, data):
    """校验领养出库 / 交接单的必填字段与可选照片，返回错误文案或 None。"""
    if not body_str(data, 'handover_name').strip():
        return '请填写交接人'
    if not body_str(data, 'signature'):
        return '请填写领养人签字'
    if not data.get('outbound_at'):
        return '请选择出库日期'
    try:
        datetime.strptime(str(data.get('outbound_at'))[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return f"出库日期「{data.get('outbound_at')}」格式不正确，应为 YYYY-MM-DD"
    return validate_image_upload(request.FILES.get('handover_photo'), '交接照片')


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
    parse_json_body(request)
    user = request.user

    # 本接口 `@role_required('hospital')`，取单必须按**本机构**。
    # 不能沿用 `get_scoped_object`（按区县）：跨区县送医时 `Adoption.district`
    # 是**动物**的区县，做手术的医院在另一个区县，出院流程里
    # 「开领养单 → 当场确认领出」是连着调的（医院端 portal 同一函数），
    # 按区县收敛会让第二步永远 404 —— 单据开出来了、动物却领不出去，
    # 领养记录永久卡在 `pending_claim`。
    # 账号没有机构时 `get_own_institution_object` 返回 None（不是跳过校验），
    # 与「不存在」同一个 404，不形成存在性预言机。
    adoption = get_own_institution_object(Adoption, pk, user, field='hospital_id')
    if adoption is None:
        return json_fail('领养记录不存在或无权访问', status=404)

    if adoption.status != 'pending_claim':
        return json_fail(f'当前领养状态({adoption.get_status_display()})不可确认领出')

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
    # 旧口径的「当场领出」同样是出院终态；新前端走 `adoption_outbound`。
    dispose_for_discharge(pet, 'pending_adopt', user)

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
@role_required('shelter')
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

    reason = body_str(data, 'reason').strip()
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
    """领养记录列表。

    **医院分支**与孪生接口 ``adoption_application_list``（领养申请列表）对齐：
    只看到本机构。第二十轮之前这里只有 `get_district_filtered_queryset`，
    医院按区县口径 —— 同区县另一家医院（襄城区有 2 家）名下的领养记录会一并
    返回。`Adoption` 直接挂在宠物上，而领养记录含领养人姓名 / 电话，
    跨机构读是**横向越权**，不是「口径宽松」。

    捕捉点与政府端**沿用原区县口径，未改动** —— 这一支不是漂移：
    ``/pets/``（``hospital_pets``）明确写着「捕捉点只看**本机构所在区县**的宠物」，
    与这里按 ``Adoption.district`` 收敛一致。真正与它们不一致的是孪生接口
    （它按 ``pet__shelter_id``，更窄）；把这里改成跟那个窄口径对齐，等于把
    「区县级收容入口」缩成「机构级」，捕捉点会看不到本区县、但动物并非由它
    捕捉的领养记录。**窄不等于对。**

    注意 `qs.filter(...)` 必须**逐支赋值**，不能写成 `qs.filter(a) | qs.filter(b)`
    —— 那样会丢掉 `qs` 上已有的前置过滤（机构隔离会被直接击穿）并产生重复行。
    """
    user = request.user
    if user.role == 'hospital':
        if user.institution_id:
            qs = Adoption.objects.filter(hospital_id=user.institution_id)
        else:
            # 没有挂靠机构时必须**空集**：写成 `filter(hospital_id=None)` 会匹配上
            # 所有「受理医院已被删除」的记录（`Adoption.hospital` 是 SET_NULL），
            # 等于完全不设限。
            qs = Adoption.objects.none()
    else:
        qs = get_district_filtered_queryset(Adoption, user)

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
@role_required('adopter')
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

    pet_id = body_int(data, 'pet_id')
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

    # 互斥校验：已有待放归记录的动物不接受领养申请
    if pet_has_pending_release(pet):
        return json_fail('该宠物已有待放归记录，暂不可申请领养')

    # 黑名单检查
    bl = check_blacklist(body_str(data, 'applicant_id_card'), body_str(data, 'applicant_phone'))
    if bl:
        return json_fail(f'您已被列入领养黑名单，无法申请：{bl.reason}')

    # 检查是否已对该宠物提交处理中的申请
    #
    # ⚠ 只排除 `rejected` 会把「自己撤回的申请」也算成「处理中」—— 领养人
    # 撤回（`cancelled`）之后就**永久无法再申请同一只动物**，而提示语写的是
    # 「请勿重复提交」，看起来像正常的防重复，实际是死锁。
    # `rejected` 与 `cancelled` 都是「这条申请已经不作数了」，必须同一口径。
    if AdoptionApplication.objects.filter(
        applicant=request.user, pet=pet,
    ).exclude(status__in=('rejected', 'cancelled')).exists():
        return json_fail('您已提交过该宠物的领养申请，请勿重复提交')

    applicant_name = body_str(data, 'applicant_name').strip()
    applicant_phone = body_str(data, 'applicant_phone').strip()
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
        applicant_id_card=body_str(data, 'applicant_id_card'),
        applicant_address=body_str(data, 'applicant_address'),
        qualification=body_str(data, 'qualification'),
        reason=body_str(data, 'reason'),
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
@role_required('hospital', 'shelter')
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
    elif user.role == 'shelter':
        qs = qs.filter(pet__shelter_id=user.institution_id)

    application = qs.filter(id=pk).first()
    if application is None:
        return json_fail('申请不存在或无权访问', status=404)

    if application.status != 'pending':
        return json_fail(f'该申请已处理（{application.get_status_display()}）')

    action = body_str(data, 'action')
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
        if pet_has_approved_application(pet):
            return json_fail('该宠物已有已批准的领养申请，无法重复批准')
        if pet_has_pending_release(pet):
            return json_fail('该宠物已有待放归记录，无法通过领养申请')
        if pet_has_pending_return(pet):
            return json_fail('该宠物已在运回捕捉点的单据上，无法通过领养申请')

    application.status = 'approved' if action == 'approve' else 'rejected'
    application.review_note = body_str(data, 'review_note')
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


@csrf_exempt
@role_required('adopter')
@login_required
def adoption_application_cancel(request, pk):
    """领养人撤回**自己**的领养申请（`pending` → `cancelled`）。

    ⚠ 这个状态从第四十六轮起才有产生端。此前 `AdoptionApplication.STATUS_CHOICES`
    里声明了 `('cancelled', '已取消')`，领养人端的 `getApplyStatusText()` /
    `getApplyStatusBadge()` 也都映射了它 —— 但**没有任何接口或按钮能写出它**。
    典型的「枚举值存在、写入路径缺失」：界面上永远看不到「已取消」，而代码里
    到处都假设它可能存在（前端两处映射就是证据）。走查时正是从这两处映射
    反查到「产生端缺失」。

    ## 只允许撤回 `pending`

    * `approved` 的撤回要走机构侧的「撤销领养」（`adoption_revoke`）——
      那会连带把动物退回待领养、重新上架，**领养人自己不该有这个权力**；
    * `rejected` / `cancelled` 已是终态，重复撤回没有意义（且会让「已取消」
      覆盖掉「已拒绝」这个更有信息量的结论）。

    ## 归属从申请单本身推导

    判据是 `applicant_id == request.user.id`，**不是**「操作者角色是领养人」——
    否则领养人 A 能撤回 B 的申请（同族缺陷本项目已踩过四次：归属不能从
    操作者推导）。「不是我的」与「不存在」必须**完全不可区分**（同一个 404、
    同一句文案），否则就成了存在性预言机。

    ## 撤回后必须能重新申请

    创建接口（`adoption_application_create`）的重复校验原先只
    `.exclude(status='rejected')`。若不同步放行 `cancelled`，领养人一撤回就
    **永久无法再申请同一只动物**，而且界面只提示「您已提交过…请勿重复提交」——
    看起来像防重复，实际是把人锁死。所以两处判据必须同一口径。
    """
    application = AdoptionApplication.objects.filter(id=pk).first()
    # 存在性预言机：「别人的申请」与「不存在」回同一个 404、同一句文案。
    if application is None or application.applicant_id != request.user.id:
        return json_fail('申请不存在或无权访问', status=404)

    if application.status != 'pending':
        return json_fail(
            f'该申请已处理（{application.get_status_display()}），无法撤回')

    application.status = 'cancelled'
    application.save(update_fields=['status'])

    # 审计用上下文：`AdoptionApplication` 没有 district 字段（沿 pet 派生）。
    request.audit_district = application.pet.district if application.pet_id else None

    return json_ok(serialize_instance(application), message='申请已撤回，可重新提交')


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
