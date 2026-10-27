"""只读数据盘点：各模型行数 + 宠物编号 + 账号用户名，用于开发库与生产库对比。

不做任何写操作。输出为稳定排序的 TSV，便于 diff。
"""
import django
django.setup()

from django.apps import apps
from django.db.models import Max


def emit(title, lines):
    print('## ' + title)
    for ln in lines:
        print(ln)
    print()


counts = []
for m in apps.get_models():
    if m._meta.app_label in ('business', 'core', 'accounts', 'supervision'):
        try:
            counts.append('%s\t%d' % (m._meta.label, m.objects.count()))
        except Exception as e:  # noqa: BLE001 盘点脚本：报错也要出结果
            counts.append('%s\tERR %s' % (m._meta.label, str(e)[:60]))
emit('counts', sorted(counts))

from business.models import Pet, Capture, MaterialTransaction  # noqa: E402

codes = sorted(Pet.objects.values_list('code', flat=True))
emit('pet_codes', codes)
emit('pet_last_created', ['%s' % Pet.objects.aggregate(Max('created_at'))['created_at__max']])
emit('capture_ledgers', sorted(Capture.objects.values_list('ledger_no', flat=True)))
emit('material_tx', ['%d' % MaterialTransaction.objects.count()])

from django.contrib.auth import get_user_model  # noqa: E402

U = get_user_model()
users = []
for u in U.objects.order_by('id'):
    users.append('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s' % (
        u.id, u.username, u.role, u.is_active, u.is_staff,
        u.district_id, u.institution_id,
        (u.created_at.strftime('%Y-%m-%d') if u.created_at else '-')))
emit('users', users)
emit('user_hash_prefixes', sorted(
    '%d\t%s\t%s' % (u.id, u.username, u.password[:12])
    for u in U.objects.order_by('id')))

# 状态覆盖：判据是「每个声明状态有没有可测的行」，开发库与生产库各出一份，diff 即缺口
from business import models as B  # noqa: E402

probe = [
    ('pet_status', B.Pet, 'status'),
    ('capture_status', B.Capture, 'status'),
    ('transfer_status', B.Transfer, 'status'),
    ('treatment_status', B.Treatment, 'status'),
    ('treatment_advised', B.Treatment, 'disposal_advised'),
    ('release_status', B.Release, 'status'),
    ('return_status', B.ReturnOrder, 'status'),
    ('adoption_status', B.Adoption, 'status'),
    ('adoption_app_status', B.AdoptionApplication, 'status'),
    ('checkin_status', B.CheckIn, 'status'),
    ('chip_status', B.Chip, 'status'),
    ('material_tx_type', B.MaterialTransaction, 'type'),
    ('material_tx_reason', B.MaterialTransaction, 'reason'),
]
from django.db.models import Count  # noqa: E402

for title, model, field in probe:
    dist = (model.objects.values(field).annotate(n=Count('id'))
            .order_by(field))
    emit(title, ['%s\t%d' % (d[field] or '(blank)', d['n']) for d in dist])

from business.services_lifecycle import pet_stage  # noqa: E402
from collections import Counter  # noqa: E402

emit('pet_stage', ['%s\t%d' % kv for kv in sorted(
    Counter(pet_stage(p) for p in Pet.objects.all()).items())])
emit('pet_total', ['%d' % Pet.objects.count()])

