"""芯片改为**逐号 + 带归属**管理（第四十六轮）。

背景
----
重构前芯片是「双轨」的，两套**互不校验**：

- `Chip` 表逐号一行，但**没有机构归属** —— 号码是全局的，查不出
  「这批货在谁手上」，医院端也无从「从本院库存里挑芯片」；
- `Material.chip_range_start/end` 只记两个字符串，与实际建出来的
  `Chip` 行**数量不校验** —— 号段写着 100 个、实际只建了 30 行，
  界面照样按号段显示 100，全程没有任何报错。

本迁移做两件事
--------------
1. 删掉 `Material` 上的号段字段（双真源的一半），芯片一律以 `Chip` 表为准；
2. 给 `Chip` 补上归属与流转所需的列 —— `institution`（持有机构）、
   `material`（所属物料）、`batch_no`（入库批次号）、`dispatch_txn`
   （在途下发单），并把 `status` 扩成**三态**（新增 `in_transit`）。

为什么要有 `in_transit`
-----------------------
物资流转的口径是「建单时发起方就出库、签收时接收方才入库，中间两边都不算」
（见 `services_stock.adjust_stock` 与 `dispatch_create` 的注释）。芯片必须跟
同一条口径走，否则在途期间「芯片数」与「库存数」必然对不上 ——
发起方库存已经 −N，芯片却还挂在他名下。签收后靠 `dispatch_txn` 反查
「这批要收的是哪些号码」，转移归属并清空。

⚠ 存量行**不回填** `institution` / `material`
--------------------------------------------
重构前的号段与捕捉点的对应关系在数据里已经丢了：`Material` 上只有一串号码，
没有「这批货在哪个机构」。同一区县可能有多个捕捉点，猜错就是把芯片记到
别的机构名下（与 `MaterialTransaction.institution` 历史行同一条处理）。
因此本迁移**只加列、不写数据**：读取侧把 NULL 当作「未归属」，
写入侧**只拦新引用、不拦存量**（否则历史芯片一颗都用不出去）。

新产生的芯片（采购入库 / 种子数据）一律带机构与物料，所以
「甲院的芯片被乙院用掉」会被拦住。
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0024_backfill_blank_doc_nos'),
        ('core', '0005_institution_created_at'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='material',
            name='chip_range_end',
        ),
        migrations.RemoveField(
            model_name='material',
            name='chip_range_start',
        ),
        migrations.AddField(
            model_name='chip',
            name='batch_no',
            field=models.CharField(blank=True, default='', max_length=50, verbose_name='入库批次号'),
        ),
        migrations.AddField(
            model_name='chip',
            name='dispatch_txn',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transit_chips', to='business.materialtransaction', verbose_name='在途下发单'),
        ),
        migrations.AddField(
            model_name='chip',
            name='institution',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='chips', to='core.institution', verbose_name='持有机构'),
        ),
        migrations.AddField(
            model_name='chip',
            name='material',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='chips', to='business.material', verbose_name='所属物料'),
        ),
        migrations.AlterField(
            model_name='chip',
            name='status',
            field=models.CharField(choices=[('available', '可用'), ('in_transit', '在途'), ('used', '已使用')], default='available', max_length=20, verbose_name='状态'),
        ),
    ]
