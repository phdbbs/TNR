"""会话失效（401）的前端契约测试。

为什么**单独成文件**、而不是塞进 `tests_frontend_consistency.py`：
后者的主题是「前端枚举映射 / 路由 / 容器与后端的一致性」，而 401 是
**会话生命周期**问题 —— 两者属于不同的提交主题，测试也必须能跟着各自的
提交**独立通过**。混在一个文件里时，任何一个主题单独提交都会让另一个
主题的用例变红，逼得人要么放弃拆分、要么放弃验证。

背景（第三十七轮）：`TNR_API._get()` 原先**完全忽略 HTTP 状态** ——
非 2xx 一律返回 `[]`。401（会话过期）因此被渲染成「所有列表都空了」：
用户看到的是「系统里没有数据」，而不是「请重新登录」，既不知道
发生了什么，也没有任何重新登录的入口。
"""
import os

from django.test import SimpleTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


class UnauthorizedRedirectContractTest(SimpleTestCase):
    """会话失效（401）必须**先提示原因、再跳登录页**，且只处理一次。

    磊哥的决策是「**先提示错误信息，再跳转**」：直接跳转的话用户无法判断
    是会话过期、还是自己点错、还是服务端出问题 —— 也就不知道该不该重新登录。
    """

    def setUp(self):
        self.api = read('static/js/tnr-api.js')
        self.common = read('static/js/tnr-common.js')

    @staticmethod
    def _method_body(source, signature):
        """取出 `signature ... {` 到同级 `  },` 之间的文本。

        `tnr-api.js` 里方法缩进是 2 空格、以 `  },` 收尾，够做关键字断言。
        """
        start = source.find(signature)
        if start < 0:
            return ''
        end = source.find('\n  },', start)
        return source[start:end if end > 0 else start + 2500]

    def test_unauthorized_handler_exists(self):
        self.assertIn(
            '_handleUnauthorized(message)', self.api,
            '缺少统一的 401 处理器 —— 每个调用点各写一遍必然漂移。')

    def test_handler_toasts_before_redirecting(self):
        """**先提示、后跳转**：提示必须早于跳转的**触发**。

        ⚠ 不能拿 `/login/?next=` 的位置当「跳转时刻」—— 它落在 `go`
        这个**闭包的定义**里，定义在前、调用在后。真正的跳转时刻是
        `setTimeout(go, …)` / `go()`。拿定义位置比较会得出反的结论。
        """
        body = self._method_body(self.api, '_handleUnauthorized(message)')
        self.assertTrue(body, '找不到 _handleUnauthorized 的方法体')
        self.assertIn('window.TNR_UI.toast(', body, '跳转前必须真的调用 toast 提示')
        self.assertIn("/login/?next=", body, '必须跳到登录页并带上 next')
        trigger = min(
            (pos for pos in (body.find('setTimeout(go'), body.find('\n      go();'))
             if pos >= 0),
            default=-1)
        self.assertGreaterEqual(trigger, 0, '找不到跳转的触发点')
        self.assertLess(
            body.index('window.TNR_UI.toast('), trigger,
            '提示必须在跳转**触发**之前 —— 反过来的话页面已经走了，提示看不到')

    def test_handler_uses_server_message_not_a_hardcoded_one(self):
        """提示文案要用**服务端返回的 message**，不能前端另编一句。

        服务端口径（`accounts.decorators.api_unauthorized`）是「请先登录」；
        前端自己编一句就出现两套说法，排查时对不上。
        """
        body = self._method_body(self.api, '_handleUnauthorized(message)')
        self.assertRegex(
            body, r'message\s*\|\|',
            '提示文案应优先用服务端 message，缺失时才用兜底文案')

    def test_handler_is_reentrant_guarded(self):
        """门户首屏并发多个请求 → 会话过期时会**同时**拿到多个 401。

        不设闸门就会连弹 N 个提示、触发 N 次跳转，`next` 还会互相覆盖。
        """
        self.assertIn('_redirecting', self.api,
                      '缺少防重入闸门：并发 401 会重复提示、重复跳转')
        body = self._method_body(self.api, '_handleUnauthorized(message)')
        self.assertIn('if (this._redirecting) return;', body,
                      '闸门必须放在方法开头，先判后设')

    def test_every_fetch_path_detects_401(self):
        """**每一个**发请求的方法都要认 401，不能只修一个。

        `_get` / `_post` / `_postForm` / `_handle` / `getData` 五条路径
        各自独立处理响应 —— 只修 `_get` 的话，写接口遇到会话过期仍然
        会把失败当业务错误提示，用户还是不知道要重新登录。
        """
        for signature in ('async _get(url)', 'async _post(url, body)',
                          'async _postForm(url, formData)',
                          'async _handle(res)', 'async getData(url)'):
            with self.subTest(method=signature):
                body = self._method_body(self.api, signature)
                self.assertTrue(body, f'找不到 {signature} 的方法体')
                self.assertIn(
                    'res.status === 401', body,
                    f'{signature} 没有识别 401 —— 会话过期时它会把失败'
                    f'静默成「没有数据」或一句业务错误')

    def test_toast_is_deduplicated(self):
        """并发 401 会让 `_handleUnauthorized` 与调用方 catch 各弹一次。

        没有去重就会看到同一句话叠 2~3 个 toast。

        ⚠ 必须断言**行为**（判据 + 记录），不能只断言字段名存在：
        变异测试实测，只写 `assertIn('_recentToasts', ...)` 时，
        把去重逻辑整段删掉（字段声明还在）**闸门仍然是绿的** ——
        等于这条用例什么都没保护。
        """
        body = self._method_body(
            self.common, "toast(message, type = 'success', duration = 3000) {")
        self.assertTrue(body, '找不到 toast 的方法体')
        self.assertIn(
            'this._recentToasts[key] =', body,
            'toast 缺少短时去重的**记录**步骤')
        self.assertRegex(
            body, r'if \(this\._recentToasts\[key\][^\n]*\)\s*return;',
            'toast 缺少短时去重的**判据** —— 只有记录、没有判断等于没去重')
