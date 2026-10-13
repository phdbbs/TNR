"""芯片状态补一态：`void`（已作废）。

为什么必须有这一态
------------------
第四十六轮把芯片改成「按号码单独管理」之后，「数量变了、号码也要跟着变」
就成了硬约束。而库存异动（`stock_adjustment`：损坏 / 丢失 / 过期报废）
会让 `MaterialStock` 减掉 N，若芯片行不动，「可用芯片数」就比库存多出 N
—— 且**没有任何报错**，只有把两边摊开对才看得出来。

于是芯片状态从三态扩成四态：

- `available`  可用
- `in_transit` 在途（随下发单出去了、对方还没签收）
- `used`       已使用（植出去了）
- `void`       已作废（损坏 / 丢失报废）← 本迁移新增

⚠ 作废**不删行**：`void` 的号码保留在表里 ——「这颗哪去了」本身就是要能答的
问题，删行等于把追溯链抹掉（与项目「逻辑删除、禁物理删除」同一条口径）。

本迁移只改 `choices`（Django 层面是 `AlterField`），**不动任何数据**：
存量行的状态值仍落在原三态里，`void` 只由新代码写入。
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0025_chip_per_number_with_institution'),
    ]

    operations = [
        migrations.AlterField(
            model_name='chip',
            name='status',
            field=models.CharField(choices=[('available', '可用'), ('in_transit', '在途'), ('used', '已使用'), ('void', '已作废')], default='available', max_length=20, verbose_name='状态'),
        ),
    ]
