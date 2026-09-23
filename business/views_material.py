"""
Task 7: 物料供应链与双台账
- 物料列表/采购/下发/签收/异动
- 捕捉点台账 / 医院台账
- 芯片号段管理
"""
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Material, MaterialTransaction, Chip
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_str, body_int,
    generate_ledger_no, get_district_filtered_queryset,
    adjust_stock, get_hospital_stock, get_scoped_object,
    get_own_institution_object, parse_int_param,
)
from core.models import Institution


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def material_list(request):
    """物料列表（含医院库存计算）"""
    user = request.user
    qs = get_district_filtered_queryset(Material, user)

    category = request.GET.get('category')
    if category:
        qs = qs.filter(category=category)

    data = []
    for m in qs:
        item = serialize_instance(m)
        # 如果用户是医院，附加该医院的库存
        if user.role == 'hospital' and user.institution_id:
            item['hospital_stock'] = get_hospital_stock(m, user.institution)
        else:
            # 列出所有关联医院的库存
            hospitals = Institution.objects.filter(
                type='hospital', district_id=m.district_id
            )
            item['hospital_stocks'] = {
                str(h.id): {
                    'name': h.name,
                    'stock': get_hospital_stock(m, h),
                }
                for h in hospitals
            }
        data.append(item)
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def purchase_create(request):
    """采购入库

    请求体示例:
    {
        "material_id": 1,
        "quantity": 100,
        "supplier": "国药集团",
        "batch_no": "B20250101",
        "expiry_date": "2025-12-31",
        "chip_range_start": "1000010001",  // 芯片才需要
        "chip_range_end": "1000010100"
    }
    """
    data = parse_json_body(request)
    user = request.user

    # ⚠ 数量**必须最先校验**。原先 `int(data.get('quantity', 0))` 排在
    # 「按名称新建物料」**之后**，于是：
    #   - `quantity=0`（或缺失）→ 先 `Material.objects.create()` 建出一条物料，
    #     紧接着 `return json_fail('采购数量必须大于0')` —— **报错了，孤儿物料留下了**；
    #   - `quantity='abc'` → `int()` 直接 `ValueError`，打成 500，同样留下孤儿物料。
    # 多表写入一律「**先全校验 → 再落库**」（项目约定）。
    # `minimum=0` 让 0 通过解析器、由下面那句业务判据给出更准确的中文提示。
    quantity, err = parse_int_param(data.get('quantity'), '采购数量', minimum=0)
    if err:
        return json_fail(err)
    if not quantity:
        return json_fail('采购数量必须大于0')

    material_id = body_int(data, 'material_id')
    material = None
    if material_id:
        # 按区县范围取物料，避免跨区县对他人物料做采购入库
        material = get_scoped_object(Material, material_id, user)

    if material is None:
        # 前端直接以名称/类别新增物料（如采购入库表单未选择已有物料）
        name = str(body_str(data, 'name')).strip()
        category = str(body_str(data, 'category')).strip()
        if not name or category not in dict(Material.CATEGORY_CHOICES):
            return json_fail('缺少物料ID或物料名称/类别')
        shelter = user.institution if user.institution and user.institution.type == 'shelter' else None
        district_id = shelter.district_id if shelter else (getattr(user, 'district_id', None) or None)
        if not district_id:
            return json_fail('缺少区县信息')
        material = Material.objects.filter(name=name, category=category, district_id=district_id).first()
        if not material:
            material = Material.objects.create(
                name=name,
                category=category,
                unit=str(body_str(data, 'unit') or '支'),
                specification=body_str(data, 'specification'),
                supplier=body_str(data, 'supplier'),
                batch_no=body_str(data, 'batch_no'),
                safety_stock=0,
                district_id=district_id,
            )

    district_id = material.district_id

    # 校验全部通过，从这里开始才写库。整体包一个事务：
    # 采购要同时写「物料行 / 采购流水 / 芯片号段」，中途任何异常都不该留下半截数据。
    with transaction.atomic():
        # 创建采购流水（捕捉点侧，hospital=None）
        txn = adjust_stock(
            material=material,
            hospital=None,
            quantity=quantity,
            txn_type='purchase',
            operator=user,
            operator_name=user.get_full_name() or user.username,
            supplier=body_str(data, 'supplier'),
            batch_no=body_str(data, 'batch_no'),
            from_to=body_str(data, 'supplier'),
            note=body_str(data, 'note', '采购入库'),
            ledger_no=generate_ledger_no('PUR'),
            district_id=district_id,
        )

        # 更新物料扩展信息
        update_fields = []
        if data.get('supplier'):
            material.supplier = data['supplier']
            update_fields.append('supplier')
        if data.get('batch_no'):
            material.batch_no = data['batch_no']
            update_fields.append('batch_no')
        if data.get('expiry_date'):
            try:
                material.expiry_date = datetime.strptime(str(data['expiry_date'])[:10], '%Y-%m-%d').date()
                update_fields.append('expiry_date')
            except (ValueError, TypeError):
                pass
        if update_fields:
            material.save(update_fields=update_fields)

        # 芯片采购：创建芯片号段（兼容前端 chip_start/chip_end 与后端 chip_range_* 两种命名）
        if material.category == 'chip':
            range_start = body_str(data, 'chip_range_start') or body_str(data, 'chip_start')
            range_end = body_str(data, 'chip_range_end') or body_str(data, 'chip_end')
            if range_start and range_end:
                _create_chip_range(range_start, range_end, material)
                material.chip_range_start = range_start
                material.chip_range_end = range_end
                material.save(update_fields=['chip_range_start', 'chip_range_end'])

    return json_ok(serialize_instance(txn), message=f'采购入库成功，增加库存 {quantity}')


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def dispatch_create(request):
    """下发至医院

    请求体示例:
    {
        "material_id": 1,
        "hospital_id": 3,
        "quantity": 50,
        "chip_numbers": ["1000010001", "1000010002"]  // 芯片可选指定号
    }
    """
    data = parse_json_body(request)
    user = request.user

    material_id = body_int(data, 'material_id')
    hospital_id = body_int(data, 'hospital_id') or body_int(data, 'to_hospital_id')
    if not material_id or not hospital_id:
        return json_fail('缺少物料或医院信息')

    # 按区县范围取物料，避免跨区县下发他人物料
    material = get_scoped_object(Material, material_id, user)
    if material is None:
        return json_fail('物料不存在或无权访问', status=404)

    hospital = Institution.objects.filter(id=hospital_id, type='hospital').first()
    if hospital is None:
        return json_fail('医院不存在')

    # 只允许下发到本区县医院，避免跨区县串货导致台账对不上
    if hospital.district_id != material.district_id:
        return json_fail('只能下发到本区县医院')

    # 与 `purchase_create` 同一口径：非法数量必须 400，不能是 500。
    # 原先裸写 `int(data.get('quantity', 0))` —— `quantity='abc'` → `ValueError` → 500；
    # `quantity='9'*24` 连 `int()` 都不报错（Python 整数无上限），要等写库才溢出。
    quantity, err = parse_int_param(data.get('quantity'), '下发数量', minimum=0)
    if err:
        return json_fail(err)
    if not quantity:
        return json_fail('下发数量必须大于0')

    if material.shelter_stock < quantity:
        return json_fail(f'捕捉点库存不足（当前库存 {material.shelter_stock}）')

    # 检查芯片号是否可用
    # ⚠ 必须挡非字符串 / 非列表（第三十五轮）：`{"chip_numbers": 123}` 会让
    # `Chip.objects.filter(number__in=123)` 抛 `TypeError: 'int' object is not
    # iterable` → 500；`{"chip_numbers": {"a": 1}}` 更隐蔽 —— dict 可迭代，
    # Django 会拿**字典的键**去查，不报错但语义完全错。
    chip_numbers = data.get('chip_numbers', [])
    if isinstance(chip_numbers, str):
        # 兼容逗号分隔字符串，避免被当成字符串逐字符迭代
        chip_numbers = [c.strip() for c in chip_numbers.split(',') if c.strip()]
    elif not isinstance(chip_numbers, list):
        chip_numbers = []
    if material.category == 'chip' and chip_numbers:
        unavailable = Chip.objects.filter(
            number__in=chip_numbers, status='used'
        ).count()
        if unavailable > 0:
            return json_fail(f'{unavailable} 个芯片号已被使用')

    # 兼容前端 chip_range（如 "1000010001-1000010010"）号段输入
    note = f'下发至 {hospital.name}（待签收）'
    chip_range = body_str(data, 'chip_range').strip()
    if material.category == 'chip' and chip_range:
        note += f'，芯片号段 {chip_range}'

    # 创建下发流水（捕捉点侧扣减库存，不关联医院，医院签收后才增加库存）
    try:
        txn = adjust_stock(
            material=material,
            hospital=None,
            quantity=quantity,
            txn_type='dispatch',
            operator=user,
            operator_name=user.get_full_name() or user.username,
            from_to=hospital.name,
            note=note,
            ledger_no=generate_ledger_no('DIS'),
            district_id=material.district_id,
        )
    except ValueError as e:
        return json_fail(str(e))
    # 关联目标医院到流水（用于医院端查看待签收列表），但不影响医院库存
    txn.hospital = hospital
    txn.save(update_fields=['hospital'])

    return json_ok(serialize_instance(txn), message=f'下发成功，数量 {quantity}（待医院签收后增加库存）')


@csrf_exempt
@role_required('hospital')
@login_required
def material_receive(request, pk):
    """医院确认签收下发物料

    签收后创建一条 receive 类型流水，增加医院库存。
    """
    user = request.user

    # 按「**本机构**」取单：不是发给本院的与不存在都返回同一个 404。
    # 原先裸 `objects.get(id=pk, type='dispatch')` + 之后单独判机构，
    # 会让「存在但不是本院的」返回 400「无权签收此物料」、而「不存在」返回 404
    # —— 两者可区分 = 可枚举 id 探知他院下发单是否存在（存在性预言机）。
    txn = get_own_institution_object(
        MaterialTransaction, pk, user, 'hospital_id', type='dispatch')
    if txn is None:
        return json_fail('下发记录不存在或无权访问', status=404)

    # 检查是否已签收（避免重复签收）
    already_received = MaterialTransaction.objects.filter(
        material=txn.material,
        hospital=txn.hospital,
        type='receive',
        ledger_no=txn.ledger_no,
    ).exists()
    if already_received:
        return json_fail('此物料已签收')

    # 标记原 dispatch 流水为已签收
    txn.note = (txn.note + ' [已签收]' if txn.note else '[已签收]').strip()
    txn.save(update_fields=['note'])

    # 创建签收流水（增加医院库存）
    receive_txn = MaterialTransaction.objects.create(
        type='receive',
        material=txn.material,
        material_name=txn.material_name,
        quantity=txn.quantity,
        unit=txn.unit,
        batch_no=txn.batch_no,
        supplier=txn.supplier,
        from_to=txn.from_to or '捕捉点下发',
        hospital=txn.hospital,
        operator=user,
        operator_name=user.get_full_name() or user.username,
        date=timezone.localdate(),
        ledger_no=txn.ledger_no,  # 复用原 dispatch 单号，便于关联
        district=txn.district,
        note=f'签收下发物料（原单号：{txn.ledger_no}）',
    )

    return json_ok(serialize_instance(receive_txn), message='签收成功，医院库存已增加')


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def stock_adjustment(request):
    """库存异动（过期/损坏/丢失）

    请求体示例:
    {
        "material_id": 1,
        "quantity": 5,
        "reason": "过期报废"
    }

    ⚠⚠ **`shelter` 是本轮（第三十七轮）补上的，不是笔误**。
    在它之前，捕捉点**没有任何库存异动权限** —— 而 `shelter_stock` 的过期物料
    又**不能下发**（`expiry_date` 判据会拦），于是捕捉点的过期物料成了
    「既不能报废、也不能下发」的死库存。这是**权限模型缺口**，
    与第二十一轮「暂不拦下发」的权宜之计是同一件事的两端：
    现在有了正规出口，捕捉点自己就能报废过期物料。

    安全性：`hospital is None` 分支在 `adjust_stock()` 内部已有
    `shelter_stock < quantity` 的**前置**余额校验（见 services.py 同名注释），
    所以开放角色**不会**把捕捉点库存扣成负数 —— 角色闸门与余额闸门是两道，
    缺一不可。`get_scoped_object` 仍按区县收敛，跨区县物料一律 404。
    """
    data = parse_json_body(request)
    user = request.user

    # ⚠ `material_id` 必须走共用解析器（第三十五轮）。原先直接
    # `material_id = data.get('material_id')` 后塞进 ORM 主键查询，传 `"abc"`
    # 会抛 `ValueError: Field 'id' expected a number but got 'abc'` → **500**。
    # 枚举实测：这是全项目**唯一**一处「畸形整型外键直接进 ORM」——
    # 同文件 `purchase_create` / `dispatch_create` 早就走了 `parse_int_param`，
    # 只有这里漏了。三处同族参数必须同一写法，否则就是孪生漂移。
    material_id, err = parse_int_param(data.get('material_id'), '物料ID')
    if err:
        return json_fail(err)
    if not material_id:
        return json_fail('缺少物料ID')

    # 按区县范围取物料，避免跨区县调整他人物料
    material = get_scoped_object(Material, material_id, user)
    if material is None:
        return json_fail('物料不存在或无权访问', status=404)

    # 与 `purchase_create` / `dispatch_create` 统一走共用解析器。
    # 原先这里虽然包了 `try/except (TypeError, ValueError)`，但
    # `int('9' * 24)` **不报错** —— 超大数要一路走到 `adjust_stock` 的库存
    # 比较才被挡住，判据靠下游兜底。同一个「数量」参数，三个接口三种写法
    # 就是孪生漂移，统一到共用解析器上。
    quantity, err = parse_int_param(data.get('quantity'), '异动数量', minimum=0)
    if err:
        return json_fail(err)
    if not quantity:
        return json_fail('异动数量必须大于0')

    hospital = None
    if user.role == 'hospital':
        hospital = user.institution
        if not hospital:
            return json_fail('缺少医院机构信息')
        # 医院库存由流水累加得出，adjust_stock 对医院侧不做余额校验，
        # 这里必须自己把关，否则异动可以把库存扣成负数。
        current = get_hospital_stock(material, hospital)
        if current < quantity:
            return json_fail(f'医院库存不足（当前 {current}，需 {quantity}）')

    txn = None
    try:
        txn = adjust_stock(
            material=material,
            hospital=hospital,
            quantity=quantity,
            txn_type='adjustment',
            operator=user,
            operator_name=user.get_full_name() or user.username,
            from_to=hospital.name if hospital else '捕捉点',
            note=data.get('reason', '库存异动'),
            ledger_no=generate_ledger_no('ADJ'),
            district_id=material.district_id,
        )
    except ValueError as e:
        return json_fail(str(e))

    return json_ok(serialize_instance(txn), message=f'异动登记成功，扣减 {quantity}')


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def material_transactions(request):
    """物资流水列表（台账）"""
    user = request.user
    qs = get_district_filtered_queryset(MaterialTransaction, user)

    txn_type = request.GET.get('type')
    if txn_type:
        qs = qs.filter(type=txn_type)

    # 非法值直接 400：原先裸写 `qs.filter(material_id=material_id)`，
    # `?material_id=abc` → `ValueError`；`?material_id=999…9`（超长数字）→
    # **`OverflowError`**（`int()` 自己不会报错，是 SQLite 绑定参数时才溢出）→ 500。
    material_id, err = parse_int_param(request.GET.get('material_id'), '物料')
    if err:
        return json_fail(err)
    if material_id:
        qs = qs.filter(material_id=material_id)

    if user.role == 'hospital':
        if user.institution_id:
            qs = qs.filter(hospital_id=user.institution_id)
        else:
            qs = qs.none()

    data = [serialize_instance(t) for t in qs]
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def shelter_stock_ledger(request):
    """捕捉点台账（采购+下发+异动）"""
    user = request.user
    qs = get_district_filtered_queryset(MaterialTransaction, user)

    # 捕捉点台账：所有 hospital=None 的流水 + dispatch 流水（单条 Q 组合，避免 | 破坏前置过滤）
    qs = qs.filter(Q(hospital__isnull=True) | Q(type='dispatch'))

    # 与 `material_transactions` 同款：非法物料 id 必须 400，不能是 500。
    material_id, err = parse_int_param(request.GET.get('material_id'), '物料')
    if err:
        return json_fail(err)
    if material_id:
        qs = qs.filter(material_id=material_id)

    data = []
    for txn in qs:
        item = serialize_instance(txn)
        data.append(item)
    return json_ok(data)


@csrf_exempt
@role_required('hospital', 'gov_city', 'gov_district')
@login_required
def hospital_stock_ledger(request):
    """医院台账（下发+消耗+异动）"""
    user = request.user
    qs = get_district_filtered_queryset(MaterialTransaction, user)

    if user.role == 'hospital':
        if user.institution_id:
            qs = qs.filter(hospital_id=user.institution_id)
        else:
            qs = qs.none()
    else:
        # 政府/捕捉点查看时只看有医院关联的流水
        qs = qs.filter(hospital__isnull=False)

    data = [serialize_instance(t) for t in qs]
    return json_ok(data)


# ============================================
# 辅助函数
# ============================================
def _create_chip_range(range_start, range_end, material):
    """根据芯片号段创建 Chip 记录

    支持纯数字号段（如 1000010001 ~ 1000010100）
    """
    try:
        start_num = int(range_start)
        end_num = int(range_end)
    except (ValueError, TypeError):
        return

    if end_num < start_num:
        return

    # 批量创建（跳过已存在的）
    existing = set(Chip.objects.filter(
        number__gte=range_start,
        number__lte=range_end
    ).values_list('number', flat=True))

    chips_to_create = []
    for n in range(start_num, end_num + 1):
        num_str = str(n)
        if num_str not in existing:
            chips_to_create.append(Chip(number=num_str))

    if chips_to_create:
        Chip.objects.bulk_create(chips_to_create, batch_size=200)
