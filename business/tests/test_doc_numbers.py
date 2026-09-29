"""全局单据化测试（第四十六轮）：编号规则 / 接收单关联 / 打印路由。

单据号规则：`{类型码}{YY}{5位流水}`（如 CAP2600001），全市统一按
「类型码 + 年份」递增；打印页 = A4 版式 + 单据号 + 溯源二维码 + 关联单据链。
"""
import re

from django.test import Client
from django.utils import timezone
from datetime import datetime

from business.models import MaterialTransaction, DocumentSequence
from business.services import generate_doc_no
from business.tests.base import (
    BusinessTestBase, assert_doc_no, make_capture, make_district, make_institution,
    make_material, make_pet, make_user,
)


class DocNumberRuleTest(BusinessTestBase):
    """generate_doc_no：格式、递增、按年隔离、类型隔离。"""

    def test_format_and_sequence(self):
        no1 = generate_doc_no('CAP')
        no2 = generate_doc_no('CAP')
        assert_doc_no(self, no1, 'CAP')
        self.assertEqual(int(no2[7:]) - int(no1[7:]), 1, '同类型同年必须连续递增')

    def test_prefixes_independent(self):
        cap = generate_doc_no('CAP')
        trf = generate_doc_no('TRF')
        assert_doc_no(self, cap, 'CAP')
        assert_doc_no(self, trf, 'TRF')
        self.assertEqual(int(trf[7:]), 1, '类型之间流水独立')

    def test_year_isolated(self):
        old = generate_doc_no('CAP', when=datetime(2025, 3, 1))
        assert_doc_no(self, old, 'CAP')
        self.assertTrue(old.startswith('CAP25'), '指定 2025 年应产生 CAP25 前缀')
        cur = generate_doc_no('CAP')
        self.assertNotEqual(cur[3:5], old[3:5], '跨年流水各自独立')

    def test_counter_row_created(self):
        generate_doc_no('EUT')
        row = DocumentSequence.objects.get(prefix='EUT', year=timezone.now().year % 100)
        self.assertEqual(row.last_no, 1)


class ReceiveDocChainTest(BusinessTestBase):
    """接收单独立 RCV 单号 + ref_no 关联下发单（单据链）。"""

    def setUp(self):
        self.district = make_district()
        self.shelter = make_institution(type='shelter', district=self.district)
        self.hospital = make_institution(type='hospital', district=self.district)
        self.shelter_user = make_user('doc_shelter', role='shelter',
                                      district=self.district, institution=self.shelter)
        self.hospital_user = make_user('doc_hosp', role='hospital',
                                       district=self.district, institution=self.hospital)
        self.material = make_material(name='狂犬疫苗', district=self.district, shelter_stock=100)

    def _dispatch(self):
        self.client.force_login(self.shelter_user)
        body = self.ok(self.post_json('/api/business/materials/purchase/', {
            'material_id': self.material.id, 'quantity': 50,
            'batch_no': 'BTEST01',
        }))
        # 采购入库后从捕捉点下发到医院
        resp = self.post_json('/api/business/materials/dispatch/', {
            'material_id': self.material.id, 'quantity': 20,
            'to_hospital_id': self.hospital.id, 'batch_no': 'BTEST01',
        })
        body = self.ok(resp)
        return body['data']

    def test_receive_gets_own_rcv_number_with_ref(self):
        data = self._dispatch()
        self.client.force_login(self.hospital_user)
        body = self.ok(self.post_json(f"/api/business/materials/{data['id']}/receive/", {}))
        recv = body['data']
        assert_doc_no(self, recv['ledger_no'], 'RCV')
        self.assertNotEqual(recv['ledger_no'], data['ledger_no'], '接收单号不得复用下发单号')
        self.assertEqual(recv['ref_no'], data['ledger_no'], 'ref_no 必须指向下发单号')

    def test_receive_twice_rejected_by_ref(self):
        data = self._dispatch()
        self.client.force_login(self.hospital_user)
        self.ok(self.post_json(f"/api/business/materials/{data['id']}/receive/", {}))
        self.expect_fail(self.post_json(f"/api/business/materials/{data['id']}/receive/", {}),
                         message='此物料已签收')


class PrintViewTest(BusinessTestBase):
    """打印路由：登录 + 区县隔离 + 版式要素（单据号/二维码/关联链）。

    ⚠ 路径在 `/print/<doc>/<pk>/`，**不在 `/api/` 下** —— 打印页返回整页 HTML，
    而 `/api/` 的契约是 JSON 信封（`core/tests.py::AllApiRoutesContractTest`
    会枚举每条 `/api/` 路由并断言 `Content-Type: application/json`）。
    曾经挂在 `/api/business/print/` 下，被那条闸门正确地拦下：
    `print/1/1/ 404 text/html`。
    """

    def setUp(self):
        self.district = make_district()
        self.shelter = make_institution(type='shelter', district=self.district)
        self.capture = make_capture(district=self.district, shelter=self.shelter,
                                    pet_codes=['TP001'])
        self.user = make_user('print_user', role='gov_district', district=self.district)
        self.other_user = make_user('print_other', role='gov_district',
                                    district=make_district(name='外区', code='WQ'))

    def test_login_required(self):
        """未登录 → 302 跳登录页（**页面**语义，不是接口的 401 JSON）。"""
        resp = Client().get(f'/print/capture/{self.capture.id}/')
        self.assertEqual(resp.status_code, 302, '未登录不得直接看到单据页')
        self.assertIn('/login/', resp['Location'], '必须重定向到登录页')

    def test_wrong_role_rejected(self):
        """无打印入口的角色（领养人）不得打印 —— 反向对照：不能一律放行。"""
        adopter = make_user('print_adopter', role='adopter', district=self.district)
        self.client.force_login(adopter)
        resp = self.client.get(f'/print/capture/{self.capture.id}/')
        self.assertIn(resp.status_code, (302, 403),
                      '领养人既不该进得来，也不该拿到单据内容')

    def test_district_isolation(self):
        self.other_client = Client()
        self.other_client.force_login(self.other_user)
        resp = self.other_client.get(f'/print/capture/{self.capture.id}/')
        self.assertEqual(resp.status_code, 404, '跨区县单据不得暴露存在性')

    def test_cross_district_and_missing_are_indistinguishable(self):
        """「存在但不属于我」与「根本不存在」必须完全不可区分。

        只测「越权被拒」是不够的 —— 把判据写成「一律 404」也能通过。
        所以成对断言：两者的**状态码 + 响应体**都要一模一样，
        否则就成了存在性预言机（能靠响应差异探测别区县有哪些单据）。
        """
        self.other_client = Client()
        self.other_client.force_login(self.other_user)
        cross = self.other_client.get(f'/print/capture/{self.capture.id}/')
        missing = self.other_client.get('/print/capture/99999999/')
        self.assertEqual(cross.status_code, missing.status_code)
        self.assertEqual(cross.content, missing.content)

    def test_renders_doc_no_qr_and_related(self):
        self.client.force_login(self.user)
        # 给该捕捉单的宠物补一条领回单，验证关联链输出
        pet = make_pet(code='TP001', district=self.district, shelter=self.shelter,
                       capture=self.capture)
        from business.models import OwnerReturn
        OwnerReturn.objects.create(
            pet=pet, pet_code=pet.code, owner_name='张三', owner_phone='13800000000',
            district=self.district, ledger_no=generate_doc_no('RET'))

        resp = self.client.get(f'/print/capture/{self.capture.id}/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(self.capture.ledger_no, html, '打印页必须含单据号')
        self.assertIn('data:image/png;base64,', html, '打印页必须内嵌溯源二维码')
        self.assertIn('关联单据', html)
        self.assertIn('领回单', html, '同宠单据应出现在关联链')
        self.assertIn('经办人签字', html, '打印后手签留白栏')

    def test_unknown_doc_type_404(self):
        self.client.force_login(self.user)
        resp = self.client.get('/print/nothing/1/')
        self.assertEqual(resp.status_code, 404)
