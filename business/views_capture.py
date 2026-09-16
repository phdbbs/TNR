"""
Task 4: 捕捉登记与主人领回
- 捕捉登记列表/详情/创建/编辑/逻辑删除
- 主人领回登记
"""
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Capture, Pet, OwnerReturn
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    generate_pet_codes, generate_ledger_no, get_district_scope,
    get_district_filtered_queryset, check_blacklist, amap_regeo,
    amap_ip_location, capture_transfer_state, capture_states_bulk,
    recalc_capture_status, get_active_pet,
)
from core.models import District, Institution


# 列表接口剔除体积巨大的签字 base64，详情接口才返回
CAPTURE_LIST_EXCLUDE = ('signature',)
PET_LIST_EXCLUDE = ('photo_capture', 'photo_group', 'photo_before',
                    'photo_after', 'photo_treatment')


def _to_float(v):
    """宽松的浮点解析：空串/非法值返回 None。"""
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _parse_return_time(v):
    """解析回收时间：支持 ISO 串与 ``datetime-local`` 的 ``YYYY-MM-DDTHH:MM``。

    前端 ``<input type="datetime-local">`` 提交的是不带时区的本地时间，
    必须补上时区信息再入库（本项目 ``USE_TZ=True``），否则会触发
    ``RuntimeWarning: received a naive datetime``。空值/非法值返回 None。
    """
    if not v:
        return None
    if hasattr(v, 'tzinfo'):  # 已是 datetime 对象
        return v if timezone.is_aware(v) else timezone.make_aware(v)
    text = str(v).strip()
    if not text:
        return None
    parsed = None
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def _get_capture_for_user(pk, user):
    """按区县范围取捕捉单，越权访问返回 None。"""
    qs = get_district_filtered_queryset(Capture, user)
    return qs.filter(id=pk).first()


def _resolve_district_scope(user, anchor, submitted):
    """解析并校验业务记录的归属区县。

    :param user: 当前操作员
    :param anchor: 承载「最可能的正确区县」的对象（捕捉单用捕捉点，主人领回用宠物）
    :param submitted: 前端显式提交的 district_id（可为 None）
    :return: (District 实例, 错误信息)；成功时错误信息为 None

    规则：
    1. 解析顺序：显式提交 → anchor 所在区县 → 操作员所属区县。
       anchor 的区县比操作员区县更贴近事实——现场把两个捕捉点操作员都挂在
       「全市（市级）」下，若以操作员区县为准，登记出来的捕捉单会全部落到市级，
       导致本区县政府在区县隔离下看不到本区数据。
    2. 必须是具体区县，不能是「全市（市级）」。
    3. 非市级操作员只能归属到自己的区县。
    """
    if submitted:
        district = District.objects.filter(pk=submitted).first()
        if district is None:
            return None, '归属区县不存在'
    else:
        district = getattr(anchor, 'district', None) or getattr(user, 'district', None)
    if district is None:
        return None, '缺少区县信息'

    if district.is_city:
        return None, '归属区县不能是市级，请选择具体区县'

    user_district = getattr(user, 'district', None)
    if user_district and not user_district.is_city and district.id != user_district.id:
        return None, '无权将记录归属到其他区县'
    return district, None


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def capture_list(request):
    """捕捉登记列表（按区县过滤，默认隐藏已逻辑删除的记录）"""
    qs = get_district_filtered_queryset(Capture, request.user)

    if request.GET.get('include_deleted') not in ('1', 'true', 'True'):
        qs = qs.filter(is_deleted=False)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    district = request.GET.get('district', '').strip()
    if district:
        qs = qs.filter(Q(district__name__icontains=district) | Q(district_id=district))

    community = request.GET.get('community', '').strip()
    if community:
        qs = qs.filter(community_name__icontains=community)

    shelter = request.GET.get('shelter', '').strip()
    if shelter:
        qs = qs.filter(shelter_name__icontains=shelter)

    keyword = request.GET.get('keyword', '').strip()
    if keyword:
        qs = qs.filter(
            Q(ledger_no__icontains=keyword)
            | Q(shelter_name__icontains=keyword)
            | Q(community_name__icontains=keyword)
            | Q(property_name__icontains=keyword)
            | Q(address__icontains=keyword)
            | Q(geo_address__icontains=keyword)
            | Q(contact_person__icontains=keyword)
            | Q(contact_phone__icontains=keyword)
            | Q(pet_codes__icontains=keyword)
            | Q(operator_name__icontains=keyword)
        )

    captures = list(qs)
    states = capture_states_bulk(captures)

    data = []
    for cap in captures:
        item = serialize_instance(cap, exclude=CAPTURE_LIST_EXCLUDE)
        state = states.get(cap.id, {})
        item['transferState'] = state
        item['transfer_state'] = state
        item['canEdit'] = state.get('can_edit', False)
        item['canDelete'] = state.get('can_delete', False)
        item['statusDisplay'] = cap.get_status_display()
        data.append(item)
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def pet_codes_preview(request):
    """预览即将生成的宠物编号（与提交后实际生成规则一致）。"""
    try:
        count = int(request.GET.get('count', 0))
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return json_fail('数量必须大于0')
    return json_ok(generate_pet_codes(count))


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def geocode_reverse(request):
    """逆地理编码：经纬度 → 地址名称（服务端代理高德API，避免前端跨域及Key泄露）。

    GET 参数：lat（纬度）、lng（经度）。前端传入的是 WGS-84（GPS）坐标，
    由前端先转换为 GCJ-02 后再请求本接口。
    """
    try:
        lat = float(request.GET.get('lat'))
        lng = float(request.GET.get('lng'))
    except (TypeError, ValueError):
        return json_fail('缺少有效的经纬度参数')
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return json_fail('经纬度超出有效范围')

    try:
        info = amap_regeo(lng, lat)
    except ValueError as e:
        return json_fail(str(e))

    info['latitude'] = lat
    info['longitude'] = lng
    return json_ok(info, message='定位解析成功')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def geocode_ip(request):
    """IP 定位兜底（浏览器精确定位不可用时的降级方案）。

    浏览器 Geolocation 仅允许在 HTTPS 或 localhost（安全源）下使用，
    通过 http://局域网IP:8000 访问时前端定位报错
    "Only secure origins are allowed"。手机与服务器通常处于同一网络
    （同一公网出口 IP），故由服务端调用高德 IP 定位，得到城市/区级
    粗略位置（rectangle 中心点），再逆地理出地址名称一并返回。
    data.precision = 'city' 标识粗定位，前端应提示用户核对详细地址。
    """
    try:
        loc = amap_ip_location()
    except ValueError as e:
        return json_fail(str(e))
    lat = loc.pop('latitude')
    lng = loc.pop('longitude')

    try:
        info = amap_regeo(lng, lat)
    except ValueError as e:
        # IP 定位成功但逆地理失败：仍返回坐标，地址留空由用户手填
        return json_ok({
            'address': '', 'province': loc.get('province', ''),
            'city': loc.get('city', ''), 'district': '',
            'latitude': lat, 'longitude': lng, 'precision': 'city',
        }, message='已获取大致位置坐标，但地址解析失败：%s' % e)

    info['latitude'] = lat
    info['longitude'] = lng
    info['precision'] = 'city'
    return json_ok(info, message='IP定位成功（城市/区级精度，请核对并完善详细地址）')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def capture_create(request):
    """创建捕捉登记（批量生成宠物档案）。

    表单字段与「新增捕捉登记」页一一对应：物业名称、所在小区、详细地址、
    定位地址/经纬度、归属区县、物业交接人、联系电话、捕捉数量、宠物编号、
    整体合影、单只照片、物业电子签名。定位得到的地址会作为默认地址写入
    address，并同时保留 geo_address 供详情页区分展示。
    """
    data = parse_json_body(request)
    if not data and request.POST:
        # 兼容 multipart/form-data 提交（合照/单只照片上传）
        data = request.POST.dict()

    shelter_id = data.get('shelter_id') or getattr(request.user, 'institution_id', None)
    if not shelter_id:
        return json_fail('缺少捕捉点信息')

    try:
        shelter = Institution.objects.get(id=shelter_id, type='shelter')
    except Institution.DoesNotExist:
        return json_fail('捕捉点不存在')

    district, err = _resolve_district_scope(request.user, shelter, data.get('district_id'))
    if err:
        return json_fail(err)

    try:
        pet_count = int(data.get('pet_count', 0))
    except (TypeError, ValueError):
        pet_count = 0
    if pet_count <= 0:
        return json_fail('动物数量必须大于0')
    if pet_count > 100:
        return json_fail('单批捕捉数量不能超过100只，请分批登记')

    # 与前端表单必填项保持一致，避免脏数据落库
    property_name = (data.get('property_name') or '').strip()
    if not property_name:
        return json_fail('物业名称不能为空')
    community_name = (data.get('community_name') or '').strip()
    if not community_name:
        return json_fail('所在小区不能为空')
    contact_person = (data.get('contact_person') or '').strip()
    if not contact_person:
        return json_fail('物业交接人不能为空')
    contact_phone = (data.get('contact_phone') or '').strip()
    if not contact_phone:
        return json_fail('联系电话不能为空')

    # 定位地址作为默认地址：前端未手填 address 时用 geo_address 兜底
    geo_address = (data.get('geo_address') or '').strip()
    address = (data.get('address') or '').strip() or geo_address

    # 批量生成宠物编号
    pet_codes = generate_pet_codes(pet_count)

    capture = Capture.objects.create(
        district=district,
        shelter=shelter,
        shelter_name=shelter.name,
        community_id=data.get('community_id') or None,
        community_name=community_name,
        address=address,
        latitude=_to_float(data.get('latitude')),
        longitude=_to_float(data.get('longitude')),
        geo_address=geo_address,
        property_name=property_name,
        contact_person=contact_person,
        contact_phone=contact_phone,
        pet_count=pet_count,
        pet_codes=','.join(pet_codes),
        signature=data.get('signature', ''),
        status='pending',  # 新建捕捉单尚未转运，状态由转运情况自动推导
        operator=request.user,
        operator_name=request.user.get_full_name() or request.user.username,
        ledger_no=generate_ledger_no('CAP'),
    )

    # 处理合照上传
    if request.FILES.get('group_photo'):
        capture.group_photo = request.FILES['group_photo']
        capture.save(update_fields=['group_photo'])

    # 批量创建宠物档案；单只照片按编号一一对应写入 pet.photo_capture
    for code in pet_codes:
        pet = Pet.objects.create(
            code=code,
            species=data.get('species', '猫'),
            status='in_transit',
            district=district,
            capture=capture,
            shelter=shelter,
        )
        photo = request.FILES.get('pet_photo_' + code)
        if photo:
            pet.photo_capture = photo
            pet.save(update_fields=['photo_capture'])

    return json_ok({
        'capture': serialize_instance(capture),
        'pet_codes': pet_codes,
    }, message=f'捕捉登记成功，生成 {pet_count} 条宠物档案')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def capture_update(request, pk):
    """编辑捕捉登记。

    规则：
    - 只有「未作废」且「本单没有任何宠物处于已转运状态」时才允许修改；
      已提交转运单（待签收/已签收）的记录必须等医院全部退回后才能改。
    - 可修改：物业名称、所在小区（文本）、详细地址、定位地址、经纬度、
      归属区县、物业交接人、联系电话、整体合影、电子签名。
    - 不可修改：捕捉点、宠物数量、宠物编号、状态（由系统推导）。
    """
    if request.method != 'POST':
        return json_fail('仅支持 POST 请求', status=405)

    capture = _get_capture_for_user(pk, request.user)
    if capture is None:
        return json_fail('捕捉记录不存在', status=404)

    if capture.is_deleted:
        return json_fail('该捕捉记录已删除，不可修改')

    state = capture_transfer_state(capture)
    if not state['can_edit']:
        return json_fail(
            f"该捕捉单已有 {state['transferred']} 只动物提交转运，不可修改；"
            '需医院全部退回后才能编辑'
        )

    data = parse_json_body(request)
    if not data and request.POST:
        data = request.POST.dict()

    # 必填项校验（仅当请求显式提供该字段时才校验，支持局部更新）
    required = {
        'property_name': '物业名称',
        'community_name': '所在小区',
        'contact_person': '物业交接人',
        'contact_phone': '联系电话',
    }
    for field, label in required.items():
        if field in data and not (data.get(field) or '').strip():
            return json_fail(f'{label}不能为空')

    updatable = ('property_name', 'community_name', 'address', 'geo_address',
                 'contact_person', 'contact_phone')
    changed = []
    for field in updatable:
        if field in data:
            value = data.get(field) or ''
            setattr(capture, field, value.strip() if isinstance(value, str) else value)
            changed.append(field)

    for field in ('latitude', 'longitude'):
        if field in data:
            setattr(capture, field, _to_float(data.get(field)))
            changed.append(field)

    if 'district_id' in data and data.get('district_id'):
        # 编辑同样要走区县解析/校验：否则可以把本区捕捉单改判到他区，
        # 或改判到「全市（市级）」从而让本区县政府看不到它。
        new_district, err = _resolve_district_scope(
            request.user, capture.shelter, data['district_id'])
        if err:
            return json_fail(err)
        if capture.district_id != new_district.id:
            capture.district = new_district
            changed.append('district_id')

    # 定位地址作为默认地址：编辑时若清空了详细地址但有定位地址，用定位地址兜底
    if 'address' in data and not (capture.address or '').strip() and capture.geo_address:
        capture.address = capture.geo_address
        if 'address' not in changed:
            changed.append('address')

    if 'signature' in data and data.get('signature'):
        capture.signature = data['signature']
        changed.append('signature')

    if request.FILES.get('group_photo'):
        capture.group_photo = request.FILES['group_photo']
        changed.append('group_photo')

    if changed:
        capture.save(update_fields=list(dict.fromkeys(changed + ['updated_at'])))

    # 区县变更时同步其下宠物档案，保持数据一致
    if 'district_id' in changed:
        Pet.objects.filter(capture=capture).update(district_id=capture.district_id)

    # 单只照片更新：按 pet_photo_<pet_id> 匹配
    for key, f in request.FILES.items():
        if key.startswith('pet_photo_'):
            pet_id = key[len('pet_photo_'):]
            pet = Pet.objects.filter(id=pet_id, capture=capture).first()
            if pet:
                pet.photo_capture = f
                pet.save(update_fields=['photo_capture'])

    result = serialize_instance(capture)
    result['transferState'] = state
    return json_ok(result, message='捕捉记录已更新')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def capture_delete(request, pk):
    """逻辑删除捕捉登记（数据库记录保留，仅打删除标记）。

    规则：
    - 本单只要存在任何「已提交且未被退回」的转运单，就不允许删除；
    - 必须等医院全部退回（所有转运单均为已驳回）后才可删除；
    - 删除为逻辑删除，同时把其下宠物档案一并标记作废。
    """
    if request.method != 'POST':
        return json_fail('仅支持 POST 请求', status=405)

    capture = _get_capture_for_user(pk, request.user)
    if capture is None:
        return json_fail('捕捉记录不存在', status=404)

    if capture.is_deleted:
        return json_fail('该捕捉记录已删除，请勿重复操作')

    state = capture_transfer_state(capture)
    if not state['can_delete']:
        return json_fail(
            f"该捕捉单已有 {state['transferred']} 只动物提交转运，不可删除；"
            '需医院全部退回（全部为未转运状态）后才能删除'
        )

    now = timezone.now()
    capture.is_deleted = True
    capture.deleted_at = now
    capture.deleted_by = request.user
    capture.status = 'void'
    capture.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by', 'status', 'updated_at'])

    # 宠物档案同步逻辑作废，避免在转运/医院端继续流转
    Pet.objects.filter(capture=capture, is_deleted=False).update(
        is_deleted=True, deleted_at=now)

    return json_ok({
        'id': capture.id,
        'is_deleted': True,
        'pet_count': state['total'],
    }, message=f"捕捉记录已删除（逻辑删除，含 {state['total']} 条宠物档案）")


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def owner_return_list(request):
    """主人领回记录列表"""
    qs = get_district_filtered_queryset(OwnerReturn, request.user)

    keyword = request.GET.get('keyword', '').strip()
    if keyword:
        qs = qs.filter(
            Q(pet_code__icontains=keyword)
            | Q(owner_name__icontains=keyword)
            | Q(owner_phone__icontains=keyword)
        )

    data = [serialize_instance(r) for r in qs]
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def owner_return_create(request, pk=None):
    """主人领回登记

    URL 中的 pk 可以是 capture_id（兼容旧路由），实际 pet_id 从请求体获取。
    """
    data = parse_json_body(request)

    pet_id = data.get('pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    # 按区县范围取宠物，并排除已逻辑删除的档案
    pet = get_active_pet(pet_id, request.user)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

    # 状态限制：只有刚捕捉（未提交转运单）的宠物可领回
    # 即：宠物未关联医院（pet.hospital 为空），表示尚未提交转运单
    if pet.hospital_id is not None:
        return json_fail('该宠物已提交转运单，不可领回。领回仅限捕捉后、转运前的宠物')

    # 防止重复领回登记（同一宠物只能领回一次）
    if pet.status == 'owner_returned':
        return json_fail('该宠物已办理过主人领回，不可重复登记')
    if pet.status != 'in_transit':
        return json_fail(f'宠物当前状态({pet.get_status_display()})不可领回')

    owner_name = data.get('owner_name', '').strip()
    if not owner_name:
        return json_fail('主人姓名不能为空')

    owner_phone = data.get('owner_phone', '')
    owner_id_card = data.get('owner_id_card', '')

    # 黑名单检查
    bl = check_blacklist(owner_id_card, owner_phone)
    if bl:
        return json_fail(f'该主人已在黑名单中：{bl.reason}')

    # 主人领回记录的区县以宠物档案的区县为准（动物就是从那个区县捕捉来的），
    # 而不是操作员所属区县——操作员可能挂在「全市（市级）」下。
    district, err = _resolve_district_scope(request.user, pet, data.get('district_id'))
    if err:
        return json_fail(err)

    record = OwnerReturn.objects.create(
        pet=pet,
        pet_code=pet.code,
        owner_name=owner_name,
        owner_phone=owner_phone,
        owner_id_card=owner_id_card,
        owner_address=data.get('owner_address', '').strip(),
        return_time=_parse_return_time(data.get('return_time')),
        reason=data.get('reason', ''),
        signature=data.get('signature', ''),
        operator=request.user,
        operator_name=request.user.get_full_name() or request.user.username,
        ledger_no=generate_ledger_no('RET'),
        district=district,
    )

    # 更新宠物状态
    pet.status = 'owner_returned'
    pet.save(update_fields=['status'])

    # 领回后本单宠物减少，捕捉单状态需要重算
    if pet.capture_id:
        recalc_capture_status(pet.capture)

    return json_ok(serialize_instance(record), message='主人领回登记成功')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district', 'hospital')
@login_required
def capture_detail(request, pk):
    """捕捉登记详情。

    返回「新增捕捉登记」表单的全部内容（含地址、定位地址、经纬度、整体合影、
    单只照片、电子签名、宠物编号等）以及关联宠物列表和转运状态，
    前端据此渲染详情抽屉并判断能否编辑 / 删除。
    """
    try:
        capture = Capture.objects.get(id=pk)
    except Capture.DoesNotExist:
        return json_fail('捕捉记录不存在', status=404)

    data = serialize_instance(capture)
    data['statusDisplay'] = capture.get_status_display()

    pets = Pet.objects.filter(capture=capture, is_deleted=False)
    data['pets'] = [serialize_instance(p) for p in pets]

    state = capture_transfer_state(capture)
    data['transferState'] = state
    data['transfer_state'] = state
    data['canEdit'] = state['can_edit']
    data['canDelete'] = state['can_delete']
    return json_ok(data)
