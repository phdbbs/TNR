"""
Task 4: 捕捉登记与主人领回
- 捕捉登记列表/详情/创建
- 主人领回登记
"""
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Capture, Pet, OwnerReturn
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    generate_pet_codes, generate_ledger_no, get_district_scope,
    get_district_filtered_queryset, check_blacklist, amap_regeo,
)
from core.models import Institution


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def capture_list(request):
    """捕捉登记列表（按区县过滤）"""
    qs = get_district_filtered_queryset(Capture, request.user)
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)
    keyword = request.GET.get('keyword', '').strip()
    if keyword:
        qs = qs.filter(ledger_no__icontains=keyword) | qs.filter(shelter_name__icontains=keyword) | qs.filter(community_name__icontains=keyword)

    data = []
    for cap in qs:
        item = serialize_instance(cap)
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
def capture_create(request):
    """创建捕捉登记（批量生成宠物档案）"""
    data = parse_json_body(request)
    if not data and request.POST:
        # 兼容 multipart/form-data 提交（合照上传）
        data = request.POST.dict()

    district_id = data.get('district_id') or getattr(request.user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    shelter_id = data.get('shelter_id') or getattr(request.user, 'institution_id', None)
    if not shelter_id:
        return json_fail('缺少捕捉点信息')

    try:
        shelter = Institution.objects.get(id=shelter_id, type='shelter')
    except Institution.DoesNotExist:
        return json_fail('捕捉点不存在')

    pet_count = int(data.get('pet_count', 0))
    if pet_count <= 0:
        return json_fail('动物数量必须大于0')
    if pet_count > 100:
        return json_fail('单批捕捉数量不能超过100只，请分批登记')

    # 批量生成宠物编号
    pet_codes = generate_pet_codes(pet_count)

    # 创建捕捉记录
    # 定位信息（前端通过浏览器定位 + 高德逆地理编码获得）
    def _to_float(v):
        try:
            return float(v) if v not in (None, '') else None
        except (TypeError, ValueError):
            return None

    capture = Capture.objects.create(
        district_id=district_id,
        shelter=shelter,
        shelter_name=shelter.name,
        community_id=data.get('community_id') or None,
        community_name=data.get('community_name', ''),
        address=data.get('address', ''),
        latitude=_to_float(data.get('latitude')),
        longitude=_to_float(data.get('longitude')),
        geo_address=data.get('geo_address', ''),
        property_name=data.get('property_name', ''),
        contact_person=data.get('contact_person', ''),
        contact_phone=data.get('contact_phone', ''),
        pet_count=pet_count,
        pet_codes=','.join(pet_codes),
        signature=data.get('signature', ''),
        status='completed',
        operator=request.user,
        operator_name=request.user.get_full_name() or request.user.username,
        ledger_no=generate_ledger_no('CAP'),
    )

    # 处理合照上传
    if request.FILES.get('group_photo'):
        capture.group_photo = request.FILES['group_photo']
        capture.save(update_fields=['group_photo'])

    # 批量创建宠物档案
    pets = []
    for code in pet_codes:
        pet = Pet.objects.create(
            code=code,
            species=data.get('species', '猫'),
            status='in_transit',
            district_id=district_id,
            capture=capture,
            shelter=shelter,
        )
        pets.append(code)

    return json_ok({
        'capture': serialize_instance(capture),
        'pet_codes': pet_codes,
    }, message=f'捕捉登记成功，生成 {pet_count} 条宠物档案')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def owner_return_list(request):
    """主人领回记录列表"""
    qs = get_district_filtered_queryset(OwnerReturn, request.user)

    keyword = request.GET.get('keyword', '').strip()
    if keyword:
        qs = qs.filter(pet_code__icontains=keyword) | qs.filter(owner_name__icontains=keyword) | qs.filter(owner_phone__icontains=keyword)

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

    try:
        pet = Pet.objects.get(id=pet_id)
    except Pet.DoesNotExist:
        return json_fail('宠物不存在')

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

    district_id = data.get('district_id') or getattr(request.user, 'district_id', None)
    if not district_id:
        district_id = pet.district_id

    record = OwnerReturn.objects.create(
        pet=pet,
        pet_code=pet.code,
        owner_name=owner_name,
        owner_phone=owner_phone,
        owner_id_card=owner_id_card,
        reason=data.get('reason', ''),
        signature=data.get('signature', ''),
        operator=request.user,
        operator_name=request.user.get_full_name() or request.user.username,
        ledger_no=generate_ledger_no('RET'),
        district_id=district_id,
    )

    # 更新宠物状态
    pet.status = 'owner_returned'
    pet.save(update_fields=['status'])

    return json_ok(serialize_instance(record), message='主人领回登记成功')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district', 'hospital')
@login_required
def capture_detail(request, pk):
    """捕捉登记详情（含关联宠物列表）"""
    try:
        capture = Capture.objects.get(id=pk)
    except Capture.DoesNotExist:
        return json_fail('捕捉记录不存在', status=404)

    data = serialize_instance(capture)
    pets = Pet.objects.filter(capture=capture)
    data['pets'] = [serialize_instance(p) for p in pets]
    return json_ok(data)
