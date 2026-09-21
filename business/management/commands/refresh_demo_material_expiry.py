"""把**演示物料**的有效期按「相对今天」重置。

背景：`seed_data` 早期把物料有效期写成了绝对日期（2025-12-31 / 2025-10-31 /
2026-06-30），已在 `3940ef6` 改成相对今天生成。但 `get_or_create(name=...,
defaults=...)` 的 `defaults` **只在创建时生效** —— 所以**已经导入过的库修不回来**，
重跑 `seed_data` 也不行。本命令专门订正这类存量行。

**判据（三个条件同时成立才改）**：
  1. 物料名在 `seed_data.SEED_MATERIALS` 里；
  2. 所属区县是**演示区县**（`SEED_MATERIAL_DISTRICT_CODE`，襄城区）；
  3. `expiry_date` **存在且已过期**。

第 2 条不能省：实测现场 4 个区县各有 4 件同名物料，只有襄城区那套是演示数据，
其余是别处产生的 —— 只按名称匹配会**误伤真实业务数据**。

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
    SEED_MATERIALS, SEED_MATERIAL_DISTRICT_CODE,
)
from business.models import Material
from core.models import District

# 演示物料名 → 有效期距今天数（从 seed_data 的清单派生，单一来源）
DEMO_MATERIAL_OFFSETS = {
    name: offset
    for (_id, name, _cat, _unit, _spec, _sup, _batch,
         _stock, _safety, offset, _cs, _ce, _dc) in SEED_MATERIALS
    if offset is not None
}


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

        district = District.objects.filter(
            code=SEED_MATERIAL_DISTRICT_CODE).first()
        if district is None:
            self.stderr.write(self.style.ERROR(
                f'找不到演示区县（code={SEED_MATERIAL_DISTRICT_CODE}），'
                f'无法确定订正范围，已中止。'))
            return
        self.stdout.write(
            f'演示区县 = {district.code}/{district.name}；'
            f'演示物料名 = {"、".join(DEMO_MATERIAL_OFFSETS)}')

        rows = list(Material.objects.filter(
            district=district,
            name__in=list(DEMO_MATERIAL_OFFSETS)).order_by('id'))

        if not rows:
            self.stdout.write('演示区县内未找到演示物料，无需订正。')
        else:
            changed = 0
            for m in rows:
                if m.expiry_date is None:
                    self.stdout.write(
                        f'  [跳过] {m.name} 未登记有效期（None），'
                        f'不代填')
                    continue
                if m.expiry_date >= today:
                    self.stdout.write(
                        f'  [跳过] {m.name} 有效期 {m.expiry_date} 未过期，'
                        f'不回滚')
                    continue
                target = today + timedelta(days=DEMO_MATERIAL_OFFSETS[m.name])
                self.stdout.write(
                    f'  [{"改写" if apply else "将改写"}] {m.name}'
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
            district=district, name__in=list(DEMO_MATERIAL_OFFSETS))
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
