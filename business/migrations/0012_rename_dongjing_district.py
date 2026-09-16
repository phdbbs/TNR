"""更正区县名笔误：「东经开发区」→「东津新区」。

第三轮把北京区县名换成了襄阳本地名，其中 `code='XC'` 那条写成了
「东经开发区」。襄阳市实际的功能区是**东津新区**（另有襄阳高新技术产业开发区、
鱼梁洲开发区），并不存在「东经开发区」——「经」是「津」的拼音笔误（jīng / jīn）。

机构地址里也跟着写了这个错名，一并更正。

**为什么放在 business 而不是 core**：本迁移要改写 `core.Institution.address`，
而 `business/0011` 恰好也会把 `I006`/`C004` 的地址写成「东经开发区和谐路X号」。
如果本迁移挂在 `core` 应用下，它只依赖 `core/0003`，与 `business/0011` 之间
**没有依赖关系、执行顺序不确定**——一旦本迁移先跑、`0011` 后跑，
笔误地址会被重新写回来。挂到 `business/0011` 之后即可保证顺序。
这也与既有惯例一致：`0010`/`0011` 同样是「地名修正」，同样在 business 应用下改 core 模型。

守卫条件：只在**当前值仍等于已知旧值**时改写，运营人员自己改过的名字不碰。
"""
from django.db import migrations

OLD_DISTRICT_NAME = '东经开发区'
NEW_DISTRICT_NAME = '东津新区'

# 机构 code → (旧地址, 新地址)
INSTITUTION_ADDRESS_FIXES = [
    ('I006', '东经开发区和谐路8号', '东津新区和谐路8号'),
    ('C004', '东经开发区和谐路6号', '东津新区和谐路6号'),
]


def rename_district(apps, schema_editor):
    District = apps.get_model('core', 'District')
    Institution = apps.get_model('core', 'Institution')

    n = District.objects.filter(code='XC', name=OLD_DISTRICT_NAME).update(
        name=NEW_DISTRICT_NAME)
    if n:
        print(f'  区县名已更正：「{OLD_DISTRICT_NAME}」→「{NEW_DISTRICT_NAME}」')

    for code, old, new in INSTITUTION_ADDRESS_FIXES:
        m = Institution.objects.filter(code=code, address=old).update(address=new)
        if m:
            print(f'  机构 {code} 地址已更正：{old} → {new}')


def noop(apps, schema_editor):
    """回滚不做处理：旧名是笔误，没有还原的意义。"""


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0011_cleanup_legacy_place_names'),
    ]

    operations = [
        migrations.RunPython(rename_district, noop),
    ]
