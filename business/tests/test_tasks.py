"""定时任务测试：诊疗完成 5 天自动转待领养。"""
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.core.signals import request_started
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from business.models import AdoptionHallListing, Treatment
from business.tasks import auto_promote_to_adoptable
from business.tests.base import BusinessTestBase, make_institution, make_pet


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
