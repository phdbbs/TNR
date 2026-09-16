"""seed_data 幂等性与自愈能力测试。

背景：DEPLOY.md / start_dev.sh / DEMO_ACCOUNTS.md 都把 `seed_data` 描述为
**幂等**命令，并且要求「每次部署或调试启动后演示账号都可用」。实际实现里
有两处做不到：

1. 机构用 `name` 作为 get_or_create 去重键，而 name 是可在界面上修改的
   展示字段 —— 改名后每次部署都会再插一套同名机构（现场库里已出现孤儿
   重复捕捉点）；
2. `_seed_users` 只在 created 分支写属性，账号一旦被停用或改了归属，
   重跑 seed_data 修不回来（现场 aixin_hosp、hd_shelter 就是被停用后
   无法登录，而部署流程看起来是"成功"的）。

这些测试把上述承诺钉死，避免再次退化。
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from accounts.models import User
from business.models import (
    Adoption, Capture, CheckIn, Chip, Message, Pet, Release, Treatment,
)
from core.models import District, Institution

SEED_USERNAMES = [
    'admin', 'cy_gov', 'hd_gov', 'cy_shelter', 'hd_shelter',
    'aixin_hosp', 'ruipeng_hosp', 'babitang_hosp', 'adopter1',
]


class SeedDataTestBase(TestCase):
    def seed(self):
        out = StringIO()
        call_command('seed_data', stdout=out)
        return out.getvalue()


class SeedIdempotencyTest(SeedDataTestBase):
    """连续执行不得产生重复数据。"""

    def test_running_twice_creates_no_duplicates(self):
        self.seed()
        snapshot = {
            'district': District.objects.count(),
            'institution': Institution.objects.count(),
            'user': User.objects.count(),
            'chip': Chip.objects.count(),
            'capture': Capture.objects.count(),
            'pet': Pet.objects.count(),
            'treatment': Treatment.objects.count(),
            'release': Release.objects.count(),
            'adoption': Adoption.objects.count(),
            'checkin': CheckIn.objects.count(),
            'message': Message.objects.count(),
        }
        self.seed()
        after = {
            'district': District.objects.count(),
            'institution': Institution.objects.count(),
            'user': User.objects.count(),
            'chip': Chip.objects.count(),
            'capture': Capture.objects.count(),
            'pet': Pet.objects.count(),
            'treatment': Treatment.objects.count(),
            'release': Release.objects.count(),
            'adoption': Adoption.objects.count(),
            'checkin': CheckIn.objects.count(),
            'message': Message.objects.count(),
        }
        self.assertEqual(after, snapshot, f'第二次执行产生了重复数据：{snapshot} -> {after}')

    def test_renamed_institution_is_not_duplicated(self):
        """机构改名后重跑 seed_data 不能再生出一套（按 code 幂等，而非 name）。"""
        self.seed()
        inst = Institution.objects.get(code='I001')
        inst.name = '襄城捕捉点（已改名）'
        inst.save(update_fields=['name'])

        self.seed()

        self.assertEqual(Institution.objects.filter(code='I001').count(), 1)
        inst.refresh_from_db()
        self.assertEqual(inst.name, '襄城捕捉点（已改名）', '种子数据不应覆盖人工改过的机构名')
        # 该区县不应出现第二个捕捉点
        self.assertEqual(
            Institution.objects.filter(type='shelter', district=inst.district).count(), 1)

    def test_renamed_district_is_not_duplicated(self):
        self.seed()
        d = District.objects.get(code='CY')
        d.name = '襄城区（已改名）'
        d.save(update_fields=['name'])

        self.seed()

        self.assertEqual(District.objects.filter(code='CY').count(), 1)
        d.refresh_from_db()
        self.assertEqual(d.name, '襄城区（已改名）')

    def test_institutions_have_unique_stable_codes(self):
        self.seed()
        codes = list(Institution.objects.exclude(code__isnull=True)
                     .values_list('code', flat=True))
        self.assertEqual(len(codes), len(set(codes)), '机构编号必须唯一')
        self.assertEqual(
            set(codes),
            {'I001', 'I002', 'I003', 'I004', 'I005', 'I006',
             'C001', 'C002', 'C003', 'C004'})


class SeedDemoAccountSelfHealingTest(SeedDataTestBase):
    """演示账号必须每次执行后都可用（DEMO_ACCOUNTS.md 的承诺）。"""

    def test_reactivates_disabled_demo_account(self):
        self.seed()
        u = User.objects.get(username='aixin_hosp')
        u.is_active = False
        u.status = 'inactive'
        u.save(update_fields=['is_active', 'status'])

        out = self.seed()

        u.refresh_from_db()
        self.assertTrue(u.is_active, '被停用的演示账号必须被重新启用')
        self.assertEqual(u.status, 'active')
        self.assertIn('aixin_hosp', out, '校准行为应在命令输出中明示')

    def test_repairs_drifted_account_ownership(self):
        self.seed()
        other = District.objects.exclude(code='CY').exclude(code='CITY').first()
        u = User.objects.get(username='cy_gov')
        u.district = other
        u.institution = None
        u.save(update_fields=['district', 'institution'])

        self.seed()

        u.refresh_from_db()
        self.assertEqual(u.district.code, 'CY')
        self.assertEqual(u.institution, None)
        self.assertEqual(u.role, 'gov_district')
        self.assertTrue(u.is_staff)

    def test_repairs_institution_binding(self):
        self.seed()
        u = User.objects.get(username='cy_shelter')
        u.institution = Institution.objects.get(code='I002')
        u.save(update_fields=['institution'])

        self.seed()

        u.refresh_from_db()
        self.assertEqual(u.institution.code, 'I001')

    def test_does_not_reset_existing_password(self):
        """已存在账号的密码不自动重置——避免静默覆盖生产环境改过的凭据。"""
        self.seed()
        u = User.objects.get(username='adopter1')
        u.set_password('ChangedByOperator#2026')
        u.save(update_fields=['password'])

        self.seed()

        u.refresh_from_db()
        self.assertTrue(u.check_password('ChangedByOperator#2026'),
                        'seed_data 不应覆盖人工修改过的密码')

    def test_new_accounts_get_demo_password(self):
        self.seed()
        for username in SEED_USERNAMES:
            u = User.objects.get(username=username)
            self.assertTrue(u.check_password('123456'), f'{username} 的初始密码应为 123456')
            self.assertTrue(u.is_active, f'{username} 应为启用状态')


class SeedChipNumberConsistencyTest(SeedDataTestBase):
    """芯片号段必须与宠物/诊疗档案里的 chip_no 对得上。

    原实现补零宽度写成 4 位，生成 11 位的 10000100001，而档案里存的是
    10 位的 1000010001，导致医院登记「芯片植入」时报「芯片不存在」。
    """

    def test_pet_chip_no_exists_in_chip_inventory(self):
        self.seed()
        pets = Pet.objects.exclude(chip_no='')
        self.assertTrue(pets.exists(), '种子数据应至少有一只带芯片的宠物')
        for pet in pets:
            self.assertTrue(
                Chip.objects.filter(number=pet.chip_no).exists(),
                f'{pet.code} 的 chip_no={pet.chip_no} 在芯片库存中查不到')

    def test_treatment_chip_no_exists_in_chip_inventory(self):
        self.seed()
        for t in Treatment.objects.exclude(chip_no=''):
            self.assertTrue(
                Chip.objects.filter(number=t.chip_no).exists(),
                f'诊疗单 {t.ledger_no} 的 chip_no={t.chip_no} 在芯片库存中查不到')

    def test_seed_chip_range_is_ten_digits(self):
        self.seed()
        nums = list(Chip.objects.values_list('number', flat=True))
        self.assertEqual(len(nums), 500)
        self.assertEqual(sorted({len(n) for n in nums}), [10],
                         f'芯片号应为 10 位，实际长度集合 {sorted({len(n) for n in nums})}')
        self.assertIn('1000010001', nums)
        self.assertIn('1000010500', nums)
