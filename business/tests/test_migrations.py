"""数据迁移测试：验证 0010 修复历史误标数据的行为与边界。

迁移是「一次性、有副作用」的代码，最容易在写完后没人验证。
这里直接调用迁移函数，覆盖它的两条规则与「不越界」的边界：
  1. 只改判「区县是市级、但捕捉点坐落在具体区县」的捕捉单
  2. 只在字段仍等于旧种子值时才改名（不覆盖运营人员手工改过的名称）
"""
import importlib

from django.apps import apps as global_apps
from django.test import TestCase

from business.models import Adoption, Capture, Pet
from business.tests.base import (
    BusinessTestBase, make_capture, make_institution, make_pet,
)
from core.models import District, Institution

MIGRATION = importlib.import_module(
    'business.migrations.0010_fix_legacy_capture_district_and_names')
CLEANUP = importlib.import_module(
    'business.migrations.0011_cleanup_legacy_place_names')


def run_migration():
    MIGRATION.fix_legacy_data(global_apps, None)


def run_cleanup():
    CLEANUP.fix_legacy_place_names(global_apps, None)


class CaptureDistrictRepairTest(BusinessTestBase):
    """捕捉单区县误标修复。"""

    def test_city_district_capture_moves_to_shelter_district(self):
        cap = make_capture(district=self.city, shelter=self.shelter_a, pet_codes=['P1'])
        pet = make_pet(code='P1', capture=cap, district=self.city)

        run_migration()

        cap.refresh_from_db()
        pet.refresh_from_db()
        self.assertEqual(cap.district, self.district_a,
                         '捕捉单应改判为捕捉点所在的甲区')
        self.assertEqual(pet.district, self.district_a,
                         '宠物档案的区县必须同步，否则列表与详情会互相矛盾')

    def test_correct_capture_is_untouched(self):
        """本来就归属正确的捕捉单不能被迁移碰到。"""
        cap = make_capture(district=self.district_b, shelter=self.shelter_b, pet_codes=['P2'])

        run_migration()

        cap.refresh_from_db()
        self.assertEqual(cap.district, self.district_b)

    def test_capture_whose_shelter_is_city_level_is_left_alone(self):
        """捕捉点本身就挂在市级时无从改判，保持原样而不是猜一个区县。"""
        city_shelter = make_institution(type='shelter', district=self.city,
                                        name='市级捕捉点')
        cap = make_capture(district=self.city, shelter=city_shelter, pet_codes=['P3'])

        run_migration()

        cap.refresh_from_db()
        self.assertEqual(cap.district, self.city)

    def test_migration_is_idempotent(self):
        cap = make_capture(district=self.city, shelter=self.shelter_a, pet_codes=['P4'])

        run_migration()
        run_migration()

        cap.refresh_from_db()
        self.assertEqual(cap.district, self.district_a)


class LegacyNameRepairTest(TestCase):
    """机构名称的北京地名残留修复。"""

    def _legacy_institution(self, code, name, address):
        # Institution.district 是必填外键，这里给一个真实区县
        district = District.objects.create(name='测试区', code=f'TM{code}', status='active')
        return Institution.objects.create(
            code=code, name=name, type='community',
            district=district, address=address, status='active')

    def test_legacy_seed_name_is_renamed(self):
        inst = self._legacy_institution('C003', '中关村南区', '樊城区中关村南区')
        cap = Capture.objects.create(
            district=inst.district, shelter=inst, shelter_name=inst.name,
            community=inst, community_name='中关村南区', pet_count=0,
            status='pending', ledger_no='CAP-MIG-0001')

        run_migration()

        inst.refresh_from_db()
        cap.refresh_from_db()
        self.assertEqual(inst.name, '樊城幸福里小区')
        self.assertEqual(inst.address, '樊城区幸福路12号')
        self.assertEqual(cap.community_name, '樊城幸福里小区',
                         '小区改名后，引用旧名的捕捉单要同步，否则放养流程按名称匹配会落空')

    def test_manually_renamed_institution_is_not_overwritten(self):
        """运营人员改过的名字不能被迁移回退成种子名。"""
        inst = self._legacy_institution('C004', '我们自己改的小区名', '东经开发区西单北大街')

        run_migration()

        inst.refresh_from_db()
        self.assertEqual(inst.name, '我们自己改的小区名',
                         '名称已被人工修改过，迁移不应覆盖')

    def test_missing_institution_is_skipped(self):
        """机构不存在时静默跳过，不能让迁移整体失败。"""
        run_migration()  # 库中没有 C003/C004/I006，应当不报错

    def test_migration_is_idempotent(self):
        inst = self._legacy_institution('C003', '中关村南区', '樊城区中关村南区')

        run_migration()
        run_migration()

        inst.refresh_from_db()
        self.assertEqual(inst.name, '樊城幸福里小区')


class LegacyPlaceNameCleanupTest(TestCase):
    """0011：机构地址与冗余名称副本的北京地名清理。"""

    def _institution(self, code, name, address):
        district = District.objects.create(
            name=f'测试区{code}', code=f'TP{code}', status='active')
        return Institution.objects.create(
            code=code, name=name, type='community',
            district=district, address=address, status='active')

    def test_legacy_address_is_replaced(self):
        inst = self._institution('I001', '襄城流浪动物捕捉点', '朝阳区建国路88号')

        run_cleanup()

        inst.refresh_from_db()
        self.assertEqual(inst.address, '襄城区檀溪路88号')

    def test_edited_address_is_not_overwritten(self):
        """运营人员改过的地址不能被迁移覆盖。"""
        inst = self._institution('I001', '襄城流浪动物捕捉点', '我们自己改的地址')

        run_cleanup()

        inst.refresh_from_db()
        self.assertEqual(inst.address, '我们自己改的地址')

    def test_denormalized_operator_name_is_replaced(self):
        """operator_name 是写入时的快照，用户改名后不会自动跟着变。"""
        shelter = make_institution(type='shelter')
        cap = Capture.objects.create(
            district=shelter.district, shelter=shelter,
            shelter_name='朝阳区流浪动物捕捉点', operator_name='朝阳捕捉点操作员',
            pet_count=0, status='pending', ledger_no='CAP-NAME-0001')

        run_cleanup()

        cap.refresh_from_db()
        self.assertEqual(cap.shelter_name, '襄城流浪动物捕捉点')
        self.assertEqual(cap.operator_name, '襄城捕捉点操作员')

    def test_custom_operator_name_is_left_alone(self):
        shelter = make_institution(type='shelter')
        cap = Capture.objects.create(
            district=shelter.district, shelter=shelter,
            operator_name='张三（临时工）',
            pet_count=0, status='pending', ledger_no='CAP-NAME-0002')

        run_cleanup()

        cap.refresh_from_db()
        self.assertEqual(cap.operator_name, '张三（临时工）')

    def test_seed_capture_address_is_fixed(self):
        shelter = make_institution(type='shelter')
        cap = Capture.objects.create(
            district=shelter.district, shelter=shelter,
            address='朝阳区阳光花园3栋', pet_count=0, status='pending',
            ledger_no='CAP-2025-0010-001')

        run_cleanup()

        cap.refresh_from_db()
        self.assertEqual(cap.address, '襄城区阳光花园3栋')

    def test_missing_institution_is_skipped(self):
        run_cleanup()  # 库中没有任何种子机构，应当不报错

    def test_adopter_address_is_fixed(self):
        """领养人地址同样是逐条固化的，种子定义改了也不会回流。"""
        shelter = make_institution(type='shelter')
        pet = make_pet(shelter=shelter)
        adoption = Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter_name='王领养',
            adopter_address='朝阳区某某小区', status='completed',
            district=shelter.district, ledger_no='ADP-CLEAN-0001')

        run_cleanup()

        adoption.refresh_from_db()
        self.assertEqual(adoption.adopter_address, '襄城区某某小区')

    def test_migration_is_idempotent(self):
        inst = self._institution('I001', '襄城流浪动物捕捉点', '朝阳区建国路88号')

        run_cleanup()
        run_cleanup()

        inst.refresh_from_db()
        self.assertEqual(inst.address, '襄城区檀溪路88号')
