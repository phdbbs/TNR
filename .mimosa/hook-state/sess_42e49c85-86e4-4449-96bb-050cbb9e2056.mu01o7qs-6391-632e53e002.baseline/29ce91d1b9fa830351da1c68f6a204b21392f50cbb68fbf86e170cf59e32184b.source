from django.apps import AppConfig


class BusinessConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'business'

    def ready(self):
        """Django 启动时自动补偿超期未转待领养的宠物。

        诊疗完成5天后，宠物状态自动从 in_treatment 转为 pending_adopt。
        此补偿逻辑不依赖 django-q2 或 cron，确保即使定时任务未运行也能正确推进状态。
        """
        # 仅在数据库就绪后执行（跳过 migrate、makemigrations 等命令）
        import os
        if os.environ.get('RUN_MAIN') != 'true' and 'runserver' not in os.environ.get('DJANGO_COMMAND', ''):
            # 非 runserver 主进程时，仍尝试执行（覆盖 gunicorn 等生产场景）
            pass

        try:
            from business.tasks import auto_promote_to_adoptable
            auto_promote_to_adoptable()
        except Exception:
            # 数据库未就绪或表不存在时静默跳过（如首次 migrate）
            pass
