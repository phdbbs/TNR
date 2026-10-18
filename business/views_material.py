"""
Task 7: 物料供应链与双台账
- 物料列表/采购/下发/签收/异动
- 捕捉点台账 / 医院台账
- 芯片：按「首号 + 数量」逐号入库 / 下发（第四十六轮重构）
"""
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Material, MaterialBatch, MaterialTransaction
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_str, body_int,
    generate_doc_no, get_district_filtered_queryset,
    adjust_stock, get_hospital_stock,
    get_own_institution_object, parse_int_param, parse_signed_int_param,
    # 第四十五轮：市级捕捉点 + 机构库存
    can_dispatch_across_districts, get_institution_stock, get_shelter_stock,
    inactive_institution_error, is_city_shelter,
    # B5：物料批次
    batch_expiry_state, fefo_sorted, get_material_batches,
    plan_fefo_deduction, NEAR_EXPIRY_DAYS,
    # 出库（报废 / 退回 / 调拨 / 盘点）
    validate_outbound_reason,
)
from business.services_chip import (
    chip_batch_create, chip_batch_dispatch, chip_batch_precheck,
    chip_batch_receive, chip_batch_void, parse_chip_numbers,
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
        "expiry_date": "2025-12-31"
    }

    **芯片**（`category='chip'`）另需「首号」——数量就是 `quantity`，
    号码按数值 +1 递增、保持位数补零，逐个建 `Chip` 行：

    {
        "material_id": 4,
        "quantity": 500,
        "chip_start": "042060107000177"   // 这批的第一个号码
    }

    强绑定：号码个数必须等于 `quantity`（`parse_chip_numbers` 直接报错，
    不存在「号码 30 个、数量填 100」这种各记各的情况）。
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
    new_material = None
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
            # ⚠ 新建物料**必须等进采购事务再建**：芯片重号等失败会让整笔
            #   回滚；在事务外先建的话，autocommit 已经提交，失败后留下的
            #   是一个从未入过库的孤儿物料行。
            new_material = dict(
                name=name,
                category=category,
                unit=str(body_str(data, 'unit') or '支'),
                specification=body_str(data, 'specification'),
                supplier=body_str(data, 'supplier'),
                batch_no=body_str(data, 'batch_no'),
                safety_stock=0,
                district_id=district_id,
            )

    # 新建路径下 material 还是 None —— 事务前的判断一律用待建属性。
    is_chip = ((material.category if material is not None
                else new_material['category']) == 'chip')
    if material is not None:
        district_id = material.district_id

    # 芯片采购：号码**必须在写库之前全部校验完**（项目约定「多表写入两段式：
    # 先全校验 → 再事务落库」）。
    #
    # ⚠ 这一步原来藏在下面的事务里、且是**静默失败**的：`_create_chip_range()`
    #   遇到非数字号段直接 `return`，调用方照样回「采购入库成功，增加库存 100」
    #   —— 库存加了 100，芯片一颗都没建，全程没有任何提示。
    #   现在非数字号段是 400 + 具体文案，且校验前置。
    #
    # 兼容期：`chip_range_start` 是重构前（第四十六轮）的参数名，字段已删除，
    # 但前端在阶段 8 之前仍可能传它，故一并读入。阶段 8 之后可只留 `chip_start`。
    chip_first_number = ''
    if is_chip:
        chip_first_number = (body_str(data, 'chip_start')
                             or body_str(data, 'chip_range_start'))
        try:
            # 数量就是这批芯片的个数 —— 「强绑定」：不存在号码 30 个、
            # 数量填 100 这种各记各的情况（`parse_chip_numbers` 直接报错）。
            parse_chip_numbers(chip_first_number, quantity)
        except ValueError as e:
            return json_fail(str(e))

    # 效期在事务**外**解析一次：批次行（adjust_stock）与主表快照用同一个值，
    # 两处各解析一遍就会出现「批次 10 月到期、主表 11 月」的漂移。
    # 解析失败静默当 None（与主表快照的既有容忍口径一致），不拦采购。
    expiry_date = None
    if data.get('expiry_date'):
        try:
            expiry_date = datetime.strptime(str(data['expiry_date'])[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            expiry_date = None

    # 校验全部通过，从这里开始才写库。整体包一个事务：
    # 采购要同时写「物料行 / 采购流水 / 机构库存 / 批次行 / 芯片逐号行」，
    # 中途任何异常都不该留下半截数据。
    #
    # ⚠ `try` 是给 `chip_batch_create()` 的重号 `ValueError` 兜底的：
    #   下发（`dispatch_create`）和异动（`stock_adjustment`）都有这层
    #   （异常转 400 + 具体号码文案），唯独采购漏了 —— 重号直接打成 500，
    #   用户只看到「服务器错误」，拿不到「芯片号已存在：xxx」。
    try:
        with transaction.atomic():
            # 新建物料放进事务里（见上面 `new_material` 的注释），
            # 之后所有分支用的都是建好的 material。
            if new_material is not None:
                material = Material.objects.create(**new_material)
            # 库存记到哪个机构（第四十五轮）：捕捉点账号 = 自己的机构。
            # 必须在 material 建好后才能算（依赖物料的区县）。
            institution = _receiving_institution(user, material)

            # 创建采购流水（捕捉点侧，hospital=None）。
            # B5 起 adjust_stock 返回**列表**（出库跨批次会拆多笔）；
            # 采购是单笔入库，取 txns[0]。
            txns = adjust_stock(
                material=material,
                hospital=None,
                quantity=quantity,
                txn_type='purchase',
                institution=institution,
                operator=user,
                operator_name=user.get_full_name() or user.username,
                supplier=body_str(data, 'supplier'),
                batch_no=body_str(data, 'batch_no'),
                expiry_date=expiry_date,
                from_to=body_str(data, 'supplier'),
                note=body_str(data, 'note', '采购入库'),
                ledger_no=generate_doc_no('PUR'),
                district_id=district_id,
            )
            txn = txns[0]

            # 更新物料扩展信息（主表 batch_no/expiry_date 保留为
            # 「最近一次采购」的快照 —— 旧界面仍显示它；真正的批次台账
            # 在 MaterialBatch，B5 起不要再用主表单值做任何判据）。
            update_fields = []
            if data.get('supplier'):
                material.supplier = data['supplier']
                update_fields.append('supplier')
            if data.get('batch_no'):
                material.batch_no = data['batch_no']
                update_fields.append('batch_no')
            if expiry_date is not None:
                material.expiry_date = expiry_date
                update_fields.append('expiry_date')
            if update_fields:
                material.save(update_fields=update_fields)

            # 芯片采购：按「首号 + 数量」逐号建行，归属本机构与本物料。
            # `batch_no` 记采购单号 —— 用于回答「这颗芯片是哪一批进来的」。
            if is_chip:
                chip_batch_create(
                    material=material,
                    institution=institution,
                    first_number=chip_first_number,
                    count=quantity,
                    batch_no=txn.ledger_no,
                )
    except ValueError as e:
        return json_fail(str(e))

    chip_note = f'，芯片 {quantity} 颗（{chip_first_number} 起）' if material.category == 'chip' else ''
    return json_ok(serialize_instance(txn),
                   message=f'采购入库成功，增加库存 {quantity}{chip_note}')


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
        "quantity": 50
    }

    **芯片**（`category='chip'`）另需「首号」——数量就是 `quantity`。
    这批号码必须**属于本机构、属于该物料、且状态为可用**，否则整个下发被拒：

    {
        "material_id": 4,
        "to_hospital_id": 3,
        "quantity": 50,
        "chip_start": "042060107000177"
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

    # 芯片下发：号码必须在**扣库存之前**校验完（只读预检）。
    #
    # ⚠ 重构前这里是「可选指定号」的 `chip_numbers` 列表 + `chip_range` 号段
    #   字符串两条路，两条路**都只是往备注里拼一句话**（`note += f'，芯片号段 …'`），
    #   芯片的归属压根没动 —— 下发了 100 颗，发起方库存扣了 100，
    #   而这 100 颗芯片在系统里仍然挂在发起方名下、状态还是「可用」，
    #   于是同一个人可以把它再下发一次。现在号码是真的逐颗转移了。
    chip_first_number = ''
    if material.category == 'chip':
        # 兼容期：`chip_range_start` 是重构前的参数名（字段已删除），
        # 前端在阶段 8 之前仍可能传它。
        chip_first_number = (body_str(data, 'chip_start')
                             or body_str(data, 'chip_range_start'))
        try:
            chip_batch_precheck(material, sender, chip_first_number, quantity)
        except ValueError as e:
            return json_fail(str(e))

    # ⚠ 文案里保留「待医院签收」这个措辞（医院端界面/历史用例都在匹配它），
    #   接收方是捕捉点时才改用「待捕捉点签收」。
    target_label = '医院' if target.type == 'hospital' else '捕捉点'
    note = f'下发至 {target.name}（待{target_label}签收）'
    if material.category == 'chip':
        note += f'，芯片 {quantity} 颗（{chip_first_number} 起）'

    # 创建下发流水：**发起机构出库**（`institution` = 发起捕捉点），
    # `hospital` 记**接收方**（第四十五轮起也可能是捕捉点）用于对方的待签收列表。
    # ⚠ 接收方签收之前**不增加**任何一方的库存 —— 否则这批货会同时算在
    #   发起方和接收方两边（「一进一出」的口径见 R45 §五第 12 条）。
    #
    # ⚠ 第四十六轮把这三步（建流水 / 记接收方 / 芯片标在途）包进同一个事务：
    #   芯片标在途失败时，扣掉的库存必须一起回滚，否则「库存少了、芯片还在」
    #   两个真源当场错开且不报错。
    #
    # B5：跨批次的下发在 `adjust_stock` 里按 FEFO 拆成**多笔**流水
    # （同 ledger_no，每笔一行批次）—— 签收按单号整单走（见 `material_receive`），
    # 芯片关联与返回给前端的都是首笔 txns[0]。
    try:
        with transaction.atomic():
            txns = adjust_stock(
                material=material,
                hospital=target,  # 对方机构（医院或捕捉点），直接随流水建
                quantity=quantity,
                txn_type='dispatch',
                institution=sender,
                operator=user,
                operator_name=user.get_full_name() or user.username,
                from_to=target.name,
                note=note,
                ledger_no=generate_doc_no('DIS'),
                district_id=material.district_id,
            )
            txn = txns[0]

            if material.category == 'chip':
                chip_batch_dispatch(material, sender, chip_first_number, quantity, txn)
    except ValueError as e:
        return json_fail(str(e))

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

    # 检查是否已签收（避免重复签收）。
    # ⚠ 关联判据用 `ref_no`：接收单自第四十六轮起有**独立**的 RCV 单号，
    #   与下发单的关联靠 `ref_no` 指向下发单号 —— 旧实现「复用下发单号」
    #   两张单无法区分，也不能单独打印归档。
    #
    # ⚠ B5 起这个判据是「按**下发单号**整单判」：跨批次下发的单子在物理上是
    #   多笔流水（同 ledger_no，每笔一行批次），若按「本笔」签收，第一笔的
    #   receive 建出来之后兄弟行会被误判成已签收（货只到了一半）。
    #   整单签收 = 签任何一笔都把全部兄弟行一起收（见下面的事务块）。
    already_received = MaterialTransaction.objects.filter(
        material=txn.material,
        hospital=txn.hospital,
        type='receive',
        ref_no=txn.ledger_no,
    ).exists()
    if already_received:
        return json_fail('此物料已签收')

    # 同一下发单的全部兄弟行（B5 跨批次拆行；单批次/历史单 = 只有 txn 自己）。
    siblings = list(MaterialTransaction.objects.filter(
        ledger_no=txn.ledger_no, type='dispatch').order_by('id'))
    if txn not in siblings:  # 理论不可达（txn 本身就是 dispatch 行），防御性兜底
        siblings.insert(0, txn)

    # 标记原 dispatch 流水为已签收 + 创建签收流水，整体一个事务。
    #
    # ⚠ 签收流水必须走 `adjust_stock()`，不能像原先那样直接
    #   `MaterialTransaction.objects.create()` —— 第四十五轮起库存由
    #   `MaterialStock` 那一行承载，绕过 `adjust_stock` 就等于
    #   **流水写了、库存没加**，两个真源当场对不上，而且没有任何报错
    #   （`check_data_integrity` 的「机构库存与流水合计不一致」会报出来）。
    # ⚠ 整块包 try：`chip_batch_receive()` 在「在途芯片数与下发数量不一致」时
    #   会抛 `ValueError`（见那里的注释）。不接住的话它会打成 500，
    #   用户只看到「服务器错误」，而真正的原因（这批芯片被人为改过）
    #   完全说不出口。接住后 400 + 具体文案，且事务照旧回滚。
    #
    # B5：每笔兄弟行各建一条 receive（同 RCV 单号、ref_no 同指下发单号），
    # 批号随行、效期取**发起方批次表**里该批号的效期 —— 接收方的批次行
    # 因此带着正确效期诞生，FEFO/过期拦截在接收侧从第一笔起就是对的。
    receive_ledger_no = generate_doc_no('RCV')
    total_qty = sum(s.quantity for s in siblings)
    try:
        with transaction.atomic():
            for s in siblings:
                s.note = (s.note + ' [已签收]' if s.note else '[已签收]').strip()
                s.save(update_fields=['note'])

            receive_txns = []
            for s in siblings:
                # 发起方该批号的批次行 → 效期随货带走（没有批次行 = 存量单，
                # 效期给 None，不拿主表快照猜 —— 快照是「最近一次采购」的，
                # 对老批次恰恰是错的）。
                sender_batch = MaterialBatch.objects.filter(
                    material=s.material, batch_no=s.batch_no,
                    institution=s.institution).first()
                receive_txns.append(adjust_stock(
                    material=s.material,
                    hospital=s.hospital,
                    quantity=s.quantity,
                    txn_type='receive',
                    institution=user.institution,
                    operator=user,
                    operator_name=user.get_full_name() or user.username,
                    from_to=s.from_to or '捕捉点下发',
                    batch_no=s.batch_no,
                    expiry_date=getattr(sender_batch, 'expiry_date', None),
                    supplier=s.supplier,
                    ledger_no=receive_ledger_no,  # 同一张接收单，多笔共用单号
                    ref_no=s.ledger_no,  # 关联下发单号（单据链：DIS → RCV）
                    # 归属区县按**接收机构**推导，不沿用发起方的（第四十五轮起
                    # 跨区县下发是允许的）：甲区发到乙区医院，这条签收记录发生在
                    # 乙区，沿用甲区会让乙区政府的台账里看不到本院入库。
                    district=user.institution.district,
                    note=f'签收下发物料（原单号：{s.ledger_no}）',
                )[0])

            # 芯片签收：把这批**在途**芯片转到本机构名下、状态回到「可用」。
            # ⚠ 必须与库存写入同一个事务 —— 库存加了、芯片没转（或反过来），
            #   「芯片数」与「库存数」当场错开，且没有任何报错。
            # ⚠ 数量与下发单不一致时 `chip_batch_receive()` 会抛错拒绝签收：
            #   照单全收会让错开被「合法化」，事后无从追。
            #   芯片走单行流水（`adjust_stock` 对 chip 不拆行），首笔即全部。
            if txn.material.category == 'chip':
                chip_batch_receive(txn, user.institution)
    except ValueError as e:
        return json_fail(str(e))

    return json_ok(serialize_instance(receive_txns[0]),
                   message=f'签收成功，{user.institution.name}库存已增加 {total_qty}')


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

    **芯片**另需「首号」（与入库 / 下发同一口径）—— 数量就是 `quantity`，
    这批号码必须**属于本机构且为可用状态**，作废后状态变 `void`
    （号码保留在表里，「这颗哪去了」要能答）：

    {
        "material_id": 4,
        "quantity": 2,
        "reason": "芯片损坏报废",
        "chip_start": "042060107000177"
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

    # 芯片异动：号码必须在**扣库存的同一步**里作废。
    #
    # ⚠ 第四十六轮补上的洞：库存异动会让 `MaterialStock` 减掉 N，而芯片行
    #   原来**不动** —— 「可用芯片数」于是比库存多出 N，且**没有任何报错**。
    #   芯片「按号码单独管理」的前提就是：每一次数量变化都有对应的号码变化。
    #   兼容期读入旧参数名 `chip_range_start`（阶段 8 之后可去掉）。
    chip_first_number = ''
    if material.category == 'chip':
        chip_first_number = (body_str(data, 'chip_start')
                             or body_str(data, 'chip_range_start'))

    txn = None
    # 整块包事务 + try：`chip_batch_void()` 在号码不属于本机构 / 状态不对时
    # 会抛 `ValueError`，异常传播出去整块回滚（不会留下「库存扣了、芯片没动」
    # 的半截状态），再在外层转成 400 + 具体文案。
    # ⚠ 不要写成「在事务里 `return json_fail()`」—— 那是**正常返回**，
    #   事务会**提交**，半截状态就留在库里了。
    try:
        with transaction.atomic():
            # B5：adjust_stock 返回列表（非芯片跨批次拆多笔），异动取首笔返回；
            # 芯片（首号作废）走单行流水，数量即全部。
            txns = adjust_stock(
                material=material,
                hospital=hospital,
                quantity=quantity,
                institution=institution,
                txn_type='adjustment',
                operator=user,
                operator_name=user.get_full_name() or user.username,
                from_to=hospital.name if hospital else '捕捉点',
                note=data.get('reason', '库存异动'),
                ledger_no=generate_doc_no('ADJ'),
                district_id=material.district_id,
            )
            txn = txns[0]
            if material.category == 'chip':
                chip_batch_void(material, institution, chip_first_number, quantity)
    except ValueError as e:
        return json_fail(str(e))

    chip_note = (f'，芯片 {quantity} 颗（{chip_first_number} 起）已作废'
                 if material.category == 'chip' else '')
    return json_ok(serialize_instance(txn),
                   message=f'异动登记成功，扣减 {quantity}{chip_note}')


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def material_outbound(request):
    """物料出库（报废 / 退回 / 调拨 / 盘点；**数量可负**）。

    请求体示例::

        {
            "material_id": 1,
            "quantity": 5,
            "batch_no": "B2",        # 可选，定向扣减；不填走 FEFO
            "reason": "scrap",       # 必填：scrap/return/transfer/inventory
            "note": "外包装破损报废"
        }

    **为什么不能复用 `stock_adjustment`**
    -----------------------------------
    库存异动是「纠正账实差异」：数量只能正、原因是一句自由文本、
    不选批号。出库是「货物真的离开仓库」：磊哥要求选批号、原因必须
    是报废/退回/调拨/盘点四个枚举之一、而且**数量可以为负**
    （负 = 实际入库或盘盈）。把三件事塞进异动接口 = 同一个接口四种语义，
    改一处牵全身；单独一类流水（`outbound`）才能让台账按原因汇总。

    **负数量的语义**：`-3` = 盘盈 / 退回，**加**库存。此时：
    - 不判余额（`adjust_stock` 里 `quantity > 0` 才判，见那里的注释）；
    - 批次方向反过来，记入 `batch_no` 指定的那一批（没有指定则退 FEFO 最前排）；
    - `reason` 仍记申报的那一个 —— 「为什么这一笔是往回流」靠它回答。

    **芯片**同样走 `chip_batch_void`（号码是真源），且**不认批号**：
    芯片逐号管理，出库凭的是「首号 + 数量」，批号只是流水上的快照字段。

    判据分两道，缺一不可：
    - 余额判据：捕捉点侧在 `adjust_stock` 内部前置；医院侧**自己把关**
      （与 `stock_adjustment` 同口径，理由见那边的长注释）；
    - 批号判据：给了 `batch_no` 就只扣那一批，余量不足直接 400，
      **不改扣别的批次**（扣错批 = 台账批号与实际出库的货对不上，
      而这个错不会报任何警）。
    """
    data = parse_json_body(request)
    user = request.user

    # 只用 POST。没有这道闸时 GET 会走到「解析请求体 → 拿到空 dict →
    # 缺物料ID」= 400，虽然也没写库，但错误文案说的是参数问题，
    # 而真正的问题是「你用错方法了」—— 两件事不能混。
    if request.method != 'POST':
        return json_fail('仅支持 POST', status=405)

    material_id, err = parse_int_param(data.get('material_id'), '物料ID')
    if err:
        return json_fail(err)
    if not material_id:
        return json_fail('缺少物料ID')

    material = get_operable_material(material_id, user)
    if material is None:
        return json_fail('物料不存在或无权访问', status=404)

    # 数量：**可负**，所以不能走 `parse_int_param`（`_INT_RE` 不允许前导 '-'）。
    # 0 必须拒 —— 出 0 件既不改变库存也不改变批次，却会生成一条
    # 看得见摸不着的流水和一张单据号。
    quantity, err = parse_signed_int_param(data.get('quantity'), '出库数量')
    if err:
        return json_fail(err)
    if quantity is None:
        return json_fail('缺少出库数量')
    if quantity == 0:
        return json_fail('出库数量不能为0')

    # 出库原因：四选一，**必填**。自由文本会让「报废 / 退回」这类
    # 汇总统计永远对不上（同一个原因 N 种写法）。
    reason, err = validate_outbound_reason(data.get('reason'))
    if err:
        return json_fail(err)

    # 定向批号（可选）。**芯片不认**：芯片凭号码出库，批号只是快照，
    # 拿批号去扣批次行会让「Σ批次 == 库存」这个等式在芯片上失效。
    batch_no = (body_str(data, 'batch_no') or '').strip()
    if material.category == 'chip':
        batch_no = ''

    institution = getattr(user, 'institution', None)
    hospital = None
    if user.role == 'hospital':
        hospital = institution
        if not hospital:
            return json_fail('缺少医院机构信息')
        # 与 `stock_adjustment` 同一道闸：`adjust_stock` 对医院侧不做余额校验。
        # 只在**正数**（真的出库）时判；负数是盘盈入库，天然不该被拦。
        if quantity > 0:
            current = get_hospital_stock(material, hospital)
            if current < quantity:
                return json_fail(f'医院库存不足（当前 {current}，需 {quantity}）')

    chip_first_number = ''
    if material.category == 'chip':
        chip_first_number = body_str(data, 'chip_start')

    from_to = hospital.name if hospital else (
        institution.name if institution else '')
    try:
        with transaction.atomic():
            txns = adjust_stock(
                material=material,
                hospital=hospital,
                quantity=quantity,
                institution=institution,
                txn_type='outbound',
                operator=user,
                operator_name=user.get_full_name() or user.username,
                from_to=from_to,
                batch_no=batch_no,
                reason=reason,
                note=body_str(data, 'note') or '物料出库',
                ledger_no=generate_doc_no('OUT'),
                district_id=material.district_id,
            )
            txn = txns[0]
            if material.category == 'chip':
                chip_batch_void(material, institution, chip_first_number, quantity)
    except ValueError as e:
        return json_fail(str(e))

    reason_label = dict(MaterialTransaction.OUTBOUND_REASON_CHOICES).get(reason, reason)
    direction = '出库扣减' if quantity > 0 else '盘盈/退回归账'
    chip_note = (f'，芯片 {quantity} 颗（{chip_first_number} 起）已作废'
                 if material.category == 'chip' else '')
    return json_ok(serialize_instance(txn),
                   message=f'出库登记成功（{reason_label}），{direction} '
                           f'{abs(quantity)}{chip_note}')


def _scoped_txn_queryset(user):
    """物资流水的可见范围。

    **医院**：只看**本院相关**的流水（`hospital_id` / `institution_id` 命中本院）。
    ⚠ 不能用 `get_district_filtered_queryset`（区县）来收口：同一区县可能有多家
    医院（襄城区就有爱心 / 瑞鹏），按区县会让 A 院读到 B 院的采购与库存流水。
    `test_material_views.LedgerViewsTest` 把这条锁死。

    **捕捉点**：本区县台账 ∪ 与本机构相关的流水。
    ⚠ 那一半「与本机构相关」不能省（第四十五轮）：跨区县下发的那条 dispatch
    归属区县是**发起方**的（市级），接收方按自己的区县过滤会**看不到
    发给自己的单** —— 于是「待签收」恒为空、货到了签不了收，
    而界面上只显示「暂无数据」，没有任何报错（C4 的另一面）。

    ⚠ 也不能反过来写成「**只**看本机构」（第一版就是这么写的，被回归抓到）：
    改造前的历史行 `institution` 为空（`dispatch` 行无法回填发起方，
    见 `0020` 迁移），只按机构收口会让这些行在捕捉点台账里**凭空消失**。
    所以捕捉点这一支是**并集**，不是替换 —— 可见面只增不减。

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
        # 医院：**只**看本院相关的流水。
        # ⚠ 不能写成 `base.filter(own)`（区县 ∩ 本院，实测的缺陷）：市级捕捉点
        # 给区县医院下发的 dispatch，归属区县是**发起方**的（市级），按医院的
        # 区县过滤会先把它滤掉 —— 「本院相关」那一半救不回来，于是
        # 「待接收物料」恒空、货到了签不了收，界面上只显示「暂无数据」（C4 同族）。
        # ⚠ 也不能写成「本区县 ∪ 本院」（同区县别院的流水会被读出来，回归抓到）：
        # 跨区县那一半由 `hospital_id=本院` 覆盖，**不需要**区县兜底 ——
        # 医院的每一行流水都必然挂在本院名下（采购 / 接收 / 消耗 / 异动）。
        return MaterialTransaction.objects.filter(own).distinct()
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
# ⚠ 第四十六轮删除了 `_create_chip_range()` —— 它是芯片双轨的源头：
#   - 号段解析失败时**静默 `return`**，调用方照样回「采购入库成功」，
#     库存加了、芯片一颗没建，全程无提示；
#   - 建出的 `Chip` 行**没有机构归属**，查不出「这批在谁手上」。
#   现在芯片一律走 `services_chip.chip_batch_create()`（「首号 + 数量」）。


# ============================================
# 物料批次（B5，只读）
# ============================================
def _batch_institution(request, material):
    """批次接口的「本机构」判定。

    捕捉点 / 医院账号 → 自己的机构（批次明细是「我这个点的库存构成」）；
    政府端没有自己的库存 → 该物料在**全部机构**上的批次按机构分组返回
    （监管视角：每批货在谁手上、还剩多少）。
    """
    inst = getattr(request.user, 'institution', None)
    if inst is not None and getattr(inst, 'type', None) in ('shelter', 'hospital'):
        return inst
    return None


def _serialize_batch(batch, today):
    """批次行的 JSON 形态。临期/过期标记在后端算好 —— 前端只管标红，
    不再各写一份「效期减几天」的判据（判据漂移是本项目反复栽的坑）。"""
    expired = bool(batch.expiry_date and batch.expiry_date < today)
    near_expiry = bool(
        not expired and batch.expiry_date
        and batch.expiry_date <= today + timedelta(days=NEAR_EXPIRY_DAYS))
    return {
        'id': batch.id,
        'batch_no': batch.batch_no,
        'expiry_date': batch.expiry_date.isoformat() if batch.expiry_date else None,
        'quantity': batch.quantity,
        'expired': expired,
        'near_expiry': near_expiry,
        'created_at': batch.created_at.isoformat(),
    }


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def material_batches(request, pk):
    """某物料的批次明细（GET）。

    - 捕捉点 / 医院账号：只返回**本机构**的批次（FEFO 顺序，含余量为 0 的
      批次档案）；
    - 政府端：返回**全部机构**的批次，按机构分组 —— 监管视角要回答的是
      「这批货发到了谁手上、还剩多少」。

    权限：物料必须在 `operable_material_queryset` 范围内（与签收 / 异动
    同一把尺子），跨区县的物料不因「知道 id」就能看到批次构成。
    """
    material = get_operable_material(pk, request.user)
    if material is None:
        return json_fail('物料不存在或无权操作', status=404)

    today = timezone.localdate()
    inst = _batch_institution(request, material)
    if inst is not None:
        batches = get_material_batches(material, inst)
        return json_ok({
            'institution_id': inst.id,
            'institution_name': inst.name,
            'batches': [_serialize_batch(b, today) for b in batches],
        })

    # 政府端：全部持有机构的批次，按机构分组（FEFO 顺序）
    groups = []
    qs = MaterialBatch.objects.filter(
        material=material, institution__status='active'
    ).select_related('institution').order_by('institution__id')
    by_inst = {}
    for b in qs:
        by_inst.setdefault(b.institution, []).append(b)
    for inst_obj, rows in by_inst.items():
        groups.append({
            'institution_id': inst_obj.id,
            'institution_name': inst_obj.name,
            'institution_type': inst_obj.type,
            'batches': [_serialize_batch(b, today) for b in fefo_sorted(rows)],
        })
    return json_ok({'institution_id': None, 'institution_name': None, 'groups': groups})


@csrf_exempt
@role_required('shelter', 'hospital')
@login_required
def material_batch_preview(request, pk):
    """FEFO 扣减预览（POST，**只读不写库** —— 与芯片 `chip_preview` 同款）。

    请求体：`{"quantity": 30, "txn_type": "consume|dispatch|adjustment|outbound",
    "batch_no": "B2（可选）"}`
    返回：`{"plan": [{batch_no, expiry_date, take}...], "available": N,
    "expired_skipped": N, "enough": true|false, "message": "..."}`

    为什么 txn_type 必传：consume 会**跳过已过期批次**（过期药品不能用于
    诊疗），dispatch / adjustment / outbound 不跳（下发 / 报废 / 出库报废
    正是过期物料的出口）—— 不告诉服务端「这是哪类操作」，预览给出的批次
    顺序与实际落库就会错开，预检就成了新的孪生漂移源。

    `batch_no` 可选：给了就只预览**那一个批次**（出库定向）。此时
    `plan` 最多一项，且该批余量不足时 `enough=false` —— 与落库判据
    `plan_fefo_deduction(batch_no=...)` 是同一份实现。

    判据与落库共用 `plan_fefo_deduction`（同一份实现）—— 预览说「这些批次
    会被扣」，提交时就扣这些批次；预览说不够，提交时就 400，不会两边各判。
    """
    if request.method != 'POST':
        return json_fail('仅支持 POST', status=405)

    material = get_operable_material(pk, request.user)
    if material is None:
        return json_fail('物料不存在或无权操作', status=404)

    inst = _batch_institution(request, material)
    if inst is None:
        return json_fail('仅捕捉点/医院账号可预览批次扣减', status=403)

    data = parse_json_body(request) or {}
    quantity = body_int(data, 'quantity')
    if quantity is None:
        return json_fail('数量必须是正整数')
    txn_type = body_str(data, 'txn_type') or 'consume'
    if txn_type not in ('consume', 'dispatch', 'adjustment', 'outbound'):
        return json_fail('txn_type 仅支持 consume/dispatch/adjustment/outbound')
    # 定向批号：只对**出库**有意义（消耗/下发仍走 FEFO，选批号会与既有
    # 「近效期先出」的判据打架）。给了但填错批号由计划阶段报错。
    batch_no = (body_str(data, 'batch_no') or '').strip()

    # 存量物料可能还没有批次行（shelter_stock 直写字段的形态）——
    # 预览里如实给「无批次台账」，别拿主表快照拼一个假计划出来。
    if not MaterialBatch.objects.filter(material=material, institution=inst).exists():
        current = get_institution_stock(material, inst)
        # ⚠ 定向批号时**不能**走这条兜底：用户指定了批号，而这里
        # 一行批次都没有，说明这个批号根本不存在 —— 必须如实说「没有」，
        # 而不是给一个「扣减不区分批次」的假计划（提交时会崩在别处）。
        if batch_no:
            return json_ok({
                'has_batches': False, 'plan': [], 'available': current,
                'expired_skipped': 0, 'enough': False,
                'message': f'该物料没有批号为「{batch_no}」的批次台账',
            })
        return json_ok({
            'has_batches': False, 'plan': [], 'available': current,
            'expired_skipped': 0, 'enough': current >= quantity,
            'message': '该物料还没有批次台账（存量数据），扣减不区分批次',
        })

    today = timezone.localdate()
    available, expired_qty = batch_expiry_state(material, inst, today)
    try:
        plan = plan_fefo_deduction(
            material, inst, quantity,
            skip_expired=(txn_type == 'consume'), today=today,
            batch_no=batch_no or None)
        message = '、'.join(
            f'批次「{b.batch_no}」扣 {take}'
            f'（{b.expiry_date.isoformat() if b.expiry_date else "无效期"}）'
            for b, take in plan)
    except ValueError as e:
        plan, message = [], str(e)

    return json_ok({
        'has_batches': True,
        'plan': [{
            'batch_no': b.batch_no,
            'expiry_date': b.expiry_date.isoformat() if b.expiry_date else None,
            'take': take,
        } for b, take in plan],
        'available': available,
        'expired_skipped': expired_qty,
        # plan 非空 = FEFO 计划成立（服务端同一份判据跑过、没抛错）；
        # 抛错时 plan 为空 + message 带原因。不要用「文案是否以某字开头」
        # 这类脆弱判断 —— 文案一改前端就跟着错。
        'enough': bool(plan),
        'message': message,
    })
