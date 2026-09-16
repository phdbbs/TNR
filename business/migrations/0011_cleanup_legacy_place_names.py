"""清理北京地名残留：机构地址 + 写入时固化的冗余名称副本。

第三轮把区县名、机构名改成了襄阳本地名称，但**只覆盖了这两处展示字段**。
还有两类数据停留在北京时期：

1. 机构地址
   机构名的改名是逐条手工做的，address 没跟着改，至今仍是
   「朝阳区建国路88号」「海淀区中关村大街15号」这类北京地址。

2. 冗余名称副本
   `operator_name` / `shelter_name` / `from_shelter_name` / `from_to` /
   `body_received_by_name` 都是在写入时从 User / Institution 拷贝下来的快照
   （为了列表页不必 JOIN，也为了保留操作时的历史名称）。用户显示名和机构名
   后来改了，这些快照不会跟着变，于是界面上出现「朝阳捕捉点操作员」。

   这里按「当前值仍等于已知旧值」的守卫做替换——只认已知的旧种子值，
   不碰运营人员自己填过的内容。

注意：这些是展示性数据，不影响业务判定；但留着会让演示看起来
「襄阳的业务数据配北京的地名」，也会干扰按名称做的排查。
"""
from django.db import migrations

# 机构 code → (旧地址, 新地址)；只在地址仍等于旧值时改写
INSTITUTION_ADDRESS_FIXES = [
    ('I001', '朝阳区建国路88号', '襄城区檀溪路88号'),
    ('I002', '海淀区中关村大街15号', '樊城区长虹路15号'),
    ('I003', '朝阳区三里屯路12号', '襄城区鼓楼路12号'),
    ('I004', '朝阳区望京SOHO旁', '襄城区人民广场旁'),
    ('I005', '海淀区五道口', '樊城区解放路'),
    ('I006', '西城区西单', '东经开发区和谐路8号'),
    ('C001', '朝阳区阳光花园', '襄城区阳光花园'),
    ('C002', '朝阳区翠湖天地', '襄城区翠湖天地'),
    ('C003', '海淀区中关村南区', '樊城区幸福路12号'),
    ('C004', '西城区西单北大街', '东经开发区和谐路6号'),
]

# 冗余名称副本：旧值 → 新值
LEGACY_NAME_REPLACEMENTS = {
    '朝阳区流浪动物捕捉点': '襄城流浪动物捕捉点',
    '海淀区流浪动物捕捉点': '樊城流浪动物捕捉点',
    '朝阳捕捉点操作员': '襄城捕捉点操作员',
    '海淀捕捉点操作员': '樊城捕捉点操作员',
    '朝阳区政府管理员': '襄城区政府管理员',
    '海淀区政府管理员': '樊城区政府管理员',
}

# (app_label, model, 字段) —— 需要做名称替换的冗余副本字段
DENORMALIZED_FIELDS = [
    ('business', 'Capture', 'shelter_name'),
    ('business', 'Capture', 'operator_name'),
    ('business', 'Transfer', 'from_shelter_name'),
    ('business', 'Transfer', 'operator_name'),
    ('business', 'Treatment', 'operator_name'),
    ('business', 'MaterialTransaction', 'operator_name'),
    ('business', 'MaterialTransaction', 'from_to'),
    ('business', 'Release', 'operator_name'),
    ('business', 'Adoption', 'operator_name'),
    ('business', 'CheckIn', 'operator_name'),
    ('business', 'Blacklist', 'operator_name'),
    ('business', 'Euthanasia', 'operator_name'),
    ('business', 'Euthanasia', 'body_received_by_name'),
]

# 种子捕捉单的地址：ledger_no → (旧地址, 新地址)
CAPTURE_ADDRESS_FIXES = [
    ('CAP-2025-0010-001', '朝阳区阳光花园3栋', '襄城区阳光花园3栋'),
    ('CAP-2025-0115-001', '海淀区中关村南区5栋', '樊城区幸福路12号5栋'),
]

# 其他逐条固化的地址（种子定义里已改对，但 get_or_create 不会更新已存在的行）
OTHER_ADDRESS_FIXES = [
    ('business', 'Adoption', 'adopter_address', '朝阳区某某小区', '襄城区某某小区'),
]


def fix_legacy_place_names(apps, schema_editor):
    Institution = apps.get_model('core', 'Institution')

    # ---- 1. 机构地址 ----
    for code, old, new in INSTITUTION_ADDRESS_FIXES:
        Institution.objects.filter(code=code, address=old).update(address=new)

    # ---- 2. 冗余名称副本 ----
    total = 0
    for app_label, model_name, field in DENORMALIZED_FIELDS:
        model = apps.get_model(app_label, model_name)
        # 字段可能在某次重构中被删掉，跳过而不是让迁移整体失败
        if not any(f.name == field for f in model._meta.concrete_fields):
            continue
        for old, new in LEGACY_NAME_REPLACEMENTS.items():
            total += model.objects.filter(**{field: old}).update(**{field: new})

    # ---- 3. 种子捕捉单的地址 ----
    Capture = apps.get_model('business', 'Capture')
    for ledger_no, old, new in CAPTURE_ADDRESS_FIXES:
        Capture.objects.filter(ledger_no=ledger_no, address=old).update(address=new)

    # ---- 4. 其他逐条固化的地址 ----
    for app_label, model_name, field, old, new in OTHER_ADDRESS_FIXES:
        model = apps.get_model(app_label, model_name)
        if not any(f.name == field for f in model._meta.concrete_fields):
            continue
        total += model.objects.filter(**{field: old}).update(**{field: new})

    if total:
        print(f'  已清理 {total} 处冗余名称副本中的北京地名')


def noop(apps, schema_editor):
    """回滚不做处理：这些旧值本身就是错的，没有还原的意义。"""


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0010_fix_legacy_capture_district_and_names'),
    ]

    operations = [
        migrations.RunPython(fix_legacy_place_names, noop),
    ]
