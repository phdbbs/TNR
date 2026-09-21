"""查询参数契约（第二十四轮）。

同一个接口、同一个角色、完全合法的会话，只用**查询参数**去操纵结果集。
前几轮扫的是「**谁**能访问」（角色矩阵）和「**哪条记录**」（id 越权 /
存在性预言机），这一轮换第三个正交维度：**参数**。三类检查：

A. **类型契约** —— 非法值必须被拒绝（4xx）或被忽略（200），**不能 500**；
B. **可见范围** —— 范围类参数不得放宽可见集合（`scripts/param_probe.py` 的 B 类）；
C. **数量上限** —— 「合法但荒谬」的数量不得让服务端真的去干那么大的活。

## 为什么这一轮的「修法」特别容易做成假修

实测：同一类「非法参数值」会以**四种互不相同**的异常炸掉接口 ——

| 场景 | 异常 |
|---|---|
| `filter(<整型外键>=非数字)` | `builtins.ValueError` |
| `filter(<整型外键>=超大数)` | **`builtins.OverflowError`** |
| `filter(<日期字段>=非日期)` | **`django.core.exceptions.ValidationError`** |
| 日期字段上做算术（`end_date + 1天`） | **`builtins.OverflowError`**（同名不同源） |

所以「随手包一层 `except ValueError`」是**假修**。更要命的是第 2 行：
`int('9' * 24)` **本身不报错**（Python 整数无上限），是 SQLite 绑定参数时才溢出
—— 靠 `try: int(x) except ValueError` **永远拦不住**，校验必须发生在**进 ORM 之前**。

见 `UncaughtExceptionTypesTest`（把四种异常逐条钉住，作为判据的校准依据）
与 `ParseIntParamTest.test_python_int_itself_accepts_huge_number`。
"""
import ast
import inspect
import re
import textwrap
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.utils import timezone

from business import services
from business.models import Capture, Material, MaterialTransaction, OwnerReturn
from business.services import (
    MAX_CAPTURE_BATCH, adjust_stock, date_upper_exclusive,
    parse_date_param, parse_int_param,
)
from business.tests.base import (
    BusinessTestBase, make_capture, make_material,
)

CAPTURES = '/api/business/captures/'
CODES_PREVIEW = '/api/business/captures/codes-preview/'
OWNER_RETURNS = '/api/business/owner-returns/'
PURCHASE = '/api/business/materials/purchase/'
DISPATCH = '/api/business/materials/dispatch/'
ADJUSTMENT = '/api/business/materials/adjustment/'
TXN_LIST = '/api/business/materials/transactions/'
SHELTER_LEDGER = '/api/business/materials/shelter-ledger/'
SV_LEDGER = '/api/supervision/ledger/'

# 「大到足以证明没有上限、又小到验证脚本跑得完」的数量值。
# 不用 `'9' * 24`：那类值在**未修复**代码上会让服务端真的去循环 10^24 次，
# 连跑校准的脚本自己都会被 OOM 杀掉（本轮实测被 SIGTERM）。
LARGE_PROBE_COUNT = 100_000

# 投毒值。后四个是**日期**专用：原先的探针毒值表里一个日期格式的串都没有，
# 于是漏掉了 `?end_date=9999-12-31`（格式完全合法，却在 `date.max + 1天` 处
# 抛 `OverflowError`）。**毒值表本身的覆盖度也是判据的一部分。**
POISON = [
    'abc', '襄城区', '9' * 24, '-1', '1.5', '',
    '9999-12-31', '0000-01-01', '2026-02-30', '2026-13-01',
]


class ParamContractMixin:
    """判据：非法参数值可以 4xx、可以 200（忽略），但**不能 5xx**。

    `param_actor` 是「这批检查用哪个账号登录」，取 `setUpTestData` 里属性的
    **名字**（如 `'gov_city'`）。子类按接口的 `role_required` 覆盖它 ——
    不登录会得到 401，而 401 会让「不能 5xx」的断言**假通过**
    （401 当然不是 5xx），这是最容易被忽略的一种空转。
    """
    param_actor = 'gov_city'

    def setUp(self):
        super().setUp()
        name = getattr(self, 'param_actor', None)
        actor = getattr(self, name, None) if name else None
        if actor is not None:
            self.login_as(actor)

    def get_param(self, url, name, value):
        return self.client.get(url, {name: value})

    def assert_no_5xx(self, url, name, values=None, label=''):
        """逐个投毒，任何一个打成 5xx 都算缺陷。

        5xx 不只是「难看」：`DEBUG=True` 下它会把完整栈、设置、局部变量
        吐给请求方；它还把「用户输入错误」误分类成「服务端故障」，
        前端无法区分、无法提示。
        """
        for v in (POISON if values is None else values):
            resp = self.get_param(url, name, v)
            self.assertNotIn(
                resp.status_code, (401, 403),
                f'{label or url} 参数 {name} 的检查没登录（HTTP {resp.status_code}）'
                f'—— 断言会假通过，请给 param_actor 指定可访问该接口的账号')
            self.assertLess(
                resp.status_code, 500,
                f'{label or url} 参数 {name}={v!r} → HTTP {resp.status_code}'
                f'（未捕获异常）\n{resp.content[:400]!r}')

    def assert_rejected(self, url, name, values, message):
        """非法值必须被**明确拒绝**（400 + 我们的文案），而不是静默忽略。

        静默忽略筛选条件 = 用户设了筛选却看到全量，属于最难发现的静默失败。
        """
        for v in values:
            resp = self.get_param(url, name, v)
            self.assertLess(resp.status_code, 500,
                            f'{url} 参数 {name}={v!r} 打成 {resp.status_code}')
            self.assertEqual(resp.status_code, 400,
                             f'{url} 参数 {name}={v!r} 应被拒绝，实际 '
                             f'{resp.status_code}：{resp.content[:200]!r}')
            self.assertIn(message, resp.json().get('message', ''),
                          f'{url} 参数 {name}={v!r} 的文案不对：'
                          f'{resp.json().get("message")!r}')


# ============================================================
# 1. 解析器本身
# ============================================================
class ParseIntParamTest(SimpleTestCase):
    """`parse_int_param` 的三态语义 + 边界。"""

    def test_python_int_itself_accepts_huge_number(self):
        """⚠ 本轮最关键的一条事实。

        `int('9' * 24)` **不报错** —— Python 整数无上限。所以
        `try: int(x) except ValueError` 这种写法对「超大数」**永远拦不住**，
        异常要等 SQLite 绑定参数时才抛（`OverflowError`）。
        结论：范围校验必须在**进 ORM 之前**做完，不能指望 `int()` 兜底。
        """
        value = int('9' * 24)
        self.assertGreater(value, 2 ** 63 - 1)
        # 同一个值交给 SQLite 才会炸（见 UncaughtExceptionTypesTest）
        self.assertEqual(parse_int_param('9' * 24, '物料')[1],
                         '物料超出有效范围')

    def test_non_numeric_rejected(self):
        self.assertEqual(parse_int_param('abc', '物料'), (None, '物料必须是数字'))

    def test_huge_number_rejected_before_orm(self):
        """⚠ 挡住它的是**长度检查**（`len(text) > 19`），不是 `maximum` 分支。

        `'9' * 24` 是 24 位，`len(text) > 19` 先命中就返回了 —— 所以这条用例
        **不能**用来证明「上限判据起作用」。真正只由 `maximum` 拦住的是 19 位
        的超大数（见 `test_over_sqlite_max_rejected`：`str(2 ** 63)`）。
        两条一起看才覆盖完整：长度挡超长串、`maximum` 挡「位数合法但越界」。
        """
        value, err = parse_int_param('9' * 24, '物料')
        self.assertIsNone(value)
        self.assertEqual(err, '物料超出有效范围')

    def test_huge_number_beyond_python_digit_limit(self):
        """超过 Python 3.11+ 的 4300 位转换上限也不能炸（先按位数挡）。"""
        value, err = parse_int_param('9' * 5000, '物料')
        self.assertIsNone(value)
        self.assertEqual(err, '物料超出有效范围')

    def test_sqlite_max_is_accepted(self):
        self.assertEqual(parse_int_param(str(2 ** 63 - 1), '物料'),
                         (2 ** 63 - 1, None))

    def test_over_sqlite_max_rejected(self):
        """⚠ 这条才是**唯一**能钉住 `maximum` 分支的用例。

        `str(2 ** 63)` = `'9223372036854775808'`，**19 位**、刚好通过长度检查，
        只能靠 `value > maximum` 拦下。删掉 `maximum` 分支它必红
        （变异 M31 的 `expect` 就指这里）。
        """
        value, err = parse_int_param(str(2 ** 63), '物料')
        self.assertIsNone(value)
        self.assertEqual(err, '物料超出有效范围')

    def test_negative_rejected(self):
        """负数在 `_INT_RE`（`^[0-9]+$`）这一关就被挡掉，报「必须是数字」。

        这样比「超出有效范围」更准确：`-1` 根本不是这个参数域的合法写法。
        """
        self.assertEqual(parse_int_param('-1', '物料'),
                         (None, '物料必须是数字'))

    def test_zero_rejected_when_minimum_is_one(self):
        self.assertEqual(parse_int_param('0', '物料'),
                         (None, '物料超出有效范围'))

    def test_zero_allowed_when_minimum_is_zero(self):
        self.assertEqual(parse_int_param('0', '采购数量', minimum=0), (0, None))

    def test_missing_and_empty_mean_absent(self):
        """缺省与空串都表示「没传这个筛选条件」，而不是「传了个非法值」。"""
        self.assertEqual(parse_int_param(None, '物料'), (None, None))
        self.assertEqual(parse_int_param('', '物料'), (None, None))
        self.assertEqual(parse_int_param('   ', '物料'), (None, None))

    def test_fullwidth_digits_rejected(self):
        """`int('１２')` 在 Python 里**是合法的**（= 12）—— 口径比业务预期宽，
        所以解析器用正则而不是裸 `int()`。"""
        self.assertEqual(int('１２'), 12)
        self.assertEqual(parse_int_param('１２', '物料'),
                         (None, '物料必须是数字'))

    def test_underscore_digits_rejected(self):
        """同理 `int('1_0')` == 10。"""
        self.assertEqual(int('1_0'), 10)
        self.assertEqual(parse_int_param('1_0', '物料'),
                         (None, '物料必须是数字'))

    def test_decimal_and_plus_rejected(self):
        self.assertEqual(parse_int_param('1.5', '物料')[1], '物料必须是数字')
        self.assertEqual(parse_int_param('+5', '物料')[1], '物料必须是数字')

    def test_maximum_bound_reported(self):
        """业务上限要报出来（用户得知道能填多少）。"""
        self.assertEqual(parse_int_param('101', '数量', minimum=0, maximum=100),
                         (None, '数量不能超过 100'))
        self.assertEqual(parse_int_param('100', '数量', minimum=0, maximum=100),
                         (100, None))


class ParseDateParamTest(SimpleTestCase):
    def test_valid_date(self):
        self.assertEqual(parse_date_param('2026-09-19', '开始日期'),
                         (date(2026, 9, 19), None))

    def test_returns_date_object_not_string(self):
        """返回的必须是 `date` 而不是 `str` —— 把字符串直接塞进
        `__date__gte` 会让 Django 抛 `ValidationError`（500）。"""
        value, _ = parse_date_param('2026-09-19', '开始日期')
        self.assertIsInstance(value, date)

    def test_iso_datetime_prefix_accepted(self):
        self.assertEqual(parse_date_param('2026-09-19T10:30:00', '开始日期'),
                         (date(2026, 9, 19), None))

    def test_unpadded_accepted(self):
        """`strptime` 本来就接受不补零，收紧了只会让老书签无谓地撞 400。"""
        self.assertEqual(parse_date_param('2026-9-1', '开始日期'),
                         (date(2026, 9, 1), None))

    def test_non_date_rejected(self):
        self.assertEqual(parse_date_param('abc', '开始日期'),
                         (None, '开始日期格式应为 YYYY-MM-DD'))

    def test_impossible_day_rejected(self):
        """`2026-02-30` 格式对、日期不存在 —— 必须由 `strptime` 挡掉。"""
        self.assertEqual(parse_date_param('2026-02-30', '开始日期'),
                         (None, '开始日期不是有效日期'))

    def test_year_zero_rejected(self):
        self.assertEqual(parse_date_param('0000-01-01', '开始日期'),
                         (None, '开始日期不是有效日期'))

    def test_missing_and_empty_mean_absent(self):
        self.assertEqual(parse_date_param(None, '开始日期'), (None, None))
        self.assertEqual(parse_date_param('', '开始日期'), (None, None))


class DateUpperExclusiveTest(SimpleTestCase):
    """第四种异常类型的专用判据：`date.max` 加一天。

    上界必须是**带时区的 datetime**：直接返回 `date` 时 Django 比较
    `DateTimeField` 会抛 `RuntimeWarning: ... received a naive datetime`
    —— 结果正确（Django 按 `TIME_ZONE` 解释），但每个日期筛选请求都刷一条，
    把真异常淹掉。这条断言就是那次修复的护栏。
    """

    def test_normal_date_uses_open_interval(self):
        upper, exclusive = date_upper_exclusive(date(2026, 9, 19))
        self.assertTrue(exclusive)
        self.assertTrue(timezone.is_aware(upper),
                        '上界必须带时区，否则 DateTimeField 会抛 naive datetime 警告')
        self.assertEqual(timezone.localtime(upper).replace(tzinfo=None),
                         datetime(2026, 9, 20, 0, 0))

    def test_date_max_does_not_raise(self):
        """`date.max + timedelta(days=1)` 会 `OverflowError`
        —— 与 SQLite 那个 `OverflowError` 同名不同源，`except ValueError` 兜不住。"""
        with self.assertRaises(OverflowError):
            date.max + timedelta(days=1)
        upper, exclusive = date_upper_exclusive(date.max)
        self.assertFalse(exclusive)
        self.assertTrue(timezone.is_aware(upper))
        self.assertEqual(timezone.localtime(upper).replace(tzinfo=None),
                         datetime(9999, 12, 31, 0, 0))

    def test_aware_day_start_is_aware(self):
        """下界（`__gte`）走同一个 helper，也必须带时区。"""
        bound = services.aware_day_start(date(2026, 9, 19))
        self.assertTrue(timezone.is_aware(bound))
        self.assertEqual(timezone.localtime(bound).replace(tzinfo=None),
                         datetime(2026, 9, 19, 0, 0))


# ============================================================
# 2. 判据的校准依据：不设防时到底会抛什么
# ============================================================
class UncaughtExceptionTypesTest(BusinessTestBase):
    """逐条钉住「裸用非法值」时的异常类型。

    这个类不测生产代码的**行为**，而是固定**事实** ——
    它是本轮所有「不能 500」断言的校准依据：证明「包一层 `except ValueError`」
    只覆盖了四种里的第一种。
    """

    def test_int_fk_with_non_numeric_raises_value_error(self):
        with self.assertRaises(ValueError):
            list(Capture.objects.filter(district_id='abc'))

    def test_int_fk_with_huge_number_raises_overflow_error(self):
        with self.assertRaises(OverflowError):
            list(Capture.objects.filter(district_id=int('9' * 24)))

    def test_date_field_with_non_date_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            list(OwnerReturn.objects.filter(return_time__date__gte='abc'))

    def test_date_arithmetic_raises_overflow_error(self):
        with self.assertRaises(OverflowError):
            date.max + timedelta(days=1)

    def test_value_error_guard_covers_only_one_of_four(self):
        """把这四种异常放在一起看：`except ValueError` 只命中第 1 种。"""
        caught = []
        for exc in (ValueError, OverflowError, ValidationError):
            caught.append(issubclass(exc, ValueError))
        self.assertEqual(caught, [True, False, False])


# ============================================================
# 3. captures 的 district 参数
# ============================================================
class CaptureDistrictParamTest(ParamContractMixin, BusinessTestBase):
    """`?district=` 既支持 id 也支持中文名。

    原实现 `Q(district__name__icontains=d) | Q(district_id=d)`：
    `Q` 的**两半都会被求值**，于是传中文名时 `district_id='襄城区'` 直接
    `ValueError` 打成 500 —— 「按区县名筛选」这条分支**从来没成功过**。
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cap_a = make_capture(district=cls.district_a, shelter=cls.shelter_a,
                                 pet_codes=['PA1'])
        cls.cap_b = make_capture(district=cls.district_b, shelter=cls.shelter_b,
                                 pet_codes=['PB1'])

    def ids(self, **params):
        self.login_as(self.gov_city)
        resp = self.client.get(CAPTURES, params)
        body = self.ok(resp)
        return {row['id'] for row in body['data']}

    def test_filter_by_district_name_works(self):
        """正向对照：按中文区县名筛选**必须**能筛出来。

        这条用例在修复前是红的（接口 500），所以它不是「顺手加的」——
        它是「这条分支到底有没有真的工作过」的证据。
        """
        self.assertEqual(self.ids(district=self.district_a.name), {self.cap_a.id})
        self.assertEqual(self.ids(district=self.district_b.name), {self.cap_b.id})

    def test_filter_by_district_id_works(self):
        self.assertEqual(self.ids(district=self.district_a.id), {self.cap_a.id})
        self.assertEqual(self.ids(district=self.district_b.id), {self.cap_b.id})

    def test_partial_name_still_matches(self):
        self.assertIn(self.cap_a.id, self.ids(district='甲'))

    def test_poison_values_never_500(self):
        """非数字不是「非法输入」而是「按名称查」，所以这里只要求不 500。"""
        self.assert_no_5xx(CAPTURES, 'district')

    def test_poison_values_do_not_widen_result(self):
        """B 类：投毒不得让结果集**变大**（筛选只能收窄）。"""
        self.login_as(self.gov_city)
        base = self.ok(self.client.get(CAPTURES))['data']
        base_ids = {row['id'] for row in base}
        for v in POISON:
            body = self.ok(self.client.get(CAPTURES, {'district': v}))['data']
            self.assertTrue({row['id'] for row in body} <= base_ids,
                            f'district={v!r} 让结果集变大了')


# ============================================================
# 4. owner-returns 的日期参数
# ============================================================
class OwnerReturnDateParamTest(ParamContractMixin, BusinessTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from business.tests.base import make_pet
        cls.pet_recent = make_pet(district=cls.district_a, shelter=cls.shelter_a)
        cls.pet_old = make_pet(district=cls.district_a, shelter=cls.shelter_a)
        cls.recent = OwnerReturn.objects.create(
            pet=cls.pet_recent, pet_code=cls.pet_recent.code,
            owner_name='近的', reason='测试', district=cls.district_a,
            return_time=timezone.now(), ledger_no='OR-PARAM-1')
        cls.old = OwnerReturn.objects.create(
            pet=cls.pet_old, pet_code=cls.pet_old.code,
            owner_name='远的', reason='测试', district=cls.district_a,
            return_time=timezone.now() - timedelta(days=30), ledger_no='OR-PARAM-2')

    def ids(self, **params):
        self.login_as(self.gov_city)
        body = self.ok(self.client.get(OWNER_RETURNS, params))
        return {row['id'] for row in body['data']}

    def test_valid_range_still_filters(self):
        """正向对照：合法日期范围必须真的筛掉范围外的记录。

        ⚠ 别把 `end_date=今天` 当成「只有今天的记录」—— 它的语义是
        「**截至**今天」，30 天前的记录照样在范围内。夹具要拉开 30 天，
        否则这条用例区分不出「筛选生效」与「筛选被忽略」。
        """
        today = timezone.localdate()
        before_both = (today - timedelta(days=45)).isoformat()   # 早于两条记录
        mid = (today - timedelta(days=15)).isoformat()           # 落在两条之间
        self.assertEqual(self.ids(start_date=today.isoformat()), {self.recent.id})
        self.assertEqual(self.ids(start_date=before_both),
                         {self.recent.id, self.old.id})
        self.assertEqual(self.ids(end_date=mid), {self.old.id})
        self.assertEqual(self.ids(end_date=today.isoformat()),
                         {self.recent.id, self.old.id})
        self.assertEqual(self.ids(start_date=today.isoformat(),
                                  end_date=today.isoformat()), {self.recent.id})

    def test_no_params_returns_both(self):
        self.assertEqual(self.ids(), {self.recent.id, self.old.id})

    def test_poison_dates_rejected_with_400(self):
        for name in ('start_date', 'end_date'):
            self.assert_rejected(OWNER_RETURNS, name,
                                 ['abc', '襄城区', '9' * 24, '-1', '1.5',
                                  '2026-02-30', '0000-01-01', '2026-13-01'],
                                 '日期')

    def test_poison_dates_never_500(self):
        for name in ('start_date', 'end_date'):
            self.assert_no_5xx(OWNER_RETURNS, name)

    def test_date_max_accepted_here(self):
        """`owner_return_list` 用 `__date__lte` 直接比较，不做算术，
        所以 `9999-12-31` 是合法输入（真正会炸的是 `ledger_center`）。"""
        self.login_as(self.gov_city)
        resp = self.client.get(OWNER_RETURNS, {'end_date': '9999-12-31'})
        self.assertEqual(resp.status_code, 200, resp.content)


# ============================================================
# 5. materials 的 material_id 参数
# ============================================================
class MaterialIdParamTest(ParamContractMixin, BusinessTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.mat_a = make_material(district=cls.district_a, name='参数契约物料A')
        cls.mat_a2 = make_material(district=cls.district_a, name='参数契约物料B')
        cls.txn_a = MaterialTransaction.objects.create(
            type='purchase', material=cls.mat_a, material_name=cls.mat_a.name,
            quantity=10, date=timezone.localdate(), district=cls.district_a,
            ledger_no='MT-PARAM-1')
        cls.txn_a2 = MaterialTransaction.objects.create(
            type='purchase', material=cls.mat_a2, material_name=cls.mat_a2.name,
            quantity=10, date=timezone.localdate(), district=cls.district_a,
            ledger_no='MT-PARAM-2')

    def ids(self, url, **params):
        self.login_as(self.gov_city)
        body = self.ok(self.client.get(url, params))
        return {row['id'] for row in body['data']}

    def test_valid_material_id_still_filters(self):
        """正向对照：合法 id 必须真的筛掉别的物料。"""
        for url in (TXN_LIST, SHELTER_LEDGER):
            self.assertEqual(self.ids(url, material_id=self.mat_a.id),
                             {self.txn_a.id}, url)

    def test_no_param_returns_all(self):
        for url in (TXN_LIST, SHELTER_LEDGER):
            self.assertEqual(self.ids(url),
                             {self.txn_a.id, self.txn_a2.id}, url)

    def test_poison_material_id_rejected(self):
        for url in (TXN_LIST, SHELTER_LEDGER):
            self.assert_rejected(url, 'material_id',
                                 ['abc', '襄城区', '9' * 24, '-1', '1.5'],
                                 '物料')

    def test_poison_material_id_never_500(self):
        for url in (TXN_LIST, SHELTER_LEDGER):
            self.assert_no_5xx(url, 'material_id')


# ============================================================
# 6. 政府端台账的 institution_id / 日期
# ============================================================
class LedgerCenterParamTest(ParamContractMixin, BusinessTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cap_a = make_capture(district=cls.district_a, shelter=cls.shelter_a,
                                 pet_codes=['LA1'])
        cls.cap_b = make_capture(district=cls.district_b, shelter=cls.shelter_b,
                                 pet_codes=['LB1'])

    def records(self, **params):
        self.login_as(self.gov_city)
        resp = self.client.get(SV_LEDGER, params)
        self.assertLess(resp.status_code, 500, resp.content)
        body = self.ok(resp)
        return body['data']['records']

    def test_institution_id_still_narrows(self):
        """正向对照：合法机构 id 必须真的收窄结果。"""
        rows = self.records(business_type='capture',
                            institution_id=self.shelter_a.id)
        self.assertEqual({r['id'] for r in rows}, {self.cap_a.id})

    def test_poison_institution_id_rejected(self):
        """`institution_id` 被**七处**台账分支共用 —— 一个非法值原先能在
        任意一处把接口打成 500，所以判据必须只解析一次、前置到所有分支之前。"""
        self.assert_rejected(SV_LEDGER, 'institution_id',
                             ['abc', '襄城区', '9' * 24, '-1', '1.5'], '机构')

    def test_poison_institution_id_never_500(self):
        self.assert_no_5xx(SV_LEDGER, 'institution_id')

    def test_poison_dates_rejected(self):
        for name in ('start_date', 'end_date'):
            self.assert_rejected(SV_LEDGER, name,
                                 ['abc', '9' * 24, '2026-02-30', '0000-01-01'],
                                 '日期')

    def test_end_date_date_max_does_not_500(self):
        """**第四种异常类型**的端到端判据。

        `_date_filter` 做「含当天」换算时要 `end_date + timedelta(days=1)`，
        `end_date = date.max`（9999-12-31）时这一步抛 `OverflowError`。
        修复前 `?end_date=9999-12-31` → 500；现在退回闭区间上界，语义等价。
        """
        self.login_as(self.gov_city)
        resp = self.client.get(SV_LEDGER, {'end_date': '9999-12-31',
                                           'business_type': 'capture'})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_start_date_date_max_does_not_500(self):
        self.login_as(self.gov_city)
        resp = self.client.get(SV_LEDGER, {'start_date': '9999-12-31',
                                           'business_type': 'capture'})
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_end_date_includes_that_day(self):
        """正向对照：开区间换算的**业务语义**不能被修 bug 改掉 ——
        结束日期必须含当天（当天非零点的记录不能被排除）。"""
        today = timezone.localdate().isoformat()
        rows = self.records(business_type='capture', start_date=today,
                            end_date=today)
        self.assertEqual({r['id'] for r in rows}, {self.cap_a.id, self.cap_b.id})

    def test_invalid_date_ignored_is_not_the_contract(self):
        """日期非法时**明确 400**，而不是像原实现那样 `except: pass` 静默忽略
        —— 静默忽略筛选 = 用户设了筛选却看到全量。"""
        self.login_as(self.gov_city)
        resp = self.client.get(SV_LEDGER, {'start_date': 'abc'})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_date_filter_emits_no_naive_datetime_warning(self):
        """日期筛选不得把裸 `date` 丢给 `DateTimeField`。

        改前 `?start_date=...&end_date=...` 的**每个**请求都会让 Django 抛
        `RuntimeWarning: DateTimeField ... received a naive datetime` ——
        筛选结果是对的（Django 按 `TIME_ZONE` 解释），但生产日志被刷满噪音，
        真异常会被淹掉。修法是上下界都过 `aware_day_start()`。
        """
        today = timezone.localdate().isoformat()
        self.login_as(self.gov_city)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            resp = self.client.get(SV_LEDGER, {'business_type': 'capture',
                                               'start_date': today,
                                               'end_date': today})
        self.assertEqual(resp.status_code, 200, resp.content)
        naive = [str(w.message) for w in caught if 'naive datetime' in str(w.message)]
        self.assertEqual(naive, [],
                         f'日期筛选产生了 naive datetime 警告：{naive}')


# ============================================================
# 7. codes-preview 的数量上限（C 类：资源耗尽）
# ============================================================
class PetCodesPreviewBoundTest(ParamContractMixin, BusinessTestBase):
    """`?count=` 原先只判 `count <= 0`，**没有上限**。

    `generate_pet_codes()` 是纯 `for i in range(count)` 循环 ——
    实测 20 万条约 23 ms，线性外推 `?count=999999999` ≈ 110 秒 CPU
    + 数十 GB 内存，进程被拖死。**这类问题探针的 A 类扫不出来**：
    接口最终仍返回 200，只是在返回之前已经把服务端吃光了。
    """

    param_actor = 'shelter_user_a'   # 该接口只对捕捉点/政府开放

    def test_valid_count_returns_codes(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.client.get(CODES_PREVIEW, {'count': 3}))
        self.assertEqual(len(body['data']), 3)

    def test_zero_and_missing_rejected(self):
        self.login_as(self.shelter_user_a)
        for params in ({'count': 0}, {}):
            self.expect_fail(self.client.get(CODES_PREVIEW, params),
                             message='数量必须大于0')

    def test_count_over_cap_rejected(self):
        self.login_as(self.shelter_user_a)
        resp = self.client.get(CODES_PREVIEW, {'count': MAX_CAPTURE_BATCH + 1})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn(str(MAX_CAPTURE_BATCH), resp.json()['message'])

    def test_absurd_count_returns_no_codes_at_all(self):
        """核心判据：`count` 荒谬时响应体必须**小** —— 证明服务端没有真的
        去生成那么多编号（不是「生成完再截断」）。

        ⚠ 这里**故意只用到 10 万**，不用 `'9' * 24`。
        实测在**未修复**的代码上，`?count=999999999999999999999999` 会让
        `generate_pet_codes()` 真的去 `range(10 ** 24)` —— 进程被 OOM 杀掉，
        **连跑验证的脚本自己都活不下来**（本轮做修复前校准时就这样被 SIGTERM 了）。
        10 万已经是上限（100）的 1000 倍，足够证明「没有上限」这个结论；
        而「超大数必须被解析器挡住」由
        `ParseIntParamTest.test_over_sqlite_max_rejected` 覆盖
        （⚠ 不是 `test_huge_number_rejected_before_orm` —— 那条用的 `'9' * 24`
        是被**长度检查**挡掉的，与 `maximum` 分支无关，见其 docstring）。
        """
        self.login_as(self.shelter_user_a)
        resp = self.client.get(CODES_PREVIEW, {'count': LARGE_PROBE_COUNT})
        self.assertEqual(resp.status_code, 400,
                         f'count={LARGE_PROBE_COUNT} 应被拒绝，实际 {resp.status_code}')
        self.assertLess(len(resp.content), 500,
                        f'响应体有 {len(resp.content)} 字节，说明编号真的被生成了')

    def test_frontend_input_max_matches_backend_cap(self):
        """捕捉表单的数量输入框 `max` 必须**等于** `MAX_CAPTURE_BATCH`。

        这是 5.14「前端收窄 / 服务端校验」这对孪生判据的**数值一致性**：
        服务端已经会拒 `>100`（上一条用例），但如果前端 `max` 写成别的数，
        就会出现「界面能填 200、提交被拒 400」—— 用户一改就撞错。
        反向（界面收窄到 50 而服务端收 100）则会让用户以为上限是 50。
        """
        src = (ROOT / 'templates' / 'portal' / 'shelter' / 'portal.html').read_text(
            encoding='utf-8')
        m = re.search(r'id="ca_petCount"[^>]*?\bmax="(\d+)"', src)
        self.assertIsNotNone(m, '找不到捕捉表单的数量输入框 ca_petCount 或其 max')
        self.assertEqual(
            int(m.group(1)), MAX_CAPTURE_BATCH,
            f'前端 max={m.group(1)} 与服务端 MAX_CAPTURE_BATCH={MAX_CAPTURE_BATCH} 不一致')

    def test_poison_count_never_500(self):
        """`count` 的毒值表**故意不含超大数** —— 理由同上：
        那类值在未修复代码上会把进程吃光，验证脚本无法存活。
        超大数的拒绝由解析器单测覆盖。"""
        self.assert_no_5xx(CODES_PREVIEW, 'count',
                           ['abc', '襄城区', '-1', '1.5', ''])

    def test_cap_is_shared_with_checkin_create(self):
        """孪生接口契约：预览与真实建单**必须守同一个上限**。

        只卡一边就是漂移 —— 预览能生成 10 万条、提交却被拒（或反过来）。
        这里从两个方向各打一次边界，证明两边认的是同一个数。
        """
        self.assertEqual(MAX_CAPTURE_BATCH, 100)
        self.login_as(self.shelter_user_a)
        # 边界内：预览给编号
        body = self.ok(self.client.get(CODES_PREVIEW,
                                       {'count': MAX_CAPTURE_BATCH}))
        self.assertEqual(len(body['data']), MAX_CAPTURE_BATCH)
        # 边界外：两边都拒，且都提到这个数
        self.assertEqual(
            self.client.get(CODES_PREVIEW,
                            {'count': MAX_CAPTURE_BATCH + 1}).status_code, 400)
        resp = self.post_json(f'{CAPTURES}create/', {
            'shelter_id': self.shelter_a.id,
            'pet_count': MAX_CAPTURE_BATCH + 1,
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn(str(MAX_CAPTURE_BATCH), resp.json()['message'])

    def test_checkin_create_huge_count_rejected(self):
        self.login_as(self.shelter_user_a)
        resp = self.post_json(f'{CAPTURES}create/', {
            'shelter_id': self.shelter_a.id, 'pet_count': '9' * 24,
        })
        self.assertEqual(resp.status_code, 400, resp.content)


# ============================================================
# 8. 请求体侧：quantity 的孪生漂移 + 孤儿记录
# ============================================================
class MaterialQuantityBodyTest(ParamContractMixin, BusinessTestBase):
    """`purchase_create` / `dispatch_create` 的 `int(data.get('quantity'))`
    原先**没有** try/except，而同族的 `stock_adjustment` **有** —— 孪生漂移。
    """

    def _fresh_material(self, stock=50):
        """每个用例自己造物料。

        不要放 `setUpTestData`：那是**类级共享对象**，`adjust_stock` 会直接改
        它的 `shelter_stock` 内存值；即使事务回滚了 DB，下一个用例读到的仍是
        上一个用例改过的数 —— 用例执行顺序一变结论就飘。
        """
        return make_material(district=self.district_a, name='请求体契约物料',
                             shelter_stock=stock)

    def test_purchase_non_numeric_quantity_rejected(self):
        self.login_as(self.shelter_user_a)
        resp = self.post_json(PURCHASE, {
            'material_id': self._fresh_material().id, 'quantity': 'abc',
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('采购数量', resp.json()['message'])

    def test_purchase_huge_quantity_rejected(self):
        self.login_as(self.shelter_user_a)
        resp = self.post_json(PURCHASE, {
            'material_id': self._fresh_material().id, 'quantity': '9' * 24,
        })
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_purchase_zero_quantity_creates_no_orphan_material(self):
        """**孤儿记录**判据。

        原实现把 `Material.objects.create()` 排在数量校验**之前**，于是
        `quantity=0` 会先建出一条物料、紧接着 `return json_fail(...)` ——
        报错了，但**孤儿物料已经落库**（没有任何流水，台账里凭空多一条物料）。
        多表写入必须「先全校验 → 再落库」。
        """
        self.login_as(self.shelter_user_a)
        name = '孤儿物料-数量为零'
        self.assertEqual(Material.objects.filter(name=name).count(), 0)
        resp = self.post_json(PURCHASE, {
            'name': name, 'category': 'vaccine', 'quantity': 0,
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(
            Material.objects.filter(name=name).count(), 0,
            '数量非法却建出了物料（落库早于校验）')

    def test_purchase_non_numeric_quantity_creates_no_orphan_material(self):
        self.login_as(self.shelter_user_a)
        name = '孤儿物料-数量非数字'
        resp = self.post_json(PURCHASE, {
            'name': name, 'category': 'vaccine', 'quantity': 'abc',
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Material.objects.filter(name=name).count(), 0,
                         '数量非法却建出了物料')

    def test_purchase_huge_quantity_creates_no_orphan_material(self):
        """超大数同样不能留下孤儿物料（`int('9'*24)` 本身不报错，
        原实现会先建物料、再在别处炸掉）。"""
        self.login_as(self.shelter_user_a)
        name = '孤儿物料-数量超大'
        resp = self.post_json(PURCHASE, {
            'name': name, 'category': 'vaccine', 'quantity': '9' * 24,
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Material.objects.filter(name=name).count(), 0)

    def test_purchase_valid_quantity_still_creates_material_and_txn(self):
        """正向对照：合法请求必须照常建物料 + 建流水 + 加库存。"""
        self.login_as(self.shelter_user_a)
        name = '正常采购物料'
        body = self.ok(self.post_json(PURCHASE, {
            'name': name, 'category': 'vaccine', 'quantity': 7,
        }))
        material = Material.objects.get(name=name)
        self.assertEqual(material.district_id, self.district_a.id)
        self.assertEqual(material.shelter_stock, 7)
        self.assertEqual(
            MaterialTransaction.objects.filter(material=material,
                                               type='purchase').count(), 1)
        self.assertTrue(body['data']['id'])

    def test_dispatch_non_numeric_quantity_rejected(self):
        self.login_as(self.shelter_user_a)
        resp = self.post_json(DISPATCH, {
            'material_id': self._fresh_material().id,
            'hospital_id': self.hospital_a.id, 'quantity': 'abc',
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('下发数量', resp.json()['message'])

    def test_dispatch_huge_quantity_rejected(self):
        self.login_as(self.shelter_user_a)
        resp = self.post_json(DISPATCH, {
            'material_id': self._fresh_material().id,
            'hospital_id': self.hospital_a.id, 'quantity': '9' * 24,
        })
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_dispatch_valid_quantity_still_works(self):
        self.login_as(self.shelter_user_a)
        mat = self._fresh_material()
        body = self.ok(self.post_json(DISPATCH, {
            'material_id': mat.id, 'hospital_id': self.hospital_a.id,
            'quantity': 5,
        }))
        self.assertTrue(body['data']['id'])
        mat.refresh_from_db()
        self.assertEqual(mat.shelter_stock, 45)

    def test_adjustment_non_numeric_quantity_rejected(self):
        """三个「数量」参数口径一致：异动也走共用解析器。

        （异动接口只对医院/政府开放，捕捉点不在其列，所以这里用 gov_city。）
        """
        self.login_as(self.gov_city)
        resp = self.post_json(ADJUSTMENT, {
            'material_id': self._fresh_material().id, 'quantity': 'abc',
        })
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('异动数量', resp.json()['message'])

    def test_adjustment_valid_quantity_still_works(self):
        self.login_as(self.gov_city)
        mat = self._fresh_material()
        self.ok(self.post_json(ADJUSTMENT, {
            'material_id': mat.id, 'quantity': 4, 'reason': '盘点',
        }))
        mat.refresh_from_db()
        self.assertEqual(mat.shelter_stock, 46)


class AdjustStockAtomicityTest(BusinessTestBase):
    """共用写库函数的「判据晚于写库」问题。

    `adjust_stock()` 原先先 `MaterialTransaction.objects.create()` 再判库存，
    库存不足时抛 `ValueError` —— 调用方看到 400，但那条流水**已经落库**，
    会实实在在出现在捕捉点台账里（数量对不上，且全程没有报错）。
    这与 `views_treatment` 里「先建诊疗记录再校验库存」是同一个反模式。
    """

    def test_insufficient_stock_leaves_no_orphan_transaction(self):
        material = make_material(district=self.district_a,
                                 name='原子性物料', shelter_stock=5)
        with self.assertRaises(ValueError):
            adjust_stock(material, None, 50, 'dispatch')
        self.assertEqual(
            MaterialTransaction.objects.filter(material=material).count(), 0,
            '库存不足却留下了流水（判据晚于写库）')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 5, '库存被改动了')

    def test_sufficient_stock_still_writes(self):
        """正向对照：够库存时必须照常写流水 + 扣库存。"""
        material = make_material(district=self.district_a,
                                 name='原子性物料2', shelter_stock=5)
        txn = adjust_stock(material, None, 3, 'dispatch')
        self.assertEqual(MaterialTransaction.objects.filter(
            material=material).count(), 1)
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 2)
        self.assertEqual(txn.type, 'dispatch')

    def test_purchase_is_not_blocked_by_stock_check(self):
        """正向对照：`purchase` 是加库存，不该被库存判据拦住。"""
        material = make_material(district=self.district_a,
                                 name='原子性物料3', shelter_stock=0)
        adjust_stock(material, None, 9, 'purchase')
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 9)


# ============================================================
# 9. 源码级契约：不许再退回裸解析
# ============================================================
PARAM_PARSER_HELPERS = {'parse_int_param', 'parse_date_param'}


def uses_shared_param_parser(view):
    """视图源码里是否**真的**调用了共用参数解析器。返回 ``(bool, 命中原因)``。

    用 **AST** 而不是字符串匹配 —— 注释不在语法树里、字符串是 ``ast.Constant``，
    两者都无法伪装成一次真实调用。（同第二十三轮的机构判据契约。）

    ⚠ 必须先 `textwrap.dedent`：`inspect.getsource` 对**嵌套函数 / 方法**返回的
    是**带缩进**的源码，直接 `ast.parse` 会抛 `IndentationError`，
    于是判据永远返回 False —— 那样「注释不能伪装」这条对照会**靠报错通过**，
    变成空转（实测踩到过）。
    """
    try:
        src = textwrap.dedent(inspect.getsource(view))
        tree = ast.parse(src)
    except (OSError, SyntaxError, IndentationError) as exc:
        return False, f'无法解析源码：{exc}'
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, 'id', None) or getattr(fn, 'attr', None)
            if name in PARAM_PARSER_HELPERS:
                return True, f'调用 {name}()'
    return False, '未发现共用解析器调用'


class ParamParserContractTest(BusinessTestBase):
    """判据：读强类型参数（id / 日期 / 数量）的视图必须走共用解析器。"""

    # 本轮修复的落点。新增读强类型参数的视图时**必须**加进来 ——
    # 这份清单就是「哪些接口已经收口」的权威记录。
    FIXED_VIEWS = [
        'capture_list', 'owner_return_list', 'pet_codes_preview',
        'capture_create',
        'material_transactions', 'shelter_stock_ledger',
        'purchase_create', 'dispatch_create', 'stock_adjustment',
        'ledger_center',
    ]

    def test_fixed_views_all_use_the_shared_parser(self):
        missing = []
        for name in self.FIXED_VIEWS:
            view = getattr(services, name, None)
            if view is None:
                view = self._lookup_view(name)
            ok, why = uses_shared_param_parser(view)
            if not ok:
                missing.append(f'{name}（{why}）')
        self.assertEqual(missing, [], '这些视图没走共用参数解析器：' + '；'.join(missing))

    @staticmethod
    def _lookup_view(name):
        from business import (views_capture, views_material)
        from supervision import views as sv_views
        for mod in (views_capture, views_material, sv_views):
            fn = getattr(mod, name, None)
            if fn is not None:
                return fn
        raise AssertionError(f'找不到视图 {name}')

    # --- 判据自身的对照：证明 AST 判定不是空转 ---------------------------
    def test_comment_cannot_fake_a_call(self):
        def fake():
            # parse_int_param(request.GET.get('x'))
            """parse_int_param"""
            return None
        ok, why = uses_shared_param_parser(fake)
        self.assertFalse(ok, f'注释/文档串骗过了判据：{why}')

    def test_string_cannot_fake_a_call(self):
        def fake():
            return 'parse_date_param(request.GET.get("x"))'
        ok, why = uses_shared_param_parser(fake)
        self.assertFalse(ok, f'字符串骗过了判据：{why}')

    def test_real_call_counts(self):
        def real():
            return parse_int_param('1', 'x')
        ok, why = uses_shared_param_parser(real)
        self.assertTrue(ok, why)

    def test_real_date_call_counts(self):
        def real():
            return parse_date_param('2026-09-19', 'x')
        ok, why = uses_shared_param_parser(real)
        self.assertTrue(ok, why)


# ============================================================================
# 界面路径：失败必须「显示出来」，不能渲染成空表
# ============================================================================
# 第二十四轮 GUI 实测（`gui-test-scripts/65_param_contract.js` 的 D2/D3）发现的
# **静默失败**：服务端已经把非法参数修成 400 了，但界面**看不出来** ——
# `TNR_API.getLedger` 原先走 `_get()`，而 `_get()`（`static/js/tnr-api.js` 顶部）
# **完全忽略 HTTP 状态**，非 2xx 时返回 `[]`。于是：
#
#     服务端：400「机构必须是数字」
#     界面：  一张**空表**（「暂无台账数据」）
#
# 用户看到的是「这个筛选条件下没有记录」，而不是「你的筛选条件被拒了」。
# 这比 500 更难排查 —— 500 至少有报错。
#
# 修法成对做：`getLedger` 状态感知（失败抛错）+ `renderLedgerTable` 接住并渲染。
ROOT = Path(__file__).resolve().parents[2]
API_JS = ROOT / 'static' / 'js' / 'tnr-api.js'
GOV_PORTAL = ROOT / 'templates' / 'portal' / 'gov' / 'portal.html'


def extract_js_block(src, header):
    """从 `header` 开始，按**大括号配对**截出整段代码（含 header）。

    不能用正则 —— 方法体里还有对象字面量、模板串插值 `${…}`，正则会在
    第一个 `}` 就截断。
    """
    i = src.index(header)
    j = src.index('{', i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f'{header} 的大括号不配对')


def strip_js_comments(src):
    """剥掉 JS 的 `//` 行注释与 `/* */` 块注释（**保持文本长度不变**）。

    ⚠ 必须先剥注释再断言 —— 否则**修复说明里的注释本身**就会把判据骗过，
    而且方向是**假阳性**：本轮实测「`getLedger` 里不能出现 `_get(`」这条判据，
    被我自己写的「这里**不能**走 `_get()`」这句注释打红。
    注释不在语法里，判据就不该看见它。

    保持长度不变是为了报错时行号/列号仍然对得上（同 6.3 的做法）。
    要识别字符串字面量，否则 `'http://x'` 里的 `//` 会被误当注释起点。
    """
    out = []
    i, n = 0, len(src)
    quote = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == '\\' and i + 1 < n:       # 转义，跳过下一个字符
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'", '`'):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == '/' and i + 1 < n and src[i + 1] == '/':
            while i < n and src[i] != '\n':
                out.append(' ')
                i += 1
            continue
        if c == '/' and i + 1 < n and src[i + 1] == '*':
            while i < n and not (src[i] == '*' and i + 1 < n and src[i + 1] == '/'):
                out.append('\n' if src[i] == '\n' else ' ')
                i += 1
            out.append('  ')
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


class LedgerApiSurfacesErrorsTest(SimpleTestCase):
    """`getLedger` 必须状态感知；台账表必须把失败显示出来。

    纯源码级断言（不跑浏览器）—— GUI 65 的 D2/D3/D5/D6 是它的**运行期**对照。

    ⚠ 第二十五轮把「状态感知」的逻辑**提成了 `getData(url)`**（严格读内核），
    `getLedger` 改成委托它。所以「判 HTTP 状态 / 抛错带服务端文案」这些断言
    要打在 `getData` 上，`getLedger` 只断言「确实委托了」。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.api_src = API_JS.read_text(encoding='utf-8')
        cls.gov_src = GOV_PORTAL.read_text(encoding='utf-8')
        # ⚠ 一律在**剥掉注释之后**的源码上断言（见 strip_js_comments 的说明）
        cls.ledger = strip_js_comments(
            extract_js_block(cls.api_src, 'async getLedger(filters)'))
        cls.getdata = strip_js_comments(
            extract_js_block(cls.api_src, 'async getData(url)'))
        cls.render = strip_js_comments(
            extract_js_block(cls.gov_src, 'async renderLedgerTable()'))

    def test_get_ledger_does_not_use_silent_get(self):
        """⚠ 核心判据：`getLedger` **不能**走 `_get()`（它忽略 HTTP 状态）。"""
        self.assertNotIn(
            '_get(', self.ledger,
            'getLedger 又用回了 _get() —— 非 2xx 会被静默渲染成空表')
        # 正向对照：证明「_get 这个串」不是全局不存在（否则上一条是空转）
        self.assertIn('this._get(', strip_js_comments(extract_js_block(
            self.api_src, 'async getInstitutions(type)')))
        # 且必须真的走了严格读内核
        self.assertIn('this.getData(', self.ledger,
                      'getLedger 没有委托 getData，状态感知逻辑丢了')

    def test_comment_stripping_is_not_vacuous(self):
        """判据自身的对照：注释剥离必须真的剥掉了、且没剥掉代码。"""
        raw = extract_js_block(self.api_src, 'async getLedger(filters)')
        self.assertIn('_get()', raw, '注释里的 _get() 不见了，这条对照失效了')
        self.assertIn('fetch(', self.getdata, '剥离把代码也剥掉了')
        self.assertEqual(len(raw), len(strip_js_comments(raw)), '剥离改变了长度')

    def test_get_data_is_status_aware(self):
        self.assertIn('res.ok', self.getdata, 'getData 没有判 HTTP 状态')
        self.assertIn('data.success', self.getdata, 'getData 没有判 success')

    def test_get_data_throws_with_server_message(self):
        """必须把**服务端文案**带出来，否则界面只能显示「加载失败」。"""
        self.assertIn('throw new Error', self.getdata)
        self.assertIn('data.message', self.getdata)

    def test_render_ledger_table_catches_and_shows_error(self):
        """`renderLedgerTable` 必须接住异常并渲染错误 —— 不能只是空表。"""
        self.assertIn('catch', self.render, 'renderLedgerTable 没接住 getLedger 的异常')
        self.assertIn('加载失败', self.render, 'renderLedgerTable 没渲染错误提示')
        self.assertIn('table-empty', self.render, '没用既有的失败渲染范式')

    def test_block_extractor_is_not_vacuous(self):
        """判据自身的对照：截出来的块必须真的是那个方法。"""
        self.assertIn('getLedger', self.ledger)
        self.assertIn('renderLedgerTable', self.render)
        self.assertLess(len(self.ledger), len(self.api_src) // 4,
                        '截出来的块过大，多半是配对算法写错了')
        self.assertLess(len(self.render), len(self.gov_src) // 4,
                        '截出来的块过大，多半是配对算法写错了')
