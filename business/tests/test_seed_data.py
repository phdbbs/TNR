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
3. `_seed_materials` 用 `name` 作去重键，而 `Material.name` 没有唯一约束：
   同名物资分布在多个区县时（本地与生产库都是 4 区县 × 4 件）会抛
   `MultipleObjectsReturned`，因 `handle()` 整体在事务里而让**全部种子
   数据回滚** —— 部署到第 6 步直接失败。

这些测试把上述承诺钉死，避免再次退化。
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from business.management.commands.seed_data import (
    SEED_MATERIALS, SEED_MATERIAL_DISTRICT_CODE,
)
from business.models import (
    Adoption, Capture, CheckIn, Chip, Material, Message, Pet, Release, Treatment,
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


class SeedMaterialExpiryTest(SeedDataTestBase):
    """种子物料的「有效期」必须是**面向未来**的。

    原实现把有效期写成了绝对日期（2025-12-31 / 2025-10-31 / 2026-06-30），
    而「有效期」不像入库日期那样属于历史事件 —— 它是面向未来的属性。
    结果是任何时间点的全新部署，演示数据一上线就「全部已过期」
    （实测 2026-09-21 部署时分别过期 83 / 264 / 325 天）。

    这些断言与「今天」无关，因此在任何日期运行都成立；一旦有人改回
    绝对日期，它们就会在过期当天开始失败。
    """

    # 允许的有效期区间（相对今天的天数），与 seed_data 里取的 90~270 对齐，
    # 留出余量以免边界日抖动。
    MIN_DAYS = 30
    MAX_DAYS = 400

    def test_materials_have_a_future_expiry_date(self):
        self.seed()
        today = timezone.localdate()
        dated = [m for m in Material.objects.all() if m.expiry_date is not None]
        self.assertTrue(dated, '种子物料应至少有一件带有效期（否则本测试形同虚设）')
        for m in dated:
            self.assertGreater(
                m.expiry_date, today,
                f'{m.name} 的有效期 {m.expiry_date} 已过期（今天 {today}）—— '
                f'种子数据不应写死绝对日期')

    def test_expiry_offset_stays_in_expected_window(self):
        """有效期应落在「今天 + 30~400 天」内。

        只断言「未过期」挡不住写死日期：若有人写死 2027-06-30，那么
        2026 年跑测试照样通过。加上区间断言后，写死日期会随时间漂出窗口。
        """
        self.seed()
        today = timezone.localdate()
        for m in Material.objects.exclude(expiry_date=None):
            delta = (m.expiry_date - today).days
            self.assertGreaterEqual(
                delta, self.MIN_DAYS,
                f'{m.name} 的有效期距今天仅 {delta} 天，不在预期窗口 '
                f'{self.MIN_DAYS}~{self.MAX_DAYS} 内')
            self.assertLessEqual(
                delta, self.MAX_DAYS,
                f'{m.name} 的有效期距今天 {delta} 天，不在预期窗口 '
                f'{self.MIN_DAYS}~{self.MAX_DAYS} 内')

    def test_chip_keeps_no_expiry_date(self):
        """芯片无有效期是刻意的，不要顺手给它补一个。"""
        self.seed()
        chip = Material.objects.get(name='宠物芯片')
        self.assertIsNone(chip.expiry_date)


class SeedMaterialMultiDistrictTest(SeedDataTestBase):
    """同名物资存在于多个区县时，seed_data 仍必须幂等（**不能崩**）。

    现场缺陷：`_seed_materials` 用 `get_or_create(name=name)` 去重，而
    `Material.name` **没有唯一约束** —— 同一件物资在多个区县各有一条是
    正常业务状态（本地与生产库都是 4 区县 × 4 件同名物资）。此时
    `get_or_create` 内部的 `self.get(name=...)` 会抛 `MultipleObjectsReturned`。

    后果比"报个错"严重得多：`handle()` 整体包在 `transaction.atomic()` 里，
    异常让**全部种子数据回滚**，而 `deploy.sh` 第 6 步就是这条命令 ——
    等于部署到一半失败，且演示账号也不会被校准。

    已改为按 **(name, district)** 取键；下面把两个方向都钉住：
      - 别的区县有同名物资 → 演示区县仍要**自己新建**一条，而不是复用它；
      - 反复执行 → 行数不变。
    """

    def _make_same_named_material_elsewhere(self, name):
        """在**非演示区县**造一条同名物资（模拟现场的多区县分布）。

        会先执行一次 `seed_data` 把区县建出来，再插入同名物资 ——
        场景等价于「库里已经跑过一次种子、后来别的区县也有了同名物资，
        现在再次部署」。
        """
        self.seed()
        other = District.objects.exclude(code=SEED_MATERIAL_DISTRICT_CODE).first()
        self.assertIsNotNone(other, '需要至少一个非演示区县才能构造该场景')
        return Material.objects.create(
            name=name, category='vaccine', unit='支', district=other,
        )

    def test_same_named_material_in_other_district_does_not_crash(self):
        """别的区县有同名物资时，seed_data 不能抛 MultipleObjectsReturned。"""
        name = SEED_MATERIALS[0][1]
        other_material = self._make_same_named_material_elsewhere(name)

        out = self.seed()   # 修复前这里直接抛异常

        self.assertIn('种子数据填充完成', out)
        demo = District.objects.get(code=SEED_MATERIAL_DISTRICT_CODE)
        self.assertTrue(
            Material.objects.filter(name=name, district=demo).exists(),
            f'演示区县应有一条自己的「{name}」')
        # 别区县那条不能被"认领"或改写
        other_material.refresh_from_db()
        self.assertNotEqual(other_material.district_id, demo.id,
                            '演示区县之外的同名物资不应被挪走')

    def test_repeated_runs_do_not_duplicate_materials(self):
        """存在跨区县同名物资时，连续执行仍不产生重复。"""
        name = SEED_MATERIALS[0][1]
        self._make_same_named_material_elsewhere(name)

        self.seed()
        snapshot = Material.objects.count()
        self.seed()

        self.assertEqual(Material.objects.count(), snapshot,
                         '重跑 seed_data 产生了重复物资')

    def test_each_demo_material_is_created_exactly_once(self):
        """演示区县里，每件种子物资恰好一条（不能多也不能少）。"""
        for _id, name, *_rest in SEED_MATERIALS:
            self._make_same_named_material_elsewhere(name)

        self.seed()
        self.seed()

        demo = District.objects.get(code=SEED_MATERIAL_DISTRICT_CODE)
        for _id, name, *_rest in SEED_MATERIALS:
            with self.subTest(name=name):
                self.assertEqual(
                    Material.objects.filter(name=name, district=demo).count(), 1,
                    f'演示区县「{name}」应恰好一条')

    def test_material_lookup_key_is_not_name_alone(self):
        """回归防线：取键必须带区县。

        直接盯住"用 name 单独取"这个写法 —— 它在多区县同名时会抛
        `MultipleObjectsReturned`，是本次缺陷的根因。
        """
        name = SEED_MATERIALS[0][1]
        self._make_same_named_material_elsewhere(name)
        self.seed()

        with self.assertRaises(Material.MultipleObjectsReturned):
            Material.objects.get(name=name)   # 说明确实存在多条同名
        # 而按 (name, district) 取是安全的 —— 这正是 seed_data 现在用的键
        demo = District.objects.get(code=SEED_MATERIAL_DISTRICT_CODE)
        self.assertIsNotNone(Material.objects.get(name=name, district=demo))
