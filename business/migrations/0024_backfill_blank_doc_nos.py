"""给**空号**单据补号（第四十六轮：全局单据化收尾）。

`0023` 只重排了**已经有号**的行（`if not row.ledger_no: continue`），
空号的一律跳过 —— 于是两类单据永远没有号，打印出来是「单据号：—」：

  ① **物料消耗流水**（`type='consume'`）：由 `views_treatment` 经
     `adjust_stock(txn_type='consume')` **间接**写入，三个调用点都没传
     `ledger_no`；且 `0023` 的 `TYPE_PREFIX` 里也没有 `consume`。
     本地库实测：68 条消耗流水里 66 条空号、2 条是旧格式 `CON-2025-…`。
  ② **存量诊疗单**：由种子/实测脚本直接插入、绕过了视图取号。

本迁移是**纯增量**的：只给 `ledger_no` 为空的行补号，**不修改任何已有单号**
（已有号可能已经印在纸上、写在档案上）。补出来的号从各类型码的
**当前计数器之后**接着排，按 `created_at` 顺序。

⚠ 不用 `generate_doc_no()`：迁移里拿不到 `services`（历史迁移必须自洽），
   而且这里要显式控制「从当前计数器续排」的语义。
"""
from django.db import migrations
from django.utils import timezone

# 与 `business/services.py::DOC_PREFIXES` 保持一致。
# ⚠ 只列**这里需要补号**的模型；`CON` 等物料类型按 `type` 分派。
DOC_MODELS = [
    ('capture', 'CAP'),
    ('transfer', 'TRF'),
    ('ownerreturn', 'RET'),
    ('release', 'REL'),
    ('adoption', 'ADP'),
    ('euthanasia', 'EUT'),
    ('treatment', 'TRE'),
]

MT_TYPE_PREFIX = {
    'purchase': 'PUR',
    'dispatch': 'DIS',
    'receive': 'RCV',
    'adjustment': 'ADJ',
    'consume': 'CON',
}


def backfill(apps, schema_editor):
    DocumentSequence = apps.get_model('business', 'DocumentSequence')
    counters = {}

    def next_no(prefix, yy):
        key = (prefix, yy)
        if key not in counters:
            row = DocumentSequence.objects.filter(prefix=prefix, year=yy).first()
            counters[key] = row.last_no if row else 0
        counters[key] += 1
        return f'{prefix}{yy:02d}{counters[key]:05d}'

    def yy_of(dt):
        # created_at 是 UTC：年份按本地时间取，避免跨年凌晨归错年
        return timezone.localtime(dt).year % 100 if dt else 26

    for name, prefix in DOC_MODELS:
        M = apps.get_model('business', name)
        for row in M.objects.filter(ledger_no='').order_by('created_at', 'id'):
            row.ledger_no = next_no(prefix, yy_of(row.created_at))
            row.save(update_fields=['ledger_no'])

    MT = apps.get_model('business', 'MaterialTransaction')
    for txn in MT.objects.filter(ledger_no='').order_by('created_at', 'id'):
        prefix = MT_TYPE_PREFIX.get(txn.type)
        if not prefix:
            continue
        txn.ledger_no = next_no(prefix, yy_of(txn.created_at))
        txn.save(update_fields=['ledger_no'])

    # 计数器对齐到已补的最大流水（不覆盖已有行，只把值抬到新号之上）
    for (prefix, yy), last in counters.items():
        DocumentSequence.objects.update_or_create(
            prefix=prefix, year=yy, defaults={'last_no': last})


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0023_renumber_doc_nos'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
