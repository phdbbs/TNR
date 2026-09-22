"""
门户端视图 - 各角色门户页面渲染 + 门户专用辅助 API。
"""
import json

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.utils import timezone
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import (
    Pet, AdoptionHallListing, Adoption, Message,
    Capture, Transfer, Treatment, Release, Euthanasia, OwnerReturn,
)
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    get_district_filtered_queryset, get_scoped_object, hospital_pet_scope,
    pet_archive_records, pet_brief, pet_photo_list,
)


# ============================================
# 捕捉点门户页面
# ============================================
@never_cache  # 门户页面禁用缓存，避免手机浏览器缓存旧版本页面
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def shelter_portal(request):
    """捕捉点端门户页面。"""
    user = request.user
    user_data = {
        'id': user.id,
        'username': user.username,
        'name': user.get_full_name() or user.username,
        'role': user.role,
        'role_display': user.get_role_display(),
        'district_id': user.district_id,
        'district_name': user.district.name if user.district else '',
        'institution_id': user.institution_id,
        'institution_name': user.institution.name if user.institution else '',
        'phone': user.phone,
    }
    return render(request, 'portal/shelter/portal.html', {
        'user_data': json.dumps(user_data, ensure_ascii=False),
    })


# ============================================
# 医院门户页面
# ============================================
@never_cache
@role_required('hospital', 'gov_city', 'gov_district')
@login_required
def hospital_portal(request):
    """宠物医院端门户页面。"""
    user = request.user
    user_data = {
        'id': user.id,
        'username': user.username,
        'name': user.get_full_name() or user.username,
        'role': user.role,
        'role_display': user.get_role_display(),
        'district_id': user.district_id,
        'district_name': user.district.name if user.district else '',
        'institution_id': user.institution_id,
        'institution_name': user.institution.name if user.institution else '',
        'phone': user.phone,
    }
    return render(request, 'portal/hospital/portal.html', {
        'user_data': json.dumps(user_data, ensure_ascii=False),
    })


# ============================================
# 政府监管门户页面
# ============================================
@never_cache
@role_required('gov_city', 'gov_district')
@login_required
def gov_portal(request):
    """政府监管端门户页面。"""
    user = request.user
    user_data = {
        'id': user.id,
        'username': user.username,
        'name': user.get_full_name() or user.username,
        'role': user.role,
        'role_display': user.get_role_display(),
        'district_id': user.district_id,
        'district_name': user.district.name if user.district else '',
        'institution_id': user.institution_id,
        'institution_name': user.institution.name if user.institution else '',
        'phone': user.phone,
    }
    return render(request, 'portal/gov/portal.html', {
        'user_data': json.dumps(user_data, ensure_ascii=False),
    })


# ============================================
# 门户专用辅助 API - 医院
# ============================================
@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def hospital_pets(request):
    """医院在院宠物列表（可按 status 过滤）。

    GET /api/business/pets/?status=in_treatment
    """
    user = request.user

    if user.role == 'hospital':
        # 医院只看分配给自己的宠物（不按区县过滤，因为转运可能跨区县）
        if user.institution_id:
            qs = Pet.objects.filter(hospital_id=user.institution_id, is_deleted=False)
        else:
            qs = Pet.objects.none()
    elif user.role == 'shelter':
        # 捕捉点只看**本机构所在区县**的宠物，与 ``transfer_create`` 的归属区县口径
        # 对齐（转运单的 anchor 是捕捉点机构 → 机构所在区县）。
        #
        # 不能按 ``user.district`` 过滤：现场把捕捉点操作员都挂在「全市（市级）」下，
        # 那样会返回**全市**宠物，备选框里混进其他区县的动物，用户点了「下发」必然
        # 被后端的跨区县校验拦下（"以下动物不属于本区县，无法转运"）——
        # 备选框里出现永远选不中的选项，比空列表更糟。
        inst = user.institution
        if inst is not None and inst.district_id:
            qs = Pet.objects.filter(district_id=inst.district_id, is_deleted=False)
        else:
            qs = Pet.objects.none()
    else:
        qs = get_district_filtered_queryset(Pet, user).filter(is_deleted=False)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    data = [serialize_instance(p) for p in qs]
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def pet_archive(request):
    """一宠一档：动物档案台账（每只动物一行，聚合全生命周期数据）。

    GET /api/business/pets/archive/[?status=..&keyword=..]

    捕捉端「全量台账 → 一宠一档」用它渲染列表；点击编号后再调
    ``pets/<id>/lifecycle/`` 拉各阶段明细与照片。

    聚合逻辑与政府端「台账中心 → 一宠一档」共用
    ``business.services.pet_archive_records``，两端字段与口径完全一致，
    不会出现「一端有出库原因、另一端没有」的漂移。

    **医院角色收敛到本机构**（第二十轮）：同一区县可能有多家医院，
    按区县过滤会让 A 院读到 B 院名下动物的完整档案与主人联系方式。
    口径与 ``hospital_pets``（`/pets/`，注释写明「医院只看分配给自己的宠物」）
    和 ``hospital_hall_listings``（`/hall-listings/`）保持一致。
    """
    user = request.user
    if user.role == 'hospital':
        qs = Pet.objects.filter(is_deleted=False).filter(
            hospital_pet_scope(user)).distinct()
    else:
        # 捕捉点与政府端按区县：捕捉点就是本区县的收容入口（与 `/pets/`
        # 的捕捉点分支同口径），政府端本就该看全区。
        qs = get_district_filtered_queryset(Pet, user).filter(is_deleted=False)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    keyword = (request.GET.get('keyword') or '').strip()
    if keyword:
        # 必须写成单个 Q()：qs.filter(a) | qs.filter(b) 会丢掉前面的
        # 区县隔离过滤，并产生重复行
        qs = qs.filter(
            Q(code__icontains=keyword)
            | Q(name__icontains=keyword)
            | Q(breed__icontains=keyword)
            | Q(chip_no__icontains=keyword)
        )

    qs = qs.select_related('district', 'shelter', 'hospital', 'capture').prefetch_related(
        'treatments', 'releases', 'adoptions', 'euthanasia_records', 'owner_returns')
    return json_ok(pet_archive_records(qs))


@csrf_exempt
@role_required('hospital', 'adopter', 'gov_city', 'gov_district')
@login_required
def hospital_hall_listings(request):
    """医院领养大厅上架信息列表（含已下架）。

    GET /api/business/hall-listings/
    """
    user = request.user
    qs = AdoptionHallListing.objects.all()

    if user.role == 'hospital':
        if user.institution_id:
            qs = qs.filter(hospital_id=user.institution_id)
        else:
            qs = qs.none()
    elif user.role == 'adopter':
        # 领养人只看已上架的领养信息
        qs = qs.filter(is_active=True)
    else:
        qs = get_district_filtered_queryset(AdoptionHallListing, user)

    data = []
    for listing in qs:
        item = serialize_instance(listing)
        item['pet'] = serialize_instance(listing.pet) if listing.pet_id else None
        data.append(item)
    return json_ok(data)


# ============================================
# 领养人门户页面
# ============================================
@never_cache
@role_required('adopter', 'gov_city', 'gov_district')
@login_required
def adopter_portal(request):
    """领养人端门户页面（登录后全功能）。"""
    user = request.user
    user_data = {
        'id': user.id,
        'username': user.username,
        'name': user.get_full_name() or user.username,
        'role': user.role,
        'role_display': user.get_role_display(),
        'district_id': user.district_id,
        'district_name': user.district.name if user.district else '',
        'institution_id': user.institution_id,
        'institution_name': user.institution.name if user.institution else '',
        'phone': user.phone,
    }
    return render(request, 'portal/adopter/portal.html', {
        'user_data': json.dumps(user_data, ensure_ascii=False),
        'public_mode': False,
    })


def adoption_hall_public(request):
    """领养大厅公开页面（无需登录）。

    未登录或非领养人角色时仅展示领养大厅；
    已登录的领养人/政府角色直接进入完整门户。
    """
    if request.user.is_authenticated and request.user.role in ('adopter', 'gov_city', 'gov_district'):
        return adopter_portal(request)

    user_data = None
    if request.user.is_authenticated:
        user_data = json.dumps({
            'id': request.user.id,
            'username': request.user.username,
            'name': request.user.get_full_name() or request.user.username,
            'role': request.user.role,
            'role_display': request.user.get_role_display(),
            'phone': request.user.phone,
        }, ensure_ascii=False)

    return render(request, 'portal/adopter/portal.html', {
        'user_data': user_data or 'null',
        'public_mode': True,
    })


# ============================================
# 门户专用辅助 API - 领养人
# ============================================
@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district')
@login_required
def my_adoptions(request):
    """领养人 - 我的领养记录列表。"""
    qs = Adoption.objects.filter(adopter=request.user).order_by('-id')
    data = []
    for a in qs:
        item = serialize_instance(a)
        if a.pet_id:
            item['pet'] = serialize_instance(a.pet)
        data.append(item)
    return json_ok(data)


@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district')
@login_required
def my_messages(request):
    """领养人 - 我的消息列表。"""
    qs = Message.objects.filter(user=request.user).order_by('-id')
    data = [serialize_instance(m) for m in qs]
    return json_ok(data)


@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district')
@login_required
def mark_message_read(request, pk):
    """领养人 - 标记消息已读。"""
    if request.method != 'POST':
        return json_fail('仅支持 POST 请求', status=405)
    try:
        msg = Message.objects.get(id=pk, user=request.user)
    except Message.DoesNotExist:
        return json_fail('消息不存在', status=404)

    msg.is_read = True
    msg.save(update_fields=['is_read'])
    # 读回执不写审计：`Message` 没有归属区县（审计里会是一条无法归属的记录），
    # 而且每次打开消息都会产生一条 —— 只会把真正有意义的业务操作挤出日志页。
    request.audit_skip = True
    return json_ok(serialize_instance(msg), message='已标记为已读')


# ============================================
# 宠物全生命周期溯源（领养人端）
# ============================================
def _stage_photos(pet, *fields):
    """取动物的指定阶段照片（只返回确实有图的），供时间线内嵌展示。"""
    wanted = set(fields)
    return [p for p in pet_photo_list(pet) if p['field'] in wanted]


@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district', 'shelter', 'hospital')
@login_required
def pet_lifecycle(request, pet_id):
    """返回指定宠物的全生命周期溯源记录。

    按时间顺序返回：捕捉、转运、诊疗、放养、领养、安乐死记录。
    按角色收敛可见范围：领养人仅限自己领养/申请过的动物，
    **医院限本院在治或本院经手过（诊疗）的动物**，
    其余角色按所属区县过滤，避免只凭主键即可遍历全量动物档案。

    医院这一支**不能按区县收敛**（第二十轮）：同一区县可能有多家医院
    （襄城区有 2 家），按区县会让 A 院读到 B 院名下动物的完整溯源记录 ——
    含主人姓名与电话。判据与 ``pet_archive`` 共用 ``hospital_pet_scope()``。
    """
    user = request.user
    if user.role == 'adopter':
        pet = Pet.objects.filter(id=pet_id).filter(
            Q(adoptions__adopter=user) | Q(adoption_applications__applicant=user)
        ).distinct().first()
    elif user.role == 'hospital':
        pet = Pet.objects.filter(id=pet_id).filter(
            hospital_pet_scope(user)).distinct().first()
    else:
        pet = get_scoped_object(Pet, pet_id, user)

    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

    events = []

    # 捕捉记录
    if pet.capture:
        cap = pet.capture
        capture_photos = _stage_photos(pet, 'photo_capture')
        if cap.group_photo:
            capture_photos.append(
                {'field': 'group_photo', 'label': '整体合影', 'url': cap.group_photo.url})
        events.append({
            'type': 'capture',
            'type_display': '捕捉登记',
            'date': timezone.localdate(cap.created_at).isoformat() if cap.created_at else '',
            'ledger_no': cap.ledger_no,
            'shelter_name': cap.shelter_name,
            'community_name': cap.community_name,
            'address': cap.address,
            'property_name': cap.property_name,
            'contact_person': cap.contact_person,
            'contact_phone': cap.contact_phone,
            'operator_name': cap.operator_name,
            'photos': capture_photos,
        })

    # 转运记录
    transfers = Transfer.objects.filter(pet_codes__contains=pet.code).order_by('created_at')
    for t in transfers:
        events.append({
            'type': 'transfer',
            'type_display': '转运交接',
            'date': timezone.localdate(t.created_at).isoformat() if t.created_at else '',
            'ledger_no': t.ledger_no,
            'from_shelter_name': t.from_shelter_name,
            'to_hospital_name': t.to_hospital_name,
            'status': t.status,
            'received_at': t.received_at.isoformat() if t.received_at else '',
            'operator_name': t.operator_name,
        })

    # 诊疗记录
    treatments = Treatment.objects.filter(pet=pet).order_by('created_at')
    for t in treatments:
        items = []
        if t.items_sterilization: items.append('绝育')
        if t.items_vaccine: items.append('疫苗')
        if t.items_deworming: items.append('驱虫')
        if t.items_chip: items.append('芯片')
        events.append({
            'type': 'treatment',
            'type_display': '诊疗记录',
            'date': timezone.localdate(t.created_at).isoformat() if t.created_at else '',
            'ledger_no': t.ledger_no,
            'hospital_name': t.hospital_name,
            'items': items,
            'chip_no': t.chip_no,
            'status': t.status,
            'operator_name': t.operator_name,
            'photos': _stage_photos(pet, 'photo_before', 'photo_after', 'photo_treatment'),
        })

    # 放养记录
    releases = Release.objects.filter(pet=pet).order_by('created_at')
    for r in releases:
        events.append({
            'type': 'release',
            'type_display': '放养记录',
            'date': r.released_at.isoformat() if r.released_at else (timezone.localdate(r.created_at).isoformat() if r.created_at else ''),
            'ledger_no': r.ledger_no,
            'community_name': r.community_name,
            'receiver_name': r.receiver_name,
            'status': r.status,
            'operator_name': r.operator_name,
        })

    # 领养记录
    adoptions = Adoption.objects.filter(pet=pet).order_by('created_at')
    for a in adoptions:
        events.append({
            'type': 'adoption',
            'type_display': '领养记录',
            'date': a.adopted_at.isoformat() if a.adopted_at else (timezone.localdate(a.created_at).isoformat() if a.created_at else ''),
            'ledger_no': a.ledger_no,
            'adopter_name': a.adopter_name,
            'hospital_name': a.hospital_name,
            'status': a.status,
            'operator_name': a.operator_name,
        })

    # 安乐死记录
    euthanasias = Euthanasia.objects.filter(pet=pet).order_by('created_at')
    for e in euthanasias:
        events.append({
            'type': 'euthanasia',
            'type_display': '安乐死记录',
            'date': e.euthanized_at.isoformat() if e.euthanized_at else (timezone.localdate(e.created_at).isoformat() if e.created_at else ''),
            'ledger_no': e.ledger_no,
            'hospital_name': e.hospital_name,
            'reason': e.reason,
            'operator_name': e.operator_name,
        })

    # 主人领回（回收）记录
    # 每只动物只可能有一条 OwnerReturn（owner_returned 状态互斥），但保留循环以兼容历史数据
    owner_returns = OwnerReturn.objects.filter(pet=pet).order_by('created_at')
    for r in owner_returns:
        events.append({
            'type': 'owner_return',
            'type_display': '回收登记',
            # ⚠ `date` 字段一律是**本地日期**（前端当 `YYYY-MM-DD` 用）。
            # 本行原来写的是 `r.return_time.isoformat()` —— `return_time` 是
            # DateTimeField，`.isoformat()` 给的是 **UTC 带偏移**的时间串，于是：
            #   ① `date` 字段语义不一致（同文件其它 6 处都是纯日期）；
            #   ② 前端 `.slice(0, 10)` / `formatDate` 拿到 **UTC 日期**，
            #      北京时间 00:00–08:00 的回收记录会显示成**前一天**。
            # 与 `created_at` 分支、以及 `views_capture` 里
            # `return_time__date__gte`（Django 转本地时区）的口径对齐。
            'date': timezone.localdate(r.return_time).isoformat() if r.return_time else (timezone.localdate(r.created_at).isoformat() if r.created_at else ''),
            'ledger_no': r.ledger_no,
            'owner_name': r.owner_name,
            'owner_phone': r.owner_phone,
            'owner_id_card': r.owner_id_card,
            'owner_address': r.owner_address,
            'reason': r.reason,
            # 签字是 base64 data URL，前端 <img> 直接展示即可
            'signature': r.signature or '',
            'operator_name': r.operator_name,
            'created_at': r.created_at.isoformat() if r.created_at else '',
        })

    # 按日期排序（空日期排最后）
    events.sort(key=lambda x: x.get('date') or '', reverse=False)

    return json_ok({
        'pet': serialize_instance(pet),
        # pet_brief / photos 为「一宠一档」详情抽屉补充：统一的中文字段标签 +
        # 按阶段排好序的照片列表（前端据此渲染照片墙并点击放大）。
        # 保留原有 pet/events 结构，领养人端的时间线不受影响。
        'pet_brief': pet_brief(pet),
        'photos': pet_photo_list(pet),
        'events': events,
    })
