"""修复历史遗留：捕捉单误挂「全市（市级）」区县 + 机构名称的北京地名残留。

两个问题同源——都是早期版本留下的数据，`get_or_create` 只在新建时应用
`defaults`，因此后来的修正（区县名、机构名的襄阳化）没能覆盖到已存在的行。

1. 捕捉单区县误标
   「新增捕捉登记」表单原先把归属区县默认成 `user.district_id`，而两个捕捉点
   操作员（cy_shelter / hd_shelter）被挂在「全市（市级）」区县下，于是现场
   7 条捕捉单全部落到市级。区县隔离是按 `district_id` 做的，后果是本区县政府
   （如襄城区 cy_gov）看不到本区捕捉点登记的捕捉单。

   这里把「区县是市级、但捕捉点机构坐落在具体区县」的捕捉单改判为该机构的
   区县，并同步其名下宠物档案的区县。只处理这种明确矛盾的情况，其余不动。

2. 机构名称残留北京地名
   C003「中关村南区」→「樊城幸福里小区」、C004「西单美居」→「开发区和谐家园」、
   I006 地址「东经开发区西单」→「东经开发区和谐路8号」。
   仅当字段仍等于旧的种子值时才改写，避免覆盖运营人员手工改过的名称。
   改名同时同步引用了旧名称的捕捉单（community_name 是文本匹配字段，
   放养流程靠它匹配小区，不同步会让那条捕捉单再也匹配不上小区）。
"""
from django.db import migrations

# 旧种子值 → 新值；只在「当前值仍等于旧值」时改写
INSTITUTION_RENAMES = [
    # (code, 字段, 旧值, 新值)
    ('C003', 'name', '中关村南区', '樊城幸福里小区'),
    ('C003', 'address', '樊城区中关村南区', '樊城区幸福路12号'),
    ('C004', 'name', '西单美居', '开发区和谐家园'),
    ('C004', 'address', '东经开发区西单北大街', '东经开发区和谐路6号'),
    ('I006', 'address', '东经开发区西单', '东经开发区和谐路8号'),
]


def fix_legacy_data(apps, schema_editor):
    District = apps.get_model('core', 'District')
    Institution = apps.get_model('core', 'Institution')
    Capture = apps.get_model('business', 'Capture')
    Pet = apps.get_model('business', 'Pet')

    # ---- 1. 机构改名（同时同步捕捉单上引用的旧名称） ----
    for code, field, old, new in INSTITUTION_RENAMES:
        inst = Institution.objects.filter(code=code, **{field: old}).first()
        if inst is None:
            continue
        setattr(inst, field, new)
        inst.save(update_fields=[field])

        # 小区改名后，引用旧小区名的捕捉单要一起改，否则放养流程按名称匹配会落空
        if field == 'name':
            Capture.objects.filter(community_id=inst.id).update(community_name=new)

    # ---- 2. 捕捉单区县误标 ----
    city_ids = list(District.objects.filter(is_city=True).values_list('id', flat=True))
    if city_ids:
        bad = Capture.objects.filter(district_id__in=city_ids).select_related('shelter')
        fixed = 0
        for cap in bad:
            shelter = cap.shelter
            target = getattr(shelter, 'district', None) if shelter else None
            # 只在捕捉点确实坐落在某个非市级区县时才改判
            if target is None or target.is_city:
                continue
            cap.district_id = target.id
            cap.save(update_fields=['district'])
            Pet.objects.filter(capture=cap).update(district_id=target.id)
            fixed += 1
        if fixed:
            print(f'  已修正 {fixed} 条捕捉单的归属区县（市级 → 捕捉点所在区县）')


def noop(apps, schema_editor):
    """回滚不做处理：改名/改判前的旧值无法唯一还原，且这些值本身是错的。"""


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0009_normalize_seed_chip_numbers'),
        ('core', '0003_institution_code'),
    ]

    operations = [
        migrations.RunPython(fix_legacy_data, noop),
    ]
