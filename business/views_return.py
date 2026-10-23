"""运回单：医院把诊疗完成的动物成批交回捕捉点（第五十四轮）。

放归不是医院直接把动物扔回小区：动物要先由医院运回捕捉点，捕捉点
签收后再按捕捉单找到原小区，与物业签字放归。本模块承载前半段：
医院开运回单（待确认）→ 捕捉点确认接收（已确认出库）/ 驳回，
医院在捕捉点确认前也可以撤回。
"""
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from core.models import Institution
from business.models import Pet, ReturnOrder
from business.services_discharge import discharge_candidate_error
from business.services_treatment import dispose_for_discharge
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_str, body_int, generate_doc_no,
    pet_has_active_adoption, pet_has_approved_application,
    pet_has_pending_release, pet_has_pending_return,
)


def _restore_pet_status(pet):
    """运回单被驳回/撤回后，动物该退回哪一态。

    `pending_adopt`（诊疗已办结、待领养）与 `in_treatment`（诊疗完成但
    出院页还没办结）都能开运回单，退回时不能一律写成「待领养」——
    否则诊疗看板会凭空少一只在院动物，而医院其实还要继续处理。
    """
    if pet.treatments.filter(disposed_at__isnull=False).exists():
        return 'pending_adopt'
    if getattr(pet, 'hall_listing', None) is not None:
        return 'pending_adopt'
    return 'in_treatment'


def _allowed_shelter_q(hospital):
    """运回单可选的捕捉点范围：市级捕捉点 + 本院所在区县的捕捉点。

    需求明确「可以是市级，也可以是本医院所在区县，不能是其他区县」。
    判据用医院的 `district`，不是动物的区县 —— 跨区县送医时动物区县
    与本院不同，按动物区县会把本院能选的捕捉点挡掉。
    """
    q = Q(district__is_city=True)
    if hospital is not None and hospital.district_id:
        q |= Q(district_id=hospital.district_id)
    return q


def _pet_brief(pet):
    return {
        'id': pet.id,
        'code': pet.code,
        'name': pet.name or '',
        'species': pet.species or '',
        'breed': pet.breed or '',
        'gender': pet.gender or '',
        'status': pet.status,
        'status_display': pet.get_status_display(),
    }


def _order_for(user, pk, *, for_update=False):
    qs = ReturnOrder.objects.all()
    if user.role == 'hospital':
        if not user.institution_id:
            return None
        qs = qs.filter(
            Q(institution_id=user.institution_id)
            | Q(institution_id__isnull=True,
                hospital_id=user.institution_id))
    elif user.role == 'shelter':
        if not user.institution_id:
            return None
        qs = qs.filter(shelter_id=user.institution_id)
    else:
        return None
    if for_update:
        qs = qs.select_for_update()
    return qs.filter(pk=pk).first()


def _serialize_order(order, pets_by_code):
    data = serialize_instance(order)
    data['pets'] = [
        _pet_brief(pets_by_code[code])
        for code in (order.pet_codes or '').split(',')
        if code.strip() and code.strip() in pets_by_code
    ]
    return data


@csrf_exempt
@role_required('hospital', 'shelter')
@login_required
def return_order_list(request):
    """运回单列表：医院看本院开出的，捕捉点看发给本站的。"""
    user = request.user
    qs = ReturnOrder.objects.all()
    if user.role == 'hospital':
        if not user.institution_id:
            qs = qs.none()
        else:
            qs = qs.filter(
                Q(institution_id=user.institution_id)
                | Q(institution_id__isnull=True,
                    hospital_id=user.institution_id))
    elif user.role == 'shelter':
        if not user.institution_id:
            qs = qs.none()
        else:
            qs = qs.filter(shelter_id=user.institution_id)
    else:
        qs = qs.none()

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    orders = list(qs.select_related('shelter', 'hospital').order_by('-id'))
    codes = set()
    for order in orders:
        codes.update(c.strip() for c in (order.pet_codes or '').split(',') if c.strip())
    pets_by_code = {
        p.code: p for p in Pet.objects.filter(code__in=codes).select_related('district')
    } if codes else {}
    return json_ok([_serialize_order(o, pets_by_code) for o in orders])


@csrf_exempt
@role_required('hospital')
@login_required
def return_order_shelter_options(request):
    """运回单可选的捕捉点（医院端）。

    通用 `institution_list` 按操作员区县过滤，区县医院**看不到市级捕捉点**，
    而运回单允许发给市级 —— 必须单独给一个口径一致的选项接口，
    否则前端只能拿到本区机构，市级那条路在界面上不存在。
    """
    user = request.user
    hospital = user.institution if user.institution_id else None
    qs = Institution.objects.filter(
        type='shelter', status='active').filter(_allowed_shelter_q(hospital))
    # 只回下拉框要用的字段：机构地址、联系人、电话对「开运回单」这件事没用，
    # 整条机构记录发给医院等于多下发一批无关的员工联系方式。
    data = [{
        'id': inst.id,
        'code': inst.code,
        'name': inst.name,
        'district_name': inst.district.name if inst.district else '',
        'is_city': bool(inst.district and inst.district.is_city),
    } for inst in qs.select_related('district').order_by('district__is_city', 'name')]
    return json_ok(data)


@csrf_exempt
@role_required('hospital')
@login_required
def return_order_create(request):
    """医院开运回单（多选动物）。

    请求体示例:
    {
        "pet_ids": [1, 2, 3],
        "note": "诊疗完成，运回放归"
    }
    """
    data = parse_json_body(request)
    user = request.user
    pet_ids = data.get('pet_ids')
    if not isinstance(pet_ids, list) or not pet_ids:
        return json_fail('请至少选择一只待放归动物')

    try:
        pet_ids = [int(x) for x in pet_ids]
    except (TypeError, ValueError):
        return json_fail('动物编号格式不正确')
    if len(pet_ids) != len(set(pet_ids)):
        return json_fail('动物重复选择，请刷新后重试')
    if len(pet_ids) > 50:
        return json_fail('单张运回单最多 50 只动物')

    pets = list(Pet.objects.select_related('shelter', 'district').filter(
        id__in=pet_ids, is_deleted=False))
    if len(pets) != len(pet_ids):
        return json_fail('部分动物不存在或档案已作废', status=404)

    if not user.institution_id:
        return json_fail('当前账号未关联医院，无法开运回单')

    for pet in pets:
        if pet.hospital_id != user.institution_id:
            return json_fail(f'{pet.code} 当前不在本院，无法运回')
        # 先于状态校验：`return_pending` 本身就是「已在另一张单上」，
        # 报「状态不可运回」会让操作员以为是动物本身有问题。
        if pet_has_pending_return(pet):
            return json_fail(f'{pet.code} 已在另一张未办结的运回单上')
        # `pending_adopt` = 已办结、待领养；`in_treatment` 只接受**诊疗已完成**
        # 的动物（出院流程还没点办结时也要能直接开运回单，由 `dispose(...,
        # defer_pet_status=True)` 接管动物状态）。
        if pet.status not in ('pending_adopt', 'in_treatment'):
            return json_fail(
                f'{pet.code} 当前状态({pet.get_status_display()})不可运回，'
                '仅诊疗完成的动物可开运回单')
        if pet.status == 'in_treatment' and not pet.treatments.filter(
                done_at__isnull=False).exists():
            return json_fail(f'{pet.code} 尚未完成诊疗，不能运回捕捉点')
        # 建议去向必须是「放回原小区」（第五十七轮）：一张运回单会同时
        # 把整批动物打上 `return_pending`，混进一只建议领养的动物，
        # 后面捕捉点签收就等于替医院把领养动物放归了。
        err = discharge_candidate_error(pet, 'release')
        if err:
            return json_fail(err)
        if pet_has_active_adoption(pet):
            return json_fail(f'{pet.code} 已有未完结的领养记录，请先撤销')
        if pet_has_approved_application(pet):
            return json_fail(f'{pet.code} 已有已批准的领养申请（待出库），请先处理')
        if pet_has_pending_release(pet):
            return json_fail(f'{pet.code} 已有未办结的放归单，请先处理')

    hospital = user.institution
    shelter_id = body_int(data, 'shelter_id')
    if shelter_id:
        shelter = Institution.objects.filter(
            id=shelter_id, type='shelter', status='active').select_related(
                'district').first()
        if shelter is None:
            return json_fail('接收捕捉点不存在或已停用')
        allowed = bool(shelter.district and (
            shelter.district.is_city
            or (hospital.district_id and shelter.district_id == hospital.district_id)))
        if not allowed:
            return json_fail('只能选择市级捕捉点，或本院所在区县的捕捉点')
    else:
        # 兼容旧前端：没显式选捕捉点时按动物的原捕捉点推导，
        # 但多捕捉点的票仍然发不出去（一张单只能有一个接收方）。
        shelters = {p.shelter_id for p in pets}
        if None in shelters:
            return json_fail('部分动物缺少关联捕捉点，无法运回')
        if len(shelters) != 1:
            return json_fail('一张运回单只能发给同一个捕捉点，请分开选择')
        shelter = pets[0].shelter

    districts = {p.district_id for p in pets}
    if len(districts) != 1:
        return json_fail('一张运回单的动物必须属于同一区县')

    order = ReturnOrder.objects.create(
        shelter=shelter,
        shelter_name=shelter.name,
        hospital=hospital,
        hospital_name=hospital.name,
        institution=hospital,
        pet_codes=','.join(p.code for p in pets),
        pet_count=len(pets),
        status='pending',
        note=body_str(data, 'note'),
        operator=user,
        operator_name=user.get_full_name() or user.username,
        ledger_no=generate_doc_no('RTN'),
        district=pets[0].district,
    )
    Pet.objects.filter(id__in=pet_ids).update(status='return_pending')
    # 返回值要与库里的新状态一致：`update()` 不刷新已取出的对象，
    # 否则刚开出的运回单在响应里仍显示「待领养」。
    for pet in pets:
        pet.status = 'return_pending'

    pets_by_code = {p.code: p for p in pets}
    return json_ok(_serialize_order(order, pets_by_code),
                   message='运回单已提交，等待捕捉点确认接收')


@csrf_exempt
@role_required('shelter')
@login_required
def return_order_receive(request, pk):
    """捕捉点确认接收（接收单）：运回单 → 已确认出库，动物进入待放归。"""
    data = parse_json_body(request)
    user = request.user
    receiver_name = body_str(data, 'receiver_name').strip()
    receiver_phone = body_str(data, 'receiver_phone').strip()
    signature = body_str(data, 'signature')
    if not receiver_name:
        return json_fail('请填写接收人')
    if not signature:
        return json_fail('请填写接收人签字')

    date_str = data.get('received_at')
    if date_str:
        try:
            received_at = datetime.strptime(str(date_str)[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return json_fail(f'接收日期「{date_str}」格式不正确，应为 YYYY-MM-DD')
    else:
        received_at = timezone.localdate()

    with transaction.atomic():
        order = _order_for(user, pk, for_update=True)
        if order is None:
            return json_fail('运回单不存在或无权访问', status=404)
        if order.status != 'pending':
            return json_fail(f'当前状态({order.get_status_display()})不可确认接收')

        codes = [c.strip() for c in (order.pet_codes or '').split(',') if c.strip()]
        pets = list(Pet.objects.select_for_update().filter(code__in=codes))
        if len(pets) != len(codes):
            return json_fail('运回单中的动物档案缺失，无法确认接收')
        for pet in pets:
            if pet.status != 'return_pending':
                return json_fail(
                    f'{pet.code} 当前状态({pet.get_status_display()})不是「待运回捕捉点」，'
                    '请刷新后重试')
        for pet in pets:
            pet.status = 'pending_release'
            pet.hospital = None
            pet.shelter = order.shelter
            pet.save(update_fields=['status', 'hospital', 'shelter'])
            # 捕捉点签收 = 动物真正离开医院，诊疗单此刻办结（去向：放回原小区）。
            dispose_for_discharge(pet, 'released', user)

        order.status = 'received'
        order.received_at = received_at
        order.receiver_name = receiver_name
        order.receiver_phone = receiver_phone
        order.signature = signature
        order.save(update_fields=[
            'status', 'received_at', 'receiver_name', 'receiver_phone', 'signature',
        ])

    pets_by_code = {p.code: p for p in pets}
    return json_ok(_serialize_order(order, pets_by_code), message='接收成功，动物进入待放归')


@csrf_exempt
@role_required('shelter')
@login_required
def return_order_reject(request, pk):
    """捕捉点驳回运回单：动物退回医院待领养，等医院修正后重开。"""
    data = parse_json_body(request)
    reason = body_str(data, 'reject_reason').strip()
    if not reason:
        return json_fail('请填写驳回原因')

    with transaction.atomic():
        order = _order_for(request.user, pk, for_update=True)
        if order is None:
            return json_fail('运回单不存在或无权访问', status=404)
        if order.status != 'pending':
            return json_fail(f'当前状态({order.get_status_display()})不可驳回')

        codes = [c.strip() for c in (order.pet_codes or '').split(',') if c.strip()]
        pets = list(Pet.objects.select_for_update().filter(code__in=codes))
        for pet in pets:
            if pet.status == 'return_pending':
                pet.status = _restore_pet_status(pet)
                pet.save(update_fields=['status'])

        order.status = 'rejected'
        order.reject_reason = reason
        order.save(update_fields=['status', 'reject_reason'])

    pets_by_code = {p.code: p for p in pets}
    return json_ok(_serialize_order(order, pets_by_code), message='已驳回运回单')


@csrf_exempt
@role_required('hospital')
@login_required
def return_order_withdraw(request, pk):
    """医院撤回尚未被捕捉点确认的运回单。"""
    if not request.user.institution_id:
        return json_fail('当前账号未关联医院，无法撤回运回单')
    with transaction.atomic():
        order = _order_for(request.user, pk, for_update=True)
        if order is None:
            return json_fail('运回单不存在或无权访问', status=404)
        if order.status != 'pending':
            return json_fail(f'当前状态({order.get_status_display()})不可撤回')

        codes = [c.strip() for c in (order.pet_codes or '').split(',') if c.strip()]
        pets = list(Pet.objects.select_for_update().filter(code__in=codes))
        for pet in pets:
            if pet.status == 'return_pending':
                pet.status = _restore_pet_status(pet)
                pet.save(update_fields=['status'])

        order.status = 'withdrawn'
        order.save(update_fields=['status'])

    pets_by_code = {p.code: p for p in pets}
    return json_ok(_serialize_order(order, pets_by_code), message='运回单已撤回')
