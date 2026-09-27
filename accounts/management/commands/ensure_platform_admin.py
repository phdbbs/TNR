"""确保系统存在一个可登录的**平台管理员**账号（`role='platform_admin'`）。

为什么必须有这个命令：第四十二轮把「全局设置」（区县/机构/账号/编号规则/
公告/操作日志）从政府端搬到了平台管理端（`/platform/`），这些接口的
`@role_required` 白名单**只认 `platform_admin`**。于是：

- 库里若没有这样一个账号，平台端就是**一整片打不开的功能** ——
  而且症状是「登录后跳回门户」或「菜单点不动」，不是报错；
- 反过来，`ensure_superuser` 建出来的 `admin` 是 `role='gov_city'`，
  **登不进平台端**（`role_required('platform_admin')` 会 403）。超级用户身份
  不参与业务角色判定 —— 这点很容易被误认为「admin 什么都能进」。

与 `ensure_superuser` 同款约定：
- 幂等：已存在**启用中**的平台管理员时不做任何改动（除非 `--reset-password`）；
- 口令优先级 `--password` > `PLATFORM_ADMIN_PASSWORD` > 随机生成；
- 输出机器可读标记行，供部署脚本提取凭据。

用法:
    python manage.py ensure_platform_admin
    python manage.py ensure_platform_admin --username platform --password 'xxx'
    PLATFORM_ADMIN_PASSWORD=xxx python manage.py ensure_platform_admin
    python manage.py ensure_platform_admin --reset-password
"""
import os
import secrets

from django.core.management.base import BaseCommand

from accounts.models import User

#: 供部署脚本提取凭据的机器可读标记行
CREDENTIALS_PREFIX = 'PLATFORM_ADMIN_CREDENTIALS='

ROLE = 'platform_admin'


class Command(BaseCommand):
    help = '确保存在一个启用的平台管理员账号（已存在则不改动口令）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--username', default='platform',
            help='平台管理员用户名（不存在则创建，默认 platform）',
        )
        parser.add_argument(
            '--password', default='',
            help='指定口令；不传则读 PLATFORM_ADMIN_PASSWORD 环境变量，仍为空则随机生成',
        )
        parser.add_argument(
            '--email', default='platform@tnr.local',
            help='新建账号时的邮箱（默认 platform@tnr.local）',
        )
        parser.add_argument(
            '--reset-password', action='store_true',
            help='即使已存在平台管理员也强制重置目标账号口令',
        )

    def handle(self, *args, **options):
        username = options['username']
        reset = options['reset_password']

        existing = User.objects.filter(role=ROLE, is_active=True)
        if existing.exists() and not reset:
            names = '、'.join(existing.values_list('username', flat=True))
            self.stdout.write(self.style.WARNING(
                f'已存在启用的平台管理员（{names}），口令未改动。'))
            self.stdout.write('如需重置口令：'
                              'PLATFORM_ADMIN_PASSWORD=新口令 '
                              'python manage.py ensure_platform_admin --reset-password')
            self.stdout.write('PLATFORM_ADMIN_RESULT=SKIP')
            return

        password = (options['password']
                    or os.environ.get('PLATFORM_ADMIN_PASSWORD')
                    or '').strip()
        generated = False
        if not password:
            password = secrets.token_urlsafe(12)
            generated = True

        user = User.objects.filter(username=username).first()
        created = user is None
        if created:
            user = User(username=username, email=options['email'])

        # 平台管理员是**全局角色**（见 `core/scope.py` 的 `GLOBAL_SCOPE_ROLES`），
        # 可见范围与区县无关，因此**不挂区县** —— 这里显式清空，避免把一个
        # 原本属于别的角色的同名账号提上来时还留着旧区县，让人误以为它受区县约束。
        user.role = ROLE
        user.district = None
        user.institution = None
        user.is_staff = True
        user.is_active = True
        user.status = 'active'
        if not user.first_name:
            user.first_name = '平台管理员'
        user.set_password(password)
        user.save()

        action = '创建' if created else '调整为平台管理员并重设口令'
        self.stdout.write(self.style.SUCCESS(f'已{action}：{username}'))
        if generated:
            self.stdout.write('（口令为本次随机生成，仅显示一次，请立即保存）')
        self.stdout.write(f'{CREDENTIALS_PREFIX}{username} {password}')
