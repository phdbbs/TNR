"""accounts 应用测试：登录/登出/角色重定向/api_me/用户模型。"""
import json

from accounts.models import User
from business.tests.base import (
    ApiMixin, make_district, make_institution, make_user,
)
from django.test import TestCase


class LoginViewTest(ApiMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.district = make_district()
        cls.city_district = make_district(name="市", code="TC1", is_city=True)
        cls.shelter = make_institution(type="shelter", district=cls.district)
        cls.hospital = make_institution(type="hospital", district=cls.district)
        cls.gov_city = make_user("lg_gov_city", role="gov_city", district=cls.city_district)
        cls.gov_district = make_user("lg_gov_d", role="gov_district", district=cls.district)
        cls.shelter_user = make_user("lg_shelter", role="shelter", institution=cls.shelter)
        cls.hospital_user = make_user("lg_hospital", role="hospital", institution=cls.hospital)
        cls.adopter = make_user("lg_adopter", role="adopter")

    def test_login_page_renders_anonymous(self):
        resp = self.client.get('/login/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/html', resp['Content-Type'])

    def test_login_success_redirects_by_role(self):
        cases = [
            (self.gov_city, '/gov/'),
            (self.gov_district, '/gov/'),
            (self.shelter_user, '/shelter/'),
            (self.hospital_user, '/hospital/'),
            (self.adopter, '/adopter/'),
        ]
        for user, expected in cases:
            self.client.logout()
            resp = self.client.post('/login/', {
                'username': user.username, 'password': '123456',
            })
            self.assertEqual(resp.status_code, 302, user.username)
            self.assertTrue(resp.url.startswith(expected),
                            f'{user.username} 应跳转 {expected}，实际 {resp.url}')

    def test_login_wrong_password_stays(self):
        resp = self.client.post('/login/', {
            'username': self.adopter.username, 'password': 'wrong',
        })
        self.assertEqual(resp.status_code, 200)

    def test_login_unknown_user_stays(self):
        resp = self.client.post('/login/', {'username': 'ghost', 'password': 'x'})
        self.assertEqual(resp.status_code, 200)

    def test_login_inactive_account_clear_message(self):
        """停用账号登录给出明确提示，而非误导性的密码错误"""
        self.gov_city.is_active = False
        self.gov_city.status = 'inactive'
        self.gov_city.save()
        resp = self.client.post('/login/', {
            'username': self.gov_city.username, 'password': '123456',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('该账号已被停用', resp.content.decode())
        # 恢复
        self.gov_city.is_active = True
        self.gov_city.status = 'active'
        self.gov_city.save()

    def test_login_missing_fields_stays(self):
        resp = self.client.post('/login/', {})
        self.assertEqual(resp.status_code, 200)

    def test_logged_in_user_bounced_from_login_page(self):
        self.client.force_login(self.adopter)
        resp = self.client.get('/login/')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith('/adopter/'))

    def test_logout_redirects_to_login(self):
        self.client.force_login(self.adopter)
        resp = self.client.get('/logout/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)
        resp = self.client.get('/adopter/')
        self.assertEqual(resp.status_code, 302, '登出后应无法访问门户')

    def test_dashboard_redirect_by_role(self):
        cases = [
            (self.gov_city, '/gov/'),
            (self.shelter_user, '/shelter/'),
            (self.hospital_user, '/hospital/'),
            (self.adopter, '/adopter/'),
        ]
        for user, expected in cases:
            self.client.force_login(user)
            resp = self.client.get('/')
            self.assertEqual(resp.status_code, 302, user.username)
            self.assertTrue(resp.url.startswith(expected))

    def test_dashboard_redirect_requires_login(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)


class ApiMeTest(ApiMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.district = make_district()
        cls.institution = make_institution(type="hospital", district=cls.district)
        cls.user = make_user("me_user", role="hospital",
                             district=cls.district, institution=cls.institution)

    def test_anonymous_401(self):
        resp = self.client.get('/api/me/')
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.json()['success'])

    def test_authed_returns_profile(self):
        self.client.force_login(self.user)
        body = self.ok(self.client.get('/api/me/'))
        data = body['data']
        self.assertEqual(data['username'], 'me_user')
        self.assertEqual(data['role'], 'hospital')
        self.assertEqual(data['district_id'], self.district.id)
        self.assertEqual(data['institution_id'], self.institution.id)


class ChangePasswordTest(ApiMixin, TestCase):
    """修改密码接口：此前前端按钮点了没反应。"""
    URL = '/api/me/password/'

    @classmethod
    def setUpTestData(cls):
        cls.district = make_district()
        cls.institution = make_institution(type="shelter", district=cls.district)
        cls.user = make_user("pwd_user", role="shelter",
                             district=cls.district, institution=cls.institution)

    def test_anonymous_gets_json_401(self):
        """未登录必须回 **401 JSON**，不能 302 到登录页。

        ⚠ 这条断言原来写的是 `assertIn(resp.status_code, (302, 403))` ——
        **把缺陷本身当成了正确行为**（连测试名都叫 `..._redirected`）。
        但 `fetch` 的默认 `redirect` 是 `'follow'`：302 会被自动跟随到
        `/login/`，最终 status 是 **200**、body 是 **HTML 登录页** ——
        前端 `res.json()` 抛 `SyntaxError` → 用户看到「点提交没反应」。
        所以 302 正是第三十三轮修掉的缺陷，不能再被断言钉住。
        """
        resp = self.client.post(self.URL, data='{}', content_type='application/json')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp['Content-Type'].split(';')[0], 'application/json')
        self.assertFalse(resp.json()['success'])

    def test_change_success_and_session_kept(self):
        self.client.force_login(self.user)
        body = self.ok(self.post_json(self.URL, {
            'old_password': '123456',
            'new_password': 'newPass2026',
            'confirm_password': 'newPass2026',
        }))
        self.assertIn('成功', body['message'])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('newPass2026'))
        # 改密后会话仍有效，可直接访问门户
        self.assertEqual(self.client.get('/api/me/').status_code, 200)

    def test_wrong_old_password(self):
        self.client.force_login(self.user)
        self.expect_fail(self.post_json(self.URL, {
            'old_password': 'wrong', 'new_password': 'newPass2026',
            'confirm_password': 'newPass2026',
        }), message='原密码错误')

    def test_mismatched_confirm(self):
        self.client.force_login(self.user)
        self.expect_fail(self.post_json(self.URL, {
            'old_password': '123456', 'new_password': 'newPass2026',
            'confirm_password': 'otherPass2026',
        }), message='两次输入的新密码不一致')

    def test_same_as_old_rejected(self):
        self.client.force_login(self.user)
        self.expect_fail(self.post_json(self.URL, {
            'old_password': '123456', 'new_password': '123456',
            'confirm_password': '123456',
        }), message='新密码不能与原密码相同')

    def test_weak_password_rejected(self):
        self.client.force_login(self.user)
        resp = self.client.post(self.URL, data='{"old_password":"123456","new_password":"123","confirm_password":"123"}',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('123456'), '弱密码不应生效')

    def test_missing_fields(self):
        self.client.force_login(self.user)
        self.expect_fail(self.post_json(self.URL, {'old_password': ''}),
                         message='请填写原密码与新密码')

    def test_get_method_rejected(self):
        self.client.force_login(self.user)
        self.expect_fail(self.client.get(self.URL), status=405, message='仅支持 POST 请求')

    def test_oversized_body_returns_json_not_html(self):
        """⚠ 回归（第二十九轮）：超大请求体不得把接口炸成 **HTML 错误页**。

        `request.body` 超 `DATA_UPLOAD_MAX_MEMORY_SIZE`（默认 2.5MB）会抛
        `RequestDataTooBig` —— 它是 `SuspiciousOperation` 子类，**不是**
        `ValueError` / `TypeError`。原实现只 catch `(ValueError, TypeError)`，
        异常冒泡成 `400 text/html`（标题 `RequestDataTooBig at /api/me/password/`，
        `DEBUG` 下还带 Traceback）。前端拿到非 JSON 响应体会 `res.json()` 抛错
        → **静默中断**，用户看到的就是「点按钮没反应」。

        修法：走 `core.http.read_json_body()`（与 business 侧同一实现）。
        """
        self.client.force_login(self.user)
        big = json.dumps({'old_password': 'a' * 3_000_000,
                          'new_password': 'newPass2026',
                          'confirm_password': 'newPass2026'})
        resp = self.client.post(self.URL, data=big,
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('application/json', resp['Content-Type'],
                      '超大请求体返回了非 JSON（HTML 错误页）—— 前端会静默中断')
        self.assertFalse(resp.json()['success'])

    def test_oversized_body_does_not_change_password(self):
        """超大请求体被拒时，密码必须**一字未改**（拒绝要发生在写库之前）。"""
        self.client.force_login(self.user)
        big = json.dumps({'old_password': '123456',
                          'new_password': 'HackedPass#2026',
                          'confirm_password': 'HackedPass#2026',
                          'pad': 'a' * 3_000_000})
        self.client.post(self.URL, data=big, content_type='application/json')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('123456'), '原密码不应被改动')
        self.assertFalse(self.user.check_password('HackedPass#2026'))


class UserModelTest(TestCase):
    def test_role_default_adopter(self):
        user = User.objects.create_user(username='default_role', password='x')
        self.assertEqual(user.role, 'adopter')

    def test_role_property_helpers(self):
        user = make_user(role='shelter')
        self.assertTrue(user.is_shelter)
        self.assertFalse(user.is_hospital)

    def test_str_includes_role_display(self):
        user = make_user(role='gov_city')
        self.assertIn('市级政府管理员', str(user))

    def test_district_set_null_on_delete(self):
        district = make_district()
        user = make_user(district=district)
        district.delete()
        user.refresh_from_db()
        self.assertIsNone(user.district)


class EnsureSuperuserCommandTest(TestCase):
    """`manage.py ensure_superuser` —— 部署脚本依赖它保证有可登录的超级管理员。

    回归背景：deploy.sh 原先把这段逻辑写成「若 username='admin' 不存在则创建
    超级用户」，但前一步 seed_data 已经建了一个**非超级用户**的 admin，
    于是分支恒为假、永不执行，而脚本结尾仍提示「超级管理员: admin / admin123456」，
    运维照提示登录必然失败。下面的用例把正确的判定条件钉死。
    """

    def _run(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('ensure_superuser', *args, stdout=out)
        return out.getvalue()

    def _credentials(self, output):
        for line in output.splitlines():
            if line.startswith('ADMIN_CREDENTIALS='):
                return line[len('ADMIN_CREDENTIALS='):].split(' ', 1)
        return None, None

    def test_promotes_existing_non_superuser_admin(self):
        """原缺陷场景：seed_data 建的 admin 是非超级用户，必须被提升。"""
        User.objects.create_user(username='admin', password='123456', role='gov_city',
                                 is_staff=True)
        output = self._run()

        admin = User.objects.get(username='admin')
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_active)
        self.assertEqual(admin.status, 'active')
        self.assertEqual(admin.role, 'gov_city', '不应改动原有业务角色')
        username, password = self._credentials(output)
        self.assertEqual(username, 'admin')
        self.assertTrue(admin.check_password(password), '脚本提示的口令必须是真实生效的口令')

    def test_creates_superuser_when_none_exists(self):
        self.assertFalse(User.objects.filter(is_superuser=True).exists())
        output = self._run()
        username, password = self._credentials(output)
        self.assertEqual(username, 'admin')
        admin = User.objects.get(username='admin')
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password(password))

    def test_skips_when_active_superuser_exists(self):
        """判定条件必须是「是否已有启用的超级管理员」，而不是某个用户名是否存在。"""
        boss = User.objects.create_superuser(username='boss', password='BossPass#2026')
        output = self._run()

        self.assertIn('ADMIN_RESULT=SKIP', output)
        self.assertNotIn('ADMIN_CREDENTIALS=', output)
        self.assertFalse(User.objects.filter(username='admin').exists(),
                         '已有超级管理员时不应再新建账号')
        boss.refresh_from_db()
        self.assertTrue(boss.check_password('BossPass#2026'), '不得重置已有超管的口令')

    def test_inactive_superuser_does_not_count(self):
        """被停用的超级管理员不算「可用」，否则会部署出一个登不进去的系统。"""
        User.objects.create_superuser(username='admin', password='OldPass#2026')
        User.objects.filter(username='admin').update(is_active=False, status='inactive')

        output = self._run()

        admin = User.objects.get(username='admin')
        self.assertTrue(admin.is_active, '必须把被停用的超管重新启用')
        self.assertEqual(admin.status, 'active')
        username, password = self._credentials(output)
        self.assertTrue(admin.check_password(password))

    def test_explicit_password_used(self):
        output = self._run('--password', 'DeployPass#2026')
        admin = User.objects.get(username='admin')
        self.assertTrue(admin.check_password('DeployPass#2026'))
        self.assertIn('DeployPass#2026', output)

    def test_admin_password_env_var_used(self):
        import os
        os.environ['ADMIN_PASSWORD'] = 'EnvPass#2026'
        try:
            self._run()
        finally:
            os.environ.pop('ADMIN_PASSWORD', None)
        self.assertTrue(User.objects.get(username='admin').check_password('EnvPass#2026'))

    def test_generated_password_is_random_per_run(self):
        first = self._credentials(self._run())[1]
        User.objects.all().delete()
        second = self._credentials(self._run())[1]
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 12)

    def test_reset_password_flag_forces_reset(self):
        boss = User.objects.create_superuser(username='admin', password='OldPass#2026')
        output = self._run('--reset-password')
        boss.refresh_from_db()
        self.assertFalse(boss.check_password('OldPass#2026'))
        self.assertIn('ADMIN_CREDENTIALS=', output)

    def test_custom_username(self):
        self._run('--username', 'superboss')
        self.assertTrue(User.objects.get(username='superboss').is_superuser)
