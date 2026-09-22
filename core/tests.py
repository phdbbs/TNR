"""core 应用测试：区县与机构模型。"""
import json

from django.apps import apps
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
from core.http import body_dict, body_list, body_str, read_json_body
from core.models import District, Institution
from tnr_system.urls import (
    api_aware_bad_request, api_aware_csrf_failure, api_aware_not_found,
    api_aware_server_error,
)


def iter_api_routes(pk='1'):
    """遍历 URLconf，产出所有 `/api/` 下的**具体路径**。

    `<int:pk>` 这类占位符替换成 `pk`（默认 `1`）。

    ⚠ `pk` 参数是为 **POST 枚举**准备的：`xxx/<pk>/withdraw/` 这类
    **不读请求体**的动作接口，如果给一个真实存在的 id，就会**真的执行副作用**
    （在测试事务里虽会回滚，但同一方法内后续请求的行为会被带偏）。
    给一个**不存在的 id**（如 `99999999`）即可让它们安全地 404。

    ⚠ 供多个「枚举式契约闸门」共用（未登录响应 / 信封形状 / 非法参数 /
    各角色访问 / POST 参数校验）。**不要在各测试类里各写一份** ——
    遍历逻辑一旦漂移，就会出现「有的闸门覆盖 78 条、有的覆盖 60 条」
    而无人察觉。
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
                    yield '/' + re.sub(r'<[^>]+>', pk, full)

    yield from walk(get_resolver())


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

    def test_every_api_route_answers_json_when_unauthenticated(self):
        c = Client(SERVER_NAME='localhost')
        routes = sorted(set(iter_api_routes()))
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


class AllApiRoutesContractTest(BusinessTestBase):
    """枚举全部 `/api/` 路由的**契约闸门**（第三十三轮）。

    与 `AllApiRoutesReturnJsonWhenUnauthenticatedTest`（未登录那一面）互补，
    这里钉的是**已登录之后**的三条契约：

    ① **响应信封形状** —— 每个响应都必须是 `{'success': bool, ...}`。
       前端普遍写 `if (res.success)`；某个接口不回这个字段，前端就会
       把成功当成失败（或反之），而且**界面上看不出接口错了**。
    ② **非法查询参数不得 500** —— 「可以 4xx、可以 200（忽略），
       但**不能 500**」（见 `MEMORY.md` 的查询参数契约，第二十四轮）。
       一个非法值会以四种不同异常炸接口：`ValueError` / `OverflowError`
       （超大数）/ `ValidationError`（非日期）/ `OverflowError`
       （`date.max + 1天`，同名不同源），所以「随手包一层 `except ValueError`」
       是假修 —— 必须走 `services.parse_int_param()` 等共用解析。
    ③ **每个角色访问每条路由都不得 500、不得非 JSON** —— 这一条最容易抓到
       `Model.DISTRICT_LOOKUP` 缺失（模型无 `district` 外键却忘了声明派生路径）
       导致的 `FieldError`：⚠ **市级走 `return all()` 绕过该分支，
       只测市级账号永远不暴露**，必须把低权限角色也跑一遍。

    ⚠ 全部是 GET。GET 语义上幂等，且整个方法跑在 `TestCase` 的事务里，
    结束即回滚 —— 不会污染夹具。**POST 不在这里枚举**：那会真的写库，
    且方法内部后续请求的行为会被前面写过的数据带偏（已有
    `AllApiRoutesReturnJsonWhenUnauthenticatedTest` 覆盖未登录 POST）。
    """

    # ⚠ 参数名是从生产代码里 `Grep 'request.GET.get('` 抄出来的**真实参数名**，
    # 不是想当然编的 —— 编出来的参数名接口根本不读，等于没测。
    BAD_QUERIES = (
        # 整型外键 / 主键（`parse_int_param` 或直接 int()）
        'id=abc',
        'id=999999999999999999999999',
        'district_id=abc',
        'institution_id=abc',
        'material_id=abc',
        # 字符串过滤项（区县 / 小区 / 捕捉点名）
        'district=abc',
        'community=abc',
        'shelter=abc',
        # 数量类（有上限，见 MAX_CAPTURE_BATCH）
        'count=abc',
        'count=999999999999999999999999',
        'limit=999999999999999999999999',
        'page_size=abc',
        # 日期类（parse_date_param）
        'date=notadate',
        'start_date=notadate',
        'end_date=2026-13-45',
        # 浮点（经纬度，裸 float() + 范围校验）
        'lat=abc',
        'lng=abc',
        'lat=999999999999999999999999',
        # 枚举 / 文本注入 / 布尔类
        'status=<script>',
        'type=__proto__',
        'role=admin',
        'business_type=<img src=x>',
        'include_deleted=yes',
        'q=%00',
    )

    def setUp(self):
        self.routes = sorted(set(iter_api_routes()))
        self.assertGreater(
            len(self.routes), 20,
            '路由枚举结果太少，说明遍历逻辑坏了（会变成假绿）')

    def _assert_all_json(self, problems, label):
        self.assertEqual(
            problems, [],
            '%s 发现以下问题：\n  ' % label + '\n  '.join(problems))

    def test_every_route_returns_a_success_envelope(self):
        self.login_as(self.gov_city)
        problems = []
        for path in self.routes:
            resp = self.client.get(path)
            ctype = (resp.get('Content-Type') or '').split(';')[0]
            if resp.status_code >= 500:
                problems.append('5xx  %s %s' % (path, resp.status_code))
                continue
            if ctype != 'application/json':
                problems.append('%s %s %s' % (path, resp.status_code, ctype))
                continue
            body = resp.json()
            if not isinstance(body, dict) or 'success' not in body:
                problems.append('%s 响应体缺少 success 字段（keys=%s）'
                                % (path, list(body)[:6]))
        self._assert_all_json(problems, '信封形状')

    def test_bad_query_params_never_500(self):
        self.login_as(self.gov_city)
        problems = []
        for path in self.routes:
            for query in self.BAD_QUERIES:
                resp = self.client.get(path + '?' + query)
                if resp.status_code >= 500:
                    problems.append('%s ?%s → %s'
                                    % (path, query, resp.status_code))
        self._assert_all_json(problems, '非法查询参数')

    def test_no_role_triggers_5xx_or_non_json(self):
        roles = (
            ('gov_district', self.gov_a),
            ('shelter', self.shelter_user_a),
            ('hospital', self.hospital_user_a),
            ('adopter', self.adopter),
        )
        for role_name, user in roles:
            with self.subTest(role=role_name):
                self.client.logout()
                self.login_as(user)
                problems = []
                for path in self.routes:
                    resp = self.client.get(path)
                    ctype = (resp.get('Content-Type') or '').split(';')[0]
                    if resp.status_code >= 500 or ctype != 'application/json':
                        problems.append('%s → %s %s'
                                        % (path, resp.status_code, ctype))
                self._assert_all_json(problems, '%s 角色' % role_name)


# ============================================
# POST 请求体契约闸门（第三十五轮）
# ============================================
#: 固定畸形 body。前 5 个是**形状**问题（顶层不是 JSON 对象），
#: 其余是**字段值**问题（每个对应一族实测踩过的解析路径）。
MALFORMED_BODIES = (
    '{}',                                          # 空对象（缺参数）
    '{',                                           # 坏 JSON
    '[1,2,3]',                                     # 顶层数组
    '"just a string"',                             # 顶层字符串
    'null',                                        # 顶层 null
    '{"id": "abc"}',
    '{"district_id": "abc"}',
    '{"institution_id": "abc"}',
    '{"count": "abc"}',
    '{"count": 999999999999999999999999}',
    '{"quantity": "abc"}',
    '{"quantity": -1}',
    '{"material_id": "abc"}',
    '{"lat": "abc", "lng": "abc"}',
    '{"start_date": "notadate"}',
    '{"date": "notadate"}',
    '{"pet_codes": "notalist"}',
    '{"pet_codes": [1, 2, 3]}',
    '{"reason": null}',
    '{"new_password": null}',
    '{"old_password": null, "new_password": null, "confirm_password": null}',
    '{"action": "__proto__"}',
    '{"status": {"$ne": null}}',
)

#: 逐参数名测试用的畸形值（每个真实参数名各发一次）。
#: 四种分别对应：非数字字符串（整型外键 / 日期的经典炸点）、
#: 超大数（`int()` 本身**不报错**，SQLite 绑定时才溢出）、
#: 数组与对象（非字符串 truthy，`.strip()` / `.get()` 都会炸）。
MALFORMED_BODY_VALUES = (
    '"abc"',
    '999999999999999999999999',
    '[1,2,3]',
    '{"$ne": null}',
)

#: 统计「数据是否被创建」时排除的表：
#: - 审计日志会因**任何**请求而增长；
#: - django_q 的表由队列维护；
#: - `sessions.Session` 是**登录动作本身**的副产品 —— 本闸门要切换 5 个
#:   角色，每次 `login_as` 都会写一行 session。它与请求体无关，
#:   不排除的话每次都会报一条 `Session: 0 → 1` 的假阳性。
_COUNT_EXCLUDE = {'core.AuditLog', 'sessions.Session'}


def collect_body_param_names():
    """从生产代码里抄出**真实**的请求体参数名。

    ⚠ 想当然编的参数名接口根本不读，**等于没测**，而测试照样绿
    （第三十四轮踩过：第一版 `BAD_QUERIES` 编了 8 个名字，全部无效）。

    ⚠ 必须同时扫两种形态：收口后大多数读取点已经从 `data.get('x')`
    变成 `body_str(data, 'x')` —— 只扫前者会让参数名从 101 掉到 45，
    **闸门自己退化成假绿**。这正是「枚举式测试的失败模式」：
    枚举源写坏了，结果集变小，而测试仍然全绿。
    """
    import pathlib
    import re

    pats = (
        re.compile(r"""(?:data|payload|body)\.get\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
        re.compile(r"""body_(?:str|int|dict|list)\(\s*data\s*,\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    )
    names = set()
    for path in pathlib.Path('.').rglob('*.py'):
        if ('.venv' in path.parts or 'migrations' in path.parts
                or path.name.startswith('tests')):
            continue
        try:
            src = path.read_text(encoding='utf-8')
        except OSError:
            continue
        for pat in pats:
            names.update(m.group(1) for m in pat.finditer(src))
    return sorted(names)


def model_counts():
    """当前各业务模型的行数（用于「非法请求不得创建数据」的对比）。"""
    out = {}
    for model in apps.get_models():
        label = model._meta.label
        if label in _COUNT_EXCLUDE or label.startswith('django_q.'):
            continue
        try:
            out[label] = model.objects.count()
        except Exception:                       # noqa: BLE001
            # 少数表在测试库里可能不存在（第三方应用未迁移），跳过即可
            pass
    return out


class AllApiPostRoutesContractTest(BusinessTestBase):
    """枚举全部 `/api/` 路由的 **POST 请求体契约闸门**（第三十五轮）。

    第三十四轮把「查询参数契约」做成了枚举闸门（`AllApiRoutesContractTest`），
    但**请求体**没有对应保障 —— 一扩就炸出一族真缺陷：

        ① 17 个接口 × 3 种非 dict body ≈ **51 处 500**
           （`[1,2,3]` / `"str"` / `null` → `AttributeError: 'x' object has
             no attribute 'get'`）
        ② 整型外键畸形值 → `ValueError: Field 'id' expected a number but got ...`
           （`pet_id` / `shelter_id` / `from_shelter_id` / `capture_id` /
             `to_hospital_id` / `district_id` / `institution_id` / `material_id`）
        ③ 字符串参数是 dict/list → `(data.get('name') or '').strip()` 炸
        ④ **`SystemConfig` 被写入 16 行** —— 任意顶层 key 都成了配置项

    ⚠ 三个必须遵守的写法（全是踩出来的）：

    **① 必须多角色跑。** 单角色（gov_city）打所有接口时，大量接口会在
    `role_required` 那层 403 提前返回，**后面的代码根本没执行** ——
    `/api/business/checkins/create/` 只对 adopter 开放，用市级账号打永远是
    403，里面 `data.get('month', '').strip()` 的缺陷一次都扫不到。
    第一版探针就是单角色，漏了一整片。这是这类闸门最隐蔽的假绿来源。

    **② 参数名必须从生产代码抄**，且两种形态都要扫 —— 见
    `collect_body_param_names()` 的说明。

    **③ `<pk>` 必须给不存在的 id**（`99999999`）。`xxx/<pk>/withdraw/`
    这类不读请求体的动作接口，给真实 id 会**真的执行副作用**。

    ⚠ 判据是**两件事**，缺一不可：
      - 状态码 < 500（「可以 4xx、可以 200（忽略），但**不能 500**」）
      - `Content-Type == application/json`（前端三个封装都是 `res.json()`，
        非 JSON 会抛 `SyntaxError` → 用户看到「点了没反应」）
    """

    def setUp(self):
        # ⚠ `99999999` 不存在 → 动作类接口安全 404，不会真执行副作用
        self.routes = sorted(set(iter_api_routes(pk='99999999')))
        self.assertGreater(
            len(self.routes), 20,
            '路由枚举结果太少（%d 条），遍历逻辑可能写坏了 —— '
            '枚举为空时所有 offender 集合都是空的，闸门会**假绿**'
            % len(self.routes))
        self.roles = (
            ('gov_city', self.gov_city),
            ('gov_district', self.gov_a),
            ('shelter', self.shelter_user_a),
            ('hospital', self.hospital_user_a),
            ('adopter', self.adopter),
        )
        # ⚠ 500 默认会让测试客户端**重新抛出异常**，整个枚举在第一处就中断，
        # 拿不到完整清单。关掉它才能一次看到全部 offender。
        self.client.raise_request_exception = False

    def _sweep(self, bodies, label):
        """用 `bodies` 打全部路由 × 全部角色，返回问题清单。"""
        problems = []
        for role_name, user in self.roles:
            self.login_as(user)
            for path in self.routes:
                for desc, body in bodies:
                    resp = self.client.post(
                        path, data=body, content_type='application/json')
                    ctype = (resp.get('Content-Type') or '').split(';')[0]
                    if resp.status_code >= 500:
                        problems.append('%s %s %s → %s'
                                        % (role_name, path, desc, resp.status_code))
                    elif ctype != 'application/json':
                        problems.append('%s %s %s → %s %s'
                                        % (role_name, path, desc,
                                           resp.status_code, ctype))
        return problems

    def _assert_no_problems(self, problems, label):
        self.assertEqual(
            [], problems,
            '以下「%s」把接口打成了 5xx 或非 JSON 响应：\n  %s\n\n'
            '前端 `TNR_API` 的三个封装（`_get` / `_post` / `_postForm`）内部都是\n'
            '`await res.json()`，非 JSON 响应会让它抛 `SyntaxError` —— 用户看到的是\n'
            '「页面空白」或「点了没反应」，而服务端日志里只有一个 4xx/5xx，\n'
            '看起来完全正常。\n\n'
            '读取请求体参数请统一走 `body_str` / `body_int` / `body_dict` /\n'
            '`body_list`（定义见 `core/http.py` 与 `business/services.py`）。'
            % (label, '\n  '.join(problems[:40])))

    def test_malformed_bodies_are_rejected_cleanly(self):
        """固定畸形 body：不得 5xx、不得非 JSON、**不得创建任何业务记录**。

        ⚠ 第三件事（数据是否被创建）是这一轮才补上的维度：探针第一次跑就
        发现 `supervision.SystemConfig` 从 0 变成 16 —— 非法请求体竟然写库了。
        只检查状态码的闸门对这类问题**完全无感**。
        """
        before = model_counts()
        problems = self._sweep(
            [(b[:32], b) for b in MALFORMED_BODIES], '固定畸形 body')
        self._assert_no_problems(problems, '固定畸形 body')

        after = model_counts()
        created = ['%s: %d → %d' % (label, before.get(label), n)
                   for label, n in after.items() if n != before.get(label)]
        self.assertEqual(
            [], created,
            '非法请求体竟然创建了业务数据：\n  %s\n\n'
            '「静默写入垃圾」比 500 更难查 —— 500 至少留下 Traceback，\n'
            '而这类数据会被当成真实业务数据渲染出来，且无法与正常数据区分。'
            % '\n  '.join(created))

    def test_every_body_param_rejects_malformed_values(self):
        """逐参数名打畸形值：覆盖「接口**真的读了**哪些参数」。

        与固定 body 那组互补 —— 固定 body 只能覆盖**想得到的**字段名，
        而这里覆盖的是源码里**实际存在**的每一个参数名（含嵌套的
        `vaccine.material_id` 这类）。

        ⚠ 这里**不**检查「数据是否被创建」：本组用的是真实参数名 +
        `"abc"` 这种合法字符串，`{"name": "abc"}` 打到创建接口会**合法地**
        建出记录，混进来就是假阳性。
        """
        names = collect_body_param_names()
        self.assertGreater(
            len(names), 40,
            '从源码抄出的请求体参数名只有 %d 个，扫描逻辑可能写坏了 —— '
            '参数名收窄会让本闸门**假绿**' % len(names))
        bodies = []
        for name in names:
            for value in MALFORMED_BODY_VALUES:
                bodies.append(('%s=%s' % (name, value[:16]),
                               '{%s: %s}' % (json.dumps(name), value)))
        problems = self._sweep(bodies, '逐参数名畸形值')
        self._assert_no_problems(problems, '逐参数名畸形值')

    def test_non_json_content_type_still_returns_json(self):
        """`application/x-www-form-urlencoded` 提交也必须回 JSON。

        这条路径走的是 `request.POST` 分支（`read_json_body` 对非
        multipart 也会去解析 body，失败返回 `{}`），与 JSON 分支是两套代码 ——
        只测 JSON 分支会漏掉这一半。
        """
        problems = []
        for role_name, user in self.roles:
            self.login_as(user)
            for path in self.routes:
                resp = self.client.post(
                    path, data='a=b',
                    content_type='application/x-www-form-urlencoded')
                ctype = (resp.get('Content-Type') or '').split(';')[0]
                if resp.status_code >= 500 or ctype != 'application/json':
                    problems.append('%s %s → %s %s'
                                    % (role_name, path, resp.status_code, ctype))
        self._assert_no_problems(problems, 'form-encoded 提交')

    def test_empty_multipart_still_returns_json(self):
        """空 multipart 提交也必须回 JSON（`request.POST` 为空的边界）。"""
        problems = []
        for role_name, user in self.roles:
            self.login_as(user)
            for path in self.routes:
                resp = self.client.post(path, data={})
                ctype = (resp.get('Content-Type') or '').split(';')[0]
                if resp.status_code >= 500 or ctype != 'application/json':
                    problems.append('%s %s → %s %s'
                                    % (role_name, path, resp.status_code, ctype))
        self._assert_no_problems(problems, '空 multipart 提交')


class ReadJsonBodyNormalisationTest(SimpleTestCase):
    """`read_json_body` 必须**只**返回 `dict`（第三十五轮收口）。

    此前它的 docstring 写着「返回值可能是 `list` 等非 dict（合法 JSON），
    **调用方自行判断**」—— 实测这条约定**没有一个调用方遵守**：约 30 处
    调用点清一色是 `data = parse_json_body(request)` 之后直接 `data.get(...)`。
    于是顶层非 dict 的 body 一律炸 `AttributeError` → 500。

    为什么必须在这里归一、而不是去 30 个调用点加 `isinstance`：
    `request.body` 的容错逻辑（`RequestDataTooBig` / 坏 JSON / multipart）
    **只应该有一份**（第二十六轮就是因为两份实现漂移才漏掉 `RequestDataTooBig`）。
    散到 30 处去加判断必然再次漂移，而漂移的表现就是「有的接口 500、
    有的接口正常」这种最难查的形态。
    """

    def _parse(self, raw, content_type='application/json'):
        return read_json_body(RequestFactory().post(
            '/api/x/', data=raw, content_type=content_type))

    def test_top_level_non_dict_is_normalised_to_empty_dict(self):
        for raw in ('[1,2,3]', '"just a string"', 'null', '123', 'true'):
            with self.subTest(body=raw):
                self.assertEqual(
                    self._parse(raw), {},
                    '顶层非 dict 的合法 JSON 必须归一成 `{}`，'
                    '否则调用方的 `data.get(...)` 会抛 AttributeError → 500')

    def test_dict_body_is_preserved(self):
        self.assertEqual(self._parse('{"a": 1, "b": [1, 2]}'),
                         {'a': 1, 'b': [1, 2]})

    def test_broken_json_returns_empty_dict(self):
        for raw in ('{', '', 'not json at all'):
            with self.subTest(body=raw):
                self.assertEqual(self._parse(raw), {})

    def test_multipart_does_not_read_body(self):
        """multipart 一律返回 `{}`（有用数据在 `request.POST` / `request.FILES`）。

        ⚠ 这里不能用 `RequestFactory.post(data={...})` 造 multipart ——
        那会真的走解析器。用 content_type 直接模拟即可，本用例只关心
        「不读 `request.body`」这一条分支。
        """
        req = RequestFactory().post(
            '/api/x/', data='whatever',
            content_type='multipart/form-data; boundary=xxx')
        self.assertEqual(read_json_body(req), {})


class BodyValueGuardTest(SimpleTestCase):
    """`body_str` / `body_dict` / `body_list` 的类型守卫（第三十五轮）。

    这三个函数的共同点：**`or 默认值` 挡不住非 falsy 的错误类型**。
    `{"name": [1, 2, 3]}` / `{"name": {"$ne": null}}` / `{"name": 123}`
    全是 truthy，`or ''` 不生效，于是直接 `.strip()` / `.get()` 炸。

    ⚠ 也**不能**用 `str(value)` 强转来「修」：`str({'$ne': None})` 得到
    字面量 `"{'$ne': None}"`，会被当成真实业务数据写进库。
    「静默写入垃圾」比 500 更难查 —— 500 至少留下 Traceback。
    """

    TRUTHY_WRONG_TYPES = ([1, 2, 3], {'$ne': None}, 123, 1.5, True)

    def test_body_str_rejects_every_non_string(self):
        for value in self.TRUTHY_WRONG_TYPES:
            with self.subTest(value=value):
                self.assertEqual(body_str({'k': value}, 'k'), '',
                                 '非字符串必须回 default，不能 `str()` 强转 —— '
                                 '强转会把垃圾值当真实数据写进库')

    def test_body_str_keeps_strings_and_honours_default(self):
        self.assertEqual(body_str({'k': ' v '}, 'k'), ' v ',
                         '不 strip —— 要不要 strip 由调用方决定，与原写法等价')
        self.assertEqual(body_str({'k': ''}, 'k'), '')
        self.assertEqual(body_str({}, 'k', 'fallback'), 'fallback')
        self.assertEqual(body_str({'k': None}, 'k', 'fallback'), 'fallback')

    def test_body_dict_rejects_every_non_dict(self):
        for value in ('abc', [1, 2, 3], 123, None):
            with self.subTest(value=value):
                self.assertEqual(body_dict({'k': value}, 'k'), {})

    def test_body_dict_keeps_dicts(self):
        self.assertEqual(body_dict({'k': {'a': 1}}, 'k'), {'a': 1})
        self.assertEqual(body_dict({}, 'k'), {})

    def test_body_list_rejects_every_non_list(self):
        for value in ('abc', {'a': 1}, 123, None):
            with self.subTest(value=value):
                self.assertEqual(body_list({'k': value}, 'k'), [])

    def test_body_list_keeps_lists(self):
        self.assertEqual(body_list({'k': [1, 2]}, 'k'), [1, 2])
        self.assertEqual(body_list({}, 'k'), [])

    def test_body_list_does_not_return_tuples(self):
        """JSON 没有 tuple —— 但接口若被 Python 内部调用可能传进来。

        ⚠ 刻意只认 `list`：`isinstance(x, (list, tuple))` 会让
        「JSON 数组」与「内部 tuple」两种来源混在一起，而 JSON 解析
        永远不会产出 tuple，放宽只会让守卫的语义变模糊。
        """
        self.assertEqual(body_list({'k': (1, 2)}, 'k'), [])
