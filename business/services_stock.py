"""
TNR 业务系统 - 机构库存服务层（从 services.py 拆出）

包含：市级捕捉点判据、机构库存（MaterialStock）读写、库存流水落账（adjust_stock）。
`business/services.py` 仍 re-export 这些名字，旧导入路径继续可用；
新代码建议直接 `from business.services_stock import ...`。
"""
from django.db.models import F, Sum
from django.utils import timezone

from business.models import Material, MaterialStock, MaterialTransaction
from core.models import Institution


# ============================================
# 市级捕捉点（第四十五轮）
# ============================================
def is_city_shelter(institution):
    """该机构是否「市级捕捉点」。

    第四十五轮新增的层级：捕捉点不再清一色是区县级，还有挂在
    「全市（市级）」下的市级捕捉点 —— 它**看得到全市数据**、**可采购**、
    **可跨区县下发**、**可转运到全市任何医院**。

    ⚠ **判据只有这一份**，供巡检豁免 / 下发范围校验 / 前端共用。
    「层级」这件事在代码里已经有两处隐式推导（`core.scope.has_global_district_scope`
    的 `district.is_city` 分支、前端 `realDistricts` 排除市级），再各写一份
    就是第四十五轮 C5 那种「同一事实四处各判一遍、改一处漏三处」的形态。

    :param institution: `core.Institution` 实例（可为 None）
    :return: True = 市级捕捉点
    """
    if institution is None:
        return False
    if getattr(institution, 'type', None) != 'shelter':
        return False
    district = getattr(institution, 'district', None)
    return bool(district and getattr(district, 'is_city', False))


def can_dispatch_across_districts(institution):
    """该发起机构能否跨区县下发（Q6：**只有市级捕捉点**可以）。

    区县之间的物料流动一律走市级中转 —— 否则区县捕捉点之间可以直接串货，
    两边的「区县合计」都对不上，区县政府看到的台账是错的。
    """
    return is_city_shelter(institution)


# ============================================
# 机构库存（第四十五轮 Q3 方案 2）
# ============================================
# 流水类型 → 对**库存归属机构**那一行的方向。
# purchase（采购入库）与 receive（签收）让机构库存增加；
# dispatch（下发）从**发起机构**出库、consume（消耗）、adjustment（异动）都是减少。
#
# ⚠ `dispatch` 的方向容易写反：它对**发起方**是减，对**接收方**是加 ——
# 而接收方的「加」是靠签收时另写一条 `receive` 实现的（见 `material_receive`）。
# 所以单看 `dispatch` 这一行，方向恒为减。
INBOUND_TXN_TYPES = ('purchase', 'receive')
OUTBOUND_TXN_TYPES = ('dispatch', 'consume', 'adjustment')


def stock_delta(txn_type, quantity):
    """这笔流水对「库存归属机构」那一行的影响（带符号）。"""
    if txn_type in INBOUND_TXN_TYPES:
        return quantity
    if txn_type in OUTBOUND_TXN_TYPES:
        return -quantity
    raise ValueError(f'未知的物资流水类型：{txn_type}')


def get_institution_stock(material, institution):
    """**直接读一行**得出机构库存 —— 不做任何累加。

    这就是「已经算好的结果」：与 `get_hospital_stock()` 那种
    「4 次聚合 × 每次盘点都全量计算」相对。

    **首次写入前的回退**：该机构这一行还没建时（存量数据、或刚建的物料
    还没发生过任何业务），返回「可结转的存量值」而不是 0 ——
    见 `_opening_quantity_for_new_row()`。不这么做的话，界面上
    「有 100」会在第一次操作前变成「有 0」，且没有任何报错。
    """
    if material is None or institution is None:
        return 0
    row = MaterialStock.objects.filter(material=material, institution=institution).first()
    if row is not None:
        return row.quantity
    return _opening_quantity_for_new_row(material, institution)


def _opening_quantity_for_new_row(material, institution):
    """首次为某物料的捕捉点建库存行时，把存量 `shelter_stock` 结转为期初。

    为什么需要：`Material.shelter_stock` 是**直接写字段**来的
    （`seed_data` 设字段、测试助手 `make_material(shelter_stock=…)` 也是），
    这些量**没有对应流水**。若首次建行时从 0 开始，那部分库存就凭空消失了
    —— 界面从「有 100」变成「有 0」，且**没有任何报错**。

    只在**无歧义**时结转（该区县**恰好一个**捕捉点）：
    `shelter_stock` 的语义是「该区县捕捉点**合计**」，多个捕捉点时
    拆不开 —— 全给第一个 = 凭猜记账（A 点凭空多出 100、B 点显示 0）。
    这种情况下留 0，并靠 `check_data_integrity` 的
    「捕捉点库存字段与机构库存合计不一致」把差异**报出来**，
    交给运维决定，而不是替它猜。

    ⚠ 只在「该物料**还没有任何捕捉点库存行**」时结转 —— 否则同一区县
    第二个捕捉点建行时会**再结转一次**，合计直接翻倍。
    """
    if getattr(institution, 'type', None) != 'shelter':
        return 0
    if not material.shelter_stock:
        return 0
    if MaterialStock.objects.filter(
            material=material, institution__type='shelter').exists():
        return 0
    shelter_count = Institution.objects.filter(
        type='shelter', district_id=material.district_id).count()
    if shelter_count != 1:
        return 0
    return material.shelter_stock


def apply_stock_delta(material, institution, txn_type, quantity):
    """把「该机构这一行」按流水方向加/减，不存在则建行。返回变化后的库存。

    ⚠ **必须在调用方的 `transaction.atomic()` 里调用**：它与流水的写入是
    「同一笔业务的两半」，一半成功一半失败会让两个真源永久对不上
    （所以 `check_data_integrity` 有一条对账规则盯着这件事）。

    ⚠ 用 `F()` 做自增，而不是「读出来 +1 再写回去」：后者在并发下会丢更新。
    """
    if institution is None:
        raise ValueError('机构库存必须指明归属机构')
    delta = stock_delta(txn_type, quantity)
    row = MaterialStock.objects.filter(
        material=material, institution=institution).first()
    if row is None:
        # 建行时可能要把存量结转为期初，见 `_opening_quantity_for_new_row()`
        opening = _opening_quantity_for_new_row(material, institution)
        row, _ = MaterialStock.objects.get_or_create(
            material=material, institution=institution,
            defaults={'quantity': opening, 'opening_quantity': opening})
    if delta:
        MaterialStock.objects.filter(pk=row.pk).update(quantity=F('quantity') + delta)
        row.refresh_from_db(fields=['quantity'])
    return row.quantity


def get_shelter_stock(material, district=None):
    """捕捉点侧库存合计（「捕捉点库存」那一列的口径）。

    口径 = 该物料在**捕捉点**机构上的机构库存之和（医院行不计入）。

    **兼容回退**：该物料**还没有任何机构库存行**时返回旧的
    `Material.shelter_stock` —— 存量数据在 `0020` 迁移里只在
    「区县恰好一个捕捉点」时才灌数，其余（0 个或多个捕捉点）**故意留着不猜**，
    读取侧必须能照旧显示，否则界面会凭空变成 0。

    :param district: 只统计该区县下的捕捉点；None = 全部捕捉点
    """
    if material is None:
        return 0
    qs = MaterialStock.objects.filter(material=material, institution__type='shelter')
    if district is not None:
        qs = qs.filter(institution__district=district)
    total = qs.aggregate(total=Sum('quantity'))['total']
    if total is None:
        return material.shelter_stock
    return total


def get_hospital_stock(material, hospital):
    """计算指定医院的物资库存（**现算**口径，逐笔累加）。

    库存 = 采购入库 + 签收 - 诊疗消耗 - 异动调整
    （以上均针对同一医院）

    注意：dispatch（下发）流水不直接增加医院库存，
    接收方签收后才创建 receive 流水增加库存。

    ⚠ 第四十五轮起医院侧**另有**一张已算好的 `MaterialStock` 行
    （`get_institution_stock()`），本函数保留为**对账基准**：
    两者的结果必须逐对相等（`check_data_integrity` 与
    `business/tests/test_material_stock.py` 都盯着这条），
    确认无偏差后才把读取切过去 —— 一次性改动既有医院库存语义风险太大。
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


def find_hospital_chip_material(hospital, district_id=None):
    """找出**该医院实际有库存**的芯片物料（第四十五轮 C7）。

    原先的取法是 `Material.objects.filter(category='chip',
    district_id=pet.district_id).first()` —— 按**宠物**的区县找芯片物料。
    跨区县送医时（市级捕捉点抓的动物送到别的区县医院、或区县医院收治外区
    动物）宠物区县与医院所在区县不是同一个，于是找到的是**别的区县**的
    芯片物料，而本院对那条物料**根本没有库存**：

    - 消耗被记到别的区县的物料上（本院真正的芯片物料**一颗都没减**）；
    - 别的区县那条物料的库存被凭空扣掉；
    - 全程不报错，只有把两边的台账摊开对才看得出来。

    查找顺序：

    1. **本院有库存**的芯片物料（按库存从多到少）—— 这才是「本院用的芯片」；
    2. 退回本院所在区县的芯片物料（改造前的口径，保证既有数据仍能走通）；
    3. 再退回传入的 `district_id`（宠物区县，改造前的口径）。

    :param hospital: 收治医院（可为 None，此时跳过第 1、2 步）
    :param district_id: 宠物/业务记录的区县 id（兜底用）
    :return: `Material` 实例或 None
    """
    if hospital is not None:
        stocked = (MaterialStock.objects
                   .filter(institution=hospital, material__category='chip',
                           quantity__gt=0)
                   .select_related('material')
                   .order_by('-quantity', 'material_id')
                   .first())
        if stocked is not None:
            return stocked.material

    fallback_districts = []
    if hospital is not None and getattr(hospital, 'district_id', None):
        fallback_districts.append(hospital.district_id)
    if district_id and district_id not in fallback_districts:
        fallback_districts.append(district_id)
    for did in fallback_districts:
        found = Material.objects.filter(
            category='chip', district_id=did).order_by('id').first()
        if found is not None:
            return found
    return None


def get_institution_ledger_total(material, institution):
    """按**库存归属机构**把流水加起来 —— 对账用的「流水合计」侧。

    与 `get_institution_stock()` 必须恒等；不等就是「增量维护漏了一次」
    或「有人绕过 `adjust_stock` 直接写流水」，两者都会让界面上的库存
    与台账对不上，而且**不报错**。
    """
    if material is None or institution is None:
        return 0
    base_qs = MaterialTransaction.objects.filter(material=material, institution=institution)
    totals = {}
    for txn_type in INBOUND_TXN_TYPES + OUTBOUND_TXN_TYPES:
        totals[txn_type] = base_qs.filter(type=txn_type).aggregate(
            total=Sum('quantity'))['total'] or 0
    return sum(
        stock_delta(t, totals[t]) for t in INBOUND_TXN_TYPES + OUTBOUND_TXN_TYPES
    )


# ============================================
# 库存调整
# ============================================
def adjust_stock(material, hospital, quantity, txn_type, institution=None, **extra):
    """创建物资流水并调整库存。

    :param material: 物料对象
    :param hospital: **对方机构**（下发时的接收方）；None 表示无对方机构
    :param quantity: 数量（正整数）
    :param txn_type: 流水类型 purchase/dispatch/receive/consume/adjustment
    :param institution: **库存归属机构** —— 本笔流水动的是谁的库存。
        捕捉点侧传发起捕捉点，医院侧传该医院。不传时退化为 `hospital`；
        两者都没有则走**兼容路径**（只动旧的 `shelter_stock` 字段，
        不写机构库存表）—— 那条路径只为历史调用与单元测试保留，
        生产代码一律显式传 `institution`。
    :param extra: 额外字段，如 operator/batch_no/supplier/from_to/note/ledger_no/district
    :return: 创建的 MaterialTransaction 对象

    ⚠ 库存判据必须在**建流水之前**。
    原先先 `MaterialTransaction.objects.create()` 再判库存，库存不足时抛
    `ValueError` —— 调用方看到 400「库存不足」，但那条流水**已经落库**，
    会实实在在出现在捕捉点台账里：数量对不上，且全程没有任何报错。
    这与 `views_treatment` 里「先建诊疗记录再校验库存」是同一个反模式
    （那一处已在早期轮次修掉，见 `test_treatment_views` 的同名用例）。
    「写库后才 `return json_fail` / `raise`」= 孤儿记录，判据一律前置。
    """
    inst = institution if institution is not None else hospital
    district = extra.get('district') or material.district
    operator = extra.get('operator')
    today = timezone.localdate()

    # 出库类操作的余额判据。**只在捕捉点侧判**。
    #
    # ⚠ 医院侧**故意不判**，这是既有契约（`stock_adjustment` 的 docstring
    #   明确写着「医院库存由流水累加得出，`adjust_stock` 对医院侧不做余额校验，
    #   这里必须自己把关」）。为什么不在第四十五轮顺手补上：医院侧**读取**
    #   暂时仍走 `get_hospital_stock()`（现算口径），而机构库存表是另一套口径 ——
    #   在这里补一道来源不同的判据，会让「用户看到的余额」与「系统判的余额」
    #   不是一回事，正是本项目反复栽过的孪生漂移。医院侧要收口，
    #   必须先做「读取切到新表」，那是独立一步。
    #
    # 机构归属明确且是捕捉点时，按**该机构**的库存判：同一区县两个捕捉点
    # 各有各的余额，拿区县合计去卡会误放行（一个点空了、另一个点还满着）。
    # 没有机构归属时（历史调用 / 单元测试）沿用旧的区县合计字段。
    if txn_type in OUTBOUND_TXN_TYPES and (inst is None or inst.type == 'shelter'):
        if inst is not None:
            current = get_institution_stock(material, inst)
            if current < quantity:
                raise ValueError(
                    f'{inst.name}库存不足（当前 {current}，需 {quantity}）')
        elif material.shelter_stock < quantity:
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
        institution=inst,
        operator=operator,
        operator_name=extra.get('operator_name', ''),
        date=today,
        ledger_no=extra.get('ledger_no', ''),
        district=district,
        note=extra.get('note', ''),
    )

    # ---- 库存落账 ----
    # 两条路径，**不要合并**：
    #  ① 有机构归属（生产路径）→ 增量维护 `MaterialStock` 那一行；
    #     若归属是捕捉点，同时把旧的 `shelter_stock` **按新表重算**，
    #     让兼容字段与真源恒等（对账规则因此可以写成硬判据）。
    #  ② 无机构归属（历史调用 / 单元测试）→ 只动旧字段，行为与改造前一致。
    if inst is not None:
        apply_stock_delta(material, inst, txn_type, quantity)
        if inst.type == 'shelter':
            material.shelter_stock = get_shelter_stock(material)
            material.save(update_fields=['shelter_stock'])
    elif hospital is None:
        if txn_type == 'purchase':
            material.shelter_stock += quantity
        elif txn_type in OUTBOUND_TXN_TYPES:
            material.shelter_stock -= quantity
        material.save(update_fields=['shelter_stock'])

    return txn
