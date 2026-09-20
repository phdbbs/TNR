"""第二十五轮：**静默读契约**。

## 这一轮要钉住的不变量

`TNR_API._get()` 丢掉响应信封（只返回 `data`），调用方拿到的 `[]` **无法区分**
「没有数据」与「服务端拒绝」。所以

    try { const rows = await TNR_API.getXxx(); }
    catch (e) { 渲染「加载失败」 }

里的 catch **覆盖不到 HTTP 错误**（403/400/500 只要响应体是合法 JSON 就被静默成空）。
这类「作者承诺了失败提示、但提示永远不会出现」的写法，本轮实测 **12 处**。

修法是给 `TNR_API` 加**严格读**（`getData` 内核 + `XxxStrict` 便捷方法，失败抛错），
并把这 12 处改成严格读。**默认语义完全不变** —— 列表页要的「失败降级成空」照旧。

本文件把这个契约钉死：
  1. 死 catch 清点必须为 **0**；
  2. 每个 `XxxStrict` 必须委托 `getData`，且必须有同名的软版本；
  3. `getData` 必须抛错、`_get` 必须保持静默（两条都是**源码级**断言）；
  4. 判据不空转：植入一处回归，清点器必须报出来。

⚠ 判据全部建立在 `scripts/jslex.py` 的剥离结果上。那个词法器本身也被本文件
断言（`LexerSelfCheckTest`）—— 它出问题的话，上面所有判据都会**静默失真**。
"""
import re
import sys
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from jslex import (  # noqa: E402
    REGEX_FLAGS, REGEX_PRECEDERS, SCRIPT_RE, audit, match_brace, strip_js,
    stripped_kinds,
)
from dead_catch_probe import (  # noqa: E402
    API_PATH, PORTALS, api_methods, classify, survey,
)

PORTAL_FILES = {n: ROOT / 'templates' / 'portal' / n / 'portal.html' for n in PORTALS}
FRONTEND_FILES = list(PORTAL_FILES.values()) + [
    ROOT / 'static' / 'js' / 'tnr-api.js',
    ROOT / 'static' / 'js' / 'tnr-common.js',
]


def read(p):
    return p.read_text(encoding='utf-8')


class LexerSelfCheckTest(SimpleTestCase):
    """词法器自身的自检 —— 它失真的话，下面所有判据都会**静默失真**。"""

    def test_audit_passes_on_every_frontend_file(self):
        for p in FRONTEND_FILES:
            with self.subTest(file=p.name):
                ok, why = audit(read(p))
                self.assertTrue(ok, f'{p} 剥离自检失败：{why}')

    def test_audit_is_not_vacuous(self):
        """正向对照：自检必须能**报出**问题，否则它只是个恒真式。"""
        src = 'const a = `x${ y }z`;\nfunction f(){ try { g(); } catch (e) { h(); } }\n'
        ok, _ = audit(src)
        self.assertTrue(ok, '正常源码不该报错')
        # 手工造一个「闭 `}` 被当成真代码输出」的失衡文本
        broken = 'function f() { return 1; }}\n'
        ok2, why = audit(broken)
        self.assertFalse(ok2, '大括号失衡必须被报出来')
        self.assertIn('大括号', why)

    def test_template_nested_backtick_does_not_desync(self):
        r"""模板字符串里嵌模板字符串 —— 第一版词法器就是栽在这里。

        `${d ? '<p>x</p>' : ''}`（内层用反引号）里的内层反引号曾被误当成外层
        结束，状态整体错位一个反引号，**整段后续代码被吞**。
        """
        src = ("const body = `\n  <div>${d ? `<p class=\"hint\">x</p>` : ''}</div>\n`;\n"
               "try { f(); } catch (e) { g(); }\n")
        st = strip_js(src)
        self.assertEqual(len(src), len(st), '长度必须不变')
        ok, why = audit(src)
        self.assertTrue(ok, why)
        # `try {` 必须还在原位置
        self.assertIn('try {', st)

    def test_quoted_regex_is_not_mistaken_for_a_string(self):
        """`/"/g` 是合法正则 —— 「体里出现引号就判否」会把它误杀。"""
        src = 'const s = String(v).replace(/"/g, \'""\');\ntry { f(); } catch (e) { g(); }\n'
        st = strip_js(src)
        self.assertEqual(len(src), len(st))
        ok, why = audit(src)
        self.assertTrue(ok, why)
        self.assertIn('try {', st)

    def test_regex_preceder_set_is_exactly_the_standard_heuristic(self):
        """正则前导字符集**钉死**为标准启发式，不多不少。

        ⚠ `<` / `>` 曾经在集合里。它们的危害**不是**「自己单独出事」——
        真实 JS 里裸 `</` 只出现在字符串/模板里（已被前面几条覆盖）。
        危害是**放大器**：一旦别处失步露出裸 `</button>`，`/` 会被当成正则
        起点、一直吃到下一个 `/`，把「一处失步」放大成「整段代码被吞」——
        实测就是这么把 gov 端 13 处 `try {` 吞到只剩 3 处的。
        算术运算符（`+ - * % ~ ^`）同理：`a + /re/` 在语法上不成立，
        放进去只会制造误判。
        """
        self.assertEqual(REGEX_PRECEDERS, set('(,=:[!&|?{};'))
        for ch in '<>+-*%~^':
            self.assertNotIn(ch, REGEX_PRECEDERS,
                             f'{ch} 不该在正则前导集里（标准启发式不含它）')

    def test_regex_flag_set_is_complete(self):
        """闭 `/` 后的合法标志集 —— 判「是不是正则」靠它兜底。"""
        self.assertEqual(REGEX_FLAGS, set('dgimsuvy'))

    def test_kinds_marks_comments(self):
        """`kinds` 必须能把注释区标出来 —— `audit` 靠它跳过注释里的 `try {`。"""
        src = '/* 这种写法里的 catch 是死代码：\n   try { f(); } catch (e) { g(); }\n*/\ntry { a(); } catch (e) { b(); }\n'
        _, kinds = stripped_kinds(src)
        first = src.index('try {')
        self.assertEqual(kinds[first], 'comment', '注释里的 try{ 应标成 comment')
        second = src.rindex('try {')
        self.assertEqual(kinds[second], 'code', '代码里的 try{ 应标成 code')


class ApiMethodClassificationTest(SimpleTestCase):
    """`TNR_API` 的方法分类 —— 分类错了，死 catch 判据就错了。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.api = api_methods(read(API_PATH))

    def test_silent_get_set_is_pinned(self):
        """⚠ 静默读方法集**钉死**：新增一个静默方法必须是有意识的行为。

        静默读 = 非 2xx 时返回 `[]`。每多一个，就多一片「失败会被渲染成空」的地盘。
        """
        expected = {
            'getAdoptionApplications', 'getAdoptions', 'getBlacklist',
            'getBusinessSupervision', 'getCapture', 'getCaptures', 'getCheckins',
            'getDashboardStats', 'getDistricts', 'getEuthanasia', 'getHallListings',
            'getHospitalLedger', 'getHospitalPets', 'getInstitutions',
            'getMaterialSupervision', 'getMaterialTransactions', 'getMaterials',
            'getOperationLogs', 'getOwnerReturns', 'getPetArchive', 'getPetLifecycle',
            'getPets', 'getReleases', 'getShelterLedger', 'getTransfers',
            'getTreatments', 'getUsers',
        }
        actual = {n for n, k in self.api.items() if k == 'get-silent'}
        self.assertEqual(
            actual, expected,
            '静默读方法集变了。新增的：%s；消失的：%s。'
            '若确实要新增，请同时确认它的调用点都不需要「失败可见」。'
            % (sorted(actual - expected), sorted(expected - actual)))

    def test_get_is_silent_and_getdata_throws(self):
        """两条内核的语义必须相反 —— 这是整轮修复的地基。"""
        self.assertEqual(self.api.get('_get'), 'silent-primitive')
        self.assertEqual(self.api.get('getData'), 'throwing')

    def test_pure_helpers_are_not_api_reads(self):
        """`getPetStatusText` 之类是**纯函数**，不发起请求。

        它们曾被误判成「抛错调用」，把「半死」的判定带偏
        （`try { …getHospitalPets()…; TNR_API.getPetStatusText(x) } catch {}`
        看起来像「有抛错调用」，其实一个请求都没有）。
        """
        for name in ('getPetStatusText', 'getPetStatusBadge', 'petAttrText',
                     'getMaterialCategoryText'):
            self.assertEqual(self.api.get(name), 'pure', f'{name} 不该被当成请求方法')

    def test_every_strict_method_delegates_to_getdata(self):
        """`XxxStrict` 必须走 `getData`（走 `_get` 就又静默了）。"""
        st = strip_js(read(API_PATH))
        strict = [n for n in self.api if n.endswith('Strict')]
        self.assertTrue(strict, '一个严格读方法都没有，本轮修复丢了')
        for name in strict:
            with self.subTest(method=name):
                m = re.search(r'\basync\s+' + name + r'\s*\([^)]*\)\s*\{', st)
                self.assertIsNotNone(m, f'{name} 没找到定义')
                body = st[m.end() - 1:match_brace(st, st.index('{', m.start())) + 1]
                self.assertIn('this.getData(', body,
                              f'{name} 没有委托 getData —— 严格读必须走内核')
                self.assertNotIn('this._get(', body,
                                 f'{name} 走了 _get —— 那就不是严格读了')

    def test_every_strict_method_has_a_soft_twin(self):
        """`XxxStrict` 必须与 `Xxx` 一一对应（URL 与返回结构一致）。"""
        for name in [n for n in self.api if n.endswith('Strict')]:
            twin = name[:-len('Strict')]
            with self.subTest(method=name):
                self.assertIn(twin, self.api, f'{name} 没有对应的软版本 {twin}')


class DeadCatchIsZeroTest(SimpleTestCase):
    """核心判据：`try { 静默读 } catch { 呈现失败 }` 必须**一处都不剩**。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.api = api_methods(read(API_PATH))
        cls.rows = {}
        for name, p in PORTAL_FILES.items():
            cls.rows[name] = survey(read(p), cls.api)

    def test_no_dead_catch_anywhere(self):
        offenders = []
        for name, rows in self.rows.items():
            for r in rows:
                if classify(r) in ('死', '半死'):
                    offenders.append(
                        f'{name} L{r["line"]} {r["method"]} 静默读={" ".join(r["sil"])}')
        self.assertEqual(
            offenders, [],
            '这些地方在 catch 里呈现了失败，但 try 里全是静默读 —— 提示永远不会出现。\n'
            '改成对应的 XxxStrict 版本：\n  ' + '\n  '.join(offenders))

    def test_scan_is_not_vacuous(self):
        """正向对照：扫描器必须**真的看见了东西**，否则上面那条是空转。"""
        total = sum(len(rows) for rows in self.rows.values())
        self.assertGreaterEqual(total, 50, f'只扫到 {total} 处 try/catch，明显不对')
        kinds = {}
        for rows in self.rows.values():
            for r in rows:
                kinds[classify(r)] = kinds.get(classify(r), 0) + 1
        # 「活」（只有抛错调用）必须占多数 —— 写操作早就用 assertOk / res.success 处理了
        self.assertGreater(kinds.get('活', 0), 20, f'分类结果可疑：{kinds}')
        # 「静默吞」（有意降级）应当仍然存在 —— 说明判据没有把降级也误报成缺陷
        self.assertGreater(kinds.get('静默吞', 0), 0, f'有意降级被误报了：{kinds}')

    def test_planted_regression_is_detected(self):
        """**判据不空转**：把一处严格读改回软版本，清点器必须报出来。

        这条是整个套件里最重要的一条 —— 没有它，上面 `test_no_dead_catch_anywhere`
        可能因为「判据写错、永远返回空」而假绿。
        """
        gov = read(PORTAL_FILES['gov'])
        self.assertIn('TNR_API.getOperationLogsStrict()', gov)
        broken = gov.replace('TNR_API.getOperationLogsStrict()',
                             'TNR_API.getOperationLogs()')
        rows = survey(broken, self.api)
        dead = [r for r in rows if classify(r) in ('死', '半死')]
        self.assertTrue(dead, '把严格读改回软版本，清点器竟然没报 —— 判据是空转的')
        self.assertIn('getOperationLogs', dead[0]['sil'])

    def test_planted_second_regression_on_a_promise_all_site(self):
        """再植一处 `Promise.all` 站点（10 个并发读里的一个）—— 同样必须报。"""
        gov = read(PORTAL_FILES['gov'])
        self.assertIn('TNR_API.getTransfersStrict(),', gov)
        broken = gov.replace('TNR_API.getTransfersStrict(),',
                             'TNR_API.getTransfers(),', 1)
        rows = survey(broken, self.api)
        dead = [r for r in rows if classify(r) in ('死', '半死')]
        self.assertTrue(dead, 'Promise.all 里的静默读没被报出来')
        self.assertIn('getTransfers', dead[0]['sil'])


class SilentReadInventoryTest(SimpleTestCase):
    """把「哪些地方**有意**降级」也钉住 —— 避免以后有人把它们当漏网之鱼「修掉」。

    有意降级 = `try { 静默读 } catch { 只降级 / 只有注释 }`：项目明确接受
    「读接口失败降级成空比整页崩掉更可接受」（见 tnr-common.js 的说明）。
    """

    EXPECTED_DELIBERATE = {
        # 捕捉详情：接口异常时降级用列表数据
        ('shelter', 'showCaptureDetail'),
        # 捕捉编辑：取不到单据/机构时降级（机构取不到 → 允许改，服务端兜底）
        ('shelter', 'showCaptureEdit'),
        # 回收登记弹窗：取不到捕捉单时用默认值
        ('shelter', 'showRecoverModal'),
        # 医院端：失败时置空/降级，行内显示「未知」或「暂无」
        ('hospital', 'renderReceiveTab'),
        ('hospital', 'renderTreatmentForm'),
        ('hospital', 'render_material'),
        ('hospital', 'renderMaterialAdjustment'),
        # 领养人端：失败时只展示领养记录
        ('adopter', 'showLifecycle'),
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.api = api_methods(read(API_PATH))
        cls.deliberate = set()
        for name, p in PORTAL_FILES.items():
            for r in survey(read(p), cls.api):
                if classify(r) == '静默吞':
                    cls.deliberate.add((name, r['method']))

    def test_deliberate_degradations_are_still_present(self):
        """有意降级仍在 —— 若它们消失了，说明判据把它们也误当成缺陷修掉了。"""
        for item in self.EXPECTED_DELIBERATE:
            with self.subTest(site=item):
                self.assertIn(item, self.deliberate,
                              f'{item} 不在「有意降级」里了，判据可能被改坏了')
