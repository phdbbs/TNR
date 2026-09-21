"""回归：定时任务必须被**真正注册**。

背景见 `DEPLOY.md` §5.2 —— 「诊疗完成 5 天后自动转待领养」
（`business.tasks.auto_promote_to_adoptable`）在生产上长期没有 `Schedule` 注册
（`django_q_schedule` 0 行），只有「接口触发 + 进程启动补偿」两条路径，
服务器长期不重启、期间又没人走「诊疗完成」流程时就不会跑。

`business/migrations/0016_register_auto_promote_schedule.py` 用数据迁移把注册固化下来，
本文件钉住它不被回退，并覆盖几个「写得出来但永远不会触发」的坑：

- `func` 是字符串点路径，写错一个字母 → 调度器只在运行时报错，迁移本身静默通过；
- `next_run` 落在过去 → 迁移刚跑完就被立刻触发一次（不该在部署瞬间发生）；
- `repeats=0` → 跑一次就停，看着像「注册了」实际不重复；
- 重跑迁移 / 运维已手工建过 → 不能产生第二条。

注意：测试库在 `migrate` 阶段就会执行该数据迁移，所以这些断言是**对真实迁移产物**的断言，
而不是自己造一条 Schedule 再断言自己造的东西。
"""
import importlib
from datetime import timedelta

from django.apps import apps as global_apps
from django.test import TestCase
from django.utils import timezone
from django.utils.module_loading import import_string
from django_q.models import Schedule

from business.tasks import auto_promote_to_adoptable

SCHEDULE_NAME = '诊疗完成5天后自动转待领养'
SCHEDULE_FUNC = 'business.tasks.auto_promote_to_adoptable'
MIGRATION_MODULE = 'business.migrations.0016_register_auto_promote_schedule'


class ScheduleRegistrationTest(TestCase):
    """数据迁移跑完后，测试库里就应该有这条 Schedule。"""

    def _schedule(self):
        return Schedule.objects.filter(func=SCHEDULE_FUNC).first()

    def test_daily_schedule_exists(self):
        s = self._schedule()
        self.assertIsNotNone(
            s, '未注册定时任务：超期宠物不会自动转待领养（见 DEPLOY.md §5.2）')
        self.assertEqual(s.schedule_type, Schedule.DAILY)
        self.assertEqual(s.repeats, -1, 'repeats 必须为 -1（永久），否则跑一次就停')
        self.assertIsNotNone(s.next_run, 'next_run 为空则永远不会触发')

    def test_next_run_is_in_the_future(self):
        """落在过去会导致「刚 migrate 完就被立刻触发」。"""
        self.assertGreater(self._schedule().next_run, timezone.now())

    def test_next_run_is_at_three_am_local(self):
        local = timezone.localtime(self._schedule().next_run)
        self.assertEqual((local.hour, local.minute), (3, 0))
        self.assertEqual((local.second, local.microsecond), (0, 0))

    def test_scheduled_func_is_importable(self):
        """点路径写错时调度器只在运行时才报错，迁移会静默通过 —— 所以单独钉一条。"""
        self.assertIs(import_string(SCHEDULE_FUNC), auto_promote_to_adoptable)

    def test_migration_function_is_idempotent(self):
        """重跑迁移（或运维已手工建过）不得产生第二条。"""
        mod = importlib.import_module(MIGRATION_MODULE)
        before = Schedule.objects.filter(name=SCHEDULE_NAME).count()
        self.assertEqual(before, 1, '测试库初始化后应恰好有一条')
        mod.register_schedule(global_apps, None)
        mod.register_schedule(global_apps, None)
        self.assertEqual(Schedule.objects.filter(name=SCHEDULE_NAME).count(), 1)

    def test_migration_does_not_overwrite_existing_next_run(self):
        """运维调过时间的话，重跑迁移不能把它改回默认值。"""
        mod = importlib.import_module(MIGRATION_MODULE)
        s = self._schedule()
        moved = timezone.now() + timedelta(days=30)
        Schedule.objects.filter(pk=s.pk).update(next_run=moved)
        mod.register_schedule(global_apps, None)
        self.assertEqual(Schedule.objects.get(pk=s.pk).next_run, moved)

    def test_reverse_migration_removes_only_own_row(self):
        mod = importlib.import_module(MIGRATION_MODULE)
        Schedule.objects.create(name=SCHEDULE_NAME, func='other.tasks.func')
        mod.unregister_schedule(global_apps, None)
        self.assertEqual(
            Schedule.objects.filter(name=SCHEDULE_NAME).count(), 1,
            '只应删掉 func 匹配的那条')
        self.assertIsNone(Schedule.objects.filter(func=SCHEDULE_FUNC).first())
