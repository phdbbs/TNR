"""存量单据号按新规则重排（第四十六轮：全局单据化）。

新规则：`{类型码}{YY}{5位流水}`（如 CAP2600001），按「类型码 + 年份」
全市统一顺序递增 —— 打印归档时人眼可数「第几张单」，且不会重号。

旧号是「日期 + 4位随机」（CAP-260925-0142），既不是顺序、理论上还会撞号。
本迁移把**存量测试数据**按业务时间序重排成新号：

  ① 非接收类单据（捕捉/转运/领回/放归/领养/安乐死/诊疗/采购/下发/异动）
     各自按类型码、按 created_at 年份独立流水；
  ② 接收单：分配独立 RCV 号，`ref_no` 指向其签收的下发单**新号**
     （旧实现接收单复用下发单号，靠这个对应关系还原单据链）；
  ③ `DocumentSequence` 计数器对齐到已发最大流水，保证新号接着排。
"""
from django.db import migrations
from django.utils import timezone


TYPE_PREFIX = {
    'purchase': 'PUR', 'dispatch': 'DIS', 'adjustment': 'ADJ',
}

PET_DOC_MODELS = [
    ('capture', 'CAP'), ('transfer', 'TRF'), ('ownerreturn', 'RET'),
    ('release', 'REL'), ('adoption', 'ADP'), ('euthanasia', 'EUT'),
    ('treatment', 'TRE'),
]


def renumber(apps, schema_editor):
    DocumentSequence = apps.get_model('business', 'DocumentSequence')
    counters = {}

    def next_no(prefix, yy):
        key = (prefix, yy)
        counters[key] = counters.get(key, 0) + 1
        return f'{prefix}{yy:02d}{counters[key]:05d}'

    def yy_of(dt):
        # created_at 是 UTC：年份按本地时间取，避免跨年凌晨归错年
        return timezone.localtime(dt).year % 100 if dt else 26

    # ① 非接收类单据，按时间序重排
    for name, prefix in PET_DOC_MODELS:
        M = apps.get_model('business', name)
        for row in M.objects.order_by('created_at', 'id'):
            if not row.ledger_no:
                continue
            row.ledger_no = next_no(prefix, yy_of(row.created_at))
            row.save(update_fields=['ledger_no'])

    dispatch_old_new = {}
    MT = apps.get_model('business', 'MaterialTransaction')
    for txn in MT.objects.exclude(type='receive').order_by('created_at', 'id'):
        if txn.type in TYPE_PREFIX and txn.ledger_no:
            old = txn.ledger_no
            txn.ledger_no = next_no(TYPE_PREFIX[txn.type], yy_of(txn.created_at))
            txn.save(update_fields=['ledger_no'])
            if txn.type == 'dispatch':
                dispatch_old_new[old] = txn.ledger_no

    # ② 接收单：独立 RCV 号 + ref_no 指向下发单新号
    # 关联还原：正常路径下接收单复用下发单号（旧 ledger_no == dispatch 旧号）；
    # 种子数据等手工单的接收号是独立的（RVC-…），此时从备注
    # 「签收下发物料（原单号：DIS-…）」里解析旧下发单号兜底。
    import re as _re
    for txn in MT.objects.filter(type='receive').order_by('created_at', 'id'):
        ref_old = txn.ledger_no
        if ref_old not in dispatch_old_new:
            m = _re.search(r'原单号[:：]\s*([A-Z]+[-0-9]+)', txn.note or '')
            if m and m.group(1) in dispatch_old_new:
                ref_old = m.group(1)
        txn.ref_no = dispatch_old_new.get(ref_old, '')
        txn.ledger_no = next_no('RCV', yy_of(txn.created_at))
        txn.save(update_fields=['ledger_no', 'ref_no'])

    # ③ 计数器对齐（新号接着存量往后排）
    for (prefix, yy), last in counters.items():
        DocumentSequence.objects.update_or_create(
            prefix=prefix, year=yy, defaults={'last_no': last})


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0022_documentsequence_materialtransaction_ref_no_and_more'),
    ]

    operations = [
        migrations.RunPython(renumber, migrations.RunPython.noop),
    ]
