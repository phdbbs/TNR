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
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from business.management.commands.seed_data import (
    SEED_DISTRICT_CODE_BY_INTERNAL, SEED_MATERIAL_DISTRICT_CODES, SEED_MATERIALS,
)
from business.models import (
    Adoption, Capture, CheckIn, Chip, Material, MaterialTransaction, Message,
    Pet, Release, Treatment,
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
            # ⚠ 本轮改动的正是这两个模型的幂等键，它们原先**不在快照里** ——
            # 键写错会让行数变化，而快照看不见，用例照样绿。
            'material': Material.objects.count(),
            'material_txn': MaterialTransaction.objects.count(),
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
            # ⚠ 本轮改动的正是这两个模型的幂等键，它们原先**不在快照里** ——
            # 键写错会让行数变化，而快照看不见，用例照样绿。
            'material': Material.objects.count(),
            'material_txn': MaterialTransaction.objects.count(),
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
        """芯片无有效期是刻意的，不要顺手给它补一个。

        ⚠ 不能用 `Material.objects.get(name='宠物芯片')`：演示区县不止一个，
        每个区县各有一条「宠物芯片」，按名称单独取会 `MultipleObjectsReturned`。
        断言要覆盖**每一个**演示区县。
        """
        self.seed()
        chips = Material.objects.filter(name='宠物芯片')
        self.assertEqual(
            chips.count(), len(SEED_MATERIAL_DISTRICT_CODES),
            '每个演示区县都应有一条「宠物芯片」')
        for chip in chips:
            with self.subTest(district=chip.district.code):
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

        ⚠ 「非演示区县」要排除**全部**演示区县（`SEED_MATERIAL_DISTRICT_CODES`，
        现在是襄城区 + 樊城区）。只排除一个的话，构造出来的"别处"可能正好落在
        另一个演示区县里，`seed_data` 就会在那里**合法地**再建一条 ——
        用例随之变红，但那是用例构造错了，不是产品缺陷。
        """
        self.seed()
        other = District.objects.exclude(
            code__in=SEED_MATERIAL_DISTRICT_CODES).first()
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
        demo = District.objects.get(code=SEED_MATERIAL_DISTRICT_CODES[0])
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
        """每件种子物资在**它自己的区县**里恰好一条（不能多也不能少）。

        ⚠ 断言必须按 (名称, 区县) 做。演示物料**同名存在于多个演示区县**
        （襄城区与樊城区各一套「狂犬疫苗」），只按名称断言在第二个演示区县
        加进来之后会立刻变成「找到了 2 条」—— 那是用例没跟上数据形态，
        不是产品缺陷。
        """
        for _id, name, *_rest in SEED_MATERIALS:
            self._make_same_named_material_elsewhere(name)

        self.seed()
        self.seed()

        for row in SEED_MATERIALS:
            name, district_code = row[1], row[-1]
            with self.subTest(name=name, district=district_code):
                d = District.objects.get(
                    code=SEED_DISTRICT_CODE_BY_INTERNAL[district_code])
                self.assertEqual(
                    Material.objects.filter(name=name, district=d).count(), 1,
                    f'{d.code}「{name}」应恰好一条')

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
        demo = District.objects.get(code=SEED_MATERIAL_DISTRICT_CODES[0])
        self.assertIsNotNone(Material.objects.get(name=name, district=demo))

    def test_every_demo_district_has_the_full_material_set(self):
        """**每个**演示区县都要有完整的一套演示物料。

        这是「补樊城区物料数据」的判据。生产实测：4 条物料全挂在襄城区，
        `hd_gov`（樊城区政府管理员）登录后「物料管理」是**一整页空白** ——
        接口返回 200、页面不报错，只是没有数据。

        所以判据必须落在**业务结果**（每个演示区县各有多少条）上，
        不能停在「命令没报错」—— 那正是这个缺陷躲过前面所有测试的原因。
        """
        self.seed()
        names = sorted({row[1] for row in SEED_MATERIALS})
        for code in SEED_MATERIAL_DISTRICT_CODES:
            with self.subTest(district=code):
                d = District.objects.get(code=code)
                got = sorted(Material.objects.filter(district=d)
                             .values_list('name', flat=True))
                self.assertEqual(
                    got, names,
                    f'{code} 的物料应恰好是演示清单全集（缺哪件都说明该区县页面会缺内容）')

    def test_every_demo_district_has_material_transactions(self):
        """只有物料没有流水，区县的「库存流水」页仍是空的。

        物料台账的意义就在流水上，所以判据同样落在业务结果上。
        """
        self.seed()
        for code in SEED_MATERIAL_DISTRICT_CODES:
            with self.subTest(district=code):
                n = MaterialTransaction.objects.filter(
                    district__code=code).count()
                self.assertGreater(
                    n, 0, f'{code} 应有库存流水，否则该区县的流水页是空的')

    def test_seed_material_district_codes_is_derived_from_the_list(self):
        """演示区县范围必须是**派生**的，不能手工维护。

        手工列表与物料清单一旦漂移（加了物料却忘了加区县），那个区县的
        演示物料过期后就永远修不回来，而订正命令不会报错 —— 只是"改得比预期少"。
        """
        derived = {SEED_DISTRICT_CODE_BY_INTERNAL[row[-1]]
                   for row in SEED_MATERIALS}
        self.assertEqual(set(SEED_MATERIAL_DISTRICT_CODES), derived)
        self.assertGreater(len(SEED_MATERIAL_DISTRICT_CODES), 1,
                           '演示区县应不止一个（襄城区 + 樊城区）')

    def test_fancheng_is_a_demo_district(self):
        """樊城区必须在内 —— 这是**业务要求**，不能靠"派生"自动满足。

        ⚠ 只断言「派生的集合 == 派生的集合」是**同义反复**：把樊城区的物料
        整段删掉，`SEED_MATERIAL_DISTRICT_CODES` 会跟着退化成 ('CY',)，
        上面那条用例照样绿。所以这里把**具体区县**钉死。

        补樊城区物料就是为了这个：生产上 `hd_gov`（樊城区政府管理员）
        登录后「物料管理」是一整页空白 —— 接口 200、页面不报错，只是没数据。
        """
        self.assertIn('HD', SEED_MATERIAL_DISTRICT_CODES)
        expected = sum(1 for row in SEED_MATERIALS if row[-1] == 'D002')
        self.assertGreater(expected, 0, '樊城区在物料清单里必须有条目')

        self.seed()
        d = District.objects.get(code='HD')
        self.assertEqual(
            Material.objects.filter(district=d).count(), expected,
            '樊城区的物料条数应与清单一致')


class RefreshDemoMaterialExpiryMultiDistrictTest(SeedDataTestBase):
    """订正命令必须覆盖**每一个**演示区县，且同名物料各按自己的偏移。

    回归背景：`DEMO_MATERIAL_OFFSETS` 原先是按**物料名**索引的字典。
    演示物料在多个演示区县里同名各有一条，于是后一条**静默覆盖**前一条 ——
    樊城区的物料会被改成襄城区的日期，命令不报错、输出看起来完全正常。
    改为按 **(区县 code, 物料名)** 索引。

    ⚠ 期望值从 **`SEED_MATERIALS` 源清单**里查，不从 `DEMO_MATERIAL_OFFSETS`
    里取 —— 后者是**被测对象**，拿它的形状去断言它自己，退回旧键形状时
    用例只会 `KeyError` 报错，而不是说明"口径错了"。
    """

    @staticmethod
    def _declared_offset(district_code, name):
        """从源清单里查「该区县该物料」声明的有效期偏移。"""
        for row in SEED_MATERIALS:
            if (SEED_DISTRICT_CODE_BY_INTERNAL[row[-1]] == district_code
                    and row[1] == name):
                return row[9]
        raise AssertionError(f'清单里没有 {district_code}/{name}')

    def test_expiry_is_corrected_in_every_demo_district(self):
        self.seed()
        today = timezone.localdate()
        rows = {}
        for code in SEED_MATERIAL_DISTRICT_CODES:
            d = District.objects.get(code=code)
            m = Material.objects.get(name='狂犬疫苗', district=d)
            m.expiry_date = today - timedelta(days=5)   # 人为弄成已过期
            m.save(update_fields=['expiry_date'])
            rows[code] = m

        out = StringIO()
        call_command('refresh_demo_material_expiry', '--apply', stdout=out)
        self.assertIn('已订正', out.getvalue())

        for code, m in rows.items():
            with self.subTest(district=code):
                m.refresh_from_db()
                self.assertEqual(
                    m.expiry_date,
                    today + timedelta(days=self._declared_offset(code, '狂犬疫苗')),
                    f'{code} 的有效期未被订正到**本区县**的偏移')

        # ⚠ 两个区县的结果必须**不同**。同名物料在两个区县的偏移故意不一样
        # （270 / 300），键退回按名称索引时两者会拿到同一个值 ——
        # 这条断言不依赖任何被测数据结构，是"静默覆盖"最直接的判据。
        self.assertNotEqual(
            rows['CY'].expiry_date, rows['HD'].expiry_date,
            '同名物料在两个演示区县被写成了同一个日期 —— 偏移发生了覆盖')


class SeedMessageIdempotencyKeyTest(SeedDataTestBase):
    """种子消息的幂等键必须带**收件人**。

    原键是 `(title, content)`。`Message` 是「一条通知发给一个人」的流水表 ——
    同一个 `(title, content)` 本来就会有多行（同一只宠物的审核通知反复产生、
    一条公告发给 N 个领养人）。本地库实测按 `(title, content)` 分组有 **9 组**
    重复，那是**真实使用产生的正常数据，不是脏数据**（已核对：同一用户同标题
    同正文的多行创建时间相隔 8~9 分钟，是反复实测留下的）。

    所以 `(title, content)` 不是这个表的业务身份。用它取键会**认领别人的行**：
    库里只要已有一条同标题同内容、但收件人不同的真实通知，`get_or_create`
    就认为"已存在"并跳过 —— 演示账号**静默收不到**这条演示消息，
    而命令照样报成功。

    ⚠ 这是**潜在**缺陷：种子清单里三条消息的键本来就互不相同，撞不上。
    用例构造的是「将来会撞」的那一形态，不是今天的形态。
    """

    def test_real_message_to_another_user_does_not_swallow_the_demo_message(self):
        self.seed()
        demo = Message.objects.get(user__username='adopter1', type='approval',
                                   title='领养审核通过')

        # 造一条**同标题同内容、但收件人不同**的真实通知
        other = User.objects.create_user(username='real_adopter', password='x')
        Message.objects.create(user=other, type=demo.type,
                               title=demo.title, content=demo.content)
        # 再把演示那条删掉 —— 模拟「账号被清理后重跑种子」
        demo.delete()

        self.seed()

        self.assertTrue(
            Message.objects.filter(user__username='adopter1',
                                   title='领养审核通过').exists(),
            '收件人不同的同标题消息不应让演示消息被跳过')


class SeedMaterialTxnIdempotencyKeyTest(SeedDataTestBase):
    """种子物资流水的幂等键必须带**类型**。

    `ledger_no` 本身就不唯一，这是业务设计而非缺陷：「下发」与「签收」是
    同一张单的两条台账，运行时两边**共用同一个** `DIS-` 单号（签收行的
    note 里写着「原单号：DIS-…」）。本地库实测：按 `ledger_no` 分组有
    **21 组**重复，加上 `type` 之后只剩 **1 组**（`''` × 66，`consume`
    消耗记录本来就没有台账编号）—— 21 组里绝大多数正是这种下发/签收配对。

    只按 `ledger_no` 取键，种子里真配一对同号的下发/签收就会**静默少建一条**
    （第二条被当成"已存在"跳过），而且不报错。
    """

    def test_pre_existing_row_with_same_ledger_no_but_other_type(self):
        self.seed()
        seeded = MaterialTransaction.objects.get(
            ledger_no='PUR-2025-0105-001', type='purchase')

        # 模拟「同一单号、另一侧台账」的真实行
        MaterialTransaction.objects.create(
            ledger_no=seeded.ledger_no, type='consume',
            material=seeded.material, material_name=seeded.material_name,
            quantity=1, unit=seeded.unit, date=seeded.date,
            district=seeded.district)
        seeded.delete()

        self.seed()

        self.assertTrue(
            MaterialTransaction.objects.filter(
                ledger_no='PUR-2025-0105-001', type='purchase').exists(),
            '同号不同侧的真实台账不应让种子流水被跳过')
