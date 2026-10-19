"""core.privacy 脱敏判据。"""
from django.test import SimpleTestCase

from core.privacy import mask_id_card, mask_phone


class MaskIdCardTest(SimpleTestCase):
    def test_keeps_head_and_tail(self):
        self.assertEqual(mask_id_card('110101199001011234'),
                         '110101********1234')

    def test_abnormal_length_is_fully_masked(self):
        # 15/18 位是合法身份证长度；16/17 位与超长串都是脏数据。
        for value in ('12345', '1101011990010112', '1101011990010112345678'):
            self.assertEqual(mask_id_card(value), '*' * len(value))

    def test_empty_stays_empty(self):
        self.assertEqual(mask_id_card(''), '')
        self.assertEqual(mask_id_card(None), '')
        self.assertEqual(mask_id_card('   '), '')


class MaskPhoneTest(SimpleTestCase):
    def test_keeps_head_and_tail(self):
        self.assertEqual(mask_phone('13800001234'), '138****1234')

    def test_short_number_is_fully_masked(self):
        # 7 位座机按「前 3 + 后 4」会原样返回，等于没脱敏。
        self.assertEqual(mask_phone('1234567'), '*******')

    def test_empty_stays_empty(self):
        self.assertEqual(mask_phone(''), '')
        self.assertEqual(mask_phone(None), '')
        self.assertEqual(mask_phone('   '), '')

    def test_trims_whitespace(self):
        self.assertEqual(mask_phone('  13800001234  '), '138****1234')
