"""core 应用测试：区县与机构模型。"""
from django.conf import settings
from django.core.exceptions import (
    RequestDataTooBig, TooManyFieldsSent, TooManyFilesSent,
)
from django.db import IntegrityError, transaction
from django.http.multipartparser import MultiPartParserError
from django.test import (
    Client, RequestFactory, SimpleTestCase, TestCase, override_settings,
)

from business.tests.base import (
    BusinessTestBase, make_district, make_institution, make_user,
)
from core.models import District, Institution
from tnr_system.urls import (
    api_aware_bad_request, api_aware_csrf_failure, api_aware_not_found,
    api_aware_server_error,
)


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


class ApiAwareBadRequestTest(SimpleTestCase):
    """`/api/` 下的 400 必须是**可读 JSON**（第三十一轮）。

    默认的 `django.views.defaults.bad_request` 渲染 HTML，而前端
    `TNR_API._postForm` 内部是 `await res.json()` —— 拿到 HTML 就抛错，
    表现为「**点提交没反应**」。这一族（`RequestDataTooBig` /
    `TooManyFilesSent` / `TooManyFieldsSent` / `MultiPartParserError`）
    都经 `handler400`，所以在这里一次性收口。
    """

    def _resp(self, path, exc):
        return api_aware_bad_request(RequestFactory().post(path), exc)

    @staticmethod
    def _body(resp):
        """⚠ 直接调用 handler 拿到的是 `JsonResponse` 本体，**没有** `.json()`
        （那是测试客户端包装后的响应才有的方法）。"""
        import json
        return json.loads(resp.content)

    def test_api_path_returns_json_envelope(self):
        resp = self._resp('/api/business/captures/create/', TooManyFilesSent())
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp['Content-Type'].split(';')[0], 'application/json')
        body = self._body(resp)
        self.assertFalse(body['success'])
        self.assertIsNone(body['data'])
        self.assertTrue(body['message'])

    def test_each_transport_exception_has_a_readable_chinese_message(self):
        cases = {
            RequestDataTooBig: '提交的数据过大',
            TooManyFilesSent: '照片数量过多',
            TooManyFieldsSent: '表单字段过多',
            MultiPartParserError: '无法解析',
        }
        for exc_type, fragment in cases.items():
            with self.subTest(exc=exc_type.__name__):
                resp = self._resp('/api/x/', exc_type('boom'))
                self.assertIn(fragment, self._body(resp)['message'])

    def test_message_never_leaks_framework_internals(self):
        """绝不能把 Django 的英文原文回给用户。

        `str(RequestDataTooBig())` 是
        `Request body exceeded settings.DATA_UPLOAD_MAX_MEMORY_SIZE.` ——
        既不可读，又把框架实现细节暴露给客户端。
        （`business/tests/test_multipart_body_limit.py` 里有同口径的断言。）
        """
        for exc_type in (RequestDataTooBig, TooManyFilesSent,
                         TooManyFieldsSent, MultiPartParserError):
            with self.subTest(exc=exc_type.__name__):
                text = self._resp('/api/x/', exc_type('x')).content.decode()
                self.assertNotIn('exceeded', text.lower())
                self.assertNotIn('DATA_UPLOAD_MAX', text)
                self.assertNotIn('settings.', text)

    def test_non_api_path_keeps_the_default_html_page(self):
        """正向对照：页面导航仍拿到 HTML 错误页，不能被改成 JSON。"""
        resp = self._resp('/login/', RequestDataTooBig('x'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('text/html', resp['Content-Type'])

    def test_handler400_is_wired_into_the_root_urlconf(self):
        """证明 `handler400` 真的被 Django 用上了（不是写了个没人调的函数）。"""
        from django.urls import get_resolver

        self.assertIs(get_resolver(None).resolve_error_handler(400),
                      api_aware_bad_request)


class ApiAwareCsrfFailureTest(SimpleTestCase):
    """`/api/` 下的 CSRF 失败也必须是**可读 JSON**（第三十二轮）。

    ⚠ 只能靠 `settings.CSRF_FAILURE_VIEW` 收口 —— `CsrfViewMiddleware` 是
    **直接返回** `HttpResponseForbidden`（不抛异常），`handler400` / `handler403`
    都接不住它。这条与 `ApiAwareBadRequestTest` 是同一个口径的两半。
    """

    def _resp(self, path, reason='CSRF cookie not set.'):
        return api_aware_csrf_failure(RequestFactory().post(path), reason=reason)

    @staticmethod
    def _body(resp):
        import json
        return json.loads(resp.content)

    def test_api_path_returns_json_envelope(self):
        resp = self._resp('/api/me/')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp['Content-Type'].split(';')[0], 'application/json')
        body = self._body(resp)
        self.assertFalse(body['success'])
        self.assertIsNone(body['data'])
        self.assertTrue(body['message'])

    def test_reason_is_not_leaked_to_the_client(self):
        """Django 的判定原因（`CSRF cookie not set.` / `Origin checking failed`）
        属实现细节，不能回给客户端。"""
        for reason in ('CSRF cookie not set.', 'Origin checking failed - x does not match y',
                       'Referer checking failed - no Referer.'):
            with self.subTest(reason=reason):
                text = self._resp('/api/me/', reason).content.decode()
                self.assertNotIn('CSRF cookie', text)
                self.assertNotIn('Origin checking', text)
                self.assertNotIn('Referer', text)
                self.assertNotIn('does not match', text)

    def test_non_api_path_keeps_the_default_html_page(self):
        """正向对照：页面导航仍拿到 HTML 403 页，不能被改成 JSON。"""
        resp = self._resp('/shelter/')
        self.assertEqual(resp.status_code, 403)
        self.assertIn('text/html', resp['Content-Type'])

    def test_csrf_failure_view_setting_is_wired(self):
        """证明 `CSRF_FAILURE_VIEW` 真的指向我们的实现。"""
        from django.utils.module_loading import import_string

        self.assertIs(import_string(settings.CSRF_FAILURE_VIEW),
                      api_aware_csrf_failure)


class ApiAwareCsrfFailureEndToEndTest(TestCase):
    """端到端：打开真实 CSRF 校验，不带 token POST 非豁免接口 → JSON 403。

    `/api/me/` 是**没有** `@csrf_exempt` 的（全站 `/api/` 里只有 3 个这样，
    另两个是 `adoptions/hall/` 与 `adoptions/hall/<pk>/`）。CSRF 中间件在
    `process_view` 阶段就返回，所以不必登录也能触发。

    ⚠ 用 `TestCase`（允许访问数据库）而不是 `SimpleTestCase`：走完整请求栈会
    触发 `business.apps` 挂在 `request_started` 上的**启动补偿**，它要查库；
    在 `SimpleTestCase` 里会被禁库拦下并记一条 ERROR 堆栈 ——
    那只是测试环境噪音，但会盖住真正的失败信号。
    """

    def test_real_csrf_rejection_on_api_path_is_json(self):
        c = Client(enforce_csrf_checks=True, SERVER_NAME='localhost')
        resp = c.post('/api/me/', data='{}', content_type='application/json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp['Content-Type'].split(';')[0], 'application/json',
                         'CSRF 失败不得把接口炸成 HTML 403 页')
        self.assertIn('安全校验', resp.json()['message'])


class ApiTransportLimitEndToEndTest(BusinessTestBase):
    """端到端：撞上传输层闸门时，前端拿到的是 **JSON** 而不是 HTML。

    这是「点提交没反应」这一族缺陷的最终防线 —— 只要响应是 JSON，
    前端就能把 `message` 显示出来，用户至少知道发生了什么。
    """

    @override_settings(DATA_UPLOAD_MAX_NUMBER_FILES=2)
    def test_too_many_files_degrades_to_readable_json(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        png = (b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
        self.login_as(self.shelter_user_a)
        resp = self.client.post('/api/business/captures/create/', {
            'district_id': str(self.district_a.id),
            'shelter_id': str(self.shelter_a.id),
            'a.png': SimpleUploadedFile('a.png', png, content_type='image/png'),
            'b.png': SimpleUploadedFile('b.png', png, content_type='image/png'),
            'c.png': SimpleUploadedFile('c.png', png, content_type='image/png'),
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp['Content-Type'].split(';')[0], 'application/json',
                         '传输层闸门不得把接口炸成 HTML 错误页')
        self.assertFalse(resp.json()['success'])
        self.assertIn('照片数量过多', resp.json()['message'])


class ApiAwareNotFoundTest(SimpleTestCase):
    """`/api/` 下的 404 必须是**可读 JSON**（第三十三轮）。

    前端打了一个不存在的接口（后端下线了接口而前端没同步、路径拼错），
    默认拿到的是 HTML 404 页 → `res.json()` 抛错 → 页面空白。
    """

    def _resp(self, path, exc=None):
        return api_aware_not_found(RequestFactory().get(path), exc)

    @staticmethod
    def _body(resp):
        import json
        return json.loads(resp.content)

    def test_api_path_returns_json_envelope(self):
        resp = self._resp('/api/business/nope/')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp['Content-Type'].split(';')[0], 'application/json')
        body = self._body(resp)
        self.assertFalse(body['success'])
        self.assertIsNone(body['data'])
        self.assertTrue(body['message'])

    def test_message_does_not_echo_the_request_path(self):
        """⚠ 不能把 `request.path` 原样回显 —— 那是反射型 XSS 的常见入口。

        路径里可以塞 `/<script>alert(1)</script>`，回显就等于把用户输入
        当成了响应内容。这里只回固定文案。
        """
        evil = '/api/<script>alert(1)</script>/'
        text = self._resp(evil).content.decode()
        self.assertNotIn('<script>', text)
        self.assertNotIn('alert(1)', text)

    def test_non_api_path_keeps_the_default_html_page(self):
        """正向对照：页面导航仍拿到 HTML 404 页，不能被改成 JSON。"""
        resp = self._resp('/nope-page/')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('text/html', resp['Content-Type'])

    def test_handler404_is_wired_into_the_root_urlconf(self):
        from django.urls import get_resolver

        self.assertIs(get_resolver(None).resolve_error_handler(404),
                      api_aware_not_found)


class ApiAwareServerErrorTest(SimpleTestCase):
    """`/api/` 下的 500 也必须是**可读 JSON**（第三十三轮）。

    ⚠ 与 400 / 404 的关键差别：`handler500` 的签名**只有一个参数**。
    写错会在 500 时**再抛一次异常**，而那次异常没有任何 handler 能接 ——
    用户看到的是裸连接断开，比 HTML 错误页更难排查。
    """

    def _resp(self, path):
        return api_aware_server_error(RequestFactory().get(path))

    @staticmethod
    def _body(resp):
        import json
        return json.loads(resp.content)

    def test_handler500_takes_exactly_one_argument(self):
        """把「签名只有一个参数」钉死，防止有人照着 handler400 改成两个。"""
        import inspect

        params = inspect.signature(api_aware_server_error).parameters
        self.assertEqual(list(params), ['request'],
                         'handler500 的签名只能是 (request)')

    def test_api_path_returns_json_envelope(self):
        resp = self._resp('/api/business/captures/')
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp['Content-Type'].split(';')[0], 'application/json')
        body = self._body(resp)
        self.assertFalse(body['success'])
        self.assertIsNone(body['data'])
        self.assertTrue(body['message'])

    def test_message_never_leaks_internals(self):
        text = self._resp('/api/x/').content.decode()
        for leak in ('Traceback', 'Exception', 'settings.', 'site-packages'):
            self.assertNotIn(leak, text)

    def test_non_api_path_keeps_the_default_html_page(self):
        resp = self._resp('/portal/')
        self.assertEqual(resp.status_code, 500)
        self.assertIn('text/html', resp['Content-Type'])

    def test_handler500_is_wired_into_the_root_urlconf(self):
        from django.urls import get_resolver

        self.assertIs(get_resolver(None).resolve_error_handler(500),
                      api_aware_server_error)


class AllApiRoutesReturnJsonWhenUnauthenticatedTest(TestCase):
    """**枚举全部 `/api/` 路由**：未登录访问时响应必须是 JSON。

    这条是第三十三轮那个缺陷的**防回归闸门**。当时的形态是：
    三处接口写的是 Django 自带的 `@login_required`（而不是 `role_required`
    或 `api_login_required`），未登录时 `redirect_to_login()` → **302**；
    而 `fetch` 默认 `redirect: 'follow'` → 跟随到 `/login/` → 最终 **200**
    + **HTML** → `res.json()` 抛 `SyntaxError`。

    ⚠ 注意**不能**只断言「状态码不是 302」：跟随后是 200，状态码看着完全正常。
    必须断言 **`Content-Type` 是 `application/json`**，并且**跟随重定向**
    （`Client` 默认就跟随）。

    ⚠ 也不能只测「已知的那三个接口」—— 那样下一个新写的裸 `@login_required`
    接口照样漏网。这里从 URLconf **动态枚举**，新增接口自动被覆盖。
    """

    @staticmethod
    def _iter_api_routes():
        """遍历 URLconf，产出所有 `/api/` 下的**具体路径**。

        `<int:pk>` 这类占位符替换成 `1`；转换器参数一律用 `1`，
        因为这一层只关心「守卫怎么回」，不关心业务是否存在该 id。
        """
        import re

        from django.urls import URLPattern, URLResolver, get_resolver

        def walk(resolver, prefix=''):
            for entry in resolver.url_patterns:
                if isinstance(entry, URLResolver):
                    yield from walk(entry, prefix + str(entry.pattern))
                elif isinstance(entry, URLPattern):
                    full = prefix + str(entry.pattern)
                    if full.startswith('api/'):
                        yield '/' + re.sub(r'<[^>]+>', '1', full)

        yield from walk(get_resolver())

    def test_every_api_route_answers_json_when_unauthenticated(self):
        c = Client(SERVER_NAME='localhost')
        routes = sorted(set(self._iter_api_routes()))
        self.assertGreater(len(routes), 20,
                           '路由枚举结果太少，说明遍历逻辑坏了（会变成假绿）')

        offenders = []
        for path in routes:
            for method in ('get', 'post'):
                resp = getattr(c, method)(path, data={})
                ctype = (resp.get('Content-Type') or '').split(';')[0]
                if ctype != 'application/json':
                    offenders.append(
                        '%s %s → %s %s%s' % (
                            method.upper(), path, resp.status_code, ctype,
                            ' (重定向到 %s)' % resp['Location']
                            if resp.get('Location') else '',
                        )
                    )

        self.assertEqual(
            offenders, [],
            '以下 /api/ 路由在未登录时没有回 JSON —— 前端 res.json() 会抛错：\n  '
            + '\n  '.join(offenders),
        )
