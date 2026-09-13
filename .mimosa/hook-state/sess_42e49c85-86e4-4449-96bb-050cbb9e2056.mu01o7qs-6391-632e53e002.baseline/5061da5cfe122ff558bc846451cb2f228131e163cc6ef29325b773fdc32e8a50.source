"""accounts 应用测试：登录/登出/角色重定向/api_me/用户模型。"""
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
