"""全局单据化测试（第四十六轮）：编号规则 / 接收单关联 / 打印路由。

单据号规则：`{类型码}{YY}{5位流水}`（如 CAP2600001），全市统一按
「类型码 + 年份」递增；打印页 = A4 版式 + 单据号 + 溯源二维码 + 关联单据链。
"""
import os
import re

from django.test import Client, SimpleTestCase
from django.utils import timezone
from datetime import datetime

from business.models import MaterialTransaction, DocumentSequence
from business.services import generate_doc_no
from business.tests.base import (
    BusinessTestBase, assert_doc_no, make_capture, make_district, make_institution,
    make_material, make_pet, make_user,
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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

    def test_unknown_prefix_rejected(self):
        """未知类型码直接抛错 —— 不生成怪号。

        单号是要印在纸上、写进档案的东西；错了比崩了更难查。
        """
        with self.assertRaises(ValueError):
            generate_doc_no('ZZZ')


class DocPrefixRegistryTest(SimpleTestCase):
    """类型码清单：每个码都必须有**产生端**，且代码里不得出现表外的码。

    针对的是一整类缺陷（见 `services.DOC_PREFIXES` 的注释）：
    **枚举值存在、写入路径缺失** —— 消费端（列表 / 打印 / 统计）齐全、
    产生端漏了，功能就是**空壳**：界面上永远空着，看起来像「暂时没有数据」，
    不报任何错。`CON`（物料消耗）就是这么漏的：68 条消耗流水 66 条空号，
    打印出来是「单据号：—」。

    ⚠ 判据必须**从源码解析调用点**，不能只断言「清单里有 CON」——
    后者在产生端被整段删掉时照样通过（典型假绿）。
    ⚠ 扫描时**排除 `tests/` 与 `migrations/`**：测试里的调用点会让
    「产生端存在」这一条恒真（测试文件自己就在调）。
    """

    @staticmethod
    def _producer_corpus():
        out = []
        root = os.path.join(ROOT, 'business')
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ('migrations', 'tests')]
            for fn in filenames:
                if fn.endswith('.py'):
                    with open(os.path.join(dirpath, fn), encoding='utf-8') as f:
                        out.append(f.read())
        return '\n'.join(out)

    def test_every_declared_code_has_a_producer(self):
        from business.services import DOC_PREFIXES
        produced = set(re.findall(r"generate_doc_no\(\s*'([A-Z]+)'",
                                  self._producer_corpus()))
        missing = sorted(set(DOC_PREFIXES) - produced)
        self.assertEqual(
            missing, [],
            '以下类型码**声明了却没有产生端**（消费端有、写入路径缺失 = 空壳）：\n  '
            + '\n  '.join('%s（%s）' % (k, DOC_PREFIXES[k]) for k in missing))

    def test_no_code_outside_the_registry(self):
        from business.services import DOC_PREFIXES
        used = set(re.findall(r"generate_doc_no\(\s*'([A-Z]+)'",
                              self._producer_corpus()))
        unknown = sorted(used - set(DOC_PREFIXES))
        self.assertEqual(
            unknown, [],
            '以下类型码在代码里取号、却不在 `DOC_PREFIXES` 里（清单漏了）：\n  '
            + '\n  '.join(unknown))

    def test_migration_prefix_map_is_a_subset(self):
        """迁移里的类型码映射不得出现清单外的码。"""
        from business.services import DOC_PREFIXES
        with open(os.path.join(ROOT, 'business/migrations/0024_backfill_blank_doc_nos.py'),
                  encoding='utf-8') as f:
            src = f.read()
        used = set(re.findall(r"'([A-Z]{3})'", src))
        self.assertTrue(used, '没解析到迁移里的类型码 —— 解析逻辑失效了')
        self.assertEqual(sorted(used - set(DOC_PREFIXES)), [])


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
