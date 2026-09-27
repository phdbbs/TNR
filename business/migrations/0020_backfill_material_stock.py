"""把存量数据搬进新的「机构库存」表（第四十五轮 Q3 方案 2）。

**具体口径不写在这里** —— 它和 `seed_data` 共用同一份实现，见
`business/stock_init.py`（含「哪些行能回填、为什么 `dispatch` 回填不了、
多个捕捉点时为什么不猜」的完整说明）。

为什么把实现提出去：全新库（走 `seed_data`）与升级库（走本迁移）必须得到
**同一种数据形态**。两处各写一份，一边改了规则另一边没改，就会出现
「升级库和全新库数字不一样」这种极难发现的偏差 —— 两边都跑成功了，
只是结果不同。

本迁移只做三件事：取**历史模型** → 调 `stock_init.initialize()` → 打印结果。
"""
from django.db import migrations

from business.stock_init import initialize


def backfill(apps, schema_editor):
    Material = apps.get_model('business', 'Material')
    MaterialStock = apps.get_model('business', 'MaterialStock')
    MaterialTransaction = apps.get_model('business', 'MaterialTransaction')
    Institution = apps.get_model('core', 'Institution')

    filled, shelter_rows, hospital_rows = initialize(
        Material, MaterialStock, MaterialTransaction, Institution)
    print(f'  [0020] 流水归属回填 {filled} 行；'
          f'捕捉点库存 {shelter_rows} 行；医院库存 {hospital_rows} 行')


def unbackfill(apps, schema_editor):
    """反向不删数据。

    `0019` 反向会把整张表删掉，这里再删一次没有意义；更重要的是，
    反向执行时表里可能已经有**上线后新增**的机构库存行，
    一把清空会把真实业务数据删掉。保留原样。
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0019_material_stock'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
