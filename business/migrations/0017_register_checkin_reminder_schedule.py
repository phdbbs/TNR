"""把「每月回访提醒」注册成真正的定时任务。

## 为什么需要这条迁移

`business/tasks.py::send_checkin_reminders` 的职责是：给「已领养、但本月
还没回访打卡」的领养人各发一条 ``checkin_reminder`` 类型消息。

在第三十七轮之前，`Message.TYPE_CHOICES` 里的 ``checkin_reminder``（回访提醒）
与 ``notice``（公告）**都没有任何真实产生点** —— 只有 `seed_data` 造过
2 条演示数据。也就是说这两个枚举值在界面上有图标、有文案、有筛选，
但真实业务里**永远不会出现**。这属于「枚举值存在、写入路径缺失」，
与 `Institution.status`「字段存在但无人读」是同一类缺口（方向相反）。

`notice` 由 `supervision.views.notice_publish` 提供产生点（人工发布）；
`checkin_reminder` 必须由**定时任务**产生 —— 没有哪个操作员会记得
每月逐个去催。所以这里把它注册进 `django_q_schedule`。

与 `0016` 同样的理由：注册写进迁移，`migrate` 在任何环境都会自动补上，
不依赖运维记着去后台点一下。

## 幂等性

`get_or_create` 以 `name` 为键：已存在则**不覆盖** `next_run` /
`repeats` / `schedule_type`，不打断运维的既有调整。

## 频率

每月 1 日 09:00（`Asia/Shanghai`，见 `settings.TIME_ZONE`）。
选月初是因为「本月还没打卡」这件事在月初才有意义 —— 月中发提醒，
领养人可能只是还没到月底。09:00 是工作时段，提醒更容易被看到。

## 为什么依赖 django_q 的迁移

`apps.get_model('django_q', 'Schedule')` 只能取到**迁移状态里已建好的**
模型；不声明依赖时会直接报
`LookupError: No installed app with label 'django_q'`。
"""
from datetime import timedelta

from django.db import migrations
from django.utils import timezone

SCHEDULE_NAME = '每月回访打卡提醒'
SCHEDULE_FUNC = 'business.tasks.send_checkin_reminders'
RUN_AT_HOUR = 9


def _next_month_first_at(hour=RUN_AT_HOUR):
    """下一个「某月 1 日 ``hour:00``」（aware datetime）。

    与 `0016` 的 `_next_run_at` 不同：这里必须**落在 1 日**。
    django-q2 的 MONTHLY 类型是「按 next_run 的日号每月重复」，
    所以 next_run 的日号决定了以后每月几号执行 —— 取「今天 + 1 个月」
    会把它固定在部署当天（如 23 日），与「月初提醒」的意图不符。
    """
    now = timezone.localtime()
    target = now.replace(day=1, hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        # 本月 1 日已过 → 下月 1 日（+32 天后取当月 1 日，可跨年跨月）
        target = (target + timedelta(days=32)).replace(day=1)
    return target


def register_schedule(apps, schema_editor):
    Schedule = apps.get_model('django_q', 'Schedule')
    obj, created = Schedule.objects.get_or_create(
        name=SCHEDULE_NAME,
        defaults={
            'func': SCHEDULE_FUNC,
            'schedule_type': 'M',   # Monthly
            'repeats': -1,          # -1 = 永久重复
            'next_run': _next_month_first_at(),
            'cluster': None,        # 不限定 cluster，任意 worker 都可认领
        },
    )
    if created:
        print(f'  已注册定时任务「{SCHEDULE_NAME}」：'
              f'每月 1 日 {RUN_AT_HOUR:02d}:00 执行 {SCHEDULE_FUNC}')
    else:
        print(f'  定时任务「{SCHEDULE_NAME}」已存在（id={obj.pk}），保持不变')


def unregister_schedule(apps, schema_editor):
    """回滚：只删本迁移自己建的那条，不碰同名的其它记录。"""
    Schedule = apps.get_model('django_q', 'Schedule')
    Schedule.objects.filter(name=SCHEDULE_NAME, func=SCHEDULE_FUNC).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0016_register_auto_promote_schedule'),
    ]

    operations = [
        migrations.RunPython(register_schedule, unregister_schedule),
    ]
