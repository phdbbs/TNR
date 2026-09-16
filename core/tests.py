"""core 应用测试：区县与机构模型。"""
from django.db import IntegrityError, transaction
from django.test import TestCase

from business.tests.base import make_district, make_institution, make_user
from core.models import District, Institution


class DistrictModelTest(TestCase):
    def test_create_defaults(self):
        district = make_district(name='测试区', code='X001')
        self.assertEqual(district.status, 'active')
        self.assertFalse(district.is_city)

    def test_code_unique(self):
        make_district(code='SAME01')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_district(code='SAME01')

    def test_city_district_flag(self):
        district = make_district(is_city=True)
        self.assertTrue(district.is_city)


class InstitutionModelTest(TestCase):
    def test_create_with_type(self):
        district = make_district()
        shelter = make_institution(type='shelter', district=district)
        self.assertEqual(shelter.type, 'shelter')
        self.assertEqual(shelter.status, 'active')

    def test_types(self):
        for t in ('shelter', 'hospital', 'community'):
            self.assertEqual(make_institution(type=t).type, t)

    def test_district_protect(self):
        institution = make_institution()
        with self.assertRaises(Exception):
            institution.district.delete()

    def test_institution_delete_sets_user_institution_null(self):
        institution = make_institution(type='hospital')
        user = make_user(role='hospital', institution=institution)
        institution.delete()
        user.refresh_from_db()
        self.assertIsNone(user.institution)


class SecuritySettingsTest(TestCase):
    """部署安全配置的不变量。

    背景：settings.py 早期给 SECRET_KEY 内置了一个公开的兜底值、DEBUG 默认 True、
    ALLOWED_HOSTS 默认 '*'。三者叠加意味着任何绕过 deploy.sh 的启动方式
    （手动 gunicorn、.env 缺失）都会静默降级到「堆栈全公开 + 共用公开密钥 +
    任意 Host」，而部署流程看起来是成功的。这里把修复后的不变量钉死。
    """

    def test_no_hardcoded_secret_key_fallback(self):
        """settings.py 源码里不得再给 SECRET_KEY 内置非空兜底。

        注意不能直接断言运行时 settings.SECRET_KEY 的值——开发机的 .env
        里完全可能合法地放着一个占位密钥，那样断言会变成「看环境脸色」。
        真正的不变量是「源码不给兜底」，运行时行为由
        ProductionConfigFailFastTest 覆盖。
        """
        import re
        from pathlib import Path

        from django.conf import settings

        source = (Path(settings.BASE_DIR) / 'tnr_system' / 'settings.py').read_text(encoding='utf-8')
        self.assertNotIn('django-insecure-dev-default-key-change-me-in-production', source)
        self.assertIsNotNone(
            re.search(r"os\.environ\.get\(\s*'SECRET_KEY'\s*,\s*''\s*\)", source),
            'SECRET_KEY 应写成 os.environ.get(\'SECRET_KEY\', \'\')，不得提供非空默认值')

    def test_cookie_hardening(self):
        from django.conf import settings
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        # CSRF 防护的兜底：业务接口普遍 @csrf_exempt，跨站 POST 不带 Cookie 全靠它
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, 'Lax')
        self.assertEqual(settings.CSRF_COOKIE_SAMESITE, 'Lax')

    def test_proxy_ssl_header_declared(self):
        from django.conf import settings
        self.assertEqual(
            settings.SECURE_PROXY_SSL_HEADER, ('HTTP_X_FORWARDED_PROTO', 'https'))

    def test_frame_and_sniff_protection(self):
        from django.conf import settings
        self.assertEqual(settings.X_FRAME_OPTIONS, 'DENY')
        self.assertTrue(settings.SECURE_CONTENT_TYPE_NOSNIFF)


class ProductionConfigFailFastTest(TestCase):
    """生产配置缺失时必须「启动即失败」，而不是悄悄跑在不安全状态。

    用子进程跑真实的 settings 加载：直接改 os.environ 再 reload 模块
    会污染同进程内的其他测试。
    """

    BASE_DIR = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.conf import settings
        cls.BASE_DIR = str(settings.BASE_DIR)

    def _run_check(self, **env_overrides):
        import os
        import subprocess
        import sys

        env = dict(os.environ)
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, 'manage.py', 'check'],
            cwd=self.BASE_DIR, env=env, capture_output=True, text=True, timeout=90,
        )

    def test_missing_secret_key_fails_fast(self):
        proc = self._run_check(DEBUG='False', SECRET_KEY='', ALLOWED_HOSTS='example.com')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('SECRET_KEY', proc.stderr + proc.stdout)

    def test_missing_allowed_hosts_fails_fast(self):
        import secrets
        proc = self._run_check(
            DEBUG='False', SECRET_KEY=secrets.token_urlsafe(50), ALLOWED_HOSTS='')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ALLOWED_HOSTS', proc.stderr + proc.stdout)

    def test_debug_mode_without_secret_key_still_boots(self):
        proc = self._run_check(DEBUG='True', SECRET_KEY='', ALLOWED_HOSTS='')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
