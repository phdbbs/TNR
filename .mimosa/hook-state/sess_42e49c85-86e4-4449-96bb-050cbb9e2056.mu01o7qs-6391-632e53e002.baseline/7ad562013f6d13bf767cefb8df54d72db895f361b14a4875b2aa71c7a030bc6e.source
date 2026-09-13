"""
Django management command: promote_adoptable
手动触发"诊疗完成5天自动转待领养"任务。
也可用于 cron 定时调用。

用法:
    python manage.py promote_adoptable           # 仅处理5天前完成的
    python manage.py promote_adoptable --force   # 强制处理所有已完成的（用于测试或补偿）
"""
from django.core.management.base import BaseCommand

from business.tasks import auto_promote_to_adoptable


class Command(BaseCommand):
    help = '诊疗完成5天后宠物自动转待领养并上架领养大厅'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='跳过5天时间限制，处理所有已完成诊疗记录（用于测试或补偿）',
        )

    def handle(self, *args, **options):
        force = options.get('force', False)
        result = auto_promote_to_adoptable(force=force)
        self.stdout.write(
            self.style.SUCCESS(result)
        )
