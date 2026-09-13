"""
门户端视图 - 各角色门户页面渲染 + 门户专用辅助 API。
"""
import json

from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import (
    Pet, AdoptionHallListing, Adoption, Message,
    Capture, Transfer, Treatment, Release, Euthanasia,
)
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    get_district_filtered_queryset,
)


# ============================================
# 捕捉点门户页面
# ============================================
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
            qs = Pet.objects.filter(hospital_id=user.institution_id)
        else:
            qs = Pet.objects.none()
    else:
        qs = get_district_filtered_queryset(Pet, user)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    data = [serialize_instance(p) for p in qs]
    return json_ok(data)


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
    return json_ok(serialize_instance(msg), message='已标记为已读')


# ============================================
# 宠物全生命周期溯源（领养人端）
# ============================================
@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district', 'shelter', 'hospital')
@login_required
def pet_lifecycle(request, pet_id):
    """返回指定宠物的全生命周期溯源记录。

    按时间顺序返回：捕捉、转运、诊疗、放养、领养、安乐死记录。
    """
    try:
        pet = Pet.objects.get(id=pet_id)
    except Pet.DoesNotExist:
        return json_fail('宠物不存在', status=404)

    events = []

    # 捕捉记录
    if pet.capture:
        cap = pet.capture
        events.append({
            'type': 'capture',
            'type_display': '捕捉登记',
            'date': timezone.localdate(cap.created_at).isoformat() if cap.created_at else '',
            'ledger_no': cap.ledger_no,
            'shelter_name': cap.shelter_name,
            'community_name': cap.community_name,
            'address': cap.address,
            'operator_name': cap.operator_name,
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

    # 按日期排序（空日期排最后）
    events.sort(key=lambda x: x.get('date') or '', reverse=False)

    return json_ok({
        'pet': serialize_instance(pet),
        'events': events,
    })
