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
    adjust_stock, get_hospital_stock,
    get_own_institution_object, parse_int_param,
    # 第四十五轮：市级捕捉点 + 机构库存
    can_dispatch_across_districts, get_institution_stock, get_shelter_stock,
    inactive_institution_error, is_city_shelter,
)
from core.models import Institution


# ============================================
# 第四十五轮：可下发范围 / 可操作物料
# ============================================
def dispatch_scope_district(user, material):
    """当前用户对该物料的**可下发范围**：None = 全市；否则 = 某个区县。

    ⚠ 范围由**发起方层级**决定，不是由物料区县决定（第四十五轮 Q5/Q6）：

    - **市级捕捉点** → 全市启用机构（医院 + 捕捉点）；
    - **区县级捕捉点** → 仅本区县（Q6：不得跨区县，物料流动走市级中转）；
    - 政府等其它角色 → 按物料区县（市级物料 = 全市）。

    为什么不能沿用旧的「`hospital.district_id != material.district_id`」：
    那条判据把**物料区县**当成了范围真源，于是两个方向同时错 ——
    ① 市级物料（区县 = 全市）对**任何**医院都判「跨区县」，市级捕捉点全被拦死；
    ② 市级下发给区县捕捉点的物料，区县捕捉点想再下发到**本区县**医院时，
    物料区县仍是全市，又会被判跨区县（Q4 的二级下发直接不可用）。
    """
    inst = getattr(user, 'institution', None)
    if is_city_shelter(inst):
        return None
    if inst is not None and getattr(inst, 'type', None) == 'shelter':
        return inst.district
    district = getattr(material, 'district', None)
    if district is not None and not district.is_city:
        return district
    return None


def dispatch_targets(user, material):
    """该用户对该物料**可下发到的接收机构**（医院 + 捕捉点）。"""
    district = dispatch_scope_district(user, material)
    qs = Institution.objects.filter(type__in=('hospital', 'shelter'), status='active')
    if district is not None:
        qs = qs.filter(district=district)
    # 不能下发给自己
    if getattr(user, 'institution_id', None):
        qs = qs.exclude(id=user.institution_id)
    return qs.order_by('type', 'id')


def operable_material_queryset(user):
    """该用户**可操作的**物料范围。

    = 区县范围内的物料 **∪ 在本机构有库存的物料**。

    为什么必须并上后一项（第四十五轮）：市级下发给区县级捕捉点的那批物料，
    其 `district` 是**市级**（市级采购时建的），只按区县收敛的话区县级捕捉点
    **看不到它** —— 于是「签收」「二级下发」「异动」全都无从下手，
    而 Q4 已确认区县级捕捉点**可以**二级下发。

    判据是「**我确实持有它**」（`MaterialStock` 里有本机构一行），
    不是放宽区县隔离：跨区县的物料照样看不到，除非它真的发到了我这里。
    """
    base = get_district_filtered_queryset(Material, user)
    if not getattr(user, 'institution_id', None):
        return base
    return Material.objects.filter(
        Q(pk__in=base.values('pk')) | Q(stocks__institution_id=user.institution_id)
    ).distinct()


def get_operable_material(material_id, user):
    """按可操作范围取单条物料；不在范围内返回 None（调用方回 404）。"""
    return operable_material_queryset(user).filter(pk=material_id).first()


def _receiving_institution(user, material):
    """本笔捕捉点侧业务**应该记到哪个机构的库存上**。

    - 捕捉点账号 → 自己的机构（第四十五轮的主路径）；
    - 其它角色（政府端）→ 该物料区县**恰好一个**捕捉点时用它，否则 None。

    ⚠ 返回 None 时走的是 `adjust_stock` 的**兼容路径**（只动旧的
    `shelter_stock` 字段、不写机构库存表）。政府端**没有**采购/下发的界面，
    这条路径只为历史调用保留；多个捕捉点时无法确定归属，
    宁可走兼容路径也不要替它挑一个（挑错就是把库存记到别人名下）。
    """
    inst = getattr(user, 'institution', None)
    if inst is not None and getattr(inst, 'type', None) == 'shelter':
        return inst
    shelters = Institution.objects.filter(
        type='shelter', district_id=getattr(material, 'district_id', None))
    return shelters.first() if shelters.count() == 1 else None


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def material_list(request):
    """物料列表（含机构库存 / 医院库存计算）

    第四十五轮起**机构维度**是主口径：

    - 捕捉点 / 医院账号：`institution_stock` = **本机构那一行**的库存
      （`MaterialStock`，直读一行、不做累加）；
    - 其它角色：额外给出 `hospital_stocks` 与 `shelter_stocks`
      （每个机构一份），按**可下发范围**枚举。

    ⚠ `shelter_stock`（顶层那个数）保留为**兼容字段**，语义不变
    （该物料捕捉点合计）。前端要显示「我这个点的库存」必须读
    `institution_stock` —— 同一区县有两个捕捉点时两者**不相等**。
    """
    user = request.user
    qs = operable_material_queryset(user)

    category = request.GET.get('category')
    if category:
        qs = qs.filter(category=category)

    data = []
    for m in qs:
        item = serialize_instance(m)
        # 本机构库存（捕捉点/医院都能读；政府端没有机构，跳过）
        if getattr(user, 'institution_id', None):
            item['institution_stock'] = get_institution_stock(m, user.institution)
        if user.role == 'hospital' and user.institution_id:
            item['hospital_stock'] = get_hospital_stock(m, user.institution)
        else:
            # 按**可下发范围**枚举机构（C6）：市级物料（区县 = 全市）以前
            # 按 `district_id=m.district_id` 去找医院，全市没有一家医院属于
            # 「全市（市级）」→ 列表恒为空、且不报错，看起来就像「还没有医院库存」。
            targets = list(dispatch_targets(user, m))
            hospitals = [i for i in targets if i.type == 'hospital']
            shelters = [i for i in targets if i.type == 'shelter']
            item['hospital_stocks'] = {
                str(h.id): {'name': h.name, 'stock': get_hospital_stock(m, h)}
                for h in hospitals
            }
            item['shelter_stocks'] = {
                str(s.id): {'name': s.name, 'stock': get_institution_stock(m, s)}
                for s in shelters
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
        # 按可操作范围取物料：区县内的物料 ∪ 在本机构有库存的物料
        # （第四十五轮：市级下发的物料其区县是市级，只按区县收敛会让
        #  区县级捕捉点连自己手上的货都补不了库）。
        material = get_operable_material(material_id, user)

    if material is None:
        # 前端直接以名称/类别新增物料（如采购入库表单未选择已有物料）
        name = str(body_str(data, 'name')).strip()
        category = str(body_str(data, 'category')).strip()
        if not name or category not in dict(Material.CATEGORY_CHOICES):
            return json_fail('缺少物料ID或物料名称/类别')
        shelter = user.institution if user.institution and user.institution.type == 'shelter' else None
        # ⚠ 市级捕捉点建出来的物料归属「全市（市级）」—— 它本来就是市级采购的，
        #   再由市级下发到各区县。区县端的可见性由「在本机构有库存」保证
        #   （见 `operable_material_queryset`），不靠改这里的区县。
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
    # 库存记到哪个机构（第四十五轮）：捕捉点账号 = 自己的机构。
    institution = _receiving_institution(user, material)

    # 校验全部通过，从这里开始才写库。整体包一个事务：
    # 采购要同时写「物料行 / 采购流水 / 机构库存 / 芯片号段」，
    # 中途任何异常都不该留下半截数据。
    with transaction.atomic():
        # 创建采购流水（捕捉点侧，hospital=None）
        txn = adjust_stock(
            material=material,
            hospital=None,
            quantity=quantity,
            txn_type='purchase',
            institution=institution,
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
    """下发至医院**或**捕捉点（第四十五轮起接收方不限于医院）

    请求体示例:
    {
        "material_id": 1,
        "hospital_id": 3,          // 接收方 = 医院（旧名，保留）
        "to_shelter_id": 5,        // 接收方 = 捕捉点（二选一，不可同时传）
        "quantity": 50,
        "chip_numbers": ["1000010001", "1000010002"]  // 芯片可选指定号
    }

    可下发范围由**发起方层级**决定（`dispatch_scope_district`）：
    市级捕捉点 → 全市；区县级捕捉点 → 本区县。
    """
    data = parse_json_body(request)
    user = request.user

    material_id = body_int(data, 'material_id')
    hospital_id = body_int(data, 'hospital_id') or body_int(data, 'to_hospital_id')
    shelter_id = body_int(data, 'to_shelter_id') or body_int(data, 'to_institution_id')
    if hospital_id and shelter_id:
        return json_fail('接收方只能选一个（医院或捕捉点）')
    target_id = hospital_id or shelter_id
    if not material_id or not target_id:
        return json_fail('缺少物料或医院信息')

    # 按可操作范围取物料（区县内 ∪ 在本机构有库存）
    material = get_operable_material(material_id, user)
    if material is None:
        return json_fail('物料不存在或无权访问', status=404)

    # 接收方：医院或捕捉点。两类机构**共用**同一条流转语义
    # （建单出库 → 对方签收入库），只是错误文案按实际类型给，
    # 免得对着捕捉点提示「医院不存在」。
    target = Institution.objects.filter(
        id=target_id, type__in=('hospital', 'shelter')).first()
    if target is None:
        if shelter_id:
            return json_fail('捕捉点不存在')
        return json_fail('医院不存在')
    if target.id == getattr(user, 'institution_id', None):
        return json_fail('不能下发给自己')

    # 停用的机构不能接收新的下发单（与 `transfer_create` 是同一条判据）。
    inst_err = inactive_institution_error(target, '接收机构')
    if inst_err:
        return json_fail(inst_err)

    # 可下发范围：按**发起方层级**判（第四十五轮 C2/Q5/Q6）。
    # 市级捕捉点跨区县放行；区县级捕捉点仍限本区县。
    scope = dispatch_scope_district(user, material)
    if scope is not None and target.district_id != scope.id:
        if target.type == 'shelter':
            return json_fail('只能下发到本区县捕捉点')
        return json_fail('只能下发到本区县医院')

    # 与 `purchase_create` 同一口径：非法数量必须 400，不能是 500。
    # 原先裸写 `int(data.get('quantity', 0))` —— `quantity='abc'` → `ValueError` → 500；
    # `quantity='9'*24` 连 `int()` 都不报错（Python 整数无上限），要等写库才溢出。
    quantity, err = parse_int_param(data.get('quantity'), '下发数量', minimum=0)
    if err:
        return json_fail(err)
    if not quantity:
        return json_fail('下发数量必须大于0')

    # 余额按**发起机构**判（第四十五轮）：同一区县两个捕捉点各有各的余额，
    # 拿区县合计去卡会误放行。没有机构归属（政府端历史路径）时沿用旧字段。
    sender = _receiving_institution(user, material)
    current = (get_institution_stock(material, sender) if sender is not None
               else material.shelter_stock)
    if current < quantity:
        if sender is not None:
            return json_fail(f'{sender.name}库存不足（当前 {current}，需 {quantity}）')
        return json_fail(f'捕捉点库存不足（当前库存 {current}）')

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
    # ⚠ 文案里保留「待医院签收」这个措辞（医院端界面/历史用例都在匹配它），
    #   接收方是捕捉点时才改用「待捕捉点签收」。
    target_label = '医院' if target.type == 'hospital' else '捕捉点'
    note = f'下发至 {target.name}（待{target_label}签收）'
    chip_range = body_str(data, 'chip_range').strip()
    if material.category == 'chip' and chip_range:
        note += f'，芯片号段 {chip_range}'

    # 创建下发流水：**发起机构出库**（`institution` = 发起捕捉点），
    # `hospital` 记**接收方**（第四十五轮起也可能是捕捉点）用于对方的待签收列表。
    # ⚠ 接收方签收之前**不增加**任何一方的库存 —— 否则这批货会同时算在
    #   发起方和接收方两边（「一进一出」的口径见 R45 §五第 12 条）。
    try:
        txn = adjust_stock(
            material=material,
            hospital=None,
            quantity=quantity,
            txn_type='dispatch',
            institution=sender,
            operator=user,
            operator_name=user.get_full_name() or user.username,
            from_to=target.name,
            note=note,
            ledger_no=generate_ledger_no('DIS'),
            district_id=material.district_id,
        )
    except ValueError as e:
        return json_fail(str(e))
    # 关联接收方到流水（用于对方查看待签收列表），但不影响对方库存
    txn.hospital = target
    txn.save(update_fields=['hospital'])

    return json_ok(serialize_instance(txn),
                   message=f'下发成功，数量 {quantity}（待{target_label}签收后增加库存）')


@csrf_exempt
@role_required('hospital', 'shelter')
@login_required
def material_receive(request, pk):
    """确认签收下发物料（**医院与捕捉点共用**）

    签收后创建一条 receive 类型流水，增加**本机构**的库存。

    ⚠ 第四十五轮把 `shelter` 加进角色白名单：市级下发给区县级捕捉点的
    那批货，区县级捕捉点原来**无法签收**（403）—— 货到了却入不了库，
    发起方已经扣了库存、接收方加不上，台账直接对不上（C4）。
    两类机构的签收语义**完全一致**，所以共用同一个视图，
    而不是再写一个只差一处判据的孪生接口（那正是本项目反复栽过的形态）。
    """
    user = request.user

    # 按「**本机构**」取单：不是发给本机构的与不存在都返回同一个 404。
    # 原先裸 `objects.get(id=pk, type='dispatch')` + 之后单独判机构，
    # 会让「存在但不是本机构的」返回 400「无权签收此物料」、而「不存在」返回 404
    # —— 两者可区分 = 可枚举 id 探知他机构下发单是否存在（存在性预言机）。
    # `hospital` 这一列虽然叫「医院」，但它的语义是「对方机构」，
    # 接收方是捕捉点时同样指向该捕捉点，所以判据一行都不用改。
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

    # 标记原 dispatch 流水为已签收 + 创建签收流水，整体一个事务。
    #
    # ⚠ 签收流水必须走 `adjust_stock()`，不能像原先那样直接
    #   `MaterialTransaction.objects.create()` —— 第四十五轮起库存由
    #   `MaterialStock` 那一行承载，绕过 `adjust_stock` 就等于
    #   **流水写了、库存没加**，两个真源当场对不上，而且没有任何报错
    #   （`check_data_integrity` 的「机构库存与流水合计不一致」会报出来）。
    with transaction.atomic():
        txn.note = (txn.note + ' [已签收]' if txn.note else '[已签收]').strip()
        txn.save(update_fields=['note'])

        receive_txn = adjust_stock(
            material=txn.material,
            hospital=txn.hospital,
            quantity=txn.quantity,
            txn_type='receive',
            institution=user.institution,
            operator=user,
            operator_name=user.get_full_name() or user.username,
            from_to=txn.from_to or '捕捉点下发',
            batch_no=txn.batch_no,
            supplier=txn.supplier,
            ledger_no=txn.ledger_no,  # 复用原 dispatch 单号，便于关联
            # 归属区县按**接收机构**推导，不沿用发起方的（第四十五轮起
            # 跨区县下发是允许的）：甲区发到乙区医院，这条签收记录发生在
            # 乙区，沿用甲区会让乙区政府的台账里看不到本院入库。
            district=user.institution.district,
            note=f'签收下发物料（原单号：{txn.ledger_no}）',
        )

    return json_ok(serialize_instance(receive_txn),
                   message=f'签收成功，{user.institution.name}库存已增加')


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

    安全性：捕捉点侧的余额校验在 `adjust_stock()` 内部**前置**完成
    （第四十五轮起按**该机构**的库存判，见 services.py 同名注释），
    所以开放角色**不会**把捕捉点库存扣成负数 —— 角色闸门与余额闸门是两道，
    缺一不可。`operable_material_queryset` 仍按区县收敛（∪ 本机构有库存的物料），
    跨区县且与本机构无关的物料一律 404。
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

    # 按可操作范围取物料（区县内 ∪ 在本机构有库存），避免跨区县调整他人物料
    material = get_operable_material(material_id, user)
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

    # 库存归属机构（第四十五轮）：医院 = 本院；捕捉点 = 本捕捉点。
    institution = getattr(user, 'institution', None)
    hospital = None
    if user.role == 'hospital':
        hospital = institution
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
            institution=institution,
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


def _scoped_txn_queryset(user):
    """物资流水的可见范围。

    机构账号（医院 / 捕捉点）：**区县范围内 ∪ 与本机构相关**的流水。
    ⚠ 那一半「与本机构相关」不能省（第四十五轮）：跨区县下发的那条 dispatch
    归属区县是**发起方**的（市级），接收方按自己的区县过滤会**看不到
    发给自己的单** —— 于是「待签收」恒为空、货到了签不了收，
    而界面上只显示「暂无数据」，没有任何报错（C4 的另一面）。

    ⚠ 也不能反过来写成「**只**看本机构」（第一版就是这么写的，被回归抓到）：
    改造前的历史行 `institution` 为空（`dispatch` 行无法回填发起方，
    见 `0020` 迁移），只按机构收口会让这些行在捕捉点台账里**凭空消失**。
    所以是**并集**，不是替换 —— 可见面只增不减。

    政府 / 平台账号：沿用区县收敛（`get_district_filtered_queryset`）。
    """
    base = get_district_filtered_queryset(MaterialTransaction, user)
    inst_id = getattr(user, 'institution_id', None)
    if user.role not in ('hospital', 'shelter') or not inst_id:
        return base
    # 单条 Q 组合，不要写成 `qs.filter(a) | qs.filter(b)`
    # （那会丢前置过滤并产生重复行，见项目约定）。
    own = Q(hospital_id=inst_id) | Q(institution_id=inst_id)
    if user.role == 'hospital':
        # 医院：本院相关的（`base` 上再收口，范围不比改造前宽）
        return base.filter(own).distinct()
    # 捕捉点：本区县台账（含无法回填归属的历史行）∪ 与本机构相关的跨区县流水
    return MaterialTransaction.objects.filter(
        Q(pk__in=base.values('pk')) | own).distinct()


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def material_transactions(request):
    """物资流水列表（台账）

    第四十五轮起**捕捉点也能读**：市级下发给区县级捕捉点的待签收单
    要靠它列出来（原先 `shelter` 也能调，但 `hospital` 那一列对捕捉点
    是空的，且按区县过滤会把跨区县的下发单滤掉）。
    """
    user = request.user
    qs = _scoped_txn_queryset(user)

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

    data = [serialize_instance(t) for t in qs]
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def shelter_stock_ledger(request):
    """捕捉点台账（采购 + 下发 + 签收 + 异动）

    口径从「`hospital` 为空 or 类型是 dispatch」改成
    **「库存归属机构是捕捉点」**（第四十五轮）：旧口径漏掉了
    「捕捉点签收市级下发」这一类流水（`hospital` 指向接收捕捉点、
    类型是 receive，两条都不满足），于是捕捉点的入库在自己台账里
    **一行都看不到**。

    ⚠ 兼容历史行：`institution` 为空的老数据仍按旧口径兜住
    （`hospital` 为空且不是 receive），否则改造前的台账会凭空少行。
    """
    user = request.user
    qs = _scoped_txn_queryset(user)

    qs = qs.filter(
        Q(institution__type='shelter')               # 新口径：动的是捕捉点库存
        | Q(institution__isnull=True, hospital__isnull=True)  # 历史行：无归属无对方
        | Q(institution__isnull=True, type='dispatch')        # 历史行：下发（发起方未知）
    )

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
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def hospital_stock_ledger(request):
    """医院台账（下发 + 签收 + 消耗 + 异动）

    第四十五轮起排除「发给捕捉点」的下发单（`hospital` 指向捕捉点）——
    它不属于医院台账。判据用**机构类型**而不是名字，所以
    「对方是医院」与「归属机构是医院」两类都收得住，历史行
    （`institution` 为空、`hospital` 是医院）也不会漏。
    """
    user = request.user
    qs = _scoped_txn_queryset(user)

    # 与医院有关的流水 = 「对方机构是医院」或「库存归属机构是医院」。
    # 单条 Q 组合；用**机构类型**判而不是机构名字，历史行
    # （`institution` 为空、`hospital` 是医院）也不会漏。
    qs = qs.filter(Q(hospital__type='hospital') | Q(institution__type='hospital'))

    data = [serialize_instance(t) for t in qs]
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'gov_city', 'gov_district')
@login_required
def dispatch_target_options(request):
    """**可下发的接收机构**列表（供前端下拉用）。

    为什么要有这个接口（第四十五轮 C8）：下拉以前是前端自己拼的
    （列出全市医院），而服务端只允许本区县 —— 用户**选了就撞 400**，
    且下拉里根本看不出哪个能选。把「能选哪些」收敛到**服务端同一份判据**
    （`dispatch_targets()`，与 `dispatch_create` 共用），
    前端照单渲染，就不可能再出现「选得到、发不出去」。

    `?material_id=` 可选：不给时按「全市 / 本区县」给出上限集合。
    """
    user = request.user
    # 与其它查询接口同款：非法值必须 400，不能是 500（三态语义，见项目约定）
    material_id, err = parse_int_param(request.GET.get('material_id'), '物料')
    if err:
        return json_fail(err)
    material = get_operable_material(material_id, user) if material_id else None

    targets = dispatch_targets(user, material)
    data = [{
        'id': t.id,
        'name': t.name,
        'type': t.type,
        'type_label': t.get_type_display(),
        'district_id': t.district_id,
        'district_name': t.district.name,
    } for t in targets.select_related('district')]
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
