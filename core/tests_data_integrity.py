"""`manage.py check_data_integrity` 的回归测试。

这个命令的价值全在**判定准确**：误报会让人不再看它的输出，漏报等于没有。
所以除了「能发现问题」，还要专门测「**正常数据不会被报出来**」——
写这个命令的第一版就踩过：`validate_operator_district()` 通过时返回 `None`，
被直接当成明细打印，实测 9 个正常账号全被误报成「不一致」。
"""
from io import StringIO

from django.core.management import call_command

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
        """误报守卫：区县与机构一致的账号一个都不该被列出来。"""
        user = make_user('di_ok_t', role='hospital',
                         district=self.district_a, institution=self.hospital_a)
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

    # ---------- 明细截断 ----------
    def test_limit_caps_detail_lines(self):
        for i in range(3):
            make_capture(district=self.district_b, shelter=self.shelter_a,
                         ledger_no=f'CAP-DI-L{i}')
        output, code = self._run('--limit', '1')
        self.assertEqual(code, 1)
        self.assertIn('其余 2 条未列出', output)

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
        return make_user(username, role='hospital',
                         district=self.district_b, institution=self.hospital_a)

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
