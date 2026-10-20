"""`manage.py check_data_integrity` 的回归测试。

这个命令的价值全在**判定准确**：误报会让人不再看它的输出，漏报等于没有。
所以除了「能发现问题」，还要专门测「**正常数据不会被报出来**」——
写这个命令的第一版就踩过：`validate_operator_district()` 通过时返回 `None`，
被直接当成明细打印，实测 9 个正常账号全被误报成「不一致」。
"""
from io import StringIO

from django.core.management import call_command

from business.models import Release
from business.tests.base import BusinessTestBase, make_capture, make_pet, make_user
from core.models import AuditLog, Institution


class CheckDataIntegrityCommandTest(BusinessTestBase):
    def _run(self, *args):
        out = StringIO()
        try:
            call_command('check_data_integrity', *args, stdout=out)
        except SystemExit as exc:
            return out.getvalue(), exc.code
        return out.getvalue(), 0

    # ---------- 基线 ----------
    def test_clean_database_passes(self):
        output, code = self._run()
        self.assertEqual(code, 0, output)
        self.assertIn('未发现一致性问题', output)

    def test_consistent_accounts_are_not_reported(self):
        """误报守卫：区县与机构一致的账号一个都不该被列出来。

        带上证号：这条测的是「区县一致性」，而没填证号的医院账号会被
        **待补录提示**列出（见 `test_hospital_without_license_is_listed_as_advisory`），
        那样 `assertNotIn` 抓到的就不是本用例要判的东西了。
        """
        user = make_user('di_ok_t', role='hospital',
                         district=self.district_a, institution=self.hospital_a,
                         vet_license='鄂兽医执字20210001号')
        output, code = self._run()
        self.assertEqual(code, 0, output)
        self.assertNotIn(user.username, output)
        self.assertIn('✓ 账号区县与机构区县不一致', output)

    # ---------- 账号 ↔ 机构 ----------
    def test_mismatched_account_is_reported(self):
        user = make_user('di_bad_t', role='hospital',
                         district=self.district_b, institution=self.hospital_a)
        output, code = self._run()
        self.assertEqual(code, 1)
        self.assertIn(user.username, output)
        self.assertIn('不一致', output)

    def test_operator_without_institution_is_reported(self):
        user = make_user('di_orphan_t', role='shelter', district=self.district_a)
        output, code = self._run()
        self.assertEqual(code, 1)
        self.assertIn(user.username, output)
        self.assertIn('缺少所属机构', output)

    def test_institution_without_code_is_reported(self):
        Institution.objects.create(name='无编号机构', type='shelter',
                                   district=self.district_a)
        output, code = self._run()
        self.assertEqual(code, 1)
        self.assertIn('缺少业务编号', output)

    # ---------- 业务记录 ↔ 归属对象 ----------
    def test_capture_district_mismatch_is_reported(self):
        capture = make_capture(district=self.district_b, shelter=self.shelter_a,
                               ledger_no='CAP-DI-0001')
        output, code = self._run()
        self.assertEqual(code, 1)
        self.assertIn('CAP-DI-0001', output)

    def test_deleted_capture_with_active_pet_is_reported(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a)
        capture.is_deleted = True
        capture.save(update_fields=['is_deleted'])
        pet = make_pet(code='DI-PET-0001', district=self.district_a, capture=capture)
        output, code = self._run()
        self.assertEqual(code, 1)
        self.assertIn(pet.code, output)
        self.assertIn('捕捉单已作废', output)

    def test_released_document_on_non_terminal_pet_is_reported(self):
        """放归单已办结、档案却还挂在流程里 —— 现网真有一条，必须报出来。

        成因是撤回一张没签收的转运单时按编号无条件回退状态，把已放归的
        动物打回「待转运」：纸质放归单与物业签字已归档，档案却是「没出院」，
        而且全程不报错。写入点已经修了，这条测的是**历史遗留行还能被查到**。
        """
        pet = make_pet(code='DI-REL-0001', district=self.district_a,
                       shelter=self.shelter_a, status='in_transit')
        Release.objects.create(
            pet=pet, pet_code=pet.code, community=self.community_a,
            community_name=self.community_a.name, status='released',
            ledger_no='REL-DI-0001', district=self.district_a,
            institution=self.shelter_a)
        output, code = self._run()
        self.assertEqual(code, 1, output)
        self.assertIn('REL-DI-0001', output)
        self.assertIn('放归已办结', output)

    def test_released_document_matching_pet_is_not_reported(self):
        """误报守卫：单据与档案一致（动物确实已放归）时一条都不该报。"""
        pet = make_pet(code='DI-REL-0002', district=self.district_a,
                       shelter=self.shelter_a, status='released')
        Release.objects.create(
            pet=pet, pet_code=pet.code, community=self.community_a,
            community_name=self.community_a.name, status='released',
            ledger_no='REL-DI-0002', district=self.district_a,
            institution=self.shelter_a)
        output, code = self._run()
        self.assertEqual(code, 0, output)
        self.assertIn('✓ 放归已办结但宠物状态未出库', output)

    # ---------- 明细截断 ----------
    def test_limit_caps_detail_lines(self):
        for i in range(3):
            make_capture(district=self.district_b, shelter=self.shelter_a,
                         ledger_no=f'CAP-DI-L{i}')
        output, code = self._run('--limit', '1')
        self.assertEqual(code, 1)
        self.assertIn('其余 2 条未列出', output)

    # ---------- 待补录提示（不阻断） ----------
    def test_hospital_without_license_is_listed_as_advisory(self):
        """存量医院账号没证号 → 只提示，**不把退出码改成 1**。

        口径是「不追溯」（`services.validate_vet_license`：只在建号/改号强制）。
        把待补录算成失败，后果不是有人去补，而是部署脚本天天红、
        大家开始忽略巡检输出 —— 那时真问题也会被一起忽略掉。
        """
        user = make_user('di_lic_t', role='hospital',
                         district=self.district_a, institution=self.hospital_a)
        output, code = self._run()
        self.assertIn(user.username, output, '没证号的医院账号没被列出来')
        self.assertIn('不阻断', output)
        self.assertEqual(code, 0, f'待补录提示不该让巡检失败：\n{output}')

    def test_hospital_with_license_not_listed(self):
        """正向对照：填了证号的账号不能被报（否则这条提示永远是一堆噪音）。"""
        user = make_user('di_lic_ok_t', role='hospital',
                         district=self.district_a, institution=self.hospital_a,
                         vet_license='鄂兽医执字20210356号')
        output, _ = self._run()
        self.assertNotIn(user.username, output)

    def test_disabled_or_non_hospital_accounts_are_not_listed(self):
        """只提示**启用中的医院账号**：捕捉点操作员本就不该有证号，
        停用账号不再产生新单据，列出来只会淹没真正要补的那几个。"""
        shelter = make_user('di_lic_sh_t', role='shelter',
                            district=self.district_a, institution=self.shelter_a)
        gone = make_user('di_lic_off_t', role='hospital', is_active=False,
                         district=self.district_a, institution=self.hospital_a)
        output, code = self._run()
        self.assertNotIn(shelter.username, output)
        self.assertNotIn(gone.username, output)
        self.assertEqual(code, 0, output)

    # ---------- 只读 ----------
    def test_command_does_not_write_anything(self):
        """巡检命令**永远**只读 —— 发现问题的下一步是人工确认，不是自动改库。"""
        make_user('di_bad2_t', role='hospital',
                  district=self.district_b, institution=self.hospital_a)

        def snapshot():
            from accounts.models import User
            from business.models import Capture, Pet
            return (User.objects.count(), Institution.objects.count(),
                    Capture.objects.count(), Pet.objects.count(),
                    AuditLog.objects.count())

        before = snapshot()
        self._run()
        self.assertEqual(snapshot(), before)


class SyncOperatorDistrictCommandTest(BusinessTestBase):
    """修复「账号区县 ≠ 机构所在区县」的历史账号。

    这条命令存在的唯一理由：巡检能**报**出这类账号，而政府端用户管理没有编辑
    入口、界面上**修不了** —— 「报了没人能修」的下一步就是没人再看巡检输出。
    """

    def _run(self, *args):
        out = StringIO()
        call_command('sync_operator_district', *args, stdout=out)
        return out.getvalue()

    def _mismatched(self, username='sync_bad_t'):
        """区县与机构不一致、**其余字段干净**的医院账号。

        证号要填上：本类的断言只看「区县修没修好」，而空证号会让这个账号
        同时出现在巡检的待补录提示里，`test_repair_closes_the_loop_with_check`
        的「修完就不该再出现在输出里」会被无关的一行顶掉。
        """
        return make_user(username, role='hospital',
                         district=self.district_b, institution=self.hospital_a,
                         vet_license=f'鄂兽医执字2021{username}号')

    def test_dry_run_does_not_write(self):
        """默认必须是预演 —— 这条命令是给运维在生产上跑的。"""
        user = self._mismatched()
        output = self._run()
        user.refresh_from_db()
        self.assertEqual(user.district_id, self.district_b.id)
        self.assertIn(user.username, output)
        self.assertIn('未写库', output)

    def test_apply_moves_account_to_institution_district(self):
        user = self._mismatched()
        output = self._run('--apply')
        user.refresh_from_db()
        self.assertEqual(user.district_id, self.district_a.id)
        self.assertIn('已修复 1 个账号', output)

    def test_city_level_shelter_operator_is_untouched(self):
        """挂「市级」的捕捉点操作员是现场约定，不是问题账号，不该被搬走。"""
        user = make_user('sync_city_t', role='shelter',
                         district=self.city, institution=self.shelter_a)
        output = self._run('--apply')
        user.refresh_from_db()
        self.assertEqual(user.district_id, self.city.id)
        self.assertIn('没有需要修复的账号', output)

    def test_username_filter_limits_scope(self):
        one = self._mismatched('sync_bad_one')
        two = self._mismatched('sync_bad_two')
        self._run('--apply', '--username', 'sync_bad_one')
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual(one.district_id, self.district_a.id)
        self.assertEqual(two.district_id, self.district_b.id)

    def test_repair_closes_the_loop_with_check(self):
        """修完之后巡检不该再报它 —— 否则这两个命令会互相打脸。"""
        user = self._mismatched()
        self._run('--apply')

        out = StringIO()
        try:
            call_command('check_data_integrity', stdout=out)
        except SystemExit as exc:
            self.fail(f'修复后巡检仍报问题（退出码 {exc.code}）：{out.getvalue()}')
        self.assertIn('未发现一致性问题', out.getvalue())
        self.assertNotIn(user.username, out.getvalue())
