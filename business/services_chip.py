"""芯片服务层 —— 按**号码**管理芯片的入库 / 下发 / 签收 / 追溯（第四十六轮新建）。

设计要点
--------
1. **`Chip` 表是唯一真源**。重构前 `Material.chip_range_start/end` 那两个
   字符串与 `Chip` 表是双真源且互不校验（号段说 100 个、实际 30 行，
   界面照样显示 100 且不报错）。现在号段字段已删除，一切都以逐号的行说话。

2. **「首号 + 数量」是唯一的录入方式**（磊哥的口径）：
   采购入库输入这批的第一个号码与数量，下发同理 ——
   号码按**数值 +1 递增、保持位数补零**推算。

3. **强绑定**：推算出的号码个数 == 数量。不存在「号码 30 个、数量填 100」
   这种各记各的情况 —— 数量对不上直接报错。

4. 归属：`institution` 为 NULL 的是重构前的**存量行**，读取侧当作
   「未归属」，写入侧**只拦新引用、不拦存量**（否则历史芯片一颗都用不出去）。

⚠ 本模块所有写函数都**必须在调用方的 `transaction.atomic()` 里**调用 ——
它们与 `adjust_stock()` 写的库存行是同一笔业务的两半，一半成功一半失败
会让「芯片数」与「库存数」永久对不上，而且不会报错。
"""

from .models import Chip

# 与 `Chip.number.max_length` 保持一致。推算出的号码超过这个长度就必须报错，
# 不能截断 —— 截断等于把两颗芯片记成同一个号码。
CHIP_NUMBER_MAX_LENGTH = 30

# 单批上限。存在的意义不是「性能」而是「防误输」：把数量误打成 100000
# 会当场建出十万行，且这些行全部带正确的号码、事后只能逐号删。
CHIP_BATCH_MAX = 5000


def parse_chip_numbers(first_number, count):
    """把「首号 + 数量」展开成这一批的全部号码。

    :param first_number: 这批的第一个号码，如 ``042060107000177``
    :param count: 这一批的数量（正整数）
    :return: 号码字符串列表（长度 == count）
    :raises ValueError: 首号非纯数字 / 数量非法 / 推算结果超长

    位数规则：**保持首号的位数补零**，跨段自然进位。
    ``('042060107000177', 3)`` → ``['042060107000177', '...178', '...179']``；
    ``('0098', 3)`` → ``['0098', '0099', '0100']``。

    ⚠ 全角数字必须单独挡掉：``'１２３'.isdigit()`` 是 **True**，
    ``int('１２３')`` 也**不报错** —— 于是全角号码会一路走到 `zfill()`，
    建出的行看着像成功了，实际在扫码枪下永远扫不出来（从 Excel 粘贴时
    很容易带进来）。`isascii()` 是唯一能挡住它的判据。
    """
    raw = str(first_number or '').strip()
    if not raw:
        raise ValueError('请填写起始芯片号')

    if not (raw.isdigit() and raw.isascii()):
        raise ValueError(f'起始芯片号必须是纯数字（如 042060107000177），当前为「{raw}」')

    width = len(raw)
    if width > CHIP_NUMBER_MAX_LENGTH:
        raise ValueError(f'起始芯片号最长 {CHIP_NUMBER_MAX_LENGTH} 位，当前 {width} 位')

    # ⚠ `int(1.5)` 得到 **1 且不报错** —— 必须额外挡小数，否则「数量 1.5」
    #   会被悄悄截成 1：建了 1 行芯片，而调用方以为自己传的是一个合法数量。
    if isinstance(count, float) and not count.is_integer():
        raise ValueError('数量必须是整数')
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise ValueError('数量必须是整数')

    if count < 1:
        raise ValueError('数量必须大于 0')
    if count > CHIP_BATCH_MAX:
        raise ValueError(f'单批最多 {CHIP_BATCH_MAX} 个，当前 {count} 个')

    start = int(raw)
    numbers = []
    for offset in range(count):
        num = str(start + offset)
        # 进位后可能变长（'999' + 1 → '1000'），这属于正常跨段，不算错；
        # 只有超过字段长度才是错 —— 那时 `zfill` 也救不回来，必须报错。
        if len(num) > CHIP_NUMBER_MAX_LENGTH:
            raise ValueError(
                f'推算出的芯片号「{num}」超过 {CHIP_NUMBER_MAX_LENGTH} 位，'
                f'请核对起始号与数量')
        numbers.append(num.zfill(width))
    return numbers


def chip_batch_create(material, institution, first_number, count, batch_no=''):
    """批量入库：按「首号 + 数量」建出这一批 `Chip` 行。

    :return: 建出的号码列表

    ⚠ 号码**全局唯一**（`Chip.number` 有 unique 约束）。先一次性查出来报错，
    而不是让 `bulk_create` 撞 `IntegrityError` 打成 500 —— 后者在事务里
    还会把连接标记成「需要回滚」，调用方连错误文案都拼不出来。
    查询**不带机构过滤**：重号既可能是「这批货入过库了」，
    也可能是「别的机构手上有同一号码」（数据事故），两种都必须拦。
    """
    if material is None or institution is None:
        raise ValueError('芯片入库必须指明物料与持有机构')

    numbers = parse_chip_numbers(first_number, count)

    existed = set(
        Chip.objects.filter(number__in=numbers).values_list('number', flat=True))
    if existed:
        sample = '、'.join(sorted(existed)[:3])
        more = '' if len(existed) <= 3 else f' 等 {len(existed)} 个'
        raise ValueError(f'芯片号已存在：{sample}{more}')

    Chip.objects.bulk_create([
        Chip(number=num, status='available', institution=institution,
             material=material, batch_no=batch_no or '')
        for num in numbers
    ], batch_size=200)
    return numbers


def _load_batch_rows(numbers, material, institution):
    """按号码取行，并校验「都存在、都属于该机构、都属于该物料」。

    **下发与作废共用这一段** —— 两处各写一份的话，迟早出现
    「下发拦住了、作废没拦」这种孪生漂移（本项目反复栽过的形态）。

    ⚠ 归属与物料校验**只拦新引用、不拦存量**：`institution` / `material`
    为 NULL 的是重构前的历史行，它们的归属信息在数据里已经丢了，一律放行
    —— 否则历史芯片一颗都动不了（「停用只拦新引用不拦存量」是同一条约定）。
    """
    rows = list(Chip.objects.filter(number__in=numbers))
    found = {r.number for r in rows}
    missing = [n for n in numbers if n not in found]
    if missing:
        sample = '、'.join(missing[:3])
        more = '' if len(missing) <= 3 else f' 等 {len(missing)} 个'
        raise ValueError(f'芯片号不存在：{sample}{more}')

    wrong_owner = [r.number for r in rows
                   if r.institution_id is not None and r.institution_id != institution.id]
    if wrong_owner:
        raise ValueError(f'以下芯片不属于 {institution.name}：{"、".join(wrong_owner[:3])}')

    wrong_material = [r.number for r in rows
                      if r.material_id is not None and r.material_id != material.id]
    if wrong_material:
        raise ValueError(
            f'以下芯片不属于物料「{material.name}」：{"、".join(wrong_material[:3])}')
    return rows


def _require_status(rows, expected, action, hint):
    """要求这批行的状态都是 `expected`，否则报错（消息里点出前 3 个号码）。"""
    bad = [r.number for r in rows if r.status != expected]
    if bad:
        raise ValueError(f'以下芯片{action}：{"、".join(bad[:3])}{hint}')


def chip_batch_precheck(material, sender, first_number, count):
    """下发前的**只读**预检 —— 返回推算出的号码，不合法则 raise。

    存在的意义：下发接口要在**扣库存之前**就把号码问题挡住。
    若把校验留到事务里的 `chip_batch_dispatch()`，异常会打成 500，
    而且用户拿不到「哪个号码不属于本院」这句文案；
    更糟的是「先扣库存再报错」会留下扣了库存却没发出芯片的半截状态。
    """
    if material is None or sender is None:
        raise ValueError('芯片下发必须指明物料与发起机构')
    numbers = parse_chip_numbers(first_number, count)
    rows = _load_batch_rows(numbers, material, sender)
    _require_status(rows, 'available', '不可用（已在途/已使用/已作废）', '')
    return numbers


def chip_batch_dispatch(material, sender, first_number, count, txn):
    """下发：把这批号码标为「在途」并挂到下发流水上。

    :param txn: 已经建好的 `dispatch` 流水（`adjust_stock` 的返回值）
    :return: 标为在途的号码列表

    为什么是「在途」而不是直接改归属：物资流转的口径是
    **建单时发起方就出库、签收时接收方才入库，中间两边都不算**。
    芯片必须跟同一条口径走，否则在途期间「芯片数」与「库存数」必然对不上
    （发起方库存已经 -N，芯片却还挂在他名下、接收方也还没到手）。
    """
    if material is None or sender is None:
        raise ValueError('芯片下发必须指明物料与发起机构')
    if txn is None:
        raise ValueError('芯片下发必须关联下发流水')

    numbers = parse_chip_numbers(first_number, count)
    rows = _load_batch_rows(numbers, material, sender)
    _require_status(rows, 'available', '不可用（已在途/已使用/已作废）', '')

    Chip.objects.filter(pk__in=[r.pk for r in rows]).update(
        status='in_transit', dispatch_txn=txn)
    return numbers


def chip_batch_void(material, institution, first_number, count):
    """作废：把这批号码标为「已作废」（损坏 / 丢失报废）。

    :return: 作废的号码列表

    为什么芯片报废必须**按号码**：库存异动会让 `MaterialStock` 减掉 N，
    若芯片行不动，「可用芯片数」就比库存多出 N，**且没有任何报错**。
    与入库 / 下发同一套「首号 + 数量」口径，判据也就只有一份。

    ⚠ 只作废 `available` 的：在途的（货在路上、两边都不算库存）与
    已使用的（库存那一步已经扣过了）都不能再作废一次，否则库存被重复扣。
    已作废的再作废一次同样是重复扣。
    """
    if material is None or institution is None:
        raise ValueError('芯片作废必须指明物料与持有机构')

    numbers = parse_chip_numbers(first_number, count)
    rows = _load_batch_rows(numbers, material, institution)
    _require_status(rows, 'available', '不可作废（已在途/已使用/已作废）', '')

    # ⚠ 作废**不删行**：`void` 保留在表里 ——「这颗哪去了」本身就是要能答的问题。
    Chip.objects.filter(pk__in=[r.pk for r in rows]).update(status='void')
    return numbers


def chip_batch_receive(txn, receiver):
    """签收：把这批在途芯片转到接收机构名下，状态回到「可用」。

    :return: 实际转移的芯片个数（非芯片物料返回 0）

    ⚠ **数量必须与下发单一致**，不一致就报错而不是照单全收：
    这是「强绑定」的下游闸门 —— 若不一致，说明在途芯片被人为改动过
    （直接改库、或并发签收），照收会让「芯片数」与「库存数」悄悄错开。
    """
    if txn is None or receiver is None:
        return 0

    material = txn.material
    if material is None or material.category != 'chip':
        return 0

    transit = Chip.objects.filter(dispatch_txn=txn, status='in_transit')
    transit_count = transit.count()
    if transit_count != txn.quantity:
        raise ValueError(
            f'在途芯片数（{transit_count}）与下发数量（{txn.quantity}）不一致，'
            f'拒绝签收，请联系管理员核对')

    transit.update(institution=receiver, status='available', dispatch_txn=None)
    return transit_count


def count_available_chips(material, institution):
    """某机构手上**还能用**的芯片个数 —— 与 `MaterialStock` 对账的基准。

    只算 `available`：`used` 的已经消耗过（库存那一步减过了），
    `in_transit` 的已经出库但对方还没签收（两边都不该算）。
    """
    if material is None or institution is None:
        return 0
    return Chip.objects.filter(
        material=material, institution=institution, status='available').count()


def available_chip_queryset(institution, material=None):
    """某机构可选的芯片（供医院端「从本院库存里挑芯片」用，v5.1 原型口径）。

    :param institution: 持有机构（本院）
    :param material: 只列该物料的；None = 该机构名下**全部**可用芯片
    :return: 按号码升序的 QuerySet

    只含 `available` —— `used` 的已经植出去了、`in_transit` 的还在路上，
    都不该出现在「挑一颗来用」的列表里。

    ⚠ 重构前的存量行（`institution` 为 NULL）天然被排除：它们的归属信息
    在数据里已经丢了，挑出来也没法确定该扣哪条物料的库存。
    历史芯片要能被用掉，走的是 `use_chip()` 的存量放行路径，不是这里。
    """
    if institution is None:
        return Chip.objects.none()
    qs = Chip.objects.filter(institution=institution, status='available')
    if material is not None:
        qs = qs.filter(material=material)
    return qs.order_by('number')
