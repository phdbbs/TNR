"""物料有效期判据：过期物料不能用于诊疗。

背景：`Material.expiry_date` 此前在整个后端**只有「写入 + 展示」两类读取点**
（`purchase_create` 写、医院门户 `formatDate` 显示），**没有一处把它当判据**。
后果是拿一支过期 325 天的疫苗做诊疗，系统照收不误，界面上只多显示一个日期。
与十八轮 `Institution.status` 同类：「字段存在但无人读」。

本文件把拦截面**精确地**钉住 —— 既要拦住「使用」，也要保证**没有**把
「采购入库」和「库存异动（过期报废）」一起禁掉，否则过期物料会永远
卡在库存里、没有补救路径。
"""
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from business.models import Material, MaterialTransaction, Treatment
from business.services import expired_material_error, get_hospital_stock
from business.tests.base import (
    BusinessTestBase, make_district, make_hospital_txn, make_material, make_pet,
)
from core.models import District

TREATMENT_URL = '/api/business/treatments/create/'
PURCHASE_URL = '/api/business/materials/purchase/'
ADJUST_URL = '/api/business/materials/adjustment/'

TODAY = timezone.localdate


def days_from_today(n):
    return TODAY() + timedelta(days=n)


class ExpiredMaterialErrorUnitTest(TestCase):
    """判据函数本身：边界必须精确到「当天到期仍然可用」。"""

    def test_none_material_passes(self):
        """material 为 None 直接放行，省得每个调用点自己判空。"""
        self.assertIsNone(expired_material_error(None))

    def test_no_expiry_date_passes(self):
        """没有有效期的物料（如芯片）永远可用。"""
        self.assertIsNone(expired_material_error(make_material(expiry_date=None)))

    def test_yesterday_is_expired(self):
        m = make_material(expiry_date=days_from_today(-1))
        err = expired_material_error(m, '用于诊疗')
        self.assertIsNotNone(err)
        self.assertIn('过期', err)
        self.assertIn('用于诊疗', err)
        self.assertIn('超期 1 天', err)

    def test_today_is_not_expired(self):
        """⚠ 边界：有效期当天**仍然可用** —— 不能写成 `<= today`。"""
        self.assertIsNone(expired_material_error(make_material(expiry_date=TODAY())))

    def test_tomorrow_is_not_expired(self):
        self.assertIsNone(
            expired_material_error(make_material(expiry_date=days_from_today(1))))

    def test_message_carries_material_name_and_days(self):
        m = make_material(name='狂犬疫苗', expiry_date=days_from_today(-264))
        err = expired_material_error(m)
        self.assertIn('狂犬疫苗', err)
        self.assertIn('超期 264 天', err)
        self.assertIn('库存异动', err, '文案要指向补救路径，否则用户不知道下一步做什么')


class TreatmentExpiredMaterialTest(BusinessTestBase):
    """诊疗创建：过期物料必须被拒，且**不留任何副作用**。"""

    def _pet(self):
        return make_pet(district=self.district_a, hospital=self.hospital_a,
                        status='in_treatment')

    def _create(self, pet, payload):
        self.login_as(self.hospital_user_a)
        body = {'pet_id': pet.id}
        body.update(payload)
        return self.post_json(TREATMENT_URL, body)

    def test_expired_vaccine_rejected(self):
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a,
                                 name='过期狂犬疫苗',
                                 expiry_date=days_from_today(-264))
        make_hospital_txn(material, self.hospital_a, 'receive', 10)

        resp = self._create(pet, {
            'items': {'vaccine': True},
            'vaccine': {'material_id': material.id, 'quantity': 1},
        })
        self.expect_fail(resp, message='不能用于诊疗')
        self.assertIn('过期狂犬疫苗', resp.json()['message'])

    def test_expired_vaccine_leaves_no_side_effect(self):
        """被拒时不能留下诊疗记录、消耗流水，也不能扣库存。"""
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a,
                                 expiry_date=days_from_today(-264))
        make_hospital_txn(material, self.hospital_a, 'receive', 10)

        self.expect_fail(self._create(pet, {
            'items': {'vaccine': True},
            'vaccine': {'material_id': material.id, 'quantity': 2},
        }), message='不能用于诊疗')

        self.assertEqual(Treatment.objects.filter(pet=pet).count(), 0)
        self.assertFalse(MaterialTransaction.objects.filter(type='consume').exists())
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 10)

    def test_expired_dewormer_rejected(self):
        pet = self._pet()
        material = make_material(category='dewormer', district=self.district_a,
                                 expiry_date=days_from_today(-83))
        make_hospital_txn(material, self.hospital_a, 'receive', 5)

        self.expect_fail(self._create(pet, {
            'items': {'deworming': True},
            'deworming': {'material_id': material.id, 'quantity': 1},
        }), message='不能用于诊疗')

    def test_expired_chip_material_rejected(self):
        """芯片物料理论上也可以设有效期，判据同样要覆盖这条路径。

        原先 `chip_material` 是在**事务块内**才查询的 —— 那次查询可能失败，
        却落在落库之后，等于把一次校验放在了副作用之后。
        """
        from business.tests.base import make_chip
        pet = self._pet()
        chip = make_chip()
        chip_material = make_material(category='chip', district=self.district_a,
                                      expiry_date=days_from_today(-10))
        make_hospital_txn(chip_material, self.hospital_a, 'receive', 5)

        self.expect_fail(self._create(pet, {
            'items': {'chip': True},
            'chip': {'chip_no': chip.number},
        }), message='不能用于诊疗')

        chip.refresh_from_db()
        self.assertEqual(chip.status, 'available', '被拒时芯片不应被标记为已使用')

    def test_partial_rejection_does_not_deduct_earlier_item(self):
        """疫苗正常、驱虫药过期 —— 整单拒绝，疫苗库存也不许被扣。"""
        pet = self._pet()
        vaccine = make_material(category='vaccine', district=self.district_a,
                                expiry_date=days_from_today(180))
        dewormer = make_material(category='dewormer', district=self.district_a,
                                 expiry_date=days_from_today(-83))
        make_hospital_txn(vaccine, self.hospital_a, 'receive', 10)
        make_hospital_txn(dewormer, self.hospital_a, 'receive', 5)

        self.expect_fail(self._create(pet, {
            'items': {'vaccine': True, 'deworming': True},
            'vaccine': {'material_id': vaccine.id, 'quantity': 2},
            'deworming': {'material_id': dewormer.id, 'quantity': 1},
        }), message='不能用于诊疗')

        self.assertEqual(get_hospital_stock(vaccine, self.hospital_a), 10,
                         '整单被拒时不应扣减前一项的库存')
        self.assertEqual(Treatment.objects.filter(pet=pet).count(), 0)

    # ---------- 正向对照：不能做成「一律禁止」 ----------

    def test_valid_material_still_accepted(self):
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a,
                                 expiry_date=days_from_today(180))
        make_hospital_txn(material, self.hospital_a, 'receive', 10)

        body = self.ok(self._create(pet, {
            'items': {'vaccine': True},
            'vaccine': {'material_id': material.id, 'quantity': 2},
        }))
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 8)
        self.assertTrue(Treatment.objects.filter(id=body['data']['id']).exists())

    def test_material_without_expiry_still_accepted(self):
        """`expiry_date=None` 是最常见的形态（芯片、未登记有效期的物料）。"""
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a,
                                 expiry_date=None)
        make_hospital_txn(material, self.hospital_a, 'receive', 10)
        self.ok(self._create(pet, {
            'items': {'vaccine': True},
            'vaccine': {'material_id': material.id, 'quantity': 1},
        }))

    def test_expiring_today_still_accepted(self):
        """边界：有效期就是今天的物料，仍然能用。"""
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a,
                                 expiry_date=TODAY())
        make_hospital_txn(material, self.hospital_a, 'receive', 10)
        self.ok(self._create(pet, {
            'items': {'vaccine': True},
            'vaccine': {'material_id': material.id, 'quantity': 1},
        }))


class ExpiredMaterialIsNotBlanketBanTest(BusinessTestBase):
    """反向对照：拦截面必须**只覆盖「使用」**。

    如果顺手把「采购入库」和「库存异动」也拦了，过期物料就**没有任何出口**
    —— 既不能报废，也不能入库，会永远卡在库存里。
    """

    def test_purchase_of_expired_material_still_allowed(self):
        """采购入库不受有效期影响：那是录入新数据，新批号有效期由操作员填。"""
        material = make_material(category='vaccine', district=self.district_a,
                                 shelter_stock=0,
                                 expiry_date=days_from_today(-30))
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(PURCHASE_URL, {
            'material_id': material.id, 'quantity': 50,
            'supplier': '国药集团', 'batch_no': 'B-NEW',
        }))
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 50)

    def test_scrapping_expired_material_still_allowed(self):
        """库存异动（过期报废）**正是**处理过期物料的正规出口，必须放行。"""
        material = make_material(category='vaccine', district=self.district_a,
                                 shelter_stock=0,
                                 expiry_date=days_from_today(-30))
        make_hospital_txn(material, self.hospital_a, 'receive', 10)

        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(ADJUST_URL, {
            'material_id': material.id, 'quantity': 10, 'reason': '过期报废',
        }))
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 0)


class RefreshDemoMaterialExpiryCommandTest(TestCase):
    """存量订正命令：只动**演示区县内、已过期**的演示物料。

    判据是「名称 + 区县 + 已过期」，三条缺一不可：

    * **不用批号** —— 实测同一套演示数据在不同环境下批号并不相同
      （生产是 `B20250101`，本机是 `KY20260901`），按批号会静默漏掉。
    * **必须限定区县** —— 现场 4 个区县各有 4 件同名物料，只有襄城区
      那套是演示数据；只按名称匹配会误伤真实业务数据。
    * **必须限定「已过期」** —— `None` 是「没登记有效期」，不等于过期，
      不能代填。
    """

    @classmethod
    def setUpTestData(cls):
        # ⚠ code 必须与 `seed_data.SEED_MATERIAL_DISTRICT_CODES` 对齐，而且
        # 演示区县**是复数**（襄城区 + 樊城区）—— 命令要求全部在场，
        # 缺任何一个都会直接中止，所以两个都得建出来。
        cls.demo_district = make_district(name='襄城区', code='CY')
        cls.demo_district_2 = make_district(name='樊城区', code='HD')
        # ⚠ 「别区县」必须是**非演示区县**。原先这里放的是樊城区 ——
        # 樊城区在补物料之后也成了演示区县，于是"别处不该被动"这条用例
        # 断言的是一个**本来就该被订正**的区县，必然失败。
        # 那是用例构造错，不是产品行为变了。
        cls.other_district = make_district(name='东津新区', code='XC')

    def _seed_material(self, name, expiry, district=None, batch_no=''):
        return make_material(name=name, batch_no=batch_no, expiry_date=expiry,
                             district=district or self.demo_district)

    def _run(self, *args):
        out = StringIO()
        call_command('refresh_demo_material_expiry', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        m = self._seed_material('狂犬疫苗', days_from_today(-264))
        out = self._run()
        m.refresh_from_db()
        self.assertEqual(m.expiry_date, days_from_today(-264), '预演不应写库')
        self.assertIn('未**写库', out)
        self.assertIn('将改写', out)

    def test_apply_sets_relative_expiry(self):
        m = self._seed_material('狂犬疫苗', days_from_today(-264))
        out = self._run('--apply')
        m.refresh_from_db()
        self.assertEqual(m.expiry_date, days_from_today(270))
        self.assertIn('已订正 1 行', out)

    def test_batch_no_is_irrelevant(self):
        """批号五花八门也要能订正到 —— 判据不是批号。"""
        m = self._seed_material('狂犬疫苗', days_from_today(-264),
                                batch_no='KY20260901')
        self._run('--apply')
        m.refresh_from_db()
        self.assertEqual(m.expiry_date, days_from_today(270))

    def test_all_three_demo_materials_are_corrected(self):
        for name in ('狂犬疫苗', '猫三联疫苗', '体内外驱虫药'):
            self._seed_material(name, days_from_today(-100))
        self._run('--apply')
        for name, offset in (('狂犬疫苗', 270), ('猫三联疫苗', 180),
                             ('体内外驱虫药', 90)):
            m = Material.objects.get(name=name)
            self.assertEqual(m.expiry_date, days_from_today(offset), name)

    def test_none_expiry_is_not_filled_in(self):
        """⚠ `expiry_date=None` 是「没登记有效期」，**不等于过期**。

        曾把它判成「需要改」，等于给用户没登记有效期的物料凭空编一个日期
        —— 现场 4 个区县里 12 件物料的有效期都是 None，会被一起改掉。
        """
        m = self._seed_material('狂犬疫苗', None)
        out = self._run('--apply')
        m.refresh_from_db()
        self.assertIsNone(m.expiry_date)
        self.assertIn('[跳过]', out)
        self.assertIn('不代填', out)

    def test_chip_without_expiry_is_untouched(self):
        chip = self._seed_material('宠物芯片', None)
        self._run('--apply')
        chip.refresh_from_db()
        self.assertIsNone(chip.expiry_date)

    def test_same_name_in_other_district_is_left_alone(self):
        """⚠ **非演示区县**里的同名过期物料不是演示数据，不能动。"""
        other = self._seed_material('狂犬疫苗', days_from_today(-264),
                                    district=self.other_district)
        out = self._run('--apply')
        other.refresh_from_db()
        self.assertEqual(other.expiry_date, days_from_today(-264))
        self.assertIn('非演示物料', out)

    def test_second_demo_district_is_also_corrected(self):
        """⚠ 演示区县是**复数**，每一个都要订正。

        回归背景：命令原先只认单个 `SEED_MATERIAL_DISTRICT_CODE`。加了第二个
        演示区县之后若不遍历，樊城区的演示物料过期后就**永远修不回来**，
        而且命令不报错 —— 只是"改得比预期少"，看不出少了谁。

        判据落在**业务结果**上（那条记录确实被改成了未来的日期），
        不依赖被测模块的任何常量或数据结构。
        """
        m = self._seed_material('狂犬疫苗', days_from_today(-264),
                                district=self.demo_district_2)
        out = self._run('--apply')
        m.refresh_from_db()
        self.assertNotEqual(
            m.expiry_date, days_from_today(-264),
            '第二个演示区县的过期物料没有被订正')
        self.assertGreater(m.expiry_date, timezone.localdate())
        self.assertIn('已订正 1 行', out)

    def test_non_demo_material_is_left_alone(self):
        """用户自己录入的过期物料是真实业务数据，命令不能碰。"""
        real = make_material(name='用户录入的疫苗', batch_no='USER-BATCH-1',
                             district=self.demo_district,
                             expiry_date=days_from_today(-100))
        out = self._run('--apply')
        real.refresh_from_db()
        self.assertEqual(real.expiry_date, days_from_today(-100))
        self.assertIn('非演示物料', out)
        self.assertIn('用户录入的疫苗', out)

    def test_future_expiry_is_not_rolled_back(self):
        """演示物料但有效期还在未来 —— 说明已订正过或人工填过，不回滚。"""
        m = self._seed_material('狂犬疫苗', days_from_today(999))
        out = self._run('--apply')
        m.refresh_from_db()
        self.assertEqual(m.expiry_date, days_from_today(999))
        self.assertIn('[跳过]', out)

    def test_idempotent(self):
        self._seed_material('狂犬疫苗', days_from_today(-264))
        self._run('--apply')
        out = self._run('--apply')
        self.assertIn('已订正 0 行', out)
        self.assertIn('[跳过]', out)

    def test_missing_demo_district_aborts(self):
        """演示区县不在（被删/改名）时**中止**，不能退化成「全区县都改」。"""
        District.objects.filter(code='CY').delete()
        err = StringIO()
        call_command('refresh_demo_material_expiry', '--apply',
                     stdout=StringIO(), stderr=err)
        self.assertIn('找不到演示区县', err.getvalue())

    def test_no_demo_material_is_reported_cleanly(self):
        out = self._run('--apply')
        self.assertIn('未找到演示物料', out)
