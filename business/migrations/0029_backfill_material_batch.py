"""把存量机构库存物化成「初始批次」行（B5 物料批次子表）。

**具体口径不写在这里** —— 它与运行时共用同一份实现，见
`business/stock_init.init_batch_rows`（含「为什么每行库存对应一条批次、
为什么不造假流水、批号为什么取主表快照」的完整说明）。

与 `0020` 同一模式：全新库（`seed_data` 调 `stock_init`）与升级库（本迁移）
必须得到**同一种数据形态**，实现只有一份，两处各写迟早漂移。

对账含义：回填后 `MaterialStock.quantity == Σ MaterialBatch.quantity`
**从第一天起精确成立**（每行库存拆出一条等量批次），这也是
`reconcile_material_stock` 批次对账判据的基线。
"""
from django.db import migrations

from business.stock_init import init_batch_rows


def backfill(apps, schema_editor):
    Material = apps.get_model('business', 'Material')
    MaterialStock = apps.get_model('business', 'MaterialStock')
    MaterialBatch = apps.get_model('business', 'MaterialBatch')
    created = init_batch_rows(Material, MaterialStock, MaterialBatch)
    print(f'  [0029] 存量批次回填 {created} 行')


def unbackfill(apps, schema_editor):
    """反向不删数据：`0028` 反向会删整张批次表，且反向执行时表里可能已有
    上线后新增的真实批次行，一把清空等于删业务数据。保留原样。"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0028_materialbatch_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
