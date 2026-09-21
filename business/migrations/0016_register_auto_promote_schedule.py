"""把「诊疗完成 5 天后自动转待领养」注册成真正的定时任务。

## 为什么需要这条迁移

`business/tasks.py::auto_promote_to_adoptable` 的职责是：诊疗完成满 5 天后，
把宠物从「诊疗中」转成「待领养」并上架领养大厅。但部署后它**从来没有被定时调度过**：

- `business/apps.py` 的启动补偿：只在每个 gunicorn 进程启动后**首次请求**跑一次；
- `business/views_treatment.py::_schedule_auto_promote()`：只在「诊疗完成」流程里投递一次；
- `manage.py promote_adoptable`：只有人工执行。

源码注释（`views_treatment.py:285`）写的是「部署时配置定时任务即可」——
作者预期运维在部署时补一条 `Schedule`，但这一步从未被自动化。
2026-09-21 首次生产部署时 `django_q_schedule` 是 0 行；**清库前的旧库同样是 0 行**
（已从备份转储 `db-tnr_system.sql` 核对），所以这不是某次部署或清库造成的，
是项目自带的缺口。

后果：**服务器长期不重启、期间又没人走「诊疗完成」流程时，
超期宠物不会被自动转待领养。**

把注册写进迁移，是为了让 `migrate` 在任何环境（开发 / 测试 / 生产 / 换机器重建）
都自动补上这条 Schedule，不再依赖人记着去后台点一下。

## 幂等性

`get_or_create` 以 `name` 为键：已存在（例如运维手工建过、或本迁移重跑）则
**不覆盖**其 `next_run` / `repeats` / `schedule_type` —— 不打断运维的既有调整。

## 频率

每天 03:00（`Asia/Shanghai`，见 `settings.TIME_ZONE`）。
该任务只处理「完成满 5 天」的记录，一天一次足够；选凌晨是为了避开业务高峰。

## 为什么依赖 django_q 的迁移

`apps.get_model('django_q', 'Schedule')` 只能取到**迁移状态里已建好的**模型；
不声明依赖时，历史模型注册表里没有 `django_q` 的 app，会直接报
`LookupError: No installed app with label 'django_q'`。
"""
from datetime import timedelta

from django.db import migrations
from django.utils import timezone

SCHEDULE_NAME = '诊疗完成5天后自动转待领养'
SCHEDULE_FUNC = 'business.tasks.auto_promote_to_adoptable'
RUN_AT_HOUR = 3


def _next_run_at(hour=RUN_AT_HOUR):
    """下一个本地时间 ``hour:00``（aware datetime）。已过则顺延到明天。"""
    now = timezone.localtime()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def register_schedule(apps, schema_editor):
    Schedule = apps.get_model('django_q', 'Schedule')
    obj, created = Schedule.objects.get_or_create(
        name=SCHEDULE_NAME,
        defaults={
            'func': SCHEDULE_FUNC,
            'schedule_type': 'D',   # Daily
            'repeats': -1,          # -1 = 永久重复
            'next_run': _next_run_at(),
            'cluster': None,        # 不限定 cluster，任意 worker 都可认领
        },
    )
    if created:
        print(f'  已注册定时任务「{SCHEDULE_NAME}」：'
              f'每天 {RUN_AT_HOUR:02d}:00 执行 {SCHEDULE_FUNC}')
    else:
        print(f'  定时任务「{SCHEDULE_NAME}」已存在（id={obj.pk}），保持不变')


def unregister_schedule(apps, schema_editor):
    """回滚：只删本迁移自己建的那条，不碰同名的其它记录。"""
    Schedule = apps.get_model('django_q', 'Schedule')
    Schedule.objects.filter(name=SCHEDULE_NAME, func=SCHEDULE_FUNC).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0015_transfer_note'),
        ('django_q', '0017_task_cluster_alter'),
    ]

    operations = [
        migrations.RunPython(register_schedule, unregister_schedule),
    ]
