"""
TNR 业务系统 - 共享服务层
提供编号生成、芯片管理、库存调整、黑名单检查、区县过滤等通用功能。
"""
import json
import random
import re
from datetime import date

from django.db.models import Sum
from django.http import JsonResponse
from django.utils import timezone

from accounts.models import User
from business.models import (
    Pet, Material, MaterialTransaction, Chip, Blacklist,
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


def serialize_instance(instance, fields=None):
    """将模型实例序列化为字典。

    - 外键返回 pk
    - 日期/时间返回 ISO 格式字符串
    - ImageField/FileField 返回 URL（有文件时）或空字符串
    - 同时返回蛇形命名（Python 惯例）和驼峰命名（前端 JS 惯例）字段
    - pet_codes 字符串自动转为数组
    """
    if instance is None:
        return None
    from django.db.models.fields.files import FieldFile

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
    chip.used_at = timezone.now().date()
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
    today = timezone.now().date()

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

    qs = Blacklist.objects.all()

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
def get_district_filtered_queryset(model, user):
    """根据用户角色返回区县过滤后的 QuerySet。

    - gov_city 或所属区县为市级 (is_city=True): 返回全部数据
    - 其他角色: 仅返回所属区县数据

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
        return model.objects.filter(district_id=district_id)
    return model.objects.none()


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
