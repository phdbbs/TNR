# 机构补「创建时间」字段。
#
# 背景：政府端「机构管理」列表新增了统一的「起止时间」搜索条件，但
# `Institution` 是**唯一没有任何时间字段**的业务列表实体。前端 `inDateRange`
# 的语义是「设了区间、而这条记录没有时间 → 排除」，所以缺字段的表现不是报错，
# 而是**一填日期就整列空表**，看起来就像「这个区间真的没有机构」。
#
# 回填策略：`auto_now_add` 对新数据自动写入当天；对存量行用
# `default=timezone.now` 兜一次（DateField 的 `to_python` 会把 datetime 截成
# date），随后 `preserve_default=False` 把默认值从模型状态里去掉，避免以后
# 建对象时被静默塞一个「今天」。
#
# ⚠ 存量行因此会统一显示为**执行本次迁移当天**建档。这是加字段的固有代价：
# 真实建档时间在库里从未记录过，编不出来。但它比另一种选择（字段可空、
# 存量行永远筛不出来）好 —— 后者会让「设了区间就看不到老机构」变成常态。

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_auditlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='institution',
            name='created_at',
            field=models.DateField(auto_now_add=True, default=django.utils.timezone.now, verbose_name='创建时间'),
            preserve_default=False,
        ),
    ]
