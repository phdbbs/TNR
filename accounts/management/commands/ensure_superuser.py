"""确保系统存在一个可登录的 Django 超级管理员。

为什么单独做成管理命令：原先这段逻辑直接写在 deploy.sh 的
`manage.py shell -c "..."` 里，既没法写单测，又踩了一个真实缺陷——
它判断的是「username='admin' 是否存在」，而前一步 seed_data 已经建了一个
**非超级用户**的 admin，于是分支恒不执行，脚本结尾却提示
「超级管理员: admin / admin123456」，运维照提示登录必然失败。

现在改为判断「是否已有启用的超级管理员」这一真正的业务条件，
并且把结果用机器可读的标记行输出给 deploy.sh，避免再靠猜输出内容。

用法:
    python manage.py ensure_superuser
    python manage.py ensure_superuser --username admin --password 'xxx'
    ADMIN_PASSWORD=xxx python manage.py ensure_superuser
"""
import os
import secrets

from django.core.management.base import BaseCommand

from accounts.models import User

# 供 deploy.sh 提取凭据的机器可读标记行
CREDENTIALS_PREFIX = 'ADMIN_CREDENTIALS='


class Command(BaseCommand):
    help = '确保存在一个启用的超级管理员账号（已存在则不改动口令）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--username', default='admin',
            help='超级管理员用户名（不存在则创建，默认 admin）',
        )
        parser.add_argument(
            '--password', default='',
            help='指定口令；不传则读 ADMIN_PASSWORD 环境变量，仍为空则随机生成',
        )
        parser.add_argument(
            '--email', default='admin@tnr.local',
            help='新建账号时的邮箱（默认 admin@tnr.local）',
        )
        parser.add_argument(
            '--reset-password', action='store_true',
            help='即使已存在超级管理员也强制重置目标账号口令',
        )

    def handle(self, *args, **options):
        username = options['username']
        reset = options['reset_password']

        existing = User.objects.filter(is_superuser=True, is_active=True)
        if existing.exists() and not reset:
            names = '、'.join(existing.values_list('username', flat=True))
            self.stdout.write(self.style.WARNING(
                f'已存在启用的超级管理员（{names}），口令未改动。'))
            self.stdout.write('如需重置口令：'
                              'ADMIN_PASSWORD=新口令 python manage.py ensure_superuser --reset-password')
            self.stdout.write('ADMIN_RESULT=SKIP')
            return

        password = (options['password'] or os.environ.get('ADMIN_PASSWORD') or '').strip()
        generated = False
        if not password:
            password = secrets.token_urlsafe(12)
            generated = True

        user = User.objects.filter(username=username).first()
        created = user is None
        if created:
            user = User(username=username, email=options['email'], role='gov_city')

        # 注意：seed_data 建的 admin 只是「市级政府管理员」，不是超级用户；
        # 这里显式提升，并同步 is_active/status，否则会造出「是超管但登不进」的账号。
        user.is_superuser = True
        user.is_staff = True
        user.is_active = True
        user.status = 'active'
        user.set_password(password)
        user.save()

        action = '创建' if created else '提升为超级管理员并重设口令'
        self.stdout.write(self.style.SUCCESS(f'已{action}：{username}'))
        if generated:
            self.stdout.write('（口令为本次随机生成，仅显示一次，请立即保存）')
        self.stdout.write(f'{CREDENTIALS_PREFIX}{username} {password}')
