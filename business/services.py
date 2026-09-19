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
from datetime import date, datetime, timedelta

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
    """解析 request.body 中的 JSON，失败时返回空字典。

    解析结果会挂在 `request.audit_payload` 上，供审计中间件在**视图执行完**
    之后回溯这次操作的请求体（例如从中取 `district_id` 判定归属区县）。
    中间件不自己读 `request.body`，是为了复用这里已有的容错逻辑。
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError, TypeError):
        data = {}
    if isinstance(data, dict):
        request.audit_payload = data
    return data


# ============================================
# 查询 / 请求参数安全解析
# ============================================
# 为什么必须统一走这里，而不是各处裸写 `int(...)` / 直接把字符串塞进 ORM：
#
# 一个**格式非法**的参数值会以**四种互不相同**的异常把接口打成 500，
# 所以「随手包一层 `except ValueError`」是**假修**：
#
#   1) `filter(<整型外键>=非数字)`      -> builtins.ValueError
#   2) `filter(<整型外键>=超大数)`      -> builtins.OverflowError
#      ⚠ `int('9' * 24)` 本身**不报错** —— Python 整数无上限。
#        是 SQLite 绑定参数时才溢出，所以校验必须发生在**进 ORM 之前**，
#        靠 `try: int(x) except ValueError` 永远拦不住。
#   3) `filter(<日期字段>=非日期)`      -> django.core.exceptions.ValidationError
#   4) 日期字段上做算术（`end_date + timedelta(days=1)`）
#                                       -> builtins.OverflowError
#      （`date.max` 加一天；与第 2 种同名不同源，`except ValueError` 同样拦不住）
#
# 返回值语义是**三态**（不用「非法就静默忽略」——静默忽略筛选条件等于
# 用户设了筛选却看到全量，属于最难被发现的一类静默失败）：
#
#   - 参数缺省 / 空串 -> `(None, None)`  调用方按「未筛选」处理
#   - 参数非法        -> `(None, 消息)`  调用方 `return json_fail(消息)`
#   - 参数合法        -> `(值, None)`
#
# 用法：
#     material_id, err = parse_int_param(request.GET.get('material_id'), '物料')
#     if err:
#         return json_fail(err)
#     if material_id:
#         qs = qs.filter(material_id=material_id)

# SQLite 的 INTEGER 是 64 位有符号，超过即绑定失败（异常类型 2）。
MAX_SQLITE_INT = 2 ** 63 - 1
# 用正则而不是裸 `int()`：`int()` 会接受 `'１２'`（全角）、`'1_0'`（下划线）、
# `'+5'`、`' 5 '` 这些「像数字但不是」的写法，口径比业务预期宽。
_INT_RE = re.compile(r'^[0-9]+$')
# 月/日允许不补零（`2026-9-1`）：Python 的 `strptime` 本来就接受这种写法，
# 收紧了只会让老书签 / 手工拼的 URL 无谓地撞 400。
_DATE_RE = re.compile(r'^(\d{4})-(\d{1,2})-(\d{1,2})')


def parse_int_param(raw, label='参数', minimum=1, maximum=MAX_SQLITE_INT):
    """把参数解析成整数（主键 id 用）。返回 ``(值, 错误消息)``，语义见本节开头。

    `minimum` 默认 1：这些参数全是自增主键，0 与负数不可能命中任何记录，
    放进 ORM 只会白跑一次查询（`-1` 还会被 SQLite 当合法整数收下）。

    `maximum` 用来挡**「合法但荒谬」**的数值 —— 那类值不会让接口报错，
    而是让服务端**跑很久 / 吃光内存**（例如 `?count=999999999` 会真的去
    循环生成 10 亿个编号）。这类问题探针扫不出来：接口最终是 200，
    只是在这之前已经把进程拖死。所以数量类参数必须显式给 `maximum`。
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    if not _INT_RE.match(text):
        return None, f'{label}必须是数字'
    # 先按**位数**挡一道，再 `int()`：Python 3.11+ 对超长数字串的转换
    # 本身有 4300 位上限（`int('9'*5000)` 会 ValueError），位数判断更稳。
    if len(text) > 19:
        return None, f'{label}超出有效范围'
    value = int(text)
    if value < minimum:
        return None, f'{label}超出有效范围'
    if value > maximum:
        # 业务上限（如单批 100）要报出来，否则用户不知道能填多少；
        # SQLite 量级的上限属于内部实现细节，不外露具体数字。
        if maximum >= MAX_SQLITE_INT:
            return None, f'{label}超出有效范围'
        return None, f'{label}不能超过 {maximum}'
    return value, None


def parse_date_param(raw, label='日期'):
    """把参数解析成 ``datetime.date``。返回 ``(值, 错误消息)``，语义见本节开头。

    返回的是 **date 对象而不是字符串** —— 直接把字符串塞进
    `__date__gte` 会让 Django 在解析阶段抛 `ValidationError`（异常类型 3）。
    允许 `2026-09-19T10:00:00` 这类 ISO 串，只取日期部分。
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    m = _DATE_RE.match(text)
    if not m:
        return None, f'{label}格式应为 YYYY-MM-DD'
    try:
        # 交给 strptime 校验月/日真实范围：`2026-02-30`、`0000-01-01` 都要拒。
        return datetime.strptime(m.group(0), '%Y-%m-%d').date(), None
    except (TypeError, ValueError):
        return None, f'{label}不是有效日期'


def date_upper_exclusive(d):
    """日期区间上界：把「含当天」的闭区间上界换成**开区间**上界。

    返回 ``(上界值, 是否开区间)``。
    `d == date.max`（9999-12-31）时加一天会 `OverflowError`（异常类型 4），
    此时退回闭区间上界 `d` —— 两者语义等价，因为不可能有比 `date.max`
    更晚的记录。**不要**用 `try: ... except ValueError` 去兜，类型不对。
    """
    try:
        return d + timedelta(days=1), True
    except OverflowError:
        return d, False


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
# 单批捕捉数量上限。`checkin_create`（真实建单）与 `pet_codes_preview`
# （编号预览）**必须守同一个数** —— 只卡一边就是孪生接口漂移：
# 预览能生成 10 万条、提交却被拒，或者反过来。
MAX_CAPTURE_BATCH = 100


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

    # ⚠ 库存判据必须在**建流水之前**。
    # 原先先 `MaterialTransaction.objects.create()` 再判库存，库存不足时抛
    # `ValueError` —— 调用方看到 400「库存不足」，但那条流水**已经落库**，
    # 会实实在在出现在捕捉点台账里：数量对不上，且全程没有任何报错。
    # 这与 `views_treatment` 里「先建诊疗记录再校验库存」是同一个反模式
    # （那一处已在早期轮次修掉，见 `test_treatment_views` 的同名用例）。
    # 「写库后才 `return json_fail` / `raise`」= 孤儿记录，判据一律前置。
    if hospital is None and txn_type in ('dispatch', 'consume', 'adjustment'):
        if material.shelter_stock < quantity:
            raise ValueError(f'捕捉点库存不足（当前 {material.shelter_stock}，需 {quantity}）')

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

    # 捕捉点侧直接调整库存字段（扣减类操作不允许库存为负；判据已在上面前置）
    if hospital is None:
        if txn_type in ('purchase',):
            material.shelter_stock += quantity
        elif txn_type in ('dispatch', 'consume', 'adjustment'):
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


def get_own_institution_object(model, pk, user, field='hospital_id', **extra):
    """按「**本机构**」取单条记录；不属于本机构或不存在一律返回 None。

    用途：医院端的写接口。**必须**用它，不要裸写 ``Model.objects.get(id=pk)`` ——
    裸取会让「记录存在但不属于本院」与「记录不存在」返回**不同**的结果，
    形成**存在性预言机**：跨机构/跨区县枚举 id 就能问出记录是否存在，
    甚至从错误文案里读出业务状态（第二十三轮实测：

        POST /api/business/transfers/<跨区县 id>/receive/
          → 400 「当前状态(received)不可签收」      ← 记录存在，且状态被读出
        POST /api/business/transfers/<不存在的 id>/receive/
          → 404 「转运记录不存在」                  ← 不存在

    两者可区分 = 可枚举。改成按机构取单后，两种情况都是同一个 404。）

    **为什么不能换成 `get_district_filtered_queryset`（按区县收敛）**：
    转运单**允许跨区县送医**（`transfer_create` 不限制目标医院的区县），
    按区县收敛会打断这条正常业务路径。所以这里按 `field`（本机构外键）收敛。

    账号没有机构时返回 None（而不是「跳过校验」）——
    `if user.institution_id and ...` 这种写法会让判据在字段为空时**静默失效**。
    """
    if not user.institution_id:
        return None
    qs = model.objects.all()
    if extra:
        qs = qs.filter(**extra)
    return qs.filter(pk=pk, **{field: user.institution_id}).first()


def hospital_pet_scope(user):
    """**医院角色**的动物可见范围：本院在治 ∪ 本院经手过（诊疗）的动物。

    返回一个 `Q`，调用方自己接 `filter()`，并**必须加 `.distinct()`**
    —— `treatments` 是反向外键，会 JOIN 出重复行。

    为什么不能按区县收敛：**同一区县可能有多家医院**。襄城区就有两家
    （爱心宠物医院 / 瑞鹏宠物医院），按区县过滤会让 A 院读到 B 院名下动物的
    完整档案 —— 含主人姓名与电话（`intake_contact` / `intake_property_name`）。
    第二十轮实测：爱心看到 16 条（含瑞鹏的 2 条），瑞鹏看到 16 条（含爱心的 5 条）。

    为什么不能只按 `hospital_id`：动物出院（放养 / 领养 / 主人领回 / 安乐死）后
    `Pet.hospital` 会被清空，只按它会让医院**查不到自己经手过的历史动物**。
    所以并上「本院诊疗过」。

    没有挂靠机构时返回**空集**而不是 `Q(hospital_id=None)`
    —— 后者会匹配上所有「尚未分配医院」的动物，等于不设限。
    """
    if not user.institution_id:
        return Q(pk__in=[])
    return (Q(hospital_id=user.institution_id)
            | Q(treatments__hospital_id=user.institution_id))


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


def resolve_operator_district(role, district, institution):
    """算出「这个账号的所属区县本应该是哪个」，用于修复已存在的不一致账号。

    与 `validate_operator_district()` **互补**：校验说没问题就返回 `None`，
    校验报错就返回应该改成的那个区县。两者必须保持一致 ——
    否则会出现「巡检报错、修复脚本却说不用改」的死循环，
    所以有专门的用例锁死这个不变式（`ResolveOperatorDistrictTest`）。

    为什么以**机构**为准：机构是有实体的（医院/捕捉点就落在那个区县），
    而账号的 `district` 只是建号时填的一个字段。实测库里那个不一致账号
    （区县=南漳县、机构=东津新区的宠安宠物诊所）也是机构侧更可信。

    :return: 应该改成哪个区县；无需修改时为 None
    """
    if validate_operator_district(role, district, institution) is None:
        return None
    return institution.district


def validate_operator_district(role, district, institution):
    """校验「账号所属区县」与「所属机构所在区县」是否自洽，返回错误信息。

    为什么必须校验：账号的 `district` 决定**读取**侧的区县隔离范围
    （`get_district_filtered_queryset`），而 `institution` 决定它在业务里代表谁。
    两者不一致时，这个账号会**看到 A 区县的档案、却以 B 区县的机构身份操作** ——
    实测库里已经存在一个这样的账号（区县=南漳县、机构=东津新区的「宠安宠物诊所」），
    而政府端的「用户管理」只有新增与启停、**没有编辑**，管理员在界面上根本改不回来。

    规则：
    - 医院操作员：账号区县必须**等于**机构所在区县（医院不能挂市级，见 `user_create`）。
    - 捕捉点操作员：允许挂「市级」—— 现场约定，两个捕捉点操作员都挂在
      「全市（市级）」下（见 `resolve_district_scope` 的说明）；
      但若挂了具体区县，就必须与机构一致。
    - 无机构的角色（gov_*）：不做校验。

    :param role: 账号角色
    :param district: 账号所属区县（District 实例，可为 None）
    :param institution: 所属机构（Institution 实例，可为 None）
    :return: 错误信息；通过时为 None
    """
    if institution is None:
        return None
    inst_district = getattr(institution, 'district', None)
    if inst_district is None:
        return None
    if district is None:
        return '账号缺少所属区县'

    if getattr(district, 'is_city', False):
        if role == 'shelter':
            return None
        return '该角色的账号所属区县必须是具体区县，不能挂市级'

    if district.id != inst_district.id:
        return (f'账号所属区县（{district.name}）与机构所在区县'
                f'（{inst_district.name}）不一致，请改为一致')
    return None


def cascade_operator_district(institution, old_district_id):
    """机构换了区县时，把挂在它下面、区县随机构走的操作员一起搬过去。

    不搬的后果与「账号区县 ≠ 机构区县」同源：操作员会看到**旧**区县的档案，
    却以**新**区县的机构身份操作。挂「市级」的捕捉点操作员是现场约定
    （`validate_operator_district` 明确允许），**不动**它们 ——
    只搬「区县恰好等于机构原区县」的那些账号。

    :return: 被一起调整的账号数
    """
    if institution is None or institution.district_id is None or old_district_id is None:
        return 0
    if old_district_id == institution.district_id:
        return 0
    return User.objects.filter(
        institution=institution, district_id=old_district_id,
    ).update(district=institution.district)


# 各角色可以创建/修改的账号角色 —— **服务端唯一真源**。
#
# 前端 `GovPortal.canCreateRole()` 早就把这套规则写了一遍（只是把角色下拉的选项
# 过滤掉），但接口**没有任何对应校验**，等于没校验：
# 实测区级管理员直接 POST `role=gov_city` + 市级 `district_id` 能建出一个
# **市级管理员**账号 —— 口令还是他自己填的，登录后 `get_district_scope()` 返回
# `None`，**全部区县的数据都看得到**。
#
# 这类「前端做了、后端没做」的校验特别危险：界面上根本点不出这个选项，
# 所以无论怎么点都发现不了，只有直接打接口才会暴露。
MANAGEABLE_ROLES = {
    'gov_city': ('gov_city', 'gov_district', 'shelter', 'hospital'),
    'gov_district': ('shelter', 'hospital'),
}


def validate_user_manage_scope(operator, role, district, institution):
    """校验「当前操作员」有没有资格创建/修改「这样一个账号」，返回错误信息。

    两条规则：

    1. **角色**必须在他可管理的集合里（`MANAGEABLE_ROLES`）；
    2. **区级管理员**只能管理「**账号所代表的对象**落在本区县」的账号。

    第 2 条的判据是「代表谁」，**不是账号自己的 `district`**：
    捕捉点操作员按现场约定挂在「全市（市级）」（见 `resolve_district_scope` 的说明），
    判账号区县会把区级管理员**唯一能建的捕捉点操作员全挡掉**。
    所以：

    - 捕捉点 → 判**机构所在区县**（操作员代表的是那个捕捉点）；
    - 医院 → 判**机构所在区县**（医院操作员两者必须一致，
      见 `validate_operator_district`，取机构侧与它天然同源）。

    :return: 错误信息；通过时为 None
    """
    operator_role = getattr(operator, 'role', None)
    allowed = MANAGEABLE_ROLES.get(operator_role, ())
    if role not in allowed:
        return '无权创建或修改该角色的账号'
    if operator_role != 'gov_district':
        return None

    if institution is not None:
        anchor_district_id = institution.district_id
    elif district is not None:
        anchor_district_id = district.id
    else:
        anchor_district_id = None

    if anchor_district_id != getattr(operator, 'district_id', None):
        return '无权管理其他区县的账号'
    return None


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


def inactive_institution_error(institution, label='机构'):
    """机构已停用时返回错误信息，否则 None。

    **「停用」必须真的拦住点什么。** 此前 `Institution.status` 在整个后端
    **没有任何读取点** —— 只有 `institution_create` 写 `status='active'`
    和 `institution_toggle_status` 翻转这两处**写入**，业务侧一次都没读过。
    后果：停用一家医院只改了列表里的一个徽标 —— 新建转运单的医院下拉照旧
    列出它、`transfer_create` 照旧接受；停用一个捕捉点后照旧能对它登记新的
    捕捉单；`institution_toggle_status` 也**不联动其下账号**（操作员照旧能登录、
    照旧能签收）。实测把接收医院停用后，转运单依然创建成功。

    与 `inactive_district_error()` 是同一条原则：**停用的实体不能再作为
    新业务的归属对象**。只拦「新引用」，不拦存量 —— 已提交给该医院的转运单
    仍要能签收/驳回，停在停用捕捉点下的历史捕捉单仍要能编辑与转运。
    所以判据只出现在**创建**路径上，编辑路径不校验（否则存量记录会被锁死）。

    前端两侧必须同步过滤下拉，否则是「界面上能选、服务端要拒」的死胡同：
    政府端早已过滤 `status === 'active'`（gov portal 三处），捕捉点端三处漏了
    —— 同一条规则只写了一半，就是跨端漂移。
    """
    if institution is not None and institution.status != 'active':
        return f'{label}「{institution.name}」已停用，不能用于新的业务记录'
    return None


def inactive_district_error(district):
    """区县已停用时返回错误信息，否则 None。

    实现只有这一份：`supervision.views._inactive_district_error()` 是薄封装，
    `user_create` 也走它。此前那条注释声称「前端所有区县下拉都写了
    `d.status === 'active'`」—— 实测**不成立**：捕捉点端新建捕捉单的归属区县
    下拉只过滤了 `!isCity`，停用区县照样能选，`resolve_district_scope()` 也照样
    接受（它只校验 `is_city`，不看 `status`）。所以这条判据必须在服务端兜住。

    消息文案与 `user_create` 的历史文案保持一致（已有用例锁死该字符串）。
    """
    if district is not None and district.status != 'active':
        return '所选区县已停用'
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
