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
from core.models import District, Institution


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
    # 必须用 localdate() 而不是 now()：now() 是 UTC，北京时间 00:00–08:00 这段
    # 生成的编号会退回前一天（9/18 凌晨生成出 TNR260917xxx），与界面显示的
    # 本地日期对不上。项目 TIME_ZONE=Asia/Shanghai、USE_TZ=True。
    today = timezone.localdate()
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
    # 同 generate_pet_codes：用本地日期，否则凌晨生成的单号会退到前一天
    date_str = timezone.localdate().strftime('%y%m%d')
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
# 一宠一档：动物档案聚合
# ============================================
# 各阶段照片字段 → 展示标签。顺序即照片墙的展示顺序。
PET_PHOTO_LABELS = (
    ('photo_capture', '捕捉照片'),
    ('photo_group', '整体合影'),
    ('photo_before', '术前'),
    ('photo_after', '术后'),
    ('photo_treatment', '诊疗'),
)


def pet_brief(pet):
    """动物档案简要信息（含全部阶段照片 URL）。

    捕捉端「一宠一档」台账、政府端「一宠一档」以及各业务详情里的
    「动物明细」都复用它，避免各端字段口径不一致（例如一端有品种、
    另一端没有，或一端漏掉捕捉照片）。
    """
    if pet is None:
        return None
    return {
        'id': pet.id,
        'code': pet.code,
        'name': pet.name,
        'species': pet.species,
        'breed': pet.breed,
        'gender': pet.gender,
        'age': pet.age,
        'color': pet.color,
        'weight': pet.weight,
        'chip_no': pet.chip_no,
        'status': pet.status,
        'status_display': pet.get_status_display(),
        'district_id': pet.district_id,
        'district_name': pet.district.name if pet.district_id else '',
        'shelter_name': pet.shelter.name if pet.shelter_id else '',
        'hospital_name': pet.hospital.name if pet.hospital_id else '',
        'photo_capture': pet.photo_capture.url if pet.photo_capture else '',
        'photo_group': pet.photo_group.url if pet.photo_group else '',
        'photo_before': pet.photo_before.url if pet.photo_before else '',
        'photo_after': pet.photo_after.url if pet.photo_after else '',
        'photo_treatment': pet.photo_treatment.url if pet.photo_treatment else '',
    }


def pet_photo_list(pet):
    """动物的全部阶段照片（只返回确实有图的），供前端渲染照片墙并点击放大。"""
    if pet is None:
        return []
    photos = []
    for field, label in PET_PHOTO_LABELS:
        f = getattr(pet, field, None)
        if f:
            photos.append({'field': field, 'label': label, 'url': f.url})
    return photos


def _first_matching(related, **match):
    """在关联集合里取「最新一条」匹配记录。

    各业务模型的 Meta.ordering 都是 ``['-id']``（新→旧），因此首个匹配项
    即最新一条。刻意用 Python 过滤而不是 ``related.filter(...)``：关联被
    ``prefetch_related`` 预取后，再调 ``.filter()`` 会绕过缓存另发一次查询，
    列表页会退化成 N+1。
    """
    for obj in related:
        if all(getattr(obj, k, None) == v for k, v in match.items()):
            return obj
    return None


def _pet_outbound(pet):
    """按动物当前状态推导「去向」：放养 / 领养 / 死亡 / 主人领回。

    :return: (发生时间 ISO 串, 去向原因, 送达单位/接收人)
    """
    if pet.status == 'released':
        rel = _first_matching(pet.releases.all(), status='released')
        if rel:
            return (rel.released_at.isoformat() if rel.released_at else '',
                    '放养', rel.community_name or '')
    elif pet.status == 'adopted':
        ad = _first_matching(pet.adoptions.all(), status='completed')
        if ad:
            unit = (ad.adopter_name or '') + (f'（{ad.hospital_name}确认）' if ad.hospital_name else '')
            return (ad.adopted_at.isoformat() if ad.adopted_at else '', '领养', unit)
    elif pet.status == 'euthanized':
        eu = _first_matching(pet.euthanasia_records.all())
        if eu:
            return (eu.euthanized_at.isoformat() if eu.euthanized_at else '',
                    '死亡', eu.hospital_name or '')
    elif pet.status == 'owner_returned':
        orr = _first_matching(pet.owner_returns.all())
        if orr:
            return (orr.created_at.isoformat() if orr.created_at else '',
                    '主人领回', orr.owner_name or '')
    return '', '', ''


def _to_camel_key(snake):
    """snake_case → camelCase（与 ``serialize_instance`` 同一套规则）。"""
    parts = str(snake).split('_')
    if len(parts) == 1:
        return parts[0]
    return parts[0] + ''.join(p.title() for p in parts[1:])


def with_camel_keys(data):
    """给字典补一份 camelCase 别名，让两种命名都能取到值。

    本项目约定「序列化结果同时产出 snake_case 与 camelCase 两套键」
    （见 ``serialize_instance``）。``pet_archive_records`` 是手工聚合、不走
    ``serialize_instance``，所以必须在这里显式补齐——政府端读 snake_case、
    捕捉端读 camelCase，缺哪一套哪一端就静默出问题：前端读 ``r.ledgerNo``
    拿到 undefined 时，**编号列整列空白、编号点不开档案，且不报任何错**。
    """
    out = dict(data)
    for key, value in data.items():
        camel = _to_camel_key(key)
        if camel != key and camel not in out:
            out[camel] = value
    return out


def pet_archive_records(pets):
    """把动物档案聚合为「一宠一档」台账行。

    一行 = 一只动物。字段覆盖：档案编号（一宠一档）、猫/狗、品种、公/母、
    芯片号、当前状态、进站（捕捉）信息、去向（放养/领养/死亡/主人领回）、
    绝育情况、诊疗/驱虫/免疫明细，以及全部阶段照片。

    捕捉端「全量台账 → 一宠一档」与政府端「台账中心 → 一宠一档」共用本函数，
    两端因此天然同源，不会出现一端有出库原因、另一端没有的漂移。

    调用方请带上 ``select_related('district', 'shelter', 'hospital', 'capture')``
    与 ``prefetch_related('treatments', 'releases', 'adoptions',
    'euthanasia_records', 'owner_returns')``，否则每只动物会多出若干次查询。
    """
    records = []
    for pet in pets:
        capture = pet.capture if pet.capture_id else None
        outbound_at, outbound_reason, delivery_unit = _pet_outbound(pet)

        sterilized, sterilized_at = False, ''
        treatment_records, deworm_records, vaccine_records = [], [], []
        # 用 sorted() 而非 .order_by()：后者会绕过 prefetch 缓存重新查库
        for t in sorted(pet.treatments.all(), key=lambda x: x.id):
            items = []
            if t.items_sterilization:
                items.append('绝育')
            if t.items_vaccine:
                items.append('疫苗')
            if t.items_deworming:
                items.append('驱虫')
            if t.items_chip:
                items.append('芯片')
            doctor = t.sterilization_surgeon or t.operator_name or ''
            hospital = (t.hospital.name if t.hospital_id else '') or t.hospital_name or ''
            trt_date = t.sterilization_surgery_date or (t.created_at.date() if t.created_at else None)
            treatment_records.append({
                'ledger_no': t.ledger_no or f'TRE-{t.id:06d}',
                'hospital': hospital,
                'doctor': doctor,
                'items': '、'.join(items) or '—',
                'date': trt_date.isoformat() if trt_date else '',
                'status': t.get_status_display(),
            })
            if t.items_sterilization and not sterilized:
                sterilized = True
                sterilized_at = t.sterilization_surgery_date.isoformat() if t.sterilization_surgery_date else ''
            if t.items_deworming:
                deworm_records.append({
                    'drug': t.deworming_type or '',
                    'date': t.deworming_date.isoformat() if t.deworming_date else '',
                    'hospital': hospital, 'doctor': doctor,
                })
            if t.items_vaccine:
                vaccine_records.append({
                    'drug': t.vaccine_type or '',
                    'date': t.vaccine_date.isoformat() if t.vaccine_date else '',
                    'hospital': hospital, 'doctor': doctor,
                })

        photos = pet_photo_list(pet)
        detail = with_camel_keys({
            'pet': pet_brief(pet),
            'photos': photos,
            'treatment_records': treatment_records,
            'deworm_records': deworm_records,
            'vaccine_records': vaccine_records,
        })
        records.append(with_camel_keys({
            'business_type': 'pet',
            'ledger_no': pet.code,
            'id': pet.id,
            'date': pet.created_at.isoformat() if pet.created_at else '',
            'name': pet.name,
            'species': pet.species,
            'breed': pet.breed,
            'gender': pet.gender,
            'age': pet.age,
            'color': pet.color,
            'chip_no': pet.chip_no,
            'status': pet.status,
            'status_display': pet.get_status_display(),
            'district_name': pet.district.name if pet.district_id else '',
            'shelter_name': pet.shelter.name if pet.shelter_id else '',
            'hospital_name': pet.hospital.name if pet.hospital_id else '',
            'intake_at': capture.created_at.isoformat() if capture and capture.created_at else '',
            'intake_from': ((capture.community_name if capture else '') or (capture.address if capture else '') or ''),
            'intake_ledger_no': capture.ledger_no if capture else '',
            'intake_property_name': capture.property_name if capture else '',
            'intake_contact': ((capture.contact_person or '') if capture else '') + (
                ' / ' + capture.contact_phone if capture and capture.contact_phone else ''),
            'outbound_at': outbound_at,
            'outbound_reason': outbound_reason,
            'delivery_unit': delivery_unit,
            'sterilized': sterilized,
            'sterilized_at': sterilized_at,
            'treatment_records': treatment_records,
            'deworm_records': deworm_records,
            'vaccine_records': vaccine_records,
            'photos': photos,
            'detail': detail,
        }))
    return records


# ============================================
# 捕捉单转运状态推导
# ============================================
# 已提交给医院、尚未被退回的转运单状态（含待签收与已签收）
ACTIVE_TRANSFER_STATUSES = ('pending', 'received')


def _split_codes(raw):
    """逗号分隔编号字符串 → 去空列表。"""
    return [c.strip() for c in (raw or '').split(',') if c.strip()]


def busy_transfer_codes(codes, district_id=None):
    """返回其中**已被未结转运单占用**的动物编号。

    未结 = ``ACTIVE_TRANSFER_STATUSES``（pending 已下发待签收 / received 已签收）。
    ``rejected``（医院驳回）与 ``void``（捕捉点撤回）**不占位**——
    被驳回的动物必须能重新被选进「待转运」列表，这正是取消「重新下发」后
    依赖的路径：宠物退回 → 状态回 in_transit → 自动出现在待转运备选框。

    用于 `transfer_create` 的预校验：同一只动物不能同时挂在多张未结单据上，
    否则医院会收到重复单、捕捉单状态推导也会跟着错乱。
    """
    codes = {c for c in (codes or []) if c}
    if not codes:
        return set()
    probe = Q()
    for code in codes:
        probe |= Q(pet_codes__contains=code)
    qs = Transfer.objects.filter(probe, status__in=ACTIVE_TRANSFER_STATUSES)
    if district_id:
        qs = qs.filter(district_id=district_id)
    busy = set()
    for t in qs:
        busy |= set(_split_codes(t.pet_codes)) & codes
    return busy


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


def resolve_district_scope(user, anchor, submitted):
    """解析并校验业务记录的归属区县。

    :param user: 当前操作员
    :param anchor: 承载「最可能的正确区县」的对象
        （捕捉单/转运单用**机构**，主人领回/诊疗/放养/领养/安乐死用**宠物**）
    :param submitted: 前端显式提交的 district_id（可为 None）
    :return: (District 实例, 错误信息)；成功时错误信息为 None

    规则：
    1. 解析顺序：显式提交 → anchor 所在区县 → 操作员所属区县。
       **anchor 的区县比操作员区县更贴近事实**：现场把两个捕捉点操作员都挂在
       「全市（市级）」下，若以操作员区县为准，登记出来的捕捉单/转运单/黑名单
       会全部落到市级，本区县政府在区县隔离下**看不到本区数据**。
    2. 必须是具体区县，不能是「全市（市级）」。
    3. 非市级操作员只能归属到自己的区县。

    这个函数原本只写在 `views_capture` 里，导致转运单与黑名单各自散落一份
    「`data.get('district_id') or user.district_id`」的写法——前端并不提交
    `district_id`，于是它们全部落到市级。统一提到服务层，新增落库点一律走它。
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


# ============================================
# 上传图片校验
# ============================================
MAX_UPLOAD_BYTES = 10 * 1024 * 1024          # 单张图片上限 10MB
ALLOWED_IMAGE_FORMATS = {'JPEG', 'JPG', 'PNG', 'GIF', 'WEBP', 'BMP', 'TIFF'}


def validate_image_upload(f, label='图片'):
    """校验上传文件**确实是图片**，返回错误信息（None 表示通过）。

    为什么必须自己校验：Django 的 ``ImageField`` 只在走 ModelForm 时才校验，
    **直接赋值不做任何检查**。而各上传接口都是

        capture.group_photo = request.FILES['group_photo']

    于是把 ``.txt``（或把任意文件改名成 ``.png``）原样写进 ``media/``：
    前端 ``<img src>`` 直接裂图，数据库里也看不出任何异常，事后无从追溯。

    文件扩展名与 ``Content-Type`` 都由客户端提供、可随意伪造，所以这里
    **按内容**校验（Pillow 实际解码），不信任文件名。

    校验过程会把文件指针移动，结束时复位，调用方仍可正常保存。
    """
    if f is None:
        return None
    size = getattr(f, 'size', 0) or 0
    if size > MAX_UPLOAD_BYTES:
        return (f'{label}大小 {size / 1024 / 1024:.1f}MB 超过 '
                f'{MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限，请压缩后再上传')
    fmt = ''
    try:
        from PIL import Image
        f.seek(0)
        img = Image.open(f)
        img.verify()                      # 真正解码一遍，非图片会抛异常
        fmt = (img.format or '').upper()
    except Exception:
        return f'{label}不是有效的图片文件，请上传 JPG / PNG 等格式的图片'
    finally:
        try:
            f.seek(0)
        except Exception:
            pass
    if fmt and fmt not in ALLOWED_IMAGE_FORMATS:
        return f'{label}格式（{fmt}）不支持，请上传 JPG / PNG 等格式的图片'
    return None


def validate_uploaded_images(files, labels=None):
    """批量校验 ``request.FILES`` 中的图片，返回第一个错误信息（None 表示全通过）。

    各接口一律在**写库之前**调用它，与「先校验完再进事务」的两段式约定一致：
    否则第 N 张图片非法时，前面已经写进去的照片会留下半成品。
    """
    labels = labels or {}
    for key, f in (files or {}).items():
        err = validate_image_upload(f, labels.get(key, '图片'))
        if err:
            return err
    return None


def resolve_community(district, community_id=None, community_name=None):
    """把「小区」解析成 ``Institution(type='community')``，解析不到返回 None。

    为什么需要它：捕捉登记的「所在小区」是**自由文本**（前端刻意用输入框 + 模糊
    搜索，不做下拉枚举），后端只把它写进 ``Capture.community_name``，
    **从不回填 ``Capture.community`` 外键**。而放养只认外键 ——
    ``release_create`` 里是 ``community = pet.capture.community``，外键为空就直接
    返回「无法匹配原小区，请指定 community_id」，可界面上根本没有指定小区的入口。
    结果就是**放养流程在界面上永远走不通**，而且不报错，页面看起来像「还没数据」；
    只有绕过界面直接调接口传 community_id 才能成功。

    解析顺序（命中即返回）：
      1. 显式传入的 ``community_id``（必须确实是 ``type='community'``）
      2. 同区县下名称**完全相同**的机构
      3. 同区县下名称**互相包含**的机构（对应前端的模糊搜索语义）

    刻意**不自动新建**机构：错别字会凭空造出一堆小区档案，而小区属于机构主数据，
    应当由管理员在政府端维护。
    """
    if community_id:
        inst = Institution.objects.filter(id=community_id, type='community').first()
        # 显式传入的也必须落在同一区县：否则可以把动物「放养」到别区的小区，
        # 而 Release 的区县取自宠物 → 记录会挂在本区、小区却在别区。
        if inst and (district is None or inst.district_id == district.id
                     or inst.district_id is None):
            return inst

    name = (community_name or '').strip()
    if not name:
        return None

    base = Institution.objects.filter(type='community')
    if district is not None:
        base = base.filter(Q(district=district) | Q(district__isnull=True))

    exact = base.filter(name=name).first()
    if exact:
        return exact
    # 模糊：用户可能只填「阳光花园」，而机构档案是「阳光花园小区」
    return base.filter(name__icontains=name).first()
