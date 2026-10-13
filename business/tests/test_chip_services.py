"""芯片服务层的纯函数判据（第四十六轮）。

`parse_chip_numbers()` 是「芯片按号码单独管理」的地基：
号码算错了，后面所有校验都建立在**错的号码**上 —— 而它们**全都不会报错**
（校验的对象是错的号码，校验本身是对的）。

⚠ 这里必须用**带前导零**的首号。用 `1000010001` 这种没有前导零的号码，
把 `num.zfill(width)` 改成 `num` 结果一模一样 —— 判据等于没测。
实测过：去掉 `zfill` 后整包测试仍然全绿，正是这条缺失暴露出来的。
"""
from django.test import SimpleTestCase

from business.services_chip import (
    CHIP_BATCH_MAX, CHIP_NUMBER_MAX_LENGTH, parse_chip_numbers,
)


class ParseChipNumbersTest(SimpleTestCase):
    def test_increments_and_keeps_width(self):
        """数值 +1 递增，**保持首号的位数补零**。"""
        self.assertEqual(
            parse_chip_numbers('042060107000177', 3),
            ['042060107000177', '042060107000178', '042060107000179'])

    def test_zero_padding_across_carry(self):
        """跨段进位后仍保持位数（`0099` + 1 = `0100`，不是 `100`）。"""
        self.assertEqual(parse_chip_numbers('0098', 3),
                         ['0098', '0099', '0100'])

    def test_count_one_returns_first_only(self):
        self.assertEqual(parse_chip_numbers('042060107000177', 1),
                         ['042060107000177'])

    def test_non_numeric_rejected(self):
        for bad in ('ABC-001', '0420-6010', '42 06', ''):
            with self.assertRaises(ValueError, msg=f'{bad!r} 应被拒'):
                parse_chip_numbers(bad, 1)

    def test_fullwidth_digits_rejected(self):
        """**全角数字必须单独挡掉**。

        `'１２３'.isdigit()` 是 True、`int('１２３')` 也不报错 —— 于是全角号码
        会一路走到 `zfill()`，建出的行看着像成功了，实际在扫码枪下永远扫不出来
        （从 Excel 粘贴时很容易带进来）。`isascii()` 是唯一能挡住它的判据。
        """
        with self.assertRaises(ValueError, msg='全角数字必须被拒'):
            parse_chip_numbers('０４２０６０１０７０００１７７', 2)

    def test_count_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError, msg=f'数量 {bad} 应被拒'):
                parse_chip_numbers('9000000001', bad)

    def test_count_over_limit_rejected(self):
        """单批上限 —— 防「把数量误打成十万」当场建出十万行。"""
        with self.assertRaises(ValueError):
            parse_chip_numbers('9000000001', CHIP_BATCH_MAX + 1)

    def test_non_integer_count_rejected(self):
        for bad in ('abc', None, 1.5):
            with self.assertRaises(ValueError, msg=f'数量 {bad!r} 应被拒'):
                parse_chip_numbers('9000000001', bad)

    def test_result_too_long_rejected(self):
        """推算结果超过字段长度必须报错，**不能截断**。

        截断等于把两颗芯片记成同一个号码 —— 而 `number` 有 unique 约束，
        第二颗会在 `bulk_create` 撞 `IntegrityError` 打成 500，
        用户只看到「服务器错误」，真正的原因（这批号码跨出字段长度了）说不出口。
        """
        with self.assertRaises(ValueError):
            parse_chip_numbers('9' * CHIP_NUMBER_MAX_LENGTH, 2)

    def test_start_longer_than_field_rejected(self):
        with self.assertRaises(ValueError):
            parse_chip_numbers('9' * (CHIP_NUMBER_MAX_LENGTH + 1), 1)

    def test_strips_whitespace(self):
        """首号两端的空白要剥掉（从表格粘贴时很常见）。"""
        self.assertEqual(parse_chip_numbers('  9000000001  ', 1), ['9000000001'])

    def test_returns_exactly_count_numbers(self):
        """返回个数必须**恰好等于**数量 —— 「强绑定」的第一道闸。"""
        for count in (1, 7, 100):
            self.assertEqual(len(parse_chip_numbers('042060107000177', count)),
                             count)
