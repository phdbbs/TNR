"""把**演示物料**的有效期按「相对今天」重置。

背景：`seed_data` 早期把物料有效期写成了绝对日期（2025-12-31 / 2025-10-31 /
2026-06-30），已在 `3940ef6` 改成相对今天生成。但 `get_or_create(name=...,
defaults=...)` 的 `defaults` **只在创建时生效** —— 所以**已经导入过的库修不回来**，
重跑 `seed_data` 也不行。本命令专门订正这类存量行。

**判据（三个条件同时成立才改）**：
  1. 物料名在 `seed_data.SEED_MATERIALS` 里；
  2. 所属区县是**演示区县**（`SEED_MATERIAL_DISTRICT_CODES`，襄城区 / 樊城区）；
  3. `expiry_date` **存在且已过期**。

第 2 条不能省：实测现场 4 个区县各有 4 件同名物料，只有演示区县那几套是
演示数据，其余是别处产生的 —— 只按名称匹配会**误伤真实业务数据**。

⚠ 演示区县是**复数**（`SEED_MATERIAL_DISTRICT_CODES` 是元组）。漏掉其中
任何一个，那个区县的演示物料过期后就**永远修不回来** —— 命令不会报错，
只是"改得比预期少"。

第 3 条也不能省：`expiry_date=None` 表示「没登记有效期」（芯片就是），
**不等于过期**。曾把它判成「需要改」，等于给用户没登记有效期的物料
凭空编一个日期。

⚠ 已过期的**非演示物料**只报告、不改，那些是真实业务数据。

用法：
    python manage.py refresh_demo_material_expiry            # 预演，只打印
    python manage.py refresh_demo_material_expiry --apply    # 真正写库
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from business.management.commands.seed_data import (
    SEED_DISTRICT_CODE_BY_INTERNAL, SEED_MATERIAL_DISTRICT_CODES, SEED_MATERIALS,
)
from business.models import Material
from core.models import District

# **(区县实际 code, 物料名)** → 有效期距今天数（从 seed_data 的清单派生，单一来源）
#
# ⚠ 键里**必须带区县**。演示物料在多个演示区县里**同名各有一条**
# （襄城区与樊城区各有一套「狂犬疫苗」），只按名称索引会让后一条
# **静默覆盖**前一条 —— 于是其中一个区县的物料被改成另一个区县的日期，
# 而命令不会报错、看起来完全正常。
DEMO_MATERIAL_OFFSETS = {
    (SEED_DISTRICT_CODE_BY_INTERNAL[district_code], name): offset
    for (_id, name, _cat, _unit, _spec, _sup, _batch,
         _stock, _safety, offset, _cs, _ce, district_code) in SEED_MATERIALS
    if offset is not None
}

# 演示物料名（去重后的全集）—— 用于 `name__in` 查询与日志展示
DEMO_MATERIAL_NAMES = sorted({name for _code, name in DEMO_MATERIAL_OFFSETS})


class Command(BaseCommand):
    help = '把演示物料的有效期按「相对今天」重置（默认预演，--apply 落库）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='真正写库；不加则只打印将要做的改动（预演）',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        today = timezone.localdate()
        self.stdout.write(f'今天 = {today}')

        districts = list(District.objects.filter(
            code__in=SEED_MATERIAL_DISTRICT_CODES).order_by('code'))
        missing = sorted(set(SEED_MATERIAL_DISTRICT_CODES)
                         - {d.code for d in districts})
        if missing:
            self.stderr.write(self.style.ERROR(
                f'找不到演示区县（code={"/".join(missing)}），'
                f'无法确定订正范围，已中止。'))
            return
        self.stdout.write(
            f'演示区县 = {"、".join(f"{d.code}/{d.name}" for d in districts)}；'
            f'演示物料名 = {"、".join(DEMO_MATERIAL_NAMES)}')

        rows = list(Material.objects.filter(
            district__in=districts,
            name__in=DEMO_MATERIAL_NAMES
        ).select_related('district').order_by('district__code', 'id'))

        if not rows:
            self.stdout.write('演示区县内未找到演示物料，无需订正。')
        else:
            changed = 0
            for m in rows:
                # ⚠ 输出必须带区县：演示物料**同名存在于多个区县**
                # （襄城区/樊城区各一套「狂犬疫苗」），只印名称看不出改的是哪一条。
                tag = f'{m.district.code}/{m.name}'
                if m.expiry_date is None:
                    self.stdout.write(
                        f'  [跳过] {tag} 未登记有效期（None），'
                        f'不代填')
                    continue
                if m.expiry_date >= today:
                    self.stdout.write(
                        f'  [跳过] {tag} 有效期 {m.expiry_date} 未过期，'
                        f'不回滚')
                    continue
                target = today + timedelta(
                    days=DEMO_MATERIAL_OFFSETS[(m.district.code, m.name)])
                self.stdout.write(
                    f'  [{"改写" if apply else "将改写"}] {tag}'
                    f'（批号 {m.batch_no or "—"}） {m.expiry_date} -> {target}')
                if apply:
                    m.expiry_date = target
                    m.save(update_fields=['expiry_date'])
                changed += 1

            if apply:
                self.stdout.write(self.style.SUCCESS(f'\n已订正 {changed} 行。'))
            else:
                self.stdout.write(self.style.WARNING(
                    f'\n以上 {changed} 行**未**写库（预演）。确认无误后加 --apply。'))

        # 非演示物料但已过期的：真实业务数据，只报告不改。
        others = list(Material.objects.filter(expiry_date__lt=today).exclude(
            district__in=districts, name__in=DEMO_MATERIAL_NAMES)
            .select_related('district').order_by('district__code', 'id'))
        if others:
            self.stdout.write(self.style.WARNING(
                f'\n⚠ 另有 {len(others)} 件**非演示物料**已过期，本命令不动它们'
                f'（属真实业务数据，需业务方决定如何处置）：'))
            for m in others:
                overdue = (today - m.expiry_date).days
                self.stdout.write(
                    f'    {m.district.code}/{m.name}'
                    f'（批号 {m.batch_no or "—"}）'
                    f'{m.expiry_date}  已过期 {overdue} 天')
