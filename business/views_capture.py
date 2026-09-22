"""
Task 4: 捕捉登记与主人领回
- 捕捉登记列表/详情/创建/编辑/逻辑删除
- 主人领回登记
"""
import re
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Capture, Pet, OwnerReturn
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    generate_pet_codes, generate_ledger_no, get_district_scope,
    get_district_filtered_queryset, check_blacklist, amap_regeo,
    amap_ip_location, client_ip, capture_transfer_state, capture_states_bulk,
    recalc_capture_status, get_active_pet, validate_uploaded_images,
    resolve_district_scope, resolve_community,
    inactive_district_error, inactive_institution_error,
    parse_int_param, parse_date_param, MAX_CAPTURE_BATCH,
)
from core.models import District, Institution


# 列表接口剔除体积巨大的签字 base64，详情接口才返回
CAPTURE_LIST_EXCLUDE = ('signature',)
PET_LIST_EXCLUDE = ('photo_capture', 'photo_group', 'photo_before',
                    'photo_after', 'photo_treatment')

# 逐只登记时的物种/性别合法取值（与 Pet.SPECIES_CHOICES / GENDER_CHOICES 对齐）
PET_SPECIES = ('猫', '狗')
PET_GENDERS = ('公', '母')

# 逐只属性字段的四个前缀。探测时必须四个都认——只认 pet_species_
# 会让「只改性别/品种/昵称」的提交整批静默失效（改了没反应，也不报错）。
PET_ATTR_PREFIXES = ('pet_species_', 'pet_gender_', 'pet_breed_', 'pet_name_')

# 宠物档案编号格式：TNR + YY(2位) + MMDD(4位) + SSS(3位序号)
PET_CODE_RE = re.compile(r'^TNR\d{9}$')


def _to_pet_id(v):
    """把表单键里的宠物主键解析为 int；非数字键返回 None（不做 500）。

    伪造或串味的键（``pet_species_abc``）直接扔给 ``filter(id=...)``
    会抛 ValueError 变成 500，这里先挡掉。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_pet_attrs(data, key, partial=False):
    """解析单只动物的物种 / 性别 / 品种 / 昵称（逐只登记）。

    表单字段命名：``pet_species_<key>`` / ``pet_gender_<key>`` /
    ``pet_breed_<key>`` / ``pet_name_<key>``，与单只照片 ``pet_photo_<key>``
    同一套规则（新增时 key 是宠物编号，编辑时 key 是宠物 id）。

    :param partial: True 时只解析**显式提供**的字段，缺失的字段不出现在
        结果里（表示「不改这一项」）。编辑场景必须用 partial——否则客户端
        只改性别时，物种会被缺省值「猫」悄悄覆盖掉。
    :return: (属性 dict, 错误信息)；成功时错误信息为 None

    取值规则：新增时缺省/空串 → 物种先回退整批字段 ``species``、再回退
    「猫」（兼容旧客户端）；性别回退空串（模型 ``blank=True``，允许未知）。
    **显式传入的非法值直接报错，不静默降级** —— 静默兜底会把用户填的
    「犬」悄悄存成「猫」，事后没有任何痕迹可查。
    """
    if partial:
        attrs = {}
        if ('pet_species_' + key) in data:
            species = (data.get('pet_species_' + key) or '').strip()
            if species not in PET_SPECIES:
                return None, f'物种「{species}」无效，只能是猫或狗'
            attrs['species'] = species
        if ('pet_gender_' + key) in data:
            gender = (data.get('pet_gender_' + key) or '').strip()
            if gender and gender not in PET_GENDERS:
                return None, f'性别「{gender}」无效，只能是公或母'
            attrs['gender'] = gender
        for field in ('breed', 'name'):
            field_key = f'pet_{field}_{key}'
            if field_key in data:
                attrs[field] = (data.get(field_key) or '').strip()[:50]
        return attrs, None

    species = ((data.get('pet_species_' + key) or '').strip()
               or (data.get('species') or '').strip() or '猫')
    if species not in PET_SPECIES:
        return None, f'物种「{species}」无效，只能是猫或狗'
    gender = (data.get('pet_gender_' + key) or '').strip()
    if gender and gender not in PET_GENDERS:
        return None, f'性别「{gender}」无效，只能是公或母'
    return {
        'species': species,
        'gender': gender,
        'breed': (data.get('pet_breed_' + key) or '').strip()[:50],
        'name': (data.get('pet_name_' + key) or '').strip()[:50],
    }, None


def _submitted_pet_codes(request, data, pet_count):
    """读取前端「预览后提交」的宠物编号。

    新增捕捉页先调 ``codes-preview`` 拿到本批编号并逐只展示，用户再按编号
    填写物种/性别/品种/昵称与单只照片（字段名 ``pet_species_<编号>`` 等）。
    如果服务端在提交时**自己重新生成**一套编号，两套编号一旦不一致，
    逐只属性与照片会**全部静默落空**——物种回退成「猫」、照片直接丢弃，
    界面上没有任何报错，事后也无法追溯。所以提交时优先沿用前端已展示的编号。

    编号被占用/数量对不上/格式非法时**直接报错**，不悄悄换一套新编号：
    换了新编号等于把用户刚填的属性全丢掉，且用户完全看不出来。

    :return: (编号列表, 错误信息)；前端未提交编号时返回 (None, None)，
        由服务端按 ``generate_pet_codes`` 生成（兼容老客户端与脚本调用）
    """
    codes = []
    # multipart/form-data 下同名键是重复字段，必须用 getlist；
    # parse_json_body 路径下 data 是 dict，值可能是 list 或逗号串
    if hasattr(request.POST, 'getlist'):
        codes = [c.strip() for c in request.POST.getlist('pet_codes') if c and c.strip()]
    if not codes:
        raw = data.get('pet_codes')
        if isinstance(raw, (list, tuple)):
            codes = [str(c).strip() for c in raw if str(c).strip()]
        elif raw:
            codes = [c.strip() for c in str(raw).split(',') if c.strip()]
    if not codes:
        return None, None

    if len(codes) != pet_count:
        return None, (f'提交的宠物编号有 {len(codes)} 个，与捕捉数量 {pet_count} '
                      '不一致，请重新生成编号后再提交')
    if len(set(codes)) != len(codes):
        return None, '提交的宠物编号存在重复，请重新生成编号后再提交'
    for code in codes:
        if not PET_CODE_RE.match(code):
            return None, f'宠物编号「{code}」格式不正确，请重新生成编号后再提交'

    # 编号是 unique 的，被占用时必须拦下——否则落库时 IntegrityError 变 500。
    # 逻辑删除的档案仍占用编号，所以这里刻意不过滤 is_deleted。
    taken = sorted(Pet.objects.filter(code__in=codes).values_list('code', flat=True))
    if taken:
        return None, (f'宠物编号 {"、".join(taken)} 已被占用，'
                      '请重新生成编号后再提交')
    return codes, None


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
    """按区县范围取捕捉单，越权访问返回 None。

    医院端额外放行「有动物转到过本院」的捕捉单：`transfer_create` 并**不**限制
    目标医院的区县（跨区县送医是允许的），若只按区县过滤，跨区县转运后医院
    点开该捕捉单详情会 404 —— 拦截会误伤这条正常路径。

    注意用**单个 `Q()`** 表达「本区县 OR 本院」，不要写成
    `qs.filter(...) | qs.filter(...)`：那样会丢前置过滤并产生重复行。
    """
    qs = get_district_filtered_queryset(Capture, user)
    if user.role == 'hospital' and user.institution_id:
        own = Capture.objects.filter(
            pet__hospital_id=user.institution_id).values('pk')
        qs = Capture.objects.filter(
            Q(pk__in=qs.values('pk')) | Q(pk__in=own)).distinct()
    return qs.filter(id=pk).first()


def _resolve_district_scope(user, anchor, submitted):
    """解析并校验业务记录的归属区县。

    实现已提到 `services.resolve_district_scope`（转运单、黑名单等落库点也要用），
    这里保留同名薄封装，避免改动本模块内既有的三处调用点。
    """
    return resolve_district_scope(user, anchor, submitted)


def _shelter_district_conflict(shelter, district):
    """捕捉点与归属区县是否自相矛盾；矛盾时返回错误信息，否则 None。

    捕捉点**本身挂在具体区县**时，它抓的动物就登记在那个区县；归属区县另填他区
    会让这张单从**执行机构所在区县**的可见范围里消失 —— 区县隔离按
    `Capture.district` 过滤，甲区捕捉点操作员看不到自己登记的单，
    既不能转运也不能作废，而乙区政府却看到一张「甲区捕捉点执行」的单。
    前端在捕捉点下拉的 change 事件里把归属区县自动同步成捕捉点所在区县
    （`#ca_shelterId` → `#ca_districtId`），但那个区县下拉**没有禁用**，
    后端原本也不校验 —— 于是这条同步只是「界面上不容易踩到」，不是约束。

    捕捉点挂在「全市（市级）」时不做约束：现场两个捕捉点就挂在市级，
    此时归属区县**必须**由操作员指定（`resolve_district_scope` 本就禁止市级归属）。

    创建与编辑共用这一个判据 —— 各写一份必然漂移。
    """
    if shelter is None or district is None:
        return None
    shelter_district = getattr(shelter, 'district', None)
    if shelter_district is None or shelter_district.is_city:
        return None
    if shelter_district.id != district.id:
        return (f'捕捉点「{shelter.name}」属于{shelter_district.name}，'
                f'归属区县必须与之一致（当前提交的是{district.name}）')
    return None


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

    # `district` 既可能是**区县 id**（前端下拉的 value），也可能是**区县名称**
    # （手输 / URL 分享）。原先写成
    #     Q(district__name__icontains=d) | Q(district_id=d)
    # —— `Q` 的**两半都会被求值**，于是传中文名时 `district_id='襄城区'`
    # 直接 `ValueError` 打成 500。也就是说「按区县名筛选」这条分支
    # **从来没成功过**，症状是「一填区县名就 500」，而按 id 筛选是好的。
    # 正确做法：先判是不是纯数字，是才拼 id 那半。
    # 这里**故意忽略**解析错误：非数字不是「非法输入」而是「按名称查」，
    # 名称那半仍然生效，筛选没有被静默丢掉。
    district = request.GET.get('district', '').strip()
    if district:
        cond = Q(district__name__icontains=district)
        district_id, _err = parse_int_param(district, '区县')
        if district_id is not None:
            cond |= Q(district_id=district_id)
        qs = qs.filter(cond)

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
    # ⚠ 必须有**上限**。`generate_pet_codes()` 是纯 `for i in range(count)` 循环，
    # 原先只判 `count <= 0`，于是 `?count=999999999` 会真的去生成 10 亿个编号：
    # 实测 20 万条约 23ms，线性外推 ≈ 110 秒 CPU + 数十 GB 内存，进程被拖死。
    # 这类「**合法但荒谬**」的参数值探针扫不出来 —— 接口最终仍返回 200，
    # 只是在返回之前已经把服务端吃光了。上限与 `checkin_create` 同源，
    # 避免「预览能生成 10 万条、提交却被拒」这种孪生漂移。
    count, err = parse_int_param(
        request.GET.get('count'), '数量', minimum=0, maximum=MAX_CAPTURE_BATCH)
    if err:
        return json_fail(err)
    if not count:
        # 参数缺省与 0 都归到同一句提示（保持原口径）
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

    ⚠ **浏览器 Geolocation 只在安全上下文（HTTPS / localhost）下才可用**，
    这是浏览器强制的，网页代码无法绕过：在 `http://公网IP` 上
    `navigator.geolocation.getCurrentPosition` 会**直接失败，连权限框都不弹**。
    所以只要站点还是 HTTP，「询问用户是否授予 GPS 权限」就做不到 ——
    要真正拿到精确定位，必须先把站点放到 HTTPS 上。

    本接口是**兜底**：由服务端调高德 IP 定位，得到城市级粗略位置
    （rectangle 中心点），再逆地理出地址名称一并返回。

    ⚠ **必须按客户端 IP 定位**（`client_ip(request)`）。此前不传 IP，
    高德就按「发起请求的一方」——也就是**服务器机房**——来定位，
    线上稳定返回「北京市东城区…」，与客户端所在城市毫无关系。

    返回里带 `source`：
      - `client_ip`  —— 按客户端网络位置定位（城市级）
      - `server_ip`  —— 拿不到可用的客户端公网 IP，退回服务器出口。
                        **这只说明服务器在哪，前端不得当作客户位置自动填表。**
    `precision = 'city'` 标识粗定位。
    """
    try:
        loc = amap_ip_location(client_ip=client_ip(request))
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
            'source': loc.get('source', ''),
        }, message='已获取大致位置坐标，但地址解析失败：%s' % e)

    info['latitude'] = lat
    info['longitude'] = lng
    info['precision'] = 'city'
    info['source'] = loc.get('source', '')
    if info['source'] == 'server_ip':
        return json_ok(info, message='未能获取您的网络位置，当前为服务器所在城市，请手动填写详细地址')
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

    # 与编辑共用同一条一致性判据（放在范围校验之后：跨区县提交应先报「无权归属」）
    conflict = _shelter_district_conflict(shelter, district)
    if conflict:
        return json_fail(conflict)

    # 「停用」必须真的拦住点什么（第十八轮）：`Institution.status` 此前在后端
    # 没有任何读取点，停用捕捉点后照旧能对它登记新捕捉单。同理，显式提交的
    # 归属区县也必须是启用状态（前端那个下拉只过滤了 `!isCity`，漏了 `status`）。
    #
    # 只拦**创建**，不拦编辑：存量记录（含停在停用捕捉点 / 停用区县下的历史
    # 捕捉单）仍要能改名、能转运、能作废，否则一次停用就把历史业务全锁死。
    inst_err = inactive_institution_error(shelter, '捕捉点')
    if inst_err:
        return json_fail(inst_err)
    if data.get('district_id'):
        # 只在**显式提交**时校验：未提交时 district 由捕捉点/操作员推导，
        # 那是存量事实，不该被状态拦住。
        district_err = inactive_district_error(district)
        if district_err:
            return json_fail(district_err)

    # 与 `pet_codes_preview` 走同一个解析器、同一个上限常量（`MAX_CAPTURE_BATCH`），
    # 避免孪生接口漂移：一边只判 `count<=0`、另一边才卡 100，
    # 就会出现「预览能生成 10 万条、提交只收 100 条」。
    pet_count, err = parse_int_param(data.get('pet_count'), '动物数量', minimum=0)
    if err:
        return json_fail(err)
    if not pet_count:
        return json_fail('动物数量必须大于0')
    if pet_count > MAX_CAPTURE_BATCH:
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

    # 批量生成宠物编号：优先沿用前端预览后提交的编号，保证逐只属性/照片
    # 的字段名（pet_species_<编号> 等）与最终落库的编号一致
    pet_codes, code_err = _submitted_pet_codes(request, data, pet_count)
    if code_err:
        return json_fail(code_err)
    if pet_codes is None:
        pet_codes = generate_pet_codes(pet_count)

    # 逐只动物的物种/性别/品种/昵称：**先全部校验通过，再进事务落库**。
    # 校验必须放在写库之前——否则第 N 只属性非法时会留下一张只建了一半的
    # 捕捉单（孤儿单据 + 编号被占用，且宠物数量对不上）。
    pet_attrs = []
    for code in pet_codes:
        parsed, err = _parse_pet_attrs(data, code)
        if err:
            return json_fail(f'动物 {code}：{err}')
        pet_attrs.append(parsed)

    # 上传图片的内容校验：ImageField 直接赋值不校验，非图片文件会被原样存进
    # media/（前端裂图、库里无痕迹）。放在事务之前，非法时整批拒绝。
    photo_labels = {'group_photo': '整体合影'}
    for code in pet_codes:
        photo_labels['pet_photo_' + code] = f'动物 {code} 的捕捉照片'
    photo_err = validate_uploaded_images(request.FILES, photo_labels)
    if photo_err:
        return json_fail(photo_err)

    with transaction.atomic():
        # 小区外键回填：前端「所在小区」是自由文本（不做下拉枚举），这里按名称
        # 匹配已有的小区机构。不回填的话 `Capture.community` 永远是 None，
        # 后续「放养」只认这个外键 → 放养流程在界面上完全走不通。
        community = resolve_community(
            district, data.get('community_id'), community_name)

        capture = Capture.objects.create(
            district=district,
            shelter=shelter,
            shelter_name=shelter.name,
            community=community,
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
        for code, attr in zip(pet_codes, pet_attrs):
            pet = Pet.objects.create(
                code=code,
                name=attr['name'],
                species=attr['species'],
                breed=attr['breed'],
                gender=attr['gender'],
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
            # 只在**确实换区县**时校验一致性：历史遗留的错配单（捕捉点与区县
            # 已不一致）仍要能原地改其它字段，也要能改回与捕捉点一致的那个区县。
            conflict = _shelter_district_conflict(capture.shelter, new_district)
            if conflict:
                return json_fail(conflict)
            # 换区县时目标区县必须是启用状态（与 `capture_create` 同一条判据）。
            # 放在「确实换区县」分支**内**：原地保存（含停在停用区县里的历史单）
            # 不受影响，否则这些单连名字都改不了，只能靠改库。
            district_err = inactive_district_error(new_district)
            if district_err:
                return json_fail(district_err)
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

    # 小区改名后要重新解析外键：否则改完名字外键仍指向旧小区，
    # 放养时会把动物放回**改名前的那个小区**（或外键仍为空而彻底无法放养）。
    if 'community_name' in data or 'district_id' in data:
        community = resolve_community(
            capture.district, data.get('community_id'), capture.community_name)
        if capture.community_id != (community.id if community else None):
            capture.community = community
            changed.append('community')

    # 图片内容校验：与创建一致，非图片文件必须在写库之前拦下
    photo_labels = {'group_photo': '整体合影'}
    for key in request.FILES:
        if key.startswith('pet_photo_'):
            photo_labels[key] = '单只捕捉照片'
    photo_err = validate_uploaded_images(request.FILES, photo_labels)
    if photo_err:
        return json_fail(photo_err)

    if request.FILES.get('group_photo'):
        capture.group_photo = request.FILES['group_photo']
        changed.append('group_photo')

    if changed:
        capture.save(update_fields=list(dict.fromkeys(changed + ['updated_at'])))

    # 区县变更时同步其下宠物档案，保持数据一致
    if 'district_id' in changed:
        Pet.objects.filter(capture=capture).update(district_id=capture.district_id)

    # 逐只动物的物种/性别/品种/昵称更新：按 pet_<字段>_<pet_id> 匹配。
    # partial=True：只改显式提交的字段，避免「只改性别」把物种重置成默认的猫。
    # 四个前缀都要探测——只认 pet_species_ 会让「只改性别/品种/昵称」的
    # 提交整批静默失效（用户改了没反应，也不报错）。
    # 与创建时同一套两段式约定：先把所有取值校验完，再逐只落库。
    pet_updates = {}
    for key in data:
        prefix = next((p for p in PET_ATTR_PREFIXES if key.startswith(p)), None)
        if prefix is None:
            continue
        raw_id = key[len(prefix):]
        pet_id = _to_pet_id(raw_id)
        if pet_id is None:
            continue          # 非法主键，忽略（不做 500）
        parsed, err = _parse_pet_attrs(data, raw_id, partial=True)
        if err:
            return json_fail(err)
        pet_updates[pet_id] = parsed

    # 单只照片更新：按 pet_photo_<pet_id> 匹配。
    # 放在属性校验之后：属性非法时整批拒绝，不留下「已经换过照片」的半成品。
    for key, f in request.FILES.items():
        if key.startswith('pet_photo_'):
            pet_id = _to_pet_id(key[len('pet_photo_'):])
            if pet_id is None:
                continue
            pet = Pet.objects.filter(id=pet_id, capture=capture).first()
            if pet:
                pet.photo_capture = f
                pet.save(update_fields=['photo_capture'])

    for pet_id, attrs in pet_updates.items():
        pet = Pet.objects.filter(id=pet_id, capture=capture).first()
        if pet is None or not attrs:
            continue
        changed_pet = [f for f in attrs if getattr(pet, f) != attrs[f]]
        if changed_pet:
            for f in changed_pet:
                setattr(pet, f, attrs[f])
            pet.save(update_fields=changed_pet)

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

    # 审计用上下文：响应体只回了 id/is_deleted/pet_count，中间件读不出归属区县，
    # 必须显式指定 —— 否则这条删除日志会因「无归属区县」而对区县政府不可见。
    request.audit_district = capture.district
    request.audit_object_repr = capture.ledger_no or capture.community_name

    return json_ok({
        'id': capture.id,
        'is_deleted': True,
        'pet_count': state['total'],
    }, message=f"捕捉记录已删除（逻辑删除，含 {state['total']} 条宠物档案）")


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def owner_return_list(request):
    """主人领回记录列表（回收记录）。

    支持筛选：keyword（综合）、start_date/end_date（回收时间）、
    owner_address（住址）、owner_name（回收人）、owner_phone（电话）、
    pet_breed（品种）。
    """
    qs = get_district_filtered_queryset(OwnerReturn, request.user)
    qs = qs.select_related('pet')

    # 时间范围（按 return_time，无值则退到 created_at）。
    # 必须**解析成 date 对象**再进 ORM：把字符串直接塞进 `__date__gte`
    # 会让 Django 在解析阶段抛 `ValidationError`（500）。前端
    # `<input type="date">` 正常只会给 `YYYY-MM-DD`，但 URL 可以手改，
    # 接口不能把非法值当成 500 处理。
    start_date, err = parse_date_param(request.GET.get('start_date'), '开始日期')
    if err:
        return json_fail(err)
    end_date, err = parse_date_param(request.GET.get('end_date'), '结束日期')
    if err:
        return json_fail(err)
    if start_date:
        qs = qs.filter(
            Q(return_time__date__gte=start_date)
            | Q(return_time__isnull=True, created_at__date__gte=start_date)
        )
    if end_date:
        qs = qs.filter(
            Q(return_time__date__lte=end_date)
            | Q(return_time__isnull=True, created_at__date__lte=end_date)
        )

    # 各字段独立筛选
    owner_address = request.GET.get('owner_address', '').strip()
    if owner_address:
        qs = qs.filter(owner_address__icontains=owner_address)

    owner_name = request.GET.get('owner_name', '').strip()
    if owner_name:
        qs = qs.filter(owner_name__icontains=owner_name)

    owner_phone = request.GET.get('owner_phone', '').strip()
    if owner_phone:
        qs = qs.filter(owner_phone__icontains=owner_phone)

    pet_breed = request.GET.get('pet_breed', '').strip()
    if pet_breed:
        qs = qs.filter(pet__breed__icontains=pet_breed)

    # 综合关键词
    keyword = request.GET.get('keyword', '').strip()
    if keyword:
        qs = qs.filter(
            Q(pet_code__icontains=keyword)
            | Q(owner_name__icontains=keyword)
            | Q(owner_phone__icontains=keyword)
            | Q(owner_address__icontains=keyword)
            | Q(pet__breed__icontains=keyword)
        )

    qs = qs.order_by('-return_time', '-id')

    # 序列化并补充宠物信息（照片、品种等供前端详情抽屉使用）
    data = []
    for r in qs:
        item = serialize_instance(r, exclude=('signature',))
        pet = r.pet
        if pet:
            item['petBreed'] = pet.breed or ''
            item['petSpecies'] = pet.species or ''
            item['petGender'] = pet.gender or ''
            item['petPhotoCapture'] = pet.photo_capture.url if pet.photo_capture and pet.photo_capture.name else ''
            item['petPhotoGroup'] = pet.photo_group.url if pet.photo_group and pet.photo_group.name else ''
            item['captureId'] = pet.capture_id
        data.append(item)
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

    **必须走 `_get_capture_for_user` 做区县范围校验**：这个接口返回的是
    物业交接人、联系电话、电子签名、经纬度等个人信息，裸查主键会让任何
    登录用户（捕捉点 / 医院 / 区县监管）遍历出**其他区县**的完整捕捉档案。
    """
    capture = _get_capture_for_user(pk, request.user)
    if capture is None:
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
