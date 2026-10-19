"""
TNR 业务系统 - 机构库存服务层（从 services.py 拆出）

包含：市级捕捉点判据、机构库存（MaterialStock）读写、库存流水落账（adjust_stock）。
`business/services.py` 仍 re-export 这些名字，旧导入路径继续可用；
新代码建议直接 `from business.services_stock import ...`。
"""
import datetime

from django.db.models import F, Sum
from django.utils import timezone

from business.models import Material, MaterialBatch, MaterialStock, MaterialTransaction
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
# dispatch（下发）从**发起机构**出库、consume（消耗）、adjustment（异动）、
# outbound（出库）都是减少。
#
# ⚠ `dispatch` 的方向容易写反：它对**发起方**是减，对**接收方**是加 ——
# 而接收方的「加」是靠签收时另写一条 `receive` 实现的（见 `material_receive`）。
# 所以单看 `dispatch` 这一行，方向恒为减。
#
# ⚠ `outbound` 的**方向由数量符号共同决定**：正数 = 出库（减），
# 负数 = 盘盈 / 退回入库（加）。`stock_delta()` 的 `-quantity` 对负数
# 天然给出正 delta，不需要特判 —— 但**余额判据只在正数时生效**，
# 否则「盘盈必填负数」会被自己的下限拦住（见 `adjust_stock`）。
INBOUND_TXN_TYPES = ('purchase', 'receive')
OUTBOUND_TXN_TYPES = ('dispatch', 'consume', 'adjustment', 'outbound')
# 允许**负数量**的流水类型 = 「撤销 / 盘盈退回」类。
#   - `adjustment`：撤销诊疗消耗（`services_treatment` 撤回处方）；
#   - `outbound`：盘盈 / 退回入库（磊哥：「出库数量可以为负，
#     表示实际入库或者盘盈」）。
# 这两个类型在 `delta > 0` 时走 `_credit_batch`，且**可能没有明确批号**
# —— 那时退回 FEFO 最前排批次（近似还原）。别的类型走 `_credit_batch`
# 一定是真的入库（采购 / 签收），批号取表单、空则「初始批次」。
REVERSAL_TXN_TYPES = ('adjustment', 'outbound')
# 出库原因白名单：与 `MaterialTransaction.OUTBOUND_REASON_CHOICES` 同源。
# 为什么不能就地写在 choices 上就完事：`batch_preview`（预览）与
# `material_outbound`（建单）**两处都要校验同一个枚举**，
# 各写一份必然漂移（孪生接口的老问题）。
OUTBOUND_REASON_CHOICES = MaterialTransaction.OUTBOUND_REASON_CHOICES
OUTBOUND_REASONS = tuple(r for r, _ in OUTBOUND_REASON_CHOICES)


def validate_outbound_reason(reason):
    """出库原因三态语义：`(None, None)` 合法 / `(None, 消息)` 非法。

    只接受**非空**的合法枚举值；空串与 None 都视为「没填」（非法）。
    预览与建单共用，防止两边口径漂移。
    """
    if reason in OUTBOUND_REASONS:
        return reason, None
    return None, '出库原因必填，且只能是报废 / 退回 / 调拨 / 盘点之一'


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

    库存 = 采购入库 + 签收 - 诊疗消耗 - 异动调整 - 出库
    （以上均针对同一医院）

    注意：dispatch（下发）流水不直接增加医院库存，
    接收方签收后才创建 receive 流水增加库存。

    ⚠⚠ **为什么这里没有 dispatch**（曾在这里栽过一次，回归当场抓住）：
    本函数按 `hospital` 字段过滤，而 `hospital` 的语义是**对方机构** ——
    下发的 `hospital` 正是**接收方**。于是一条「A 院 → B 院 下发 10 件」
    在 B 院的这个函数里会被算成「本院出库 10 件」，库存扣成 -10，
    而 B 院其实一件都还没收到（要靠签收才产生 receive 流水）。
    `get_institution_ledger_total()` 按 `institution`（**归属机构**）过滤，
    那条 dispatch 属于发起方那一侧，所以它看不见。
    **两个函数的过滤维度不同，公式就不能照抄** —— 一统一就错，
    而错的样子是「库存是负数」这种一眼能看出的假象，
    更容易被当成「别的问题」放过去。

    ⚠ **出库数量可以为负**（盘盈 / 退回入库）：`stock_delta()` 的 `-quantity`
    对负数量天然给出正 delta，所以这个式子**不需要为负数特判** ——
    一行 `outbound` 负数量在这里就是「加库存」。

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

    outbound_total = base_qs.filter(type='outbound').aggregate(
        total=Sum('quantity')
    )['total'] or 0

    return (purchase_total + receive_total
            - consume_total - adjustment_total - outbound_total)


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
# 物料批次（B5）
# ============================================
NEAR_EXPIRY_DAYS = 30  # 临期提示窗口（前端批次明细标红用，判据只有这一份）


def _initial_batch_no(material):
    """存量/未登记批号的「初始批次」命名 —— 与 `stock_init.initial_batch_no` 同口径。"""
    from business import stock_init
    return stock_init.initial_batch_no(material)


def fefo_sorted(batches):
    """FEFO 排序：近效期在前、无效期垫底；同效期（或同无效期）按 id 先后。

    ⚠ 在 Python 里排而不是 ORM `order_by(F('expiry_date').asc(nulls_last=True))`：
    MySQL/MariaDB 不支持 `NULLS LAST` 语法，写在 ORM 里换库就 500。
    批次行数「一个机构 × 一种物料」通常是个位数，内存排序零成本。
    """
    return sorted(batches, key=lambda b: (b.expiry_date is None, b.expiry_date or '', b.id))


def get_material_batches(material, institution):
    """某机构在某物料上的批次明细（FEFO 顺序）。含余量为 0 的批次 ——
    批次档案（批号/效期）在耗尽后仍要可查（追溯、签收回填效期都靠它）。"""
    if material is None or institution is None:
        return []
    return fefo_sorted(MaterialBatch.objects.filter(
        material=material, institution=institution))


def batch_expiry_state(material, institution, today=None):
    """批次余量按「是否过期」二分 → (可用, 已过期)。

    「有任一未过期余量 → 可用」这条判据在**预检**
    （`services.expired_material_error`）与**落库**（`plan_fefo_deduction`）
    共用这一份 —— 两处各写一遍就是「预检说能用、提交却被拦」的孪生漂移。
    """
    today = today or timezone.localdate()
    available = expired = 0
    for b in MaterialBatch.objects.filter(material=material, institution=institution):
        if b.quantity <= 0:
            continue
        if b.expiry_date and b.expiry_date < today:
            expired += b.quantity
        else:
            available += b.quantity
    return available, expired


def batch_available_quantity(material, institution):
    """未过期批次可用量合计（`expired_material_error` 的批次级判据用）。"""
    return batch_expiry_state(material, institution)[0]


def materialize_initial_batches(material, institution):
    """把「有机构库存、但还没有任何批次行」物化成一条初始批次。

    什么时候发生：B5 迁移只回填**当时已有** `MaterialStock` 行的物料；
    `shelter_stock` 直写字段、还没建过库存行的存量物料走
    `_opening_quantity_for_new_row` 的结转路径，第一次业务发生时
    库存行与批次行必须**同时**诞生，否则「库存行 == Σ批次」从第一笔起
    就不成立（对账规则从上线第一天就全是红）。

    幂等：已有任何批次行 → 直接返回，不重复结转。
    """
    if material is None or institution is None:
        return
    if MaterialBatch.objects.filter(material=material, institution=institution).exists():
        return
    current = get_institution_stock(material, institution)
    if current <= 0:
        return
    MaterialBatch.objects.create(
        material=material, institution=institution,
        batch_no=_initial_batch_no(material),
        expiry_date=getattr(material, 'expiry_date', None),
        quantity=current)


def plan_fefo_deduction(material, institution, quantity, skip_expired=False,
                        today=None, batch_no=None):
    """出库扣减计划（FEFO）：返回 [(批次, 扣减量), ...]。

    - 近效期先出、同效期按入库先后（`fefo_sorted`）；
    - `skip_expired=True`（consume）时跳过已过期批次 —— 过期药品不能用于
      诊疗，但**不是**扣它旁边的有效批次：扣错批次 = 台账上的批号与
      实际用在动物身上的药对不上，出了不良反应查不到源头；
    - dispatch / adjustment / outbound **不**跳过：过期物料的正规出口就是
      「下发退回供应商 / 异动报废 / 出库报废」，再拦就把死库存焊死在账上；
    - `batch_no` 给定（出库定向）时**只扣这一个批次**，余量不足直接报错 ——
      **绝不静默改扣别的批次**。用户在下拉里选的是「这一批」，扣了另一批
      等于台账批号与实际出库的货对不上，而这个错不会报任何警；
    - 定向批号也不跳过过期：报废的入口之一正是过期批次。

    :raises ValueError: 可用量不足。文案区分三种成因（定向批次余量不足 /
        全部过期 vs 普通不足），「全部批次过期」要引导用户走异动报废，
        而不是让他反复减数量重试。
    """
    today = today or timezone.localdate()
    batches = [b for b in get_material_batches(material, institution) if b.quantity > 0]
    if batch_no:
        target = batch_no.strip()
        usable = [b for b in batches if b.batch_no == target]
        if not usable:
            raise ValueError(
                f'「{material.name}」没有批号为「{target}」的可用批次'
                f'（该批号不存在或已无余量）')
        plan, remaining = [], quantity
        for b in usable:
            take = min(b.quantity, remaining)
            if take > 0:
                plan.append((b, take))
                remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            raise ValueError(
                f'「{material.name}」批号「{target}」余量不足'
                f'（可用 {usable[0].quantity}，需 {quantity}）')
        return plan
    usable = [b for b in batches
              if not (skip_expired and b.expiry_date and b.expiry_date < today)]
    plan, remaining = [], quantity
    for b in usable:
        take = min(b.quantity, remaining)
        if take > 0:
            plan.append((b, take))
            remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        available = sum(b.quantity for b in usable)
        expired_qty = sum(b.quantity for b in batches) - available
        if skip_expired and available == 0 and expired_qty > 0:
            raise ValueError(
                f'「{material.name}」可用批次已全部过期（共 {expired_qty}），'
                f'不能用于诊疗；请先做库存异动（过期报废）处理')
        if expired_qty > 0:
            raise ValueError(
                f'「{material.name}」可用库存不足（可用 {available}，'
                f'需 {quantity}，另有 {expired_qty} 已过期被跳过）')
        raise ValueError(
            f'「{material.name}」批次库存不足（可用 {available}，需 {quantity}）')
    return plan


def _parse_expiry(value):
    """把外部传入的效期规整成 date；无效/空 → None（不猜）。"""
    if value in (None, ''):
        return None
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _credit_batch(material, inst, txn_type, amount, extra):
    """入库方向的批次记入：返回 (批次, 数量)。

    - purchase / receive：批号取 extra（表单/下发单），空则退「初始批次」；
      同批号再次入库 = 同一批货，**累加**并更新效期；
    - 负数 **撤销类**（`adjustment` 撤销诊疗消耗 / `outbound` 盘盈退回）：
      有明确批号时**记回该批号**（用户申报的就是这一批，记到别的批次上
      等于账实不符）；没有明确批号时退 FEFO 最前排的批次
      （撤销所退的量通常正是刚从前排扣走的，近似还原）；
      一行批次都没有时按主表快照批号建一条。

    ⚠ 这里的「撤销类」清单必须与 `adjust_stock` 里
      `delta = stock_delta(...) > 0 → _credit_batch` 的路径**正好对齐**：
      只有「本类型允许负数量」才可能走到这里且没有明确批号。
      把它们写死成两个常量表（见 `REVERSAL_TXN_TYPES`）而不是各判一遍，
      否则新增一种可负类型时会漏 —— 漏了不报错，只会把退回记到一个
      凭空多出来的「初始批次」行上，Σ批次 == 库存的等式看着还成立。
    """
    expiry = _parse_expiry(extra.get('expiry_date'))
    explicit = (extra.get('batch_no') or '').strip()
    if explicit:
        # 任何入库方向都听调用方指定的批号（采购 / 签收原有行为，
        # 盘盈/退回的定向记回复用它）
        batch_no = explicit
    elif txn_type in REVERSAL_TXN_TYPES:
        batches = list(MaterialBatch.objects.filter(material=material, institution=inst))
        if batches:
            return fefo_sorted(batches)[0], amount
        batch_no = _initial_batch_no(material)
    else:
        batch_no = _initial_batch_no(material)
    batch, _created = MaterialBatch.objects.get_or_create(
        material=material, institution=inst, batch_no=batch_no,
        defaults={'quantity': 0, 'expiry_date': expiry})
    if expiry is not None and batch.expiry_date != expiry:
        # 同批号再次入库：效期随之更新（批号相同 = 同一批货，新效期为准）。
        MaterialBatch.objects.filter(pk=batch.pk).update(expiry_date=expiry)
    return batch, amount


def _deduct_batches(material, inst, amount, txn_type, batch_no=None):
    """出库方向的 FEFO 扣减计划。没有任何批次行 → 返回 None（legacy 回退）。

    legacy 回退的场景：测试助手 / 历史调用直接造流水，机构还没有批次行 ——
    此时批次行数为 0，硬走 `plan_fefo_deduction` 会把「无批次」当成
    「库存为 0」拦下来，旧行为回归全挂。

    ⚠ 定向批号（`batch_no`）**不走** legacy 回退：调用方明确指定了批号，
    就必须真的扣那一行；批号不存在时报错，而不是「按主表快照写一行流水、
    批次行一个没动」—— 那样 Σ批次 ≠ 库存，`check_data_integrity` 会报脏数据，
    而界面上的批次余量完全不动。
    """
    if batch_no and str(batch_no).strip():
        return plan_fefo_deduction(
            material, inst, amount, skip_expired=(txn_type == 'consume'),
            batch_no=batch_no)
    if not MaterialBatch.objects.filter(material=material, institution=inst).exists():
        return None
    return plan_fefo_deduction(
        material, inst, amount, skip_expired=(txn_type == 'consume'))


# ============================================
# 库存调整
# ============================================
def adjust_stock(material, hospital, quantity, txn_type, institution=None, **extra):
    """创建物资流水并调整库存（B5 起带**批次记账**）。

    :param material: 物料对象
    :param hospital: **对方机构**（下发时的接收方）；None 表示无对方机构
    :param quantity: 数量（正数；`outbound` 的负数表示退回 / 盘盈入库，
        负数仅用于 adjustment 的撤销退回与本类型）
    :param txn_type: 流水类型 purchase/dispatch/receive/consume/adjustment/outbound
    :param institution: **库存归属机构** —— 本笔流水动的是谁的库存。
        捕捉点侧传发起捕捉点，医院侧传该医院。不传时退化为 `hospital`；
        两者都没有则走**兼容路径**（只动旧的 `shelter_stock` 字段，
        不写机构库存表、也不记批次）—— 那条路径只为历史调用与单元测试
        保留，生产代码一律显式传 `institution`。
    :param extra: 额外字段，如 operator/batch_no/expiry_date/supplier/
        from_to/note/ledger_no/district 或 district_id/ref_no/reason
    :return: 创建的 MaterialTransaction **列表**。B5 起出库跨批次会自动拆成
        多笔（同 ledger_no/note，每笔 batch_no = 所扣批次、quantity = 该批
        扣减量）——「一笔业务」在物理上就是 N 行，返回单条会让调用方
        漏改兄弟行（下发的兄弟行就是签收的入口，漏改 = 签收链路断裂）。

    批次规则（只对 institution 明确的生产路径生效，legacy 路径行为不变）：
    - 入库（purchase/receive）：按批号 get_or_create 批次行并累加，效期随之
      更新；效期从 extra['expiry_date'] 传入（采购 = 表单，签收 = 发起方批次）；
    - 出库（dispatch/consume/adjustment/outbound）：FEFO 扣减，consume 跳过已过期批次；
    - **定向批号**：`extra['batch_no']` 给定（outbound）时只扣那一行，
      余量不足直接报错，不改扣别的批次；
    - 负数 outbound（退回 / 盘盈）：有明确批号时记回该批号，
      否则退 FEFO 最前排；
    - 负数 adjustment（撤销退回）：退回 FEFO 最前排批次。

    ⚠ 库存判据必须在**建流水之前**。
    原先先 `MaterialTransaction.objects.create()` 再判库存，库存不足时抛
    `ValueError` —— 调用方看到 400「库存不足」，但那条流水**已经落库**，
    会实实在在出现在捕捉点台账里：数量对不上，且全程没有任何报错。
    这与 `views_treatment` 里「先建诊疗记录再校验库存」是同一个反模式
    （那一处已在早期轮次修掉，见 `test_treatment_views` 的同名用例）。
    「写库后才 `return json_fail` / `raise`」= 孤儿记录，判据一律前置。
    """
    inst = institution if institution is not None else hospital
    # 归属区县：显式传入优先，其次退回物料区县。
    # ⚠ 四个调用点（采购 / 下发 / 盘点 / 出库）一律传 `district_id=`（pk），
    # 而这里原先只读 `extra['district']` —— 显式区县被**静默丢弃**，
    # 流水永远落到 `material.district`。R45 口径要求业务记录归属**实际区县**
    # （市级绝不能作业务归属），所以必须让显式值生效，而不是删掉这个参数。
    if extra.get('district') is not None:
        district_fields = {'district': extra['district']}
    elif extra.get('district_id') is not None:
        district_fields = {'district_id': extra['district_id']}
    else:
        district_fields = {'district': material.district}
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
    #
    if (txn_type in OUTBOUND_TXN_TYPES
            and (inst is None or inst.type == 'shelter')):
        if inst is not None:
            current = get_institution_stock(material, inst)
            if current < quantity:
                raise ValueError(
                    f'{inst.name}库存不足（当前 {current}，需 {quantity}）')
        elif material.shelter_stock < quantity:
            raise ValueError(f'捕捉点库存不足（当前 {material.shelter_stock}，需 {quantity}）')

    # ---- 批次计划（B5）----
    # 必须在建流水**之前**：计划抛错（全过期/批次不足）时不能留下半截子流水。
    # legacy 路径（无机构归属）不做批次记账，batch_plan 保持 None。
    batch_plan = None
    if inst is not None:
        # 存量结转：还没物化批次行的库存，趁第一笔业务一起物化
        materialize_initial_batches(material, inst)
        delta = stock_delta(txn_type, quantity) if quantity else 0
        if delta > 0:
            batch_plan = [_credit_batch(material, inst, txn_type, delta, extra)]
        elif delta < 0:
            batch_plan = _deduct_batches(
                material, inst, -delta, txn_type, batch_no=extra.get('batch_no'))

    # 芯片**不拆行**：号码才是芯片的真源（`Chip` 表逐号管理，全部挂在
    # 同一条 dispatch 流水下），拆行会让 `chip_batch_receive` 的
    # 「在途数 == 下发单数量」强绑定校验当场错开（数量只落在第一行上）。
    # 批次行照常按 FEFO 计划更新（Σ批次 == 库存的等式不破），
    # 只是流水保持单行、批号沿用主表快照。
    split_rows = batch_plan is not None and material.category != 'chip'

    common_fields = dict(
        type=txn_type,
        reason=extra.get('reason', ''),
        material=material,
        material_name=material.name,
        unit=material.unit,
        supplier=extra.get('supplier', material.supplier),
        from_to=extra.get('from_to', ''),
        hospital=hospital,
        institution=inst,
        operator=operator,
        operator_name=extra.get('operator_name', ''),
        date=today,
        ledger_no=extra.get('ledger_no', ''),
        ref_no=extra.get('ref_no', ''),  # 接收单关联下发单号（第四十六轮单据链）
        note=extra.get('note', ''),
        **district_fields,
    )

    txns = []
    if batch_plan and split_rows:
        if stock_delta(txn_type, quantity) < 0:
            # 出库拆行：每笔流水 = 「从某批次扣了多少」，quantity 为正、
            # 方向由 type 表达（与既有流水的符号约定一致）。
            for batch, take in batch_plan:
                txns.append(MaterialTransaction.objects.create(
                    **common_fields, quantity=take, batch_no=batch.batch_no))
        else:
            # 入库（含负数 adjustment 撤销退回）：单批记入，
            # quantity 原样保留（撤销退回时它就是负数）。
            batch, _amount = batch_plan[0]
            txns.append(MaterialTransaction.objects.create(
                **common_fields, quantity=quantity, batch_no=batch.batch_no))
    else:
        # 芯片（批次行更新但不拆行）或 legacy 无批次路径：单行流水。
        txns.append(MaterialTransaction.objects.create(
            **common_fields,
            quantity=quantity,
            batch_no=extra.get('batch_no', material.batch_no)))

    # ---- 库存落账 ----
    # 两条路径，**不要合并**：
    #  ① 有机构归属（生产路径）→ 增量维护 `MaterialStock` 那一行；
    #     若归属是捕捉点，同时把旧的 `shelter_stock` **按新表重算**，
    #     让兼容字段与真源恒等（对账规则因此可以写成硬判据）。
    # ② 无机构归属（历史调用 / 单元测试）→ 只动旧字段，行为与改造前一致。
    if inst is not None:
        total_delta = stock_delta(txn_type, quantity) if quantity else 0
        # 批次行增量维护 —— 与 MaterialStock 同一事务、同一「已算好的结果」
        # 口径；出库按计划逐批减、入库单批加，Σ批次 == 库存行 恒等。
        if batch_plan:
            if total_delta < 0:
                for batch, take in batch_plan:
                    MaterialBatch.objects.filter(pk=batch.pk).update(
                        quantity=F('quantity') - take)
            else:
                batch = batch_plan[0][0]
                MaterialBatch.objects.filter(pk=batch.pk).update(
                    quantity=F('quantity') + total_delta)
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

    return txns
