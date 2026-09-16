"""修正种子芯片号段：11 位 → 10 位。

背景：seed_data 早期用 ``f'1000010{i:04d}'`` 生成芯片号，补零宽度写成 4 位，
产生的是 11 位的 ``10000100001``-``10000100500``；而宠物档案
（Pet.chip_no）与诊疗记录（Treatment.chip_no）里用的是 10 位的
``1000010001``/``1000010006``/``1000010011``/``1000010016``（与
views_treatment 的接口示例、原型注释「号段1000010001-1000010500」一致）。

结果是芯片库存与业务档案对不上号：给这几只宠物登记「芯片植入」时，
treatment_create / use_chip 按 chip_no 查 Chip 查不到，直接报
「芯片 1000010001 不存在」。

本迁移把芯片表里 11 位的种子号规整为 10 位。只处理明确匹配
``^1000010\\d{4}$`` 的记录，且目标号不存在时才改写——不碰人工录入的
其他号段（如界面按号段批量导入的芯片）。
"""
import re

from django.db import migrations

SEED_CHIP_RE = re.compile(r'^1000010(\d{4})$')


def normalize_chip_numbers(apps, schema_editor):
    Chip = apps.get_model('business', 'Chip')

    existing = set(Chip.objects.values_list('number', flat=True))
    to_fix = []
    for chip in Chip.objects.all():
        m = SEED_CHIP_RE.match(chip.number or '')
        if not m:
            continue
        target = '1000010%03d' % int(m.group(1))
        if target == chip.number or target in existing:
            continue
        to_fix.append((chip, target))

    for chip, target in to_fix:
        chip.number = target
        chip.save(update_fields=['number'])
        existing.add(target)


def denormalize_chip_numbers(apps, schema_editor):
    """回滚：把 10 位种子号还原为 11 位（保持与旧种子数据一致）。"""
    Chip = apps.get_model('business', 'Chip')

    existing = set(Chip.objects.values_list('number', flat=True))
    to_fix = []
    for chip in Chip.objects.all():
        m = re.match(r'^1000010(\d{3})$', chip.number or '')
        if not m:
            continue
        target = '1000010%04d' % int(m.group(1))
        if target in existing:
            continue
        to_fix.append((chip, target))

    for chip, target in to_fix:
        chip.number = target
        chip.save(update_fields=['number'])
        existing.add(target)


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0008_blacklist_deleted_at_blacklist_is_deleted'),
    ]

    operations = [
        migrations.RunPython(normalize_chip_numbers, denormalize_chip_numbers),
    ]
