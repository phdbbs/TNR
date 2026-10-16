"""机构库存对账 —— 把「已经算好的库存」拉回与流水一致（第四十五轮 Q3 方案 2）。

背景
----
`MaterialStock.quantity` 是**增量维护**的：每次采购/下发/签收/消耗/异动，
在同一个事务里把对应机构那一行加或减。这换来了「看库存 = 直接读一行，
不做全量累加」，代价是**一旦有哪条路径漏了维护，数字就会慢慢飘**，
而且全程不报错 —— 界面上的库存与台账对不上，没有任何提示。

所以必须有一个能**主动发现并修回**的工具：
- `check_data_integrity` 负责**发现**（只读，退出码 1）；
- 本命令负责**修复**（默认 dry-run 只打印，加 `--apply` 才写库）。

修法
----
以**流水**为准重算：`quantity = opening_quantity + 按机构归属的流水合计`。
流水是完整记录（每一笔业务都留痕），机构库存是它的**缓存**，
缓存错了就按真源重建 —— 反过来（改流水去迁就缓存）会抹掉审计痕迹。

⚠ 本命令**不会**改 `MaterialTransaction`，一行都不动。

用法：
    python manage.py reconcile_material_stock            # 只报告（dry-run）
    python manage.py reconcile_material_stock --apply    # 真的修
    python manage.py reconcile_material_stock --shelter-field
        # 顺带把兼容字段 `Material.shelter_stock` 按新表重算
        # （默认**不动**它：存量数据的旧值与新表本来就可能不等，
        #   这是预期内的，不是缺陷 —— 见 0020 迁移的说明）
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from business.models import Material, MaterialBatch, MaterialStock
from business.services import get_institution_ledger_total, get_shelter_stock


class Command(BaseCommand):
    help = '按流水重算机构库存（默认 dry-run，--apply 才写库）'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真的写库；不加则只打印将要做的改动')
        parser.add_argument('--shelter-field', action='store_true',
                            help='同时把兼容字段 Material.shelter_stock 按机构库存重算')
        parser.add_argument('--limit', type=int, default=50,
                            help='最多列出多少条明细（默认 50）')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        limit = max(1, options['limit'])

        rows = (MaterialStock.objects
                .select_related('material', 'institution').order_by('id'))

        planned = []
        for row in rows:
            ledger = get_institution_ledger_total(row.material, row.institution)
            expected = row.opening_quantity + ledger
            if row.quantity != expected:
                planned.append((row, expected))

        if not planned:
            self.stdout.write(self.style.SUCCESS('机构库存与流水全部一致，无需修复'))
        else:
            self.stdout.write(self.style.WARNING(
                f'发现 {len(planned)} 行机构库存与流水不一致：'))
            for row, expected in planned[:limit]:
                self.stdout.write(
                    f'    - {row.institution.name} / {row.material.name}：'
                    f'{row.quantity} → {expected}'
                    f'（期初 {row.opening_quantity} + 流水 '
                    f'{expected - row.opening_quantity}）')
            if len(planned) > limit:
                self.stdout.write(f'    …（其余 {len(planned) - limit} 行未列出）')

        if apply_changes and planned:
            with transaction.atomic():
                for row, expected in planned:
                    MaterialStock.objects.filter(pk=row.pk).update(quantity=expected)
            self.stdout.write(self.style.SUCCESS(f'已修正 {len(planned)} 行'))

        # 兼容字段（默认不动）
        if options['shelter_field']:
            drift = []
            for material in Material.objects.iterator():
                current = get_shelter_stock(material)
                if current != material.shelter_stock:
                    drift.append((material, current))
            if not drift:
                self.stdout.write('兼容字段 Material.shelter_stock 与新表一致')
            else:
                self.stdout.write(self.style.WARNING(
                    f'发现 {len(drift)} 条物料的 shelter_stock 与新表不一致：'))
                for material, current in drift[:limit]:
                    self.stdout.write(
                        f'    - {material.name}：{material.shelter_stock} → {current}')
                if apply_changes:
                    with transaction.atomic():
                        for material, current in drift:
                            Material.objects.filter(pk=material.pk).update(
                                shelter_stock=current)
                    self.stdout.write(self.style.SUCCESS(f'已修正 {len(drift)} 条物料'))

        # ---- B5：批次对账（库存行 == Σ批次）----
        # 批次行与库存行同为「已算好的结果」，两者必须精确相等；不等 =
        # 批次记账路径漏了一次维护（或有人绕过 adjust_stock 直改批次）。
        # 修法与主对账同向：以**库存行**为基准把批次余量整体重铺
        # （比例分配无意义 —— 批次是物理批次，库存行是它们的合计，
        #   合计对了、分布错了只能按 FEFO 顺序前铺，至少与扣减顺序一致）。
        batch_drift = []
        for row in MaterialStock.objects.select_related('material', 'institution'):
            batch_total = MaterialBatch.objects.filter(
                material=row.material, institution=row.institution
            ).aggregate(total=Sum('quantity'))['total'] or 0
            if batch_total != row.quantity:
                batch_drift.append((row, batch_total))
        if not batch_drift:
            self.stdout.write('机构库存与批次明细全部一致')
        else:
            self.stdout.write(self.style.WARNING(
                f'发现 {len(batch_drift)} 行机构库存与批次合计不一致：'))
            for row, batch_total in batch_drift[:limit]:
                self.stdout.write(
                    f'    - {row.institution.name} / {row.material.name}：'
                    f'库存 {row.quantity}，批次合计 {batch_total}')
            if len(batch_drift) > limit:
                self.stdout.write(f'    …（其余 {len(batch_drift) - limit} 行未列出）')
            if apply_changes:
                with transaction.atomic():
                    for row, batch_total in batch_drift:
                        # 差额（库存 - 批次合计）落到 **FEFO 前排**的批次上：
                        # 与扣减顺序同向，误差集中在最快被消耗核对掉的批次，
                        # 而不是按比例摊薄到每个批次（那会把漂移伪装成正常）。
                        # 没有任何批次行时（库存行在、批次从未物化）按主表
                        # 快照补一条初始批次 —— 与 0029 迁移同口径。
                        delta = row.quantity - batch_total
                        first = MaterialBatch.objects.filter(
                            material=row.material, institution=row.institution
                        ).order_by('expiry_date', 'id').first()
                        if first is not None:
                            MaterialBatch.objects.filter(pk=first.pk).update(
                                quantity=first.quantity + delta)
                        elif row.quantity:
                            from business.stock_init import initial_batch_no
                            MaterialBatch.objects.create(
                                material=row.material, institution=row.institution,
                                batch_no=initial_batch_no(row.material),
                                expiry_date=row.material.expiry_date,
                                quantity=row.quantity)
                self.stdout.write(self.style.SUCCESS(
                    f'已按库存行修正 {len(batch_drift)} 组批次余量'))

        if not apply_changes and (planned or batch_drift or options['shelter_field']):
            self.stdout.write('')
            self.stdout.write('这是 **dry-run**，什么都没有写。确认无误后加 --apply。')
