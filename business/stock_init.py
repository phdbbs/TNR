from django.db.models import Sum

"""机构库存的**存量初始化** —— 迁移与种子数据共用同一份口径（第四十五轮）。

为什么要有这个模块
------------------
「把存量数据搬进 `MaterialStock`」这件事有**两个触发点**：

1. `0020_backfill_material_stock` 迁移 —— 老库升级时跑一次；
2. `seed_data` —— 全新库 / `--flush` 之后造演示数据时跑。

两处若各写一份，迟早漂移：迁移里改了「多捕捉点不猜」的规则，
种子数据里还是老的均分逻辑，于是**全新库与升级库的数据形态不一致** ——
这种不一致极难发现，因为两边都「跑成功了」，只是数字不同。

于是把口径提在这里，两边**传模型类进来**：迁移传历史模型（`apps.get_model`），
种子数据传当前模型。函数只依赖传入的模型，不 import 任何 app 模块，
所以放在迁移里引用是安全的（迁移只依赖签名，不依赖实现随版本变化）。

口径
----
- 回填 `MaterialTransaction.institution`：只在**无歧义**时做
  （医院侧总能；捕捉点侧只在该区县**恰好一个**捕捉点时）；
- 捕捉点库存行：`quantity = Material.shelter_stock`（界面原值不变），
  `opening_quantity = shelter_stock − 已回填的流水合计`
  → 于是 `quantity == opening_quantity + 流水合计` **精确成立**；
- 医院库存行：`opening_quantity = 0`、`quantity = 流水合计`
  （正好等于 `services.get_hospital_stock()`，成为双跑基准）。

幂等：只在「该物料还没有任何机构库存行」时建行，重跑不会翻倍。
"""


def shelter_map(Institution):
    """区县 id → 该区县的捕捉点列表。"""
    mapping = {}
    for inst in Institution.objects.filter(type='shelter'):
        mapping.setdefault(inst.district_id, []).append(inst)
    return mapping


def sole_shelter(district_id, shelters):
    """该区县唯一的捕捉点；0 个或多个时返回 None（**不猜**）。"""
    found = shelters.get(district_id) or []
    return found[0] if len(found) == 1 else None


def backfill_txn_institution(MaterialTransaction, Institution):
    """回填流水的「库存归属机构」。返回改动行数。

    | 流水类型 | 归属机构 | 能否回填 |
    |---|---|---|
    | `purchase` / `adjustment`（`hospital` 为空） | 发起捕捉点 | 该区县恰好一个捕捉点时可 |
    | `receive` / `consume` / `adjustment`（`hospital` 不为空） | 该医院 | 总能 |
    | `dispatch` | 发起捕捉点 | **不能** —— 历史行只记了接收方 |

    `dispatch` 行回填不了是本改造**唯一**无法补齐的历史缺口。
    它不影响对账：那些行 `institution` 保持 NULL，既不进任何机构的流水合计、
    也不进任何机构库存行，两边同时缺席 = 仍然相等。
    现场两个捕捉点操作员当初都挂在「全市（市级）」下，同一区县若有第二个
    捕捉点，猜谁发的就是把库存记到别的机构名下 —— 宁可留空。
    """
    shelters = shelter_map(Institution)
    filled = 0
    for txn in MaterialTransaction.objects.filter(institution__isnull=True).iterator():
        target = None
        if txn.hospital_id:
            if txn.type != 'dispatch':
                target = txn.hospital_id
        else:
            sole = sole_shelter(txn.district_id, shelters)
            target = sole.id if sole is not None else None
        if target:
            MaterialTransaction.objects.filter(pk=txn.pk).update(institution_id=target)
            filled += 1
    return filled


def ledger_total(MaterialTransaction, material_id, institution_id):
    """按「库存归属机构」把流水加起来（与 `services.get_institution_ledger_total` 同口径）。"""
    base = MaterialTransaction.objects.filter(
        material_id=material_id, institution_id=institution_id)

    def _sum(*types):
        total = 0
        for txn_type in types:
            total += base.filter(type=txn_type).aggregate(
                total=Sum('quantity'))['total'] or 0
        return total

    return _sum('purchase', 'receive') - _sum('dispatch', 'consume', 'adjustment')


def init_stock_rows(Material, MaterialStock, MaterialTransaction, Institution):
    """建立机构库存行。返回 (捕捉点行数, 医院行数)。"""
    shelters = shelter_map(Institution)
    shelter_rows = 0
    hospital_rows = 0

    for material in Material.objects.iterator():
        if MaterialStock.objects.filter(material_id=material.id).exists():
            continue  # 幂等闸门
        sole = sole_shelter(material.district_id, shelters)
        if sole is None:
            # 0 个或多个捕捉点 —— 无法无损对应，跳过。
            # ⚠ 多个时**不能**均分、也不能全给第一个：那都是凭猜记账，
            #   且没有任何报错（一个点凭空多出库存、另一个显示 0）。
            #   读取侧 `get_shelter_stock()` 会回退到 `shelter_stock`，界面照旧。
            continue
        ledger = ledger_total(MaterialTransaction, material.id, sole.id)
        MaterialStock.objects.create(
            material_id=material.id,
            institution_id=sole.id,
            quantity=material.shelter_stock,
            opening_quantity=material.shelter_stock - ledger,
        )
        shelter_rows += 1

    pairs = (MaterialTransaction.objects
             .filter(hospital__isnull=False, type__in=('receive', 'consume', 'adjustment'))
             .values_list('material_id', 'hospital_id').distinct())
    for material_id, hospital_id in pairs:
        if MaterialStock.objects.filter(
                material_id=material_id, institution_id=hospital_id).exists():
            continue
        MaterialStock.objects.create(
            material_id=material_id,
            institution_id=hospital_id,
            quantity=ledger_total(MaterialTransaction, material_id, hospital_id),
            opening_quantity=0,
        )
        hospital_rows += 1

    return shelter_rows, hospital_rows


def initialize(Material, MaterialStock, MaterialTransaction, Institution):
    """完整初始化：先回填流水归属，再建库存行。返回 (回填行数, 捕捉点行数, 医院行数)。"""
    filled = backfill_txn_institution(MaterialTransaction, Institution)
    shelter_rows, hospital_rows = init_stock_rows(
        Material, MaterialStock, MaterialTransaction, Institution)
    return filled, shelter_rows, hospital_rows
