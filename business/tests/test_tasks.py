"""定时任务测试：诊疗完成 5 天自动转待领养。"""
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.core.signals import request_started
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from business.models import (
    Adoption, AdoptionHallListing, CheckIn, Message, Treatment,
)
from business.tasks import auto_promote_to_adoptable, send_checkin_reminders
from business.tests.base import (
    BusinessTestBase, make_institution, make_pet, make_user,
)


def _completed_treatment(pet, days_ago=None):
    treatment = Treatment.objects.create(
        pet=pet, pet_code=pet.code, hospital=pet.hospital,
        status='completed', district=pet.district)
    if days_ago is not None:
        Treatment.objects.filter(id=treatment.id).update(
            created_at=timezone.now() - timedelta(days=days_ago))
    return treatment


class AutoPromoteTest(BusinessTestBase):
    def test_promotes_after_5_days(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=6)
        result = auto_promote_to_adoptable()
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')
        listing = AdoptionHallListing.objects.get(pet=pet)
        self.assertTrue(listing.is_active)
        self.assertEqual(listing.hospital, self.hospital_a)
        self.assertIn('Promoted 1', result)

    def test_not_promoted_before_5_days(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=4)
        auto_promote_to_adoptable()
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment')

    def test_non_treatment_pet_untouched(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='adopted')
        _completed_treatment(pet, days_ago=10)
        auto_promote_to_adoptable()
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'adopted')

    def test_force_promotes_all_completed(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=0)
        auto_promote_to_adoptable(force=True)
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')

    def test_no_duplicate_listing(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=6)
        auto_promote_to_adoptable()
        auto_promote_to_adoptable()
        self.assertEqual(AdoptionHallListing.objects.filter(pet=pet).count(), 1)

    def test_reactivates_existing_inactive_listing(self):
        """领养完成时上架记录会被下架，宠物回到待领养后必须重新上架。

        原实现用 get_or_create，命中已存在的下架记录时不会置回 is_active，
        导致宠物状态是「待领养」却不出现在领养大厅。
        """
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        listing = AdoptionHallListing.objects.create(
            pet=pet, hospital=self.hospital_a, is_active=False)
        _completed_treatment(pet, days_ago=6)
        auto_promote_to_adoptable()
        listing.refresh_from_db()
        self.assertTrue(listing.is_active, '已下架的上架记录应被重新激活')

    def test_deleted_pet_not_promoted(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment', is_deleted=True)
        _completed_treatment(pet, days_ago=6)
        auto_promote_to_adoptable()
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment')
        self.assertFalse(AdoptionHallListing.objects.filter(pet=pet).exists())


class StartupCompensationTest(BusinessTestBase):
    """启动补偿的**触发方式**：`ready()` 不查库，补偿挂到首个请求且只跑一次。

    背景（2026-09-21 生产部署时实测）：原先 `BusinessConfig.ready()` 直接调用
    `auto_promote_to_adoptable()`，于是

    * 每个进程启动都写库 → `manage.py` 的**任意**命令都打印
      `RuntimeWarning: Accessing the database during app initialization`；
    * `migrate` 期间表还不存在，异常被 `except Exception: pass` 吞掉，
      真实错误永久消失；
    * `if os.environ.get('RUN_MAIN') != 'true' and ...: pass` 是**空分支**，
      条件怎么写都不产生效果。

    改法：`ready()` 只挂信号，补偿在**首个请求**时执行（那时数据库必然可用），
    失败走 `logger.exception` 留痕。
    """

    def setUp(self):
        super().setUp()
        self._arm(True)

    def tearDown(self):
        # 还原成「已跑过」：这个开关是**进程级全局状态**，本类之外的用例
        # 不该被它影响（否则后续用例的首个请求会意外触发一次补偿）。
        self._arm(False)
        super().tearDown()

    @staticmethod
    def _arm(armed):
        """armed=True 时允许补偿再跑一次；False 时置成「已完成」。"""
        from business import apps as business_apps
        with business_apps._compensation_lock:
            business_apps._compensation_done = not armed
        request_started.disconnect(dispatch_uid=business_apps._COMPENSATION_UID)
        request_started.connect(business_apps.run_startup_compensation,
                                dispatch_uid=business_apps._COMPENSATION_UID)

    def test_ready_does_not_touch_database(self):
        """`ready()` 里查库就是这次要修的根因，必须钉住。"""
        with CaptureQueriesContext(connection) as ctx:
            apps.get_app_config('business').ready()
        self.assertEqual(
            list(ctx.captured_queries), [],
            'ready() 不得访问数据库 —— Django 会抛 RuntimeWarning，'
            '且 migrate 期间表还不存在')

    def test_first_request_triggers_compensation(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=6)

        self.client.get('/')          # 任意请求都会触发 request_started

        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')
        self.assertTrue(AdoptionHallListing.objects.get(pet=pet).is_active)

    def test_compensation_runs_only_once_per_process(self):
        self.client.get('/')          # 先把这次机会用掉

        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=6)
        self.client.get('/')          # 第二次请求不该再补偿

        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment',
                         '补偿应在每个进程只跑一次，不应每次请求都跑')

    def test_failure_is_logged_not_swallowed(self):
        """补偿失败 = 「该上架的动物没上架」，界面上看不出来，必须留日志。"""
        with mock.patch('business.tasks.auto_promote_to_adoptable',
                        side_effect=RuntimeError('模拟补偿失败')):
            with self.assertLogs('business.apps', level='ERROR') as captured:
                self.client.get('/')      # 异常被接住，不应把请求打崩

        self.assertTrue(
            any('启动补偿' in line for line in captured.output),
            f'补偿失败必须记进日志，实际输出：{captured.output}')

    def test_failure_does_not_break_the_request(self):
        """补偿失败不能连累用户请求：兜底逻辑坏了，正常业务也要能走。"""
        with mock.patch('business.tasks.auto_promote_to_adoptable',
                        side_effect=RuntimeError('模拟补偿失败')):
            resp = self.client.get('/')
        self.assertNotEqual(resp.status_code, 500)


class CheckinReminderTest(BusinessTestBase):
    """每月回访提醒（`send_checkin_reminders`）。

    `Message.TYPE_CHOICES` 里的 ``checkin_reminder``（回访提醒）在第三十七轮
    之前**没有任何真实产生点** —— 只有 `seed_data` 造过 2 条演示数据，
    界面上有图标、有文案、有筛选，真实业务里却永远不会出现。
    这一组用例把这个产生点钉住：它必须真的产生消息，且不该重复产生。
    """

    #: 用于区分「没传 adopter」与「显式传 None（线下登记、无账号）」
    _DEFAULT = object()

    def _adopt(self, pet, adopter=_DEFAULT, status='completed'):
        return Adoption.objects.create(
            pet=pet, pet_code=pet.code,
            adopter=self.adopter if adopter is self._DEFAULT else adopter,
            adopter_name='测试领养人', status=status,
            adopted_at=timezone.localdate(), district=pet.district)

    def _checkin(self, pet, month, status='pending', adopter=None):
        return CheckIn.objects.create(
            pet=pet, pet_code=pet.code, adopter=adopter or self.adopter,
            adopter_name='测试领养人', month=month, status=status)

    @staticmethod
    def _this_month():
        return timezone.localdate().strftime('%Y-%m')

    @staticmethod
    def _last_month():
        first = timezone.localdate().replace(day=1)
        return (first - timedelta(days=1)).strftime('%Y-%m')

    def test_sends_reminder_when_not_checked_in(self):
        pet = make_pet(district=self.district_a, status='adopted')
        self._adopt(pet)
        result = send_checkin_reminders()
        self.assertIn('Sent 1', result)
        msg = Message.objects.get(user=self.adopter, type='checkin_reminder')
        self.assertIn(pet.code, msg.content)

    def test_no_reminder_when_checked_in_this_month(self):
        pet = make_pet(district=self.district_a, status='adopted')
        self._adopt(pet)
        self._checkin(pet, self._this_month())
        self.assertIn('Sent 0', send_checkin_reminders())

    def test_rejected_checkin_still_reminds(self):
        """被驳回的打卡需要重新提交 —— 那正是提醒的意义，不能算「已打卡」。"""
        pet = make_pet(district=self.district_a, status='adopted')
        self._adopt(pet)
        self._checkin(pet, self._this_month(), status='rejected')
        self.assertIn('Sent 1', send_checkin_reminders())

    def test_last_month_checkin_does_not_count(self):
        """上月打过卡不能顶替本月 —— 否则第二个月起就再也不会提醒。"""
        pet = make_pet(district=self.district_a, status='adopted')
        self._adopt(pet)
        self._checkin(pet, self._last_month())
        self.assertIn('Sent 1', send_checkin_reminders())

    def test_no_duplicate_within_same_month(self):
        """同一个月重复执行任务，不能反复催同一个人。"""
        pet = make_pet(district=self.district_a, status='adopted')
        self._adopt(pet)
        send_checkin_reminders()
        send_checkin_reminders()
        self.assertEqual(
            Message.objects.filter(type='checkin_reminder').count(), 1,
            '同一月份的提醒必须去重')

    def test_pending_claim_not_reminded(self):
        """还没领出（pending_claim）的动物不该催打卡 —— 它还没交到领养人手上。"""
        pet = make_pet(district=self.district_a, status='pending_claim')
        self._adopt(pet, status='pending_claim')
        self.assertIn('Sent 0', send_checkin_reminders())

    def test_multi_pet_adopter_gets_one_per_pet(self):
        """同一人领养两只，**每只**各自要打卡 —— 不能因一只打过就漏另一只。"""
        pet1 = make_pet(district=self.district_a, status='adopted')
        pet2 = make_pet(district=self.district_a, status='adopted')
        self._adopt(pet1)
        self._adopt(pet2)
        self._checkin(pet1, self._this_month())     # 只给 pet1 打卡
        self.assertIn('Sent 1', send_checkin_reminders())
        msgs = Message.objects.filter(type='checkin_reminder')
        self.assertEqual(msgs.count(), 1)
        self.assertIn(pet2.code, msgs.first().content)

    def test_deleted_pet_not_reminded(self):
        pet = make_pet(district=self.district_a, status='adopted',
                       is_deleted=True)
        self._adopt(pet)
        self.assertIn('Sent 0', send_checkin_reminders())

    def test_inactive_adopter_not_reminded(self):
        """停用账号收不到提醒：发了也没人看，只会在库里堆未读。"""
        adopter = make_user(role='adopter', is_active=False)
        pet = make_pet(district=self.district_a, status='adopted')
        self._adopt(pet, adopter=adopter)
        self.assertIn('Sent 0', send_checkin_reminders())

    def test_adoption_without_account_not_reminded(self):
        """线下领养登记可能没有对应账号（adopter 为 SET_NULL）—— 不能炸。"""
        pet = make_pet(district=self.district_a, status='adopted')
        self._adopt(pet, adopter=None)
        self.assertIn('Sent 0', send_checkin_reminders())

    def test_force_ignores_dedup(self):
        """`force=True` 跳过去重（补发场景）—— 但**不能**跳过「已打卡」判定。"""
        pet = make_pet(district=self.district_a, status='adopted')
        self._adopt(pet)
        send_checkin_reminders()
        self.assertIn('Sent 1', send_checkin_reminders(force=True))
        self._checkin(pet, self._this_month())
        self.assertIn('Sent 0', send_checkin_reminders(force=True),
                      'force 只跳过「本月已发过」的去重，不该覆盖「已经打过卡」')
