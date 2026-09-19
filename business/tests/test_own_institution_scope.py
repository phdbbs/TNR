"""第二十三轮：**存在性预言机** —— 按 id 取单的接口不能透露记录是否存在。

第二十二轮扫的是「接口 × 角色矩阵」（横向：同一资源，哪些角色能进）。
这一轮换一个**正交维度**：同一个接口、同一个角色，只把 id 换成
「**真实但不属于我**的」，响应还透不透露信息。

侦察方式：`scripts/scope_probe.py` —— 把**全部带 id 的接口 × 全部 5 个角色**
用 `transaction.atomic()` + `set_rollback(True)` 扫一遍（不留脏数据），
把「跨机构/跨区县的真实 id」与「保证不存在的 id」的响应做**自校准对比**：
判据不依赖错误文案的字面量，只要求**两者完全一致**。

实测三处漏点（都在医院端，都栽在同一个写法上）：

    try:
        obj = Model.objects.get(id=pk)        # ← 裸取，先拿到再说
    except Model.DoesNotExist:
        return json_fail('…不存在', status=404)
    if obj.status != 'pending':               # ← **状态检查排在机构检查之前**
        return json_fail(f'当前状态({obj.status})不可签收')
    if user.institution_id != obj.hospital_id:
        return json_fail('无权签收…')

于是同一个「不属于我的 id」有三种可区分的结果：

    不存在            → 404「转运记录不存在」
    存在、状态≠pending → 400「当前状态(received)不可签收」  ← **状态被读出**
    存在、状态=pending → 400「无权签收此转运记录」          ← **存在性被读出**

攻击者只要枚举 id，就能问出他院单据是否存在、甚至业务状态。

修复：新增共用取数函数 `services.get_own_institution_object()`，
**按本机构外键取单** —— 不属于本院与不存在**返回同一个 404**。

**为什么不能改成按区县收敛**：转运允许跨区县送医（`transfer_create`
不限制目标医院的区县），按区县收敛会打断这条正常业务路径。
所以本文件的**第一组**用例是正向对照：跨区县送医必须**照常走得通**。

配套：`business/tests/test_write_scope.py` 的源码级契约扫描已从「文本匹配」
升级为 **AST 判定**（既认字面量比较，也认共用函数调用，且不被注释/字符串骗过）。
"""
import datetime

from django.utils import timezone

from business.models import AdoptionApplication, MaterialTransaction, Transfer
from business.tests.base import (BusinessTestBase, make_institution, make_material,
                                 make_pet, make_user)

TRANSFER_URL = '/api/business/transfers/'
MATERIAL_URL = '/api/business/materials/'
APP_REVIEW_URL = '/api/business/adoptions/applications/{pk}/review/'

# 保证不存在的 id —— 用它做「自校准基准」：任何「存在但不是我的」响应
# 都必须与它**完全一致**，否则就是预言机。
GHOST_ID = 99999999


class OracleMixin:
    """「他院的」与「不存在的」必须完全不可区分。"""

    def assert_indistinguishable(self, template, real_id, attacker, label,
                                 forbidden_words=()):
        """同一条请求打两个 id，要求状态码 + 文案**逐字相同**。

        `forbidden_words`：额外检查文案里**不许出现**的片段
        （例如真实状态 `received` —— 那是把业务状态写进错误文案的直接证据）。
        """
        self.login_as(attacker)
        real = self.post_json(template.format(pk=real_id))
        ghost = self.post_json(template.format(pk=GHOST_ID))

        self.assertEqual(real.status_code, ghost.status_code,
                         f'{label}：状态码不同（{real.status_code} vs '
                         f'{ghost.status_code}）= 可枚举 id')
        self.assertEqual(real.json().get('message'), ghost.json().get('message'),
                         f'{label}：文案不同（{real.json().get("message")!r} vs '
                         f'{ghost.json().get("message")!r}）= 可枚举 id')
        self.assertFalse(real.json().get('success'), f'{label}：居然成功了')
        self.assertEqual(real.status_code, 404,
                         f'{label}：期望 404（「不存在」语义），实际 {real.status_code}')

        msg = real.json().get('message') or ''
        for word in forbidden_words:
            self.assertNotIn(word, msg,
                             f'{label}：错误文案里泄露了 {word!r} → {msg!r}')
        return real


class CrossDistrictReferralStillWorksTest(BusinessTestBase):
    """正向对照：修复**不能**把跨区县送医掐掉。

    `transfer_receive` 原先**没有**区县判据是**对的**（转运本就允许跨区县），
    所以这组用例是本次修复的**判别性测试**：
    如果谁把 `get_own_institution_object` 换成了区县收敛，这里会立刻红。
    """

    def _send_a_to_b(self):
        """甲区捕捉点 → 乙区医院，走真实接口（`transfer_create`）。"""
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       status='in_transit')
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{TRANSFER_URL}create/', {
            'from_shelter_id': self.shelter_a.id,
            'to_hospital_id': self.hospital_b.id,
            'pet_codes': [pet.code],
            'pet_count': 1,
        }))
        transfer_id = body['data'][0]['id']
        self.assertEqual(Transfer.objects.get(id=transfer_id).to_hospital_id,
                         self.hospital_b.id, '跨区县送医没落库')
        return pet, transfer_id

    def test_cross_district_receive_succeeds(self):
        """甲区送 → **乙区医院签收**，必须成功。"""
        pet, transfer_id = self._send_a_to_b()
        self.login_as(self.hospital_user_b)
        body = self.ok(self.post_json(f'{TRANSFER_URL}{transfer_id}/receive/'),
                       '跨区县签收被拦了 —— 修预言机不该掐断正常路径')
        self.assertEqual(body['data']['status'], 'received')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment')

    def test_cross_district_reject_succeeds(self):
        """甲区送 → 乙区医院**驳回**，必须成功。"""
        pet, transfer_id = self._send_a_to_b()
        self.login_as(self.hospital_user_b)
        body = self.ok(self.post_json(f'{TRANSFER_URL}{transfer_id}/reject/',
                                      {'reason': '本院满床'}),
                       '跨区县驳回被拦了')
        self.assertEqual(body['data']['status'], 'rejected')
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_transit', '驳回后宠物应回退为在途')

    def test_own_material_receive_succeeds(self):
        """正向对照：本院签收本院的下发单，必须成功。"""
        material = make_material(district=self.district_a, shelter_stock=50)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{MATERIAL_URL}dispatch/', {
            'material_id': material.id, 'hospital_id': self.hospital_a.id,
            'quantity': 10,
        }))
        txn_id = body['data']['id']
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(f'{MATERIAL_URL}{txn_id}/receive/'), '本院签收被拦了')


class HospitalExistenceOracleTest(OracleMixin, BusinessTestBase):
    """医院端三条接口：跨机构 / 跨区县的 id 必须与不存在的 id 不可区分。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        d = cls.district_a
        # **同区县**另一家医院 —— 用来排除「只是因为跨了区县才被拦」
        cls.hospital_a2 = make_institution(type='hospital', district=d, name='甲区第二医院')
        cls.other_hospital_user = make_user('ois_other', role='hospital', district=d,
                                            institution=cls.hospital_a2)
        # 医院账号但**没有机构** —— 判据不能因为字段为空就静默失效
        cls.no_inst_user = make_user('ois_none', role='hospital', district=d,
                                     institution=None)

        # 受害方（他院）名下的真实单据
        cls.v_transfer = Transfer.objects.create(
            to_hospital=cls.hospital_a2, district=d, status='pending', pet_count=1,
            ledger_no='TRF-OIS-P', to_hospital_name=cls.hospital_a2.name)
        cls.v_transfer_done = Transfer.objects.create(
            to_hospital=cls.hospital_a2, district=d, status='received', pet_count=1,
            ledger_no='TRF-OIS-D', to_hospital_name=cls.hospital_a2.name)
        cls.x_transfer = Transfer.objects.create(
            to_hospital=cls.hospital_b, district=cls.district_a, status='pending',
            pet_count=1, ledger_no='TRF-OIS-X',
            to_hospital_name=cls.hospital_b.name)
        cls.v_material = make_material(district=d, name='预言机物料')
        cls.v_txn = MaterialTransaction.objects.create(
            type='dispatch', material=cls.v_material,
            material_name=cls.v_material.name, quantity=3,
            hospital=cls.hospital_a2, district=d,
            date=timezone.localdate(), ledger_no='MT-OIS')

    RECEIVE = TRANSFER_URL + '{pk}/receive/'
    REJECT = TRANSFER_URL + '{pk}/reject/'
    M_RECEIVE = MATERIAL_URL + '{pk}/receive/'

    # ---------- 转运签收 ----------
    def test_receive_other_hospital_same_district(self):
        """同区县另一家医院的**待签收**单 —— 原先 400「无权签收」，现在 404。"""
        self.assert_indistinguishable(self.RECEIVE, self.v_transfer.id,
                                      self.hospital_user_a, '签收·同区县他院')

    def test_receive_cross_district(self):
        self.assert_indistinguishable(self.RECEIVE, self.x_transfer.id,
                                      self.hospital_user_a, '签收·跨区县')

    def test_receive_does_not_leak_status(self):
        """**这条钉死本轮真正的危害**：已签收的他院单，文案里不许出现 `received`。

        修复前这里是 `400「当前状态(received)不可签收」` ——
        不止确认记录存在，还把业务状态直接告诉了攻击者。
        """
        self.assert_indistinguishable(self.RECEIVE, self.v_transfer_done.id,
                                      self.hospital_user_a, '签收·状态泄露',
                                      forbidden_words=('received', '状态'))

    def test_receive_refusal_writes_nothing(self):
        """被拒后**库里的值一字未改**（拦截要让下游连带写入也不发生）。"""
        self.login_as(self.hospital_user_a)
        self.post_json(self.RECEIVE.format(pk=self.v_transfer.id))
        self.v_transfer.refresh_from_db()
        self.assertEqual(self.v_transfer.status, 'pending', '被拒后转运单被改')
        self.assertIsNone(self.v_transfer.received_at, '被拒后写了签收时间')

    def test_receive_ghost_id_is_404(self):
        self.login_as(self.hospital_user_a)
        resp = self.post_json(self.RECEIVE.format(pk=GHOST_ID))
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_receive_account_without_institution_refused(self):
        """医院账号但机构为空 —— 必须 404，**不能**因为字段为空就放行。"""
        self.assert_indistinguishable(self.RECEIVE, self.v_transfer.id,
                                      self.no_inst_user, '签收·账号无机构')

    # ---------- 转运驳回 ----------
    def test_reject_other_hospital_same_district(self):
        self.assert_indistinguishable(self.REJECT, self.v_transfer.id,
                                      self.hospital_user_a, '驳回·同区县他院')

    def test_reject_cross_district(self):
        self.assert_indistinguishable(self.REJECT, self.x_transfer.id,
                                      self.hospital_user_a, '驳回·跨区县')

    def test_reject_does_not_leak_status(self):
        self.assert_indistinguishable(self.REJECT, self.v_transfer_done.id,
                                      self.hospital_user_a, '驳回·状态泄露',
                                      forbidden_words=('received', '状态'))

    def test_reject_refusal_writes_nothing(self):
        self.login_as(self.hospital_user_a)
        self.post_json(self.REJECT.format(pk=self.v_transfer.id), {'reason': '越权尝试'})
        self.v_transfer.refresh_from_db()
        self.assertEqual(self.v_transfer.status, 'pending', '被拒后转运单被驳回')
        self.assertEqual(self.v_transfer.reject_reason, '', '被拒后写了驳回理由')

    def test_reject_account_without_institution_refused(self):
        self.assert_indistinguishable(self.REJECT, self.v_transfer.id,
                                      self.no_inst_user, '驳回·账号无机构')

    # ---------- 物料签收 ----------
    def test_material_receive_other_hospital_same_district(self):
        """原先 400「无权签收此物料」，现在与「不存在」同一个 404。"""
        self.assert_indistinguishable(self.M_RECEIVE, self.v_txn.id,
                                      self.hospital_user_a, '物料签收·同区县他院')

    def test_material_receive_refusal_writes_nothing(self):
        before = MaterialTransaction.objects.count()
        self.login_as(self.hospital_user_a)
        self.post_json(self.M_RECEIVE.format(pk=self.v_txn.id))
        self.assertEqual(MaterialTransaction.objects.count(), before,
                         '被拒后仍写出了签收流水')
        self.v_txn.refresh_from_db()
        self.assertNotIn('已签收', self.v_txn.note or '', '被拒后原单被打上已签收')

    def test_material_receive_account_without_institution_refused(self):
        self.assert_indistinguishable(self.M_RECEIVE, self.v_txn.id,
                                      self.no_inst_user, '物料签收·账号无机构')

    def test_material_receive_rejects_non_dispatch_on_own_hospital(self):
        """**本院**的采购单也不能签收 —— 这条钉的是视图里的 `type='dispatch'` 过滤。

        夹具必须把 `hospital` 设成**本院**：否则单据会先被机构过滤挡掉，
        用例就会「因为别的原因通过」，对 `type` 过滤**毫无判别力**。
        （`test_material_views.test_receive_non_dispatch_txn_rejected` 原先
        正是漏了 `hospital`，变异 M20 实测「删掉 type 过滤它照样绿」。）
        """
        purchase = MaterialTransaction.objects.create(
            type='purchase', material=self.v_material,
            material_name=self.v_material.name, quantity=1,
            hospital=self.hospital_a, district=self.district_a,
            date=timezone.localdate(), ledger_no='MT-OIS-PUR')
        self.assert_indistinguishable(self.M_RECEIVE, purchase.id,
                                      self.hospital_user_a, '物料签收·本院采购单')
        self.assertFalse(
            MaterialTransaction.objects.filter(type='receive',
                                               ledger_no='MT-OIS-PUR').exists(),
            '采购单被当成下发单签收了（写出了 receive 流水）')


class AdoptionApplicationReviewOracleTest(OracleMixin, BusinessTestBase):
    """领养申请审核：**探针在演示库上跳过了这条**（库里没有跨区县申请）。

    「跳过」不等于「通过」—— 所以这组用例必须由单测夹具补上。
    该接口的实现本来就对（按角色收敛 + `.first()` + 同一个 404），
    这组用例的作用是把它**锁住**，防止以后被改回裸取。
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.pet_a = make_pet(district=cls.district_a, shelter=cls.shelter_a,
                             hospital=cls.hospital_a, status='pending_adopt')
        cls.app_a = AdoptionApplication.objects.create(
            pet=cls.pet_a, hospital=cls.hospital_a, status='pending',
            applicant_name='甲区申请人')

        cls.pet_a2 = make_pet(district=cls.district_a, shelter=cls.shelter_a,
                              hospital=cls.hospital_a, status='pending_adopt')
        cls.app_a2 = AdoptionApplication.objects.create(
            pet=cls.pet_a2, hospital=cls.hospital_a, status='pending',
            applicant_name='甲区申请人二')

    def test_other_hospital_cannot_review(self):
        self.assert_indistinguishable(APP_REVIEW_URL, self.app_a.id,
                                      self.hospital_user_b, '审核·跨机构医院')

    def test_other_district_gov_cannot_review(self):
        """乙区市/区级账号审甲区的申请 —— 必须与「不存在」不可区分。"""
        self.assert_indistinguishable(APP_REVIEW_URL, self.app_a.id,
                                      self.gov_b, '审核·跨区县区级')

    def test_own_hospital_can_review(self):
        """正向对照：本院审本院的申请，必须成功。"""
        self.login_as(self.hospital_user_a)
        self.ok(self.post_json(APP_REVIEW_URL.format(pk=self.app_a.id),
                               {'action': 'approve'}),
                '本院审核被拦了')
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.status, 'approved')

    def test_own_district_gov_can_review(self):
        """正向对照：本区县区级账号审本区县的申请，必须成功。"""
        self.login_as(self.gov_a)
        self.ok(self.post_json(APP_REVIEW_URL.format(pk=self.app_a2.id),
                               {'action': 'approve'}),
                '本区县审核被拦了')
        self.app_a2.refresh_from_db()
        self.assertEqual(self.app_a2.status, 'approved')


class OwnInstitutionHelperTest(BusinessTestBase):
    """共用取数函数本体：`services.get_own_institution_object()`。"""

    def test_returns_none_without_institution(self):
        from business.services import get_own_institution_object
        user = make_user('ois_h_none', role='hospital', district=self.district_a,
                         institution=None)
        transfer = Transfer.objects.create(
            to_hospital=self.hospital_a, district=self.district_a, status='pending',
            pet_count=1, ledger_no='TRF-OIS-H', to_hospital_name=self.hospital_a.name)
        self.assertIsNone(
            get_own_institution_object(Transfer, transfer.id, user, 'to_hospital_id'),
            '账号无机构时应返回 None（而不是「跳过校验」把记录给出去）')

    def test_returns_object_for_own_institution(self):
        from business.services import get_own_institution_object
        transfer = Transfer.objects.create(
            to_hospital=self.hospital_a, district=self.district_a, status='pending',
            pet_count=1, ledger_no='TRF-OIS-H2', to_hospital_name=self.hospital_a.name)
        got = get_own_institution_object(Transfer, transfer.id,
                                         self.hospital_user_a, 'to_hospital_id')
        self.assertIsNotNone(got, '本院记录应能取到（正向对照）')
        self.assertEqual(got.id, transfer.id)

    def test_returns_none_for_other_institution(self):
        from business.services import get_own_institution_object
        transfer = Transfer.objects.create(
            to_hospital=self.hospital_b, district=self.district_a, status='pending',
            pet_count=1, ledger_no='TRF-OIS-H3', to_hospital_name=self.hospital_b.name)
        self.assertIsNone(
            get_own_institution_object(Transfer, transfer.id,
                                       self.hospital_user_a, 'to_hospital_id'))

    def test_extra_filters_are_applied(self):
        """`**extra` 也要生效（`material_receive` 靠它锁定 `type='dispatch'`）。"""
        from business.services import get_own_institution_object
        material = make_material(district=self.district_a)
        purchase = MaterialTransaction.objects.create(
            type='purchase', material=material, material_name=material.name,
            quantity=1, hospital=self.hospital_a, district=self.district_a,
            date=datetime.date.today(), ledger_no='MT-OIS-P')
        self.assertIsNone(
            get_own_institution_object(MaterialTransaction, purchase.id,
                                       self.hospital_user_a, 'hospital_id',
                                       type='dispatch'),
            'extra 过滤没生效 —— 采购单被当成了下发单')
