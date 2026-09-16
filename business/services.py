"""
TNR 业务系统 - 共享服务层
提供编号生成、芯片管理、库存调整、黑名单检查、区县过滤等通用功能。
"""
import json
import os
import random
import re
import urllib.parse
import urllib.request
from datetime import date

from django.db.models import Q, Sum
from django.http import JsonResponse
from django.utils import timezone

from accounts.models import User
from business.models import (
    Pet, Material, MaterialTransaction, Chip, Blacklist, Transfer,
    Release, Adoption,
)


# ============================================
# JSON 响应工具
# ============================================
def json_ok(data=None, message='操作成功'):
    """成功 JSON 响应"""
    return JsonResponse({'success': True, 'data': data, 'message': message})


def json_fail(message='操作失败', data=None, status=400):
    """失败 JSON 响应"""
    return JsonResponse({'success': False, 'data': data, 'message': message}, status=status)


def parse_json_body(request):
    """解析 request.body 中的 JSON，失败时返回空字典。"""
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}


# ============================================
# 地图服务（高德逆地理编码）
# ============================================
def amap_regeo(lng, lat):
    """高德逆地理编码：经纬度（GCJ-02）→ 地址名称。

    Key 从环境变量 TNR_AMAP_KEY 读取（.env 中配置，见 .env.example）。
    此接口必须在服务端调用：浏览器直连 restapi.amap.com 存在跨域（CORS）限制，
    且 Key 不应暴露到前端。

    成功返回 dict（address/province/city/district），失败抛 ValueError（中文错误信息）。
    """
    key = os.environ.get('TNR_AMAP_KEY', '').strip()
    if not key:
        raise ValueError('未配置地图服务Key，请在 .env 中设置 TNR_AMAP_KEY（高德开放平台申请）')
    url = 'https://restapi.amap.com/v3/geocode/regeo?' + urllib.parse.urlencode({
        'key': key,
        'location': '%.6f,%.6f' % (lng, lat),  # 高德要求：经度在前，纬度在后
        'extensions': 'base',
    })
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'TNR-System/1.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:  # 网络超时 / DNS 失败等
        raise ValueError('地图服务请求失败：%s' % e)
    if str(data.get('status')) != '1':
        # status=0：常见于 Key 无效（INVALID_USER_KEY）、配额超限（DAILY_QUERY_OVER_LIMIT）等
        raise ValueError('地图服务返回错误：%s（infocode=%s）' % (data.get('info', '未知错误'), data.get('infocode', '')))
    regeocode = data.get('regeocode') or {}
    component = regeocode.get('addressComponent') or {}
    return {
        'address': (regeocode.get('formatted_address') or '').strip(),
        'province': component.get('province') or '',
        'city': component.get('city') if isinstance(component.get('city'), str) else '',
        'district': component.get('district') if isinstance(component.get('district'), str) else '',
    }
def amap_ip_location():
    """高德 IP 定位：按服务器出口公网 IP 粗略定位（城市/区级精度）。

    使用场景：浏览器 Geolocation 仅允许在 HTTPS 或 localhost 下使用，
    本系统常通过 http://局域网IP:8000 访问，前端 GPS 定位会被浏览器
    拒绝（"Only secure origins are allowed"）。手机与服务器通常处于
    同一网络（同一公网出口 IP），因此由服务端调用 IP 定位可得到
    与手机一致的城市级位置。

    成功返回 dict（province/city/adcode/latitude/longitude，
    经纬度取城市范围 rectangle 的中心点，GCJ-02 坐标），
    失败抛 ValueError（中文错误信息）。
    """
    key = os.environ.get('TNR_AMAP_KEY', '').strip()
    if not key:
        raise ValueError('未配置地图服务Key，请在 .env 中设置 TNR_AMAP_KEY（高德开放平台申请）')
    url = 'https://restapi.amap.com/v3/ip?' + urllib.parse.urlencode({'key': key})
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'TNR-System/1.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:  # 网络超时 / DNS 失败等
        raise ValueError('IP定位服务请求失败：%s' % e)
    if str(data.get('status')) != '1':
        raise ValueError('IP定位服务返回错误：%s（infocode=%s）' % (
            data.get('info', '未知错误'), data.get('infocode', '')))
    province = data.get('province') or ''
    # 直辖市/无法定位时 city 可能是空列表 []
    city = data.get('city') if isinstance(data.get('city'), str) else ''
    rectangle = data.get('rectangle') or ''
    lat = lng = None
    if rectangle:
        # rectangle 格式："minLng,minLat;maxLng,maxLat"（高德 v3 实际用分号，
        # 兼容部分文档示例中的波浪号 "~"），取对角线中心点
        try:
            corners = [p for p in re.split(r'[;~]', rectangle) if p.strip()]
            (min_lng, min_lat) = tuple(float(v) for v in corners[0].split(','))
            (max_lng, max_lat) = tuple(float(v) for v in corners[1].split(','))
            lng = round((min_lng + max_lng) / 2, 6)
            lat = round((min_lat + max_lat) / 2, 6)
        except (ValueError, IndexError):
            pass
    if lat is None or lng is None:
        raise ValueError('IP定位未获取到有效位置范围（服务器可能处于内网或运营商无法识别）')
    return {
        'province': province,
        'city': city,
        'adcode': data.get('adcode') or '',
        'latitude': lat,
        'longitude': lng,
    }


def serialize_instance(instance, fields=None, exclude=None):
    """将模型实例序列化为字典。

    - 外键返回 pk
    - 日期/时间返回 ISO 格式字符串
    - ImageField/FileField 返回 URL（有文件时）或空字符串
    - 同时返回蛇形命名（Python 惯例）和驼峰命名（前端 JS 惯例）字段
    - pet_codes 字符串自动转为数组
    - exclude: 需要剔除的字段名集合（如列表接口剔除体积巨大的 signature base64）
    """
    if instance is None:
        return None
    from django.db.models.fields.files import FieldFile

    skip = set(exclude or ())

    def _to_camel(snake):
        """snake_case → camelCase"""
        parts = str(snake).split('_')
        if len(parts) == 1:
            return parts[0]
        return parts[0] + ''.join(p.title() for p in parts[1:])

    data = {}
    for f in instance._meta.concrete_fields:
        if fields and f.name not in fields:
            continue
        if f.name in skip:
            continue
        value = getattr(instance, f.attname, None)
        if value is None:
            data[f.name] = None
        elif isinstance(value, FieldFile):
            # ImageField/FileField: 有文件返回 URL，无文件返回空串
            data[f.name] = value.url if value.name else ''
        elif hasattr(value, 'isoformat'):
            data[f.name] = value.isoformat()
        else:
            data[f.name] = value

        # 添加驼峰命名别名（前端 JS 使用）
        camel_key = _to_camel(f.name)
        if camel_key != f.name:
            if f.name == 'pet_codes' and isinstance(data[f.name], str) and data[f.name]:
                # pet_codes 字符串转数组
                data[camel_key] = [p.strip() for p in data[f.name].split(',') if p.strip()]
            else:
                data[camel_key] = data[f.name]

        # 外键额外输出 attname（如 district_id），供前端筛选/映射使用
        if f.is_relation and f.many_to_one:
            fk_key = f.attname
            fk_value = getattr(instance, fk_key, None)
            data[fk_key] = fk_value
            fk_camel = _to_camel(fk_key)
            if fk_camel != fk_key:
                data[fk_camel] = fk_value

    # 特殊处理：pet_codes 如果是字符串，也转数组放驼峰字段
    if 'pet_codes' in data and isinstance(data['pet_codes'], str) and data['pet_codes']:
        data['petCodes'] = [p.strip() for p in data['pet_codes'].split(',') if p.strip()]
    elif 'pet_codes' in data and (data['pet_codes'] is None or data['pet_codes'] == ''):
        data['petCodes'] = []

    return data


# ============================================
# 编号生成
# ============================================
def generate_pet_codes(count, year=None):
    """批量生成宠物档案编号。

    格式: TNR + YY(2位) + MMDD(4位) + SSS(3位序号)
    示例: TNR250101001 (2025年1月1日第1个)

    :param count: 生成数量
    :param year: 指定年份，默认取当前年份
    :return: 编号字符串列表
    """
    today = timezone.now()
    yy = str(year if year else today.year)[-2:]
    mmdd = today.strftime('%m%d')
    prefix = f'TNR{yy}{mmdd}'

    # 查找当天已有编号中的最大序号
    max_seq = 0
    existing_codes = Pet.objects.filter(code__startswith=prefix).values_list('code', flat=True)
    for code in existing_codes:
        try:
            seq_str = code[len(prefix):]
            seq = int(seq_str)
            if seq > max_seq:
                max_seq = seq
        except (ValueError, IndexError):
            continue

    codes = []
    for i in range(count):
        seq = max_seq + 1 + i
        codes.append(f'{prefix}{seq:03d}')
    return codes


def generate_ledger_no(prefix):
    """生成台账编号。

    格式: prefix + '-' + YYMMDD + '-' + SSS(3位随机)
    示例: CAP-250101-001

    :param prefix: 前缀，如 CAP/TRF/TRE 等
    :return: 台账编号字符串
    """
    today = timezone.now()
    date_str = today.strftime('%y%m%d')
    rand = f'{random.randint(0, 9999):04d}'
    return f'{prefix}-{date_str}-{rand}'


# ============================================
# 芯片管理
# ============================================
def use_chip(chip_no, pet):
    """标记芯片为已使用。

    :param chip_no: 芯片号
    :param pet: 关联的宠物对象
    :return: True 表示成功
    :raises ValueError: 芯片不存在或已使用
    """
    try:
        chip = Chip.objects.get(number=chip_no)
    except Chip.DoesNotExist:
        raise ValueError(f'芯片 {chip_no} 不存在')

    if chip.status == 'used':
        raise ValueError(f'芯片 {chip_no} 已被使用')

    chip.status = 'used'
    chip.pet = pet
    chip.used_at = timezone.localdate()
    chip.save(update_fields=['status', 'pet', 'used_at'])

    # 同步写入宠物档案
    pet.chip_no = chip_no
    pet.save(update_fields=['chip_no'])

    return True


# ============================================
# 库存调整
# ============================================
def adjust_stock(material, hospital, quantity, txn_type, **extra):
    """创建物资流水并调整库存。

    - hospital=None: 捕捉点侧，直接调整 material.shelter_stock
    - hospital!=None: 医院侧，库存通过流水计算，不直接修改字段

    :param material: 物料对象
    :param hospital: 机构对象（医院），None 表示捕捉点
    :param quantity: 数量（正整数）
    :param txn_type: 流水类型 purchase/dispatch/consume/adjustment
    :param extra: 额外字段，如 operator/batch_no/supplier/from_to/note/ledger_no/district
    :return: 创建的 MaterialTransaction 对象
    """
    district = extra.get('district') or material.district
    operator = extra.get('operator')
    today = timezone.localdate()

    txn = MaterialTransaction.objects.create(
        type=txn_type,
        material=material,
        material_name=material.name,
        quantity=quantity,
        unit=material.unit,
        batch_no=extra.get('batch_no', material.batch_no),
        supplier=extra.get('supplier', material.supplier),
        from_to=extra.get('from_to', ''),
        hospital=hospital,
        operator=operator,
        operator_name=extra.get('operator_name', ''),
        date=today,
        ledger_no=extra.get('ledger_no', ''),
        district=district,
        note=extra.get('note', ''),
    )

    # 捕捉点侧直接调整库存字段（扣减类操作不允许库存为负）
    if hospital is None:
        if txn_type in ('purchase',):
            material.shelter_stock += quantity
        elif txn_type in ('dispatch', 'consume', 'adjustment'):
            if material.shelter_stock < quantity:
                raise ValueError(f'捕捉点库存不足（当前 {material.shelter_stock}，需 {quantity}）')
            material.shelter_stock -= quantity
        material.save(update_fields=['shelter_stock'])

    return txn


def get_hospital_stock(material, hospital):
    """计算指定医院的物资库存。

    库存 = 采购入库 + 医院签收 - 诊疗消耗 - 异动调整
    （以上均针对同一医院）

    注意：dispatch（下发）流水不直接增加医院库存，
    医院签收后才创建 receive 流水增加库存。
    """
    if hospital is None:
        return 0

    base_qs = MaterialTransaction.objects.filter(material=material, hospital=hospital)

    purchase_total = base_qs.filter(type='purchase').aggregate(
        total=Sum('quantity')
    )['total'] or 0

    receive_total = base_qs.filter(type='receive').aggregate(
        total=Sum('quantity')
    )['total'] or 0

    consume_total = base_qs.filter(type='consume').aggregate(
        total=Sum('quantity')
    )['total'] or 0

    adjustment_total = base_qs.filter(type='adjustment').aggregate(
        total=Sum('quantity')
    )['total'] or 0

    return purchase_total + receive_total - consume_total - adjustment_total


# ============================================
# 黑名单检查
# ============================================
def check_blacklist(id_card, phone):
    """检查身份证或电话是否在黑名单中。

    匹配规则：
    - 电话精确匹配
    - 身份证匹配：清洗掩码后按「前6位 + 后4位」联合比对，
      避免仅按前6位（地址码）误伤同区县的所有人

    :return: 匹配的 Blacklist 记录，或 None
    """
    if not id_card and not phone:
        return None

    # 已移出黑名单的记录不再参与拦截
    qs = Blacklist.objects.filter(is_deleted=False)

    # 电话精确匹配
    if phone:
        match = qs.filter(phone=phone).first()
        if match:
            return match

    # 身份证匹配：前6位+后4位联合（掩码场景仍可定位到人）
    if id_card:
        clean_id = re.sub(r'[^0-9Xx]', '', id_card).upper()
        if len(clean_id) >= 6:
            head, tail = clean_id[:6], clean_id[-4:]
            for bl in qs.exclude(id_card=''):
                bl_clean = re.sub(r'[^0-9Xx]', '', bl.id_card).upper()
                if len(bl_clean) >= 10 and bl_clean[:6] == head and bl_clean[-4:] == tail:
                    return bl

    return None


# ============================================
# 区县数据隔离
# ============================================
def district_lookup_path(model):
    """返回该模型用于区县隔离的查询路径（默认 `district`）。

    绝大多数模型自带 `district` 外键，按 `district_id` 过滤即可。
    少数模型本身没有该字段，区县需要沿外键派生——它们用类属性
    `DISTRICT_LOOKUP` 声明路径（如 `CheckIn` / `AdoptionHallListing`
    的 `'pet__district'`，区县随所属宠物走）。

    没有这个兜底时，这类模型会被直接 `filter(district_id=...)` 打穿：
    对「所属区县为具体区县」的用户（区县政府）抛 `FieldError` → 接口 500，
    而对市级用户反而正常——所以只测市级账号是发现不了的。
    """
    return getattr(model, 'DISTRICT_LOOKUP', 'district')


def get_district_filtered_queryset(model, user):
    """根据用户角色返回区县过滤后的 QuerySet。

    - gov_city 或所属区县为市级 (is_city=True): 返回全部数据
    - 其他角色: 仅返回所属区县数据

    区县过滤路径由 `district_lookup_path()` 决定：模型可用
    `DISTRICT_LOOKUP` 声明派生路径，默认按自身的 `district` 过滤。

    :param model: 模型类
    :param user: 已登录的 User 对象
    :return: QuerySet
    """
    if user.role == 'gov_city':
        return model.objects.all()

    # 市级用户（如捕捉点操作员）可见全部数据
    district = getattr(user, 'district', None)
    if district and getattr(district, 'is_city', False):
        return model.objects.all()

    district_id = getattr(user, 'district_id', None)
    if district_id:
        lookup = district_lookup_path(model)
        return model.objects.filter(**{f'{lookup}_id': district_id})
    return model.objects.none()


def get_scoped_object(model, pk, user, **extra):
    """按区县权限范围取单条记录；越权或不存在一律返回 None。

    统一替代裸 ``Model.objects.get(id=pk)``——后者不做任何范围校验，
    任何登录用户只要猜到主键就能跨区县读取/操作他人数据。
    """
    qs = get_district_filtered_queryset(model, user)
    if extra:
        qs = qs.filter(**extra)
    return qs.filter(pk=pk).first()


def get_active_pet(pk, user):
    """取用户权限范围内、未被逻辑删除的宠物档案；不存在或越权返回 None。"""
    return get_scoped_object(Pet, pk, user, is_deleted=False)


def pet_has_pending_release(pet):
    """宠物是否已有「待放养」记录。

    用于防止同一只动物被放养流程与领养流程同时占用（双重承诺）。
    """
    return Release.objects.filter(pet=pet, status='pending').exists()


def pet_has_active_adoption(pet):
    """宠物是否存在未完结的领养记录（待领出视为占用中）。"""
    return Adoption.objects.filter(pet=pet, status='pending_claim').exists()


# ============================================
# 捕捉单转运状态推导
# ============================================
# 已提交给医院、尚未被退回的转运单状态（含待签收与已签收）
ACTIVE_TRANSFER_STATUSES = ('pending', 'received')


def _split_codes(raw):
    """逗号分隔编号字符串 → 去空列表。"""
    return [c.strip() for c in (raw or '').split(',') if c.strip()]


def capture_transfers(capture):
    """取与本捕捉单相关的全部转运单（含未回填 capture 外键的历史数据）。

    早期转运单由前端「选择在途宠物」直接下发，未回填 capture 外键，
    因此这里额外按宠物编号做精确比对补齐，避免漏算导致状态失真。
    """
    transfers = list(Transfer.objects.filter(capture=capture))

    codes = list(Pet.objects.filter(capture=capture).values_list('code', flat=True))
    if codes:
        legacy_q = Q()
        for code in codes:
            legacy_q |= Q(pet_codes__contains=code)
        # contains 只是预筛（存在子串误命中），下面按编号集合精确取交集
        for t in Transfer.objects.filter(capture__isnull=True).filter(legacy_q):
            if set(_split_codes(t.pet_codes)) & set(codes):
                transfers.append(t)
    return transfers


def capture_transfer_state(capture):
    """统计捕捉单下宠物的转运情况，供状态推导与编辑/删除权限判断。

    返回 dict:
      total        本单未删除的宠物数
      transferred  已提交转运单且尚未退回的宠物数（= 已转运，含待签收与已签收）
      received     医院已签收的宠物数（用于区分「转运中」与「部分完成」）
      settled      已离开本单流程的宠物数（医院签收 / 主人领回 / 已死亡…，
                   即宠物状态不再是 in_transit；达成 total 即为「完成」）
      rejected     被医院退回、已回到未转运状态的宠物数
      can_edit     是否允许编辑（未作废 且 无任何宠物处于已转运状态）
      can_delete   是否允许删除（同上；须医院全部退回）
    """
    pets = Pet.objects.filter(capture=capture, is_deleted=False)
    total = pets.count()
    codes = set(pets.values_list('code', flat=True))
    # 「已离开本单流程」以宠物自身状态为准：签收后转 in_treatment、
    # 主人领回转 owner_returned、死亡转 euthanized，都不会再是 in_transit。
    settled = pets.exclude(status='in_transit').count() if total else 0

    active_codes, received_codes, rejected_codes = set(), set(), set()
    for t in capture_transfers(capture):
        matched = set(_split_codes(t.pet_codes)) & codes
        if not matched:
            continue
        if t.status in ACTIVE_TRANSFER_STATUSES:
            active_codes |= matched
            if t.status == 'received':
                received_codes |= matched
        elif t.status == 'rejected':
            rejected_codes |= matched

    # 同一宠物既被退回又重新下发时，以「当前仍在转运」为准
    rejected_codes -= active_codes
    received_codes &= active_codes
    transferred = len(active_codes)

    not_deleted = not capture.is_deleted
    return {
        'total': total,
        'transferred': transferred,
        'received': len(received_codes),
        'settled': settled,
        'rejected': len(rejected_codes),
        'untouched': max(total - transferred - len(rejected_codes), 0),
        'can_edit': not_deleted and transferred == 0,
        'can_delete': not_deleted and transferred == 0,
    }


def capture_states_bulk(captures):
    """批量计算多张捕捉单的转运状态，避免列表接口 N+1 查询。

    :param captures: Capture 可迭代对象（或 QuerySet）
    :return: {capture_id: 与 capture_transfer_state 同结构的 dict}
    """
    captures = list(captures)
    if not captures:
        return {}

    # 宠物编号 → 所属捕捉单（仅未删除宠物参与统计），同时统计「已离开流程」数
    code_to_capture = {}
    codes_by_capture = {}
    settled_by_capture = {}
    for cap_id, code, pet_status in Pet.objects.filter(
        capture__in=captures, is_deleted=False
    ).values_list('capture_id', 'code', 'status'):
        code_to_capture[code] = cap_id
        codes_by_capture.setdefault(cap_id, set()).add(code)
        if pet_status != 'in_transit':
            settled_by_capture[cap_id] = settled_by_capture.get(cap_id, 0) + 1

    # 编号 → 转运状态标记。优先级 pending > received > rejected：
    # 被驳回后重新下发的宠物必须算「转运中」，而不是停留在旧的 rejected。
    code_flag = {}
    _FLAG_RANK = {'pending': 3, 'received': 2, 'rejected': 1}
    all_codes = set(code_to_capture)
    if all_codes:
        probe = Q()
        for code in all_codes:
            probe |= Q(pet_codes__contains=code)
        for t in Transfer.objects.filter(probe):
            if t.status not in _FLAG_RANK:
                continue
            for code in _split_codes(t.pet_codes):
                if code in all_codes and _FLAG_RANK[t.status] > _FLAG_RANK.get(code_flag.get(code), 0):
                    code_flag[code] = t.status

    result = {}
    for cap in captures:
        codes = codes_by_capture.get(cap.id, set())
        transferred = sum(1 for c in codes if code_flag.get(c) in ('pending', 'received'))
        received = sum(1 for c in codes if code_flag.get(c) == 'received')
        rejected = sum(1 for c in codes if code_flag.get(c) == 'rejected')
        total = len(codes)
        not_deleted = not cap.is_deleted
        result[cap.id] = {
            'total': total,
            'transferred': transferred,
            'received': received,
            'settled': settled_by_capture.get(cap.id, 0),
            'rejected': rejected,
            'untouched': max(total - transferred - rejected, 0),
            'can_edit': not_deleted and transferred == 0,
            'can_delete': not_deleted and transferred == 0,
        }
    return result


def recalc_capture_status(capture, save=True):
    """按宠物转运情况重新推导并写回捕捉单状态。

    规则：
    - 已逻辑删除 → void 已作废
    - 无宠物 / 无任何有效转运单 → pending 待转运
    - 部分宠物已提交转运单 → partial 部分转运
    - 全部宠物已提交转运单 → completed 已完成
    """
    if capture.is_deleted:
        new_status = 'void'
    else:
        state = capture_transfer_state(capture)
        if state['total'] == 0 or state['transferred'] == 0:
            new_status = 'pending'
        elif state['transferred'] < state['total']:
            new_status = 'partial'
        else:
            new_status = 'completed'

    if save and capture.status != new_status:
        capture.status = new_status
        capture.save(update_fields=['status', 'updated_at'])
    return new_status


def get_district_scope(request):
    """从 request 中获取区县范围。

    优先使用中间件设置的 user_district_scope，其次从 user.district_id 获取。
    None 表示可见全部（市级管理员或所属区县为市级的用户）。
    """
    # 中间件已设置 user_district_scope（可能为 None 表示可见全部）
    if hasattr(request, 'user_district_scope'):
        return request.user_district_scope
    # 兜底：中间件未设置时手动计算
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        if user.role == 'gov_city':
            return None
        # 所属区县为市级的用户可见全部
        district = getattr(user, 'district', None)
        if district and getattr(district, 'is_city', False):
            return None
        return user.district_id
    return None
