"""修复「账号所属区县 ≠ 所属机构所在区县」的历史账号（**默认只读**）。

为什么需要一个专门的命令：`check_data_integrity` 会报出这类账号，但
**政府端的「用户管理」只有新增与启停、没有编辑**，Django admin 又要超管手工点，
所以巡检报出来的问题在界面上根本没有出口。实测库里就有一个
（区县=南漳县、机构=东津新区的宠安宠物诊所）：它会看到南漳县的档案、
却以一家东津新区的医院身份写数据，而且巡检会一直报它 ——
「报了没人能修」的下一步就是没人再看巡检输出。

为什么单独一个命令、而不是给 `check_data_integrity` 加 `--fix`：
那条命令的价值就在于**可以在任何环境放心跑**（生产、只读副本、排查现场），
一旦它可能写库，这个价值就没了。

判定与 `business.services.resolve_operator_district()` 完全共用一份逻辑，
与建号校验 `validate_operator_district()` 构成互补（有不变式用例锁死）。

用法：
    python manage.py sync_operator_district                  # 预演，只打印
    python manage.py sync_operator_district --username nzzs  # 只看一个账号
    python manage.py sync_operator_district --apply          # 真正写库

挂「市级」的捕捉点操作员是现场约定（`validate_operator_district` 明确允许），
不会被搬动。
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import User
from business.services import resolve_operator_district


class Command(BaseCommand):
    help = '把「账号区县 ≠ 机构所在区县」的账号改为与机构一致（默认预演，--apply 才写库）'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真正写库；不加则只预演（默认）')
        parser.add_argument('--username', default=None,
                            help='只处理指定账号')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        username = options['username']

        qs = (User.objects.filter(institution__isnull=False)
              .select_related('district', 'institution__district').order_by('id'))
        if username:
            qs = qs.filter(username=username)

        planned = []
        for user in qs:
            target = resolve_operator_district(user.role, user.district, user.institution)
            if target is None or target.id == user.district_id:
                continue
            planned.append((user, target))

        if not planned:
            self.stdout.write(self.style.SUCCESS('没有需要修复的账号'))
            return

        for user, target in planned:
            old_name = user.district.name if user.district else '（空）'
            self.stdout.write(
                f'  {user.username}（{user.get_role_display()}）：'
                f'{old_name} → {target.name}')

        if not apply_changes:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'预演：{len(planned)} 个账号待修复，**未写库**。'
                f'确认无误后加 --apply 执行。'))
            return

        # 逐条写：这些账号往往属于不同机构，批量 update 会把区县抹成同一个。
        with transaction.atomic():
            for user, target in planned:
                user.district = target
                user.save(update_fields=['district'])

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'已修复 {len(planned)} 个账号'))
        self.stdout.write('建议再跑一次 `manage.py check_data_integrity` 确认。')
