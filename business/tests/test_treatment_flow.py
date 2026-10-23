"""诊疗流程（v5.1 三步流程）判据 —— 第四十六轮。

覆盖：开单 / 检查单 / 医嘱处方 / 诊断 / 执行扣库存 / 撤销退回 /
诊疗完成 / 办结改状态 / 看板分组 / 阶段过滤 / 权限收口。

⚠ 判据一律落在**业务结果**上（库存真变了、芯片真绑上了、状态真改了），
不停在状态码 —— 本地全绿 + 生产 200 也可能功能 100% 不可用。

⚠ 反向对照成对：只测「被拒」的话，把判据写成「一律拒绝」也能通过。
凡是有「拦住」的用例，都配一条「正常路径能过」的对照。
"""
import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase
from PIL import Image

from business import services_treatment as st
from business.models import (
    AdoptionHallListing, Chip, MaterialStock, MaterialTransaction, Pet,
    Treatment, TreatmentItem,
)
from business.services import get_hospital_stock
from business.tests.base import (
    BusinessTestBase, make_chip, make_hospital_txn, make_institution,
    make_material, make_pet, make_user,
)

URL = '/api/business/treatments/'


class FlowBase(BusinessTestBase):
    """诊疗流程用例基类：甲区医院 + 一只在院猫 + 一批药品。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.med_a = make_material(category='medicine', name='头孢曲松钠',
                                  district=cls.district_a)
        cls.med_b = make_material(category='medicine', name='美洛昔康注射液',
                                  district=cls.district_a)
        cls.vaccine = make_material(category='vaccine', name='猫三联',
                                    district=cls.district_a)
        make_hospital_txn(cls.med_a, cls.hospital_a, 'receive', 20)
        make_hospital_txn(cls.med_b, cls.hospital_a, 'receive', 20)
        make_hospital_txn(cls.vaccine, cls.hospital_a, 'receive', 10)

    def setUp(self):
        self.login_as(self.hospital_user_a)
        self.pet = make_pet(status='in_treatment', district=self.district_a,
                            hospital=self.hospital_a, species='猫')

    # ---- 助手 ----
    def _open(self, pet=None):
        resp = self.post_json(URL + 'open/', {'pet_id': (pet or self.pet).id})
        return self.ok(resp)['data']

    def _check(self, tid, submit=True, **kw):
        body = {'temperature': 38.5, 'temperature_flag': 'normal'}
        body.update(kw)
        if not submit:
            body['draft'] = True
        return self.post_json(f'{URL}{tid}/check/', body)

    def _plan(self, tid, items, advice='术后观察精神与食欲'):
        return self.post_json(f'{URL}{tid}/plan/',
                              {'advice': advice, 'items': items})

    def _diagnosed(self, tid, items=None):
        """把单子推到「已诊断」，返回 detail。"""
        self.ok(self._check(tid))
        items = items or [{'key': 'other', 'planned_date': '2026-03-01',
                           'rx': [self.med_a.id]}]
        self.ok(self._plan(tid, items))
        return self.ok(self.post_json(f'{URL}{tid}/diagnose/', {}))['data']

    def _first_item_id(self, detail):
        return detail['items'][0]['id']

    def _stock(self, material):
        return get_hospital_stock(material, self.hospital_a)

    def _dispose(self, tid, disposal, **kwargs):
        """直接调服务层办结 —— 第五十六轮起诊疗页不再有 HTTP 办结入口。"""
        return st.dispose(Treatment.objects.get(id=tid), disposal,
                          self.hospital_user_a, **kwargs)


# ============================================================
# 选项表：真源只有一份
# ============================================================
class ItemMetaTest(TestCase):
    def test_item_meta_covers_every_choice(self):
        """`ITEM_META` 的 key 集合必须与 `TreatmentItem.ITEM_CHOICES` 完全一致。

        少一个 key = 那个处置项目在界面上**永远没有入口**
        （「枚举值存在、产生点缺失」—— 列表永远空，像「暂时没有数据」）。
        多一个 key = 界面上出现一个后端不认的项目，提交必 400。
        """
        meta_keys = {m['key'] for m in st.ITEM_META}
        choice_keys = {k for k, _ in TreatmentItem.ITEM_CHOICES}
        self.assertEqual(meta_keys, choice_keys)

    def test_every_disposal_maps_to_a_pet_status(self):
        """办结去向的 key 必须都是 `Pet.STATUS_CHOICES` 里真实存在的状态。

        `dispose()` 是把去向 key **直接赋值**给 `pet.status` 的，
        多一个不存在于 choices 的 key，就会写进一个「界面渲染不出来」的状态。
        """
        pet_status = {k for k, _ in Pet.STATUS_CHOICES}
        for key, _ in Treatment.DISPOSAL_CHOICES:
            self.assertIn(key, pet_status)

    def test_test_options_cover_both_species(self):
        """两个物种都要有传染病项，且**人畜共患病必须两边都在**。

        原来这条只断言「每项 3 个」—— 一个数量断言既抓不到「把狂犬删了」，
        也抓不到「把猫瘟换成了猫传腹」，改一次清单就得跟着改一次数字。
        现在断言的是**内容**：狂犬病（法定报告的人畜共患疫病）猫狗都得有，
        弓形虫只有猫有（终末宿主是猫）。
        """
        for species in ('猫', '狗'):
            keys = [t['key'] for t in st.check_options(species)['infectious_tests']]
            self.assertTrue(keys, f'{species} 没有传染病检查项')
            self.assertIn('rabies', keys, f'{species} 缺狂犬病 —— 人畜共患、法定报告')
            self.assertEqual('toxoplasma' in keys, species == '猫',
                             f'弓形虫只应出现在{species}的清单里' if species == '狗'
                             else '弓形虫应出现在猫的清单里')
            self.assertEqual(len(keys), len(set(keys)), f'{species} 有重复项')

    def test_options_expose_fever_threshold(self):
        """发烧线随选项表下发 —— 前端「手改数值自动判定」与后端交叉校验
        必须同一条线；漏发的话前端 `syncTempFlag` 直接不动作，
        等于自动判定静默失效、矛盾组合只能靠 400 兜底。"""
        options = st.check_options('猫')
        self.assertEqual(options['fever_threshold'], st.FEVER_THRESHOLD)


# ============================================================
# 开单
# ============================================================
class OpenTreatmentTest(FlowBase):
    def test_open_creates_treatment(self):
        detail = self._open()
        self.assertEqual(detail['stage'], 'checking')
        self.assertEqual(detail['petCode'], self.pet.code)
        self.assertFalse(detail['isDisposed'])
        self.assertTrue(detail['ledgerNo'].startswith('TRE'))

    def test_open_reuses_in_progress_treatment(self):
        """重复进入不能开第二张单 —— 否则台账里同一只动物多张空单。"""
        first = self._open()
        second = self._open()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(Treatment.objects.filter(pet=self.pet).count(), 1)

    def test_open_rejects_disallowed_status(self):
        pet = make_pet(status='adopted', district=self.district_a,
                       hospital=self.hospital_a)
        resp = self.post_json(URL + 'open/', {'pet_id': pet.id})
        self.expect_fail(resp, 400, '不可诊疗')

    def test_open_other_hospital_pet_indistinguishable_from_missing(self):
        """别院的动物与「不存在的 id」必须完全不可区分（存在性预言机）。"""
        other = make_institution(type='hospital', district=self.district_a,
                                 name='甲区第二医院')
        foreign = make_pet(status='in_treatment', district=self.district_a,
                           hospital=other)
        r1 = self.post_json(URL + 'open/', {'pet_id': foreign.id})
        r2 = self.post_json(URL + 'open/', {'pet_id': 99999999})
        b1 = self.expect_fail(r1, 404)
        b2 = self.expect_fail(r2, 404)
        self.assertEqual(b1['message'], b2['message'])

    def test_open_signature_snapshot_from_account(self):
        """R46 口径 2：兽医证号与电子章**挂账号**，落库时快照。"""
        self.hospital_user_a.vet_license = '鄂兽医执字第 20210356 号'
        self.hospital_user_a.save(update_fields=['vet_license'])
        self.hospital_a.seal_text = '甲区医院电子章'
        self.hospital_a.save(update_fields=['seal_text'])

        detail = self._open()
        treatment = Treatment.objects.get(id=detail['id'])
        self.assertEqual(treatment.vet_license, '鄂兽医执字第 20210356 号')
        self.assertEqual(treatment.hospital_seal, '甲区医院电子章')

    def test_open_requires_hospital_role(self):
        self.login_as(self.shelter_user_a)
        resp = self.post_json(URL + 'open/', {'pet_id': self.pet.id})
        self.assertEqual(resp.status_code, 403)


# ============================================================
# 第一步：诊疗前检查
# ============================================================
class CheckStepTest(FlowBase):
    def test_draft_does_not_advance_stage(self):
        """暂存**不动** `checked_at` —— 否则「暂存一下就变诊疗中」，
        待接诊列表会莫名其妙清空。"""
        detail = self._open()
        body = self.ok(self._check(detail['id'], submit=False))['data']
        self.assertEqual(body['stage'], 'checking')
        self.assertFalse(body['checkedAt'])

    def test_submit_sets_checked_at(self):
        detail = self._open()
        body = self.ok(self._check(detail['id']))['data']
        self.assertEqual(body['stage'], 'treating')
        self.assertTrue(body['checkedAt'])

    def test_submit_requires_temperature(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature_flag': 'normal'})
        self.expect_fail(resp, 400, '体温检测')

    def test_submit_requires_temperature_flag(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/', {'temperature': 38.5})
        self.expect_fail(resp, 400, '体温判定')

    def test_other_required_checks_may_stay_empty(self):
        """常见病 / 传染病 / 皮肤黏膜**允许全空**也能提交（业务 2026-10-19 口径）。

        这条是**口径的守卫**，不是「顺手测个正常路径」：
        把这三项也加进 `submit` 门禁看起来是让数据更完整，实际后果是
        「都填阴性」—— 假阴性会流进疫病监测统计，比空白更危险。
        三态里的 `none`（未检）就是为这个现场留的诚实档位。
        如果将来真要收口，先改这条用例并说明**怎么防止逼出假数据**。
        """
        detail = self._open()
        body = self.ok(self._check(detail['id'], temperature=38.5,
                                   temperature_flag='normal'))['data']
        self.assertEqual(body['stage'], 'treating', '必检项全空却提交不了 —— 口径变了')
        self.assertEqual(body['commonFindings'] or [], [],
                         '没填的常见病检查不该被兜底成某一项')
        # 整节没填 ⇒ **不落任何键**：打印出来是「—」（这一节没做），
        # 而不是 5 个「未检」。两者含义不同：前者是「医生没勾这张表」，
        # 后者是「逐项确认过都没检」。逐项确认的口径由
        # `test_infectious_tests_filled_with_none` 锁（传了就把未传的项补 none）。
        self.assertEqual(body['infectious'], {},
                         '一次都没提交传染病检查的单据，不该被写入一串 none —— 那是伪造勾选')

    def test_non_numeric_temperature_rejected(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': '正常', 'temperature_flag': 'normal'})
        self.expect_fail(resp, 400, '必须是数字')

    def test_out_of_range_temperature_rejected(self):
        """405 这种「小数点打漏了」必须拦 —— 存进去只会污染体温趋势。"""
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': 405, 'temperature_flag': 'fever'})
        self.expect_fail(resp, 400, '超出合理范围')

    def test_invalid_temperature_flag_rejected(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': 38.5, 'temperature_flag': 'hot'})
        self.expect_fail(resp, 400, '不合法')

    # ---- 体温数值与判定的交叉校验（B3）----
    # 背景：点 chip 自动填数值、手改数值不联动判定 —— 「40.2℃ + 正常」
    # 这种矛盾组合曾经可以照常提交，打印单与体温趋势互相矛盾且无人核对。
    def test_fever_temperature_cannot_be_marked_normal(self):
        """发烧线的数值配「正常」判定必须拦 —— 这是「测了高温却漏改判定」，
        漏标的发烧会绕过疫病监测，是交叉校验里危害最大的方向。"""
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': 40.2, 'temperature_flag': 'normal'})
        self.expect_fail(resp, 400, '矛盾')

    def test_normal_temperature_cannot_be_marked_fever(self):
        """反向矛盾同样拦：判定与数值必须落在发烧线同侧，数据才可统计。"""
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': 38.5, 'temperature_flag': 'fever'})
        self.expect_fail(resp, 400, '矛盾')

    def test_fever_pair_submits_ok(self):
        """正常对照：数值与判定一致时必须放行（只测「拦」的话，
        把校验写成「一律拒绝」也能通过）。"""
        detail = self._open()
        body = self.ok(self._check(detail['id'], temperature=40.2,
                                   temperature_flag='fever'))['data']
        self.assertEqual(body['temperature'], 40.2)
        self.assertEqual(body['temperature_flag'], 'fever')

    # ---- 提交后锁定（第四十八轮）----
    # 「点保存下一步，返回后是不能再修改记录」。前端把第 1 步整页置灰，
    # 服务端 `apply_check()` 也必须拒绝 —— 否则直调 API 一样能改，
    # 而「医嘱基于什么体检结论下的」就没有凭据了。

    def test_check_cannot_be_modified_after_submit(self):
        detail = self._open()
        self.ok(self._check(detail['id']))
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': 39.9, 'temperature_flag': 'fever'})
        self.expect_fail(resp, 400, '已提交')

    def test_check_cannot_be_drafted_after_submit(self):
        """暂存同样被拒 —— 锁定不是「只拦提交」。"""
        detail = self._open()
        self.ok(self._check(detail['id']))
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'check_note': '改一笔', 'draft': True})
        self.expect_fail(resp, 400, '已提交')

    def test_submitted_check_values_stay_untouched(self):
        """被拒之后，库里的检查结论必须**一个字段都没变**。"""
        detail = self._open()
        self.ok(self._check(detail['id'], temperature=38.5,
                            temperature_flag='normal'))
        self.post_json(f'{URL}{detail["id"]}/check/',
                       {'temperature': 41.0, 'temperature_flag': 'fever'})
        from business.models import Treatment
        tx = Treatment.objects.get(id=detail['id'])
        self.assertEqual(tx.temperature, 38.5)
        self.assertEqual(tx.temperature_flag, 'normal')

    def test_draft_before_submit_can_be_saved_repeatedly(self):
        """正向对照：提交**之前**暂存可以反复保存（不能做成「一次都不能改」）。"""
        detail = self._open()
        self.ok(self._check(detail['id'], submit=False, temperature=38.5,
                            temperature_flag='normal'))
        self.ok(self._check(detail['id'], submit=False, temperature=39.0,
                            temperature_flag='normal'))
        from business.models import Treatment
        tx = Treatment.objects.get(id=detail['id'])
        self.assertEqual(tx.temperature, 39.0)
        self.assertIsNone(tx.checked_at, '暂存不应把检查单锁定')

    def test_threshold_boundary_is_fever(self):
        """边界值 39.5 恰好等于发烧线：≥ 判发烧（与前端自动判定同一条线）。"""
        self.assertEqual(st.fever_judge(39.5), 'fever')
        self.assertEqual(st.fever_judge(39.4), 'normal')
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': 39.5, 'temperature_flag': 'normal'})
        self.expect_fail(resp, 400, '矛盾')
        body = self.ok(self._check(detail['id'], temperature=39.5,
                                   temperature_flag='fever'))['data']
        self.assertEqual(body['temperature_flag'], 'fever')

    def test_draft_allows_temperature_without_flag(self):
        """暂存允许填一半：只填数值没选判定时不触发交叉校验
        （提交时两者都必填，那时才校验组合）。"""
        detail = self._open()
        body = self.ok(self._check(detail['id'], submit=False,
                                   temperature=40.2, temperature_flag=''))['data']
        self.assertEqual(body['temperature'], 40.2)

    def test_draft_rejects_contradictory_pair(self):
        """暂存也拦矛盾组合 —— 暂存不是「存错的豁免」，矛盾数据一旦落库，
        看板/打印都会展示出来。"""
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'temperature': 40.2, 'temperature_flag': 'normal',
                               'draft': True})
        self.expect_fail(resp, 400, '矛盾')

    def test_stale_contradiction_blocks_later_save(self):
        """存量矛盾单（修复前落库的旧数据）在任何一次保存时被拦下来
        逼着修正 —— 交叉校验对**最终组合**生效，而不是只看本次传没传体温。
        前端每次保存都显式带体温字段，浏览器路径行为一致。"""
        detail = self._open()
        stale = Treatment.objects.get(id=detail['id'])
        stale.temperature = 40.2
        stale.temperature_flag = 'normal'
        stale.save(update_fields=['temperature', 'temperature_flag'])
        resp = self.post_json(f'{URL}{detail["id"]}/check/',
                              {'check_note': '精神可'}, )
        self.expect_fail(resp, 400, '矛盾')

    def test_common_findings_conflict_rejected(self):
        """「无异常」与其它项互斥 —— 前端 chips 拦不住时服务端必须拦。"""
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'common_findings': ['无异常', '有跳蚤']})
        self.expect_fail(resp, 400, '不能与其它')

    def test_common_findings_saved_as_snapshot(self):
        detail = self._open()
        body = self.ok(self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'common_findings': ['体表寄生虫', '有跳蚤']}))['data']
        self.assertEqual(body['commonFindings'], '体表寄生虫、有跳蚤')
        self.assertEqual(body['commonFindingsList'], ['体表寄生虫', '有跳蚤'])

    def test_infectious_test_of_wrong_species_rejected(self):
        """猫的病种传给狗必须 400 —— 否则会存下一个界面上渲染不出来的键。"""
        dog = make_pet(status='in_treatment', district=self.district_a,
                       hospital=self.hospital_a, species='狗')
        detail = self._open(dog)
        resp = self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'infectious_tests': {'fpv': 'neg'}})
        self.expect_fail(resp, 400, '不适用')

    def test_infectious_tests_filled_with_none(self):
        """没传的项补 `none` —— 「没做」必须显式存在，不能靠键缺失暗示。"""
        detail = self._open()
        body = self.ok(self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'infectious_tests': {'fpv': 'neg'}}))['data']
        self.assertEqual(body['infectious']['fpv'], 'neg')
        self.assertEqual(body['infectious']['fhv'], 'none')
        self.assertEqual(body['infectious']['fcv'], 'none')

    def test_base_info_written_back_to_pet(self):
        detail = self._open()
        self.ok(self.post_json(f'{URL}{detail["id"]}/check/', {
            'temperature': 38.5, 'temperature_flag': 'normal',
            'weight': '3.2', 'breed': '中华田园猫'}))
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.weight, '3.2')
        self.assertEqual(self.pet.breed, '中华田园猫')

    def test_check_rejected_after_dispose(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self._dispose(tid, 'pending_adopt')
        resp = self._check(tid)
        self.expect_fail(resp, 400, '已办结')


# ============================================================
# 第二步：医嘱与处方
# ============================================================
class PlanStepTest(FlowBase):
    def test_plan_requires_date_when_rx_present(self):
        """「时间与处方互为必填」—— 有处方没日期 = 待补全，拦住。"""
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'other', 'rx': [self.med_a.id]}])
        self.expect_fail(resp, 400, '未填预计执行时间')

    def test_item_without_rx_is_skipped(self):
        """三样全空的项目 = 跳过，不落库 —— 落库就成了「必执行」，
        会把「诊疗完成」永久卡住。"""
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        body = self.ok(self._plan(tid, [
            {'key': 'other', 'planned_date': '2026-03-01', 'rx': [self.med_a.id]},
            {'key': 'deworming', 'planned_date': '2026-03-01'},
        ]))['data']
        keys = {i['itemKey'] for i in body['items']}
        self.assertEqual(keys, {'other'})

    def test_plan_rejects_unknown_item_key(self):
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'massage', 'planned_date': '2026-03-01'}])
        self.expect_fail(resp, 400, '不合法')

    def test_plan_rejects_rx_without_hospital_stock(self):
        """本院没库存的药不能开进处方（原型「无库存 = 不可选」）。"""
        empty = make_material(category='medicine', name='阿托品注射液',
                              district=self.district_a)
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'other', 'planned_date': '2026-03-01',
                                 'rx': [empty.id]}])
        self.expect_fail(resp, 400, '库存为 0')

    def test_plan_rejects_chip_category_as_rx(self):
        """芯片不能当处方药开 —— 它走「芯片号」那条路。"""
        chip_material = make_material(category='chip', name='宠物芯片',
                                      district=self.district_a)
        make_hospital_txn(chip_material, self.hospital_a, 'receive', 5)
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'other', 'planned_date': '2026-03-01',
                                 'rx': [chip_material.id]}])
        self.expect_fail(resp, 400, '不是可开处方的物资')

    def test_plan_rejects_malformed_rx_id(self):
        """`rx` 元素畸形必须报 400 并指出字段，不能是 500。

        这条判据原来钉在旧「快速登记」接口的嵌套 `material_id` 上
        （`test_nested_material_id_malformed_is_rejected`）：`{"rx": [[1,2,3]]}`
        会让 `Material.objects.filter(pk=[1,2,3])` 抛 `TypeError`
        （字符串抛 `ValueError`、超长数字串把 MySQL 的 out-of-range 一路带出来）。
        接口下线后同类参数换了个位置，判据必须跟着搬过来。
        """
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        for value in ([[1, 2, 3]], ['abc'], [{'a': 1}], [True],
                      ['999999999999999999999999'], [-1]):
            with self.subTest(rx=value):
                resp = self._plan(tid, [{'key': 'other',
                                         'planned_date': '2026-03-01',
                                         'rx': value}])
                self.assertEqual(resp.status_code, 400,
                                 '畸形 rx 应该是 400，实际 %s：%r'
                                 % (resp.status_code, resp.content[:200]))
                self.assertIn('处方药品ID', resp.json().get('message', ''),
                              '文案要指向具体字段，实际：%r'
                              % resp.json().get('message'))

        # 反向对照：正常数字 id 仍然能开方（否则上面的拒绝就是「一律拒绝」）
        resp = self._plan(tid, [{'key': 'other', 'planned_date': '2026-03-01',
                                 'rx': [self.med_a.id]}])
        self.assertEqual(resp.status_code, 200, resp.content[:200])

    def test_plan_rejected_after_diagnosed(self):
        """已诊断必须先「撤回诊断」才能改 —— 否则改完处方再执行，
        库存按新处方扣，账实不符且无痕迹。"""
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)
        resp = self._plan(tid, [{'key': 'other', 'planned_date': '2026-03-01',
                                 'rx': [self.med_a.id]}])
        self.expect_fail(resp, 400, '撤回诊断')

    def test_plan_syncs_legacy_item_flags(self):
        """打印页 / 监管统计 / 政府端列表读的是旧 `items_*` 四个布尔。"""
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        self.ok(self._plan(tid, [
            {'key': 'vaccine', 'planned_date': '2026-03-01', 'rx': [self.vaccine.id]},
            {'key': 'other', 'planned_date': '2026-03-01', 'rx': [self.med_a.id]},
        ]))
        treatment = Treatment.objects.get(id=tid)
        self.assertTrue(treatment.items_vaccine)
        self.assertFalse(treatment.items_sterilization)
        self.assertFalse(treatment.items_chip)
        # `other` 没有对应的旧列，不该被误映射到某一列上
        self.assertFalse(treatment.items_deworming)

    def test_chip_item_requires_chip_no(self):
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        resp = self._plan(tid, [{'key': 'chip', 'planned_date': '2026-03-01'}])
        self.expect_fail(resp, 400, '必须填写芯片号')


class DiagnoseTest(FlowBase):
    def test_diagnose_requires_advice_or_items(self):
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        self.ok(self._plan(tid, [], advice=''))
        resp = self.post_json(f'{URL}{tid}/diagnose/', {})
        self.expect_fail(resp, 400, '至少填写医嘱')

    def test_diagnose_requires_check_submitted(self):
        detail = self._open()
        resp = self.post_json(f'{URL}{detail["id"]}/diagnose/', {})
        self.expect_fail(resp, 400, '诊疗前检查')

    def test_diagnose_sets_timestamp(self):
        detail = self._open()
        tid = detail['id']
        body = self._diagnosed(tid)
        self.assertTrue(body['diagnosedAt'])

    def test_undiagnose_clears(self):
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)
        body = self.ok(self.post_json(f'{URL}{tid}/undiagnose/', {}))['data']
        self.assertFalse(body['diagnosedAt'])

    def test_undiagnose_blocked_when_item_executed(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        resp = self.post_json(f'{URL}{tid}/undiagnose/', {})
        self.expect_fail(resp, 400, '先撤销执行')


# ============================================================
# 第三步：执行（扣库存）
# ============================================================
class ExecuteStepTest(FlowBase):
    def test_execute_consumes_hospital_stock(self):
        """**业务结果判据**：库存真的少了 1。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        before = self._stock(self.med_a)

        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.assertEqual(self._stock(self.med_a), before - 1)

    def test_execute_marks_item_done(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        body = self.ok(self.post_json(
            f'{URL}{tid}/items/{item_id}/execute/', {}))['data']
        self.assertTrue(body['items'][0]['isDone'])
        self.assertTrue(body['items'][0]['doneAt'])

    def test_execute_twice_rejected(self):
        """重复执行 = 重复扣库存。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {})
        self.expect_fail(resp, 400, '无需重复执行')

    def test_execute_requires_diagnose(self):
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        body = self.ok(self._plan(tid, [
            {'key': 'other', 'planned_date': '2026-03-01', 'rx': [self.med_a.id]}]))['data']
        item_id = self._first_item_id(body)
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {})
        self.expect_fail(resp, 400, '诊断完成')

    def test_insufficient_stock_leaves_nothing_behind(self):
        """两段式：第 2 条药库存不足时，第 1 条**不能**被扣。

        边校验边落库的话，第 1 条已扣、项目未完成（`is_done` 还是 False），
        用户再点一次执行 → 第 1 条被扣两次。
        """
        detail = self._open()
        tid = detail['id']
        self.ok(self._check(tid))
        self.ok(self._plan(tid, [{
            'key': 'other', 'planned_date': '2026-03-01',
            'rx': [self.med_a.id, self.med_b.id]}]))
        self.ok(self.post_json(f'{URL}{tid}/diagnose/', {}))
        item = TreatmentItem.objects.get(treatment_id=tid)
        # 处方开好之后把 med_b 抽干 —— 模拟「开方后被别处用掉」
        make_hospital_txn(self.med_b, self.hospital_a, 'consume', 20)

        before_a = self._stock(self.med_a)
        resp = self.post_json(f'{URL}{tid}/items/{item.id}/execute/', {})
        self.expect_fail(resp, 400, '库存不足')
        self.assertEqual(self._stock(self.med_a), before_a, '第一条药被扣了')

    def test_execute_chip_binds_pet_and_consumes_material(self):
        """芯片执行：绑档案 + 状态变 used + **芯片物料库存 -1**。"""
        chip_material = make_material(category='chip', name='宠物芯片',
                                      district=self.district_a)
        make_hospital_txn(chip_material, self.hospital_a, 'receive', 5)
        chip = make_chip(number='CHIPFLOW0001', institution=self.hospital_a,
                         material=chip_material)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        before = self._stock(chip_material)

        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))

        chip.refresh_from_db()
        self.pet.refresh_from_db()
        self.assertEqual(chip.status, 'used')
        self.assertEqual(chip.pet_id, self.pet.id)
        self.assertEqual(self.pet.chip_no, chip.number)
        self.assertEqual(self._stock(chip_material), before - 1)

    def test_execute_chip_of_other_hospital_rejected(self):
        other = make_institution(type='hospital', district=self.district_a,
                                 name='甲区第二医院')
        chip = make_chip(number='CHIPFLOW0002', institution=other)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {})
        self.expect_fail(resp, 400, '不属于')

    def test_execute_chip_already_used_rejected(self):
        chip = make_chip(number='CHIPFLOW0003', status='used',
                         institution=self.hospital_a)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {})
        self.expect_fail(resp, 400, '已被使用')

    def test_execute_item_of_other_treatment_404(self):
        """处置项目 id 与诊疗单不匹配时按不存在处理（不做跨单越权）。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        resp = self.post_json(f'{URL}{tid}/items/99999999/execute/', {})
        self.expect_fail(resp, 404)


# ============================================================
# 撤销执行
# ============================================================
class UndoStepTest(FlowBase):
    def test_undo_returns_stock(self):
        """**业务结果判据**：库存真的加回来了。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        before = self._stock(self.med_a)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/undo/', {}))
        self.assertEqual(self._stock(self.med_a), before)

    def test_undo_marks_item_not_done(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        body = self.ok(self.post_json(
            f'{URL}{tid}/items/{item_id}/undo/', {}))['data']
        self.assertFalse(body['items'][0]['isDone'])

    def test_undo_releases_chip(self):
        chip = make_chip(number='CHIPUNDO0001', institution=self.hospital_a)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/undo/', {}))

        chip.refresh_from_db()
        self.pet.refresh_from_db()
        self.assertEqual(chip.status, 'available')
        self.assertIsNone(chip.pet_id)
        self.assertEqual(self.pet.chip_no, '')

    def test_undo_clears_treatment_chip_fields(self):
        """撤销植入要清掉诊疗单上的芯片号/植入日期 —— `execute_item` 的对称清理。

        原实现只清 `pet.chip_no` 与 `Chip` 行，`treatment.chip_no` 原样留着：
        打印页/台账照样显示「已植入 xxx」，与动物档案（已清空）自相矛盾。
        """
        chip = make_chip(number='CHIPUNDO0003', institution=self.hospital_a)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))

        treatment = Treatment.objects.get(pk=tid)
        self.assertEqual(treatment.chip_no, chip.number)
        self.assertIsNotNone(treatment.chip_date)

        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/undo/', {}))
        treatment.refresh_from_db()
        self.assertEqual(treatment.chip_no, '')
        self.assertIsNone(treatment.chip_date)

    def test_undo_chip_rebound_to_other_pet_rejected(self):
        """芯片已被别的档案绑走时不能退回 —— 否则一颗芯片两条档案。"""
        chip = make_chip(number='CHIPUNDO0002', institution=self.hospital_a)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chip.number, 'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))

        other_pet = make_pet(status='in_treatment', district=self.district_a,
                             hospital=self.hospital_a)
        chip.pet = other_pet
        chip.save(update_fields=['pet'])

        resp = self.post_json(f'{URL}{tid}/items/{item_id}/undo/', {})
        self.expect_fail(resp, 400, '已绑定其它动物')

    def test_undo_chip_changed_after_our_read_is_not_resurrected(self):
        """芯片在「读」与「写回」之间被作废时，撤销不能把它复活。

        ⚠ 直接调私有服务函数而不是走接口：这条要盯的是**读后写前的竞态窗口**，
        顺序执行的接口用例永远撞不进去（上一那条款例已经被友好判据拦住了，
        把 CAS 变异回盲 `chip.save()` 它照样通过）。这里用一层薄壳把竞写
        注入到那次读之后。

        旧实现读完只判 `pet`，随后整行覆盖：作废（已随库存异动扣过一次）
        会被撤销改回 `available`，「可用芯片数」比库存多出一颗 ——
        `count_available_chips()` 与 `MaterialStock` 的对账当场错开，且不报错。
        """
        chip = make_chip(number='CHIPUNDO0004', institution=self.hospital_a,
                         pet=self.pet, status='used')
        self.pet.chip_no = chip.number
        self.pet.save(update_fields=['chip_no'])

        class RacingQuerySet:
            def __init__(self, qs):
                self._qs = qs

            def first(self):
                row = self._qs.first()
                # 竞手在这一瞬间走「库存异动」把这颗号作废
                Chip.objects.filter(pk=row.pk).update(status='void')
                return row

            def update(self, *args, **kwargs):
                return self._qs.update(*args, **kwargs)

        class RacingManager:
            def filter(self, *args, **kwargs):
                return RacingQuerySet(Chip.objects.filter(*args, **kwargs))

        class RacingChip:
            objects = RacingManager()

        with mock.patch.object(st, 'Chip', RacingChip):
            with self.assertRaises(ValueError) as ctx:
                st._release_chip(chip.number, self.pet)
        self.assertIn('刚刚被他人改动', str(ctx.exception))

        chip.refresh_from_db()
        self.pet.refresh_from_db()
        self.assertEqual(chip.status, 'void', '撤销不能把已作废的芯片改回可用')
        self.assertEqual(self.pet.chip_no, chip.number,
                         '退回没成功时宠物档案也不该被清空（两半同生同死）')

    def test_undo_not_executed_rejected(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        resp = self.post_json(f'{URL}{tid}/items/{item_id}/undo/', {})
        self.expect_fail(resp, 400, '尚未执行')


# ============================================================
# 执行/撤销的并发窗口（第五十二轮 CAS）
# ============================================================
class ItemClaimRaceTest(FlowBase):
    """同一张单的「执行」与「撤销」各自只能生效一次 —— 判据在 UPDATE 里。

    ⚠ 为什么必须直接调服务函数并传**两个不同的内存对象**：
    `ExecuteStepTest.test_repeat_execute_...` 那条顺序双击的用例走的是
    「第二次重新读库」，`is_done` 已经是 True，友好判据就把它拦下了 ——
    把 CAS 变异回盲 `item.save()` 它照样通过。真实的竞态是**两个请求都在
    对方写库之前读到了 `is_done=False`**（两人同开一张单、手机端卡顿重试），
    所以这里用两个独立加载的行来表示这两笔请求。

    ⚠ 断言落在库存与流水条数上，不停在报错文案：这条竞态的后果是
    「同一张处方扣两遍药、生成两条 CON 流水，两次都返回成功」，
    只有把账摊开才看得出来。

    反向对照 = 上面各条正常路径用例（另开一张单执行/撤销都照旧成功）。
    """

    def _started(self):
        """开单 → 检查 → 医嘱（一条药）→ 诊断，返回 (treatment, item_id)。"""
        detail = self._open()
        tid = detail['id']
        diagnosed = self._diagnosed(tid)      # 处置项目要读**这一步**返回的 detail
        return Treatment.objects.get(pk=tid), self._first_item_id(diagnosed)

    def test_second_execute_with_a_stale_row_consumes_once(self):
        treatment, item_id = self._started()
        before = self._stock(self.med_a)
        mine = TreatmentItem.objects.get(pk=item_id)
        rival = TreatmentItem.objects.get(pk=item_id)   # 同一瞬间的第二笔请求
        user = self.hospital_user_a

        with transaction.atomic():
            st.execute_item(treatment, mine, user)
        self.assertEqual(self._stock(self.med_a), before - 1)

        with self.assertRaises(ValueError) as ctx:
            with transaction.atomic():
                st.execute_item(treatment, rival, user)
        self.assertIn('刚刚被他人执行', str(ctx.exception))
        self.assertEqual(
            self._stock(self.med_a), before - 1,
            '抢输的一方把同一张处方又扣了一遍 —— 账实不符且不报错')
        self.assertEqual(
            MaterialTransaction.objects.filter(
                type='consume', note__contains=self.pet.code).count(), 1,
            '同一次执行生成了两条消耗流水')

    def test_second_undo_with_a_stale_row_returns_stock_once(self):
        treatment, item_id = self._started()
        before = self._stock(self.med_a)
        user = self.hospital_user_a
        with transaction.atomic():
            st.execute_item(treatment, TreatmentItem.objects.get(pk=item_id), user)
        self.assertEqual(self._stock(self.med_a), before - 1)

        mine = TreatmentItem.objects.get(pk=item_id)
        rival = TreatmentItem.objects.get(pk=item_id)   # 都读到 is_done=True

        with transaction.atomic():
            st.undo_item(treatment, mine, user)
        self.assertEqual(self._stock(self.med_a), before)

        with self.assertRaises(ValueError) as ctx:
            with transaction.atomic():
                st.undo_item(treatment, rival, user)
        self.assertIn('刚刚被他人处理', str(ctx.exception))
        self.assertEqual(
            self._stock(self.med_a), before,
            '抢输的一方把药又退了一遍 —— 库存凭空多出一倍')
        self.assertEqual(
            MaterialTransaction.objects.filter(
                type='adjustment', note__contains='撤销').count(), 1,
            '一次撤销生成了两条退回流水')

    def test_execute_failure_after_claim_rolls_the_claim_back(self):
        """抢下项目之后半截炸了，`is_done` 必须跟着回滚。

        否则单据显示「已执行」而药没扣、芯片没植 —— 用户不会再点第二次
        （界面说已经做完了），这一笔永远补不回来。

        ⚠ 这条用例顺手钉住一个**测试写法**的坑：`assertRaises` 必须套在
        `atomic()` **外面**。写在里面等于把异常吞在事务块内，块正常退出 →
        savepoint 提交 → 「回滚」判据恒假（本条第一版就是这么写的，
        于是它把「抢下项目后炸了、`is_done` 留着 True」误判成通过）。
        """
        treatment, item_id = self._started()
        before = self._stock(self.med_a)
        item = TreatmentItem.objects.get(pk=item_id)
        with mock.patch.object(st, 'adjust_stock',
                               side_effect=RuntimeError('库存服务挂了')):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    st.execute_item(treatment, item, self.hospital_user_a)

        fresh = TreatmentItem.objects.get(pk=item_id)
        self.assertFalse(fresh.is_done, '回滚后项目应仍是未执行，可以重做')
        self.assertIsNone(fresh.done_at)
        self.assertEqual(self._stock(self.med_a), before)
        # 正向对照：同一条项目随后正常执行成功
        with transaction.atomic():
            st.execute_item(treatment, TreatmentItem.objects.get(pk=item_id),
                            self.hospital_user_a)
        self.assertEqual(self._stock(self.med_a), before - 1)


# ============================================================
# 诊疗完成 / 办结
# ============================================================
class DoneAndDisposeTest(FlowBase):
    def test_done_requires_all_must_items(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        resp = self.post_json(f'{URL}{tid}/done/', {})
        self.expect_fail(resp, 400, '未完成')

    def test_done_after_execute(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        body = self.ok(self.post_json(f'{URL}{tid}/done/', {}))['data']
        self.assertEqual(body['stage'], 'done')
        self.assertTrue(body['doneAt'])
        self.assertFalse(body['isDisposed'])

    def test_dispose_requires_done(self):
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)
        with self.assertRaisesMessage(ValueError, '诊疗完成'):
            self._dispose(tid, 'pending_adopt')

    def test_done_records_advice_without_disposing(self):
        """「去向建议」只记建议，**不能**碰 `disposal` / 动物状态。

        反向对照：若这里顺手写了 `disposal`（= 已办结），`disposed_at` 一到
        动物进终态，六张出院单据全部 400「当前状态不可办理」，诊疗单还会印出
        「办结去向：领养」而动物明明还在院 —— 界面承诺的事没发生。
        """
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        body = self.ok(self.post_json(
            f'{URL}{tid}/done/', {'disposal_advised': 'owner_returned'}))['data']

        self.assertEqual(body['disposal_advised_label'], '主人领回')
        self.assertEqual(body['disposal_label'], '', '建议不得写成办结去向')
        self.assertFalse(body['is_disposed'])
        t = Treatment.objects.get(id=tid)
        self.assertEqual(t.disposal_advised, 'owner_returned')
        self.assertEqual(t.disposal, '')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'in_treatment')
        # 动物还在院 → 出院单据必须还开得出来；而且单据完成才办结诊疗单。
        body = self.ok(self.post_json('/api/business/owner-returns/create/', {
            'pet_id': self.pet.id, 'owner_name': '张领回'}))
        self.assertTrue(body['data']['id'])
        t.refresh_from_db()
        self.assertTrue(t.disposed_at, '出院单据完成时才落 disposed_at')
        self.assertEqual(t.disposal, 'owner_returned')

    def test_done_rejects_unknown_advice(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        resp = self.post_json(f'{URL}{tid}/done/', {'disposal_advised': 'adopt_me'})
        self.expect_fail(resp, 400, '不合法')
        t = Treatment.objects.get(id=tid)
        self.assertIsNone(t.done_at, '非法建议不得把单子推进到待出院')

    def test_board_carries_advice_for_pending_discharge(self):
        """待出院行要带出建议，下一班的人一眼知道该开哪张单。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {'disposal_advised': 'released'}))
        body = self.ok(self.get_json(URL + 'board/'))['data']
        row = next(r for r in body['pending_discharge'] if r['treatment_id'] == tid)
        self.assertEqual(row['disposal_advised'], 'released')
        self.assertEqual(row['disposal_advised_label'], '放回原小区')

    def test_dispose_changes_pet_status(self):
        """**业务结果判据**：动物的状态真的变成了所选去向。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self._dispose(tid, 'pending_adopt')

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_adopt')
        treatment = Treatment.objects.get(id=tid)
        self.assertEqual(treatment.status, 'completed')
        self.assertEqual(treatment.disposal, 'pending_adopt')
        self.assertTrue(treatment.disposed_at)

    def test_dispose_adopt_lists_pet_in_hall(self):
        """去向「领养」必须**当场**挂进领养大厅。

        旧口径靠 `auto_promote_to_adoptable()` 在 5 天后补这一步；它的判据是
        「`Treatment.status='completed'` 且 `Pet.status='in_treatment'`」，
        而新流程这两件事在同一条 UPDATE 里同时发生 —— 中间态不存在，定时任务
        永远命中不到。不在这里补，动物就是「状态待领养、大厅里查不到」的幽灵，
        而办结提示还写着「信息进入领养大厅」。
        """
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self._dispose(tid, 'pending_adopt')

        listing = AdoptionHallListing.objects.get(pet=self.pet)
        self.assertTrue(listing.is_active, '待领养的动物必须在大厅里可见')
        self.assertEqual(listing.hospital_id, self.hospital_a.id)
        self.assertIsNotNone(listing.published_at)

    def test_dispose_adopt_reactivates_existing_listing(self):
        """已有下架记录时必须显式置回 `is_active`（`get_or_create` 不会）。"""
        AdoptionHallListing.objects.create(pet=self.pet, is_active=False)
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self._dispose(tid, 'pending_adopt')

        self.assertTrue(AdoptionHallListing.objects.get(pet=self.pet).is_active)

    def test_dispose_other_destinations_do_not_list_pet(self):
        """反向对照：放归 / 安乐死等去向**不该**上架 —— 否则就是「一律上架」。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self._dispose(tid, 'released')

        self.assertFalse(AdoptionHallListing.objects.filter(pet=self.pet).exists())

    def test_dispose_dead_status_is_writable(self):
        """`dead`（死亡/无害化）是新拆出来的状态，必须真的能写出来 ——
        「枚举值存在、写入路径缺失」的判据。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self._dispose(tid, 'dead')

        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'dead')

    def test_dispose_invalid_key_rejected(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        with self.assertRaisesMessage(ValueError, '不合法'):
            self._dispose(tid, 'released_to_moon')

    def test_dispose_twice_rejected(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        self._dispose(tid, 'pending_adopt')
        with self.assertRaisesMessage(ValueError, '已办结'):
            self._dispose(tid, 'released')


class AdviceEditTest(FlowBase):
    """「建议去向」的可改窗口与锁死条件（第六十一轮补全用例）。

    出院四个去向能选到哪些动物**完全**由这张单的 `disposal_advised` 决定，
    所以业务方要的路径是「先把建议改了，再回去院选择」。反过来，动物一旦被
    出院单据接管就必须锁死 —— 那时改建议不会改动任何单据，只会造出
    「单据说领养、建议说放归」两张皮，而且两边都能走到已办结。
    """

    def _pending(self, advised='pending_adopt'):
        """推到「诊疗完成、尚未办结」（= 待出院），返回诊疗单 id。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {'disposal_advised': advised}))
        return tid

    def _give_shelter(self):
        """运回单 / 放归单要知道送回哪个捕捉点。"""
        self.pet.shelter = self.shelter_a
        self.pet.save(update_fields=['shelter'])

    def _advice(self, tid, advised):
        return self.post_json(f'{URL}{tid}/advice/', {'disposal_advised': advised})

    def test_advice_editable_while_pending_discharge(self):
        tid = self._pending('pending_adopt')
        done_at = Treatment.objects.get(id=tid).done_at
        body = self.ok(self._advice(tid, 'released'))['data']

        self.assertEqual(body['disposal_advised'], 'released')
        self.assertEqual(body['disposal_advised_label'], '放回原小区')
        t = Treatment.objects.get(id=tid)
        self.assertEqual(t.disposal, '', '改建议不等于办结')
        self.assertIsNone(t.disposed_at)
        # 「诊疗完成」的时刻是既成事实：重存建议把它改成现在，
        # 待出院时长会凭空归零，看板与统计跟着骗人。
        self.assertEqual(t.done_at, done_at)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'in_treatment')

    def test_advice_edit_re_routes_discharge_candidates(self):
        """改完建议，出院候选**立刻**跟着分流 —— 用户要的就是这条闭环。"""
        self._give_shelter()
        tid = self._pending('pending_adopt')
        self.expect_fail(self.post_json(
            '/api/business/return-orders/create/', {'pet_ids': [self.pet.id]}),
            message='不能按「放归」办理出院')
        self.ok(self._advice(tid, 'released'))
        self.ok(self.post_json(
            '/api/business/return-orders/create/', {'pet_ids': [self.pet.id]}))

    def test_advice_cannot_be_cleared(self):
        tid = self._pending('released')
        self.expect_fail(self._advice(tid, ''), message='不合法')
        self.assertEqual(Treatment.objects.get(id=tid).disposal_advised,
                         'released', '被拒的清空不得改动建议')

    def test_advice_needs_done_first(self):
        """还没「诊疗完成」的单没有可改窗口：建议要随「诊疗完成」一起提交。

        放开的话，「先随便填个建议 → 改口 → 再点完成」会绕掉完成那一刻的
        必执行项校验，看板上的待出院时长也会从一张没做完的单上凭空出现。
        """
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)
        self.expect_fail(self._advice(tid, 'released'),
                         message='请先点击「诊疗完成」')
        self.assertIsNone(Treatment.objects.get(id=tid).done_at)

    def test_advice_locked_while_return_order_pending(self):
        """动物已在未办结的运回单上 → 建议锁死；运回单撤回后自动放行。"""
        self._give_shelter()
        tid = self._pending('released')
        order = self.ok(self.post_json(
            '/api/business/return-orders/create/', {'pet_ids': [self.pet.id]}))['data']
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'return_pending')

        self.expect_fail(self._advice(tid, 'pending_adopt'),
                         message='已经在出院单据流程里')
        self.assertEqual(Treatment.objects.get(id=tid).disposal_advised,
                         'released', '拒绝不得把建议改掉')

        # ⚠ 单据还在办、档案状态却被别的流程改过（存量行真出现过这种形状）。
        # 只信 `Pet.status` 的话，这只动物会变成「运回单承诺放归、建议却改成
        # 领养」—— 两张出院路径同时认领同一只动物。
        Pet.objects.filter(pk=self.pet.pk).update(status='in_treatment')
        self.expect_fail(self._advice(tid, 'pending_adopt'),
                         message='未办结的运回单')

        self.ok(self.post_json(f'/api/business/return-orders/{order["id"]}/withdraw/', {}))
        self.ok(self._advice(tid, 'pending_adopt'))

    def test_advice_locked_after_treatment_closed(self):
        tid = self._pending('pending_adopt')
        self._dispose(tid, 'pending_adopt')
        self.expect_fail(self._advice(tid, 'released'), message='本单已办结')

    def test_advice_locked_when_pet_already_discharged(self):
        """钉死第五十七轮的缺口：`done_at` 已落、`disposed_at` 空，但动物已是终态。

        「没点诊疗完成就先领养出库」会留下这种旧单：看板把它算作待出院、
        行上还能打开流程。不拦住的话，等于给一只**已被领养**的猫重新建议去向。
        """
        tid = self._pending('pending_adopt')
        Pet.objects.filter(pk=self.pet.pk).update(status='adopted')
        self.expect_fail(self._advice(tid, 'released'), message='动物已出院')
        self.assertEqual(Treatment.objects.get(id=tid).disposal_advised,
                         'pending_adopt', '终态动物的建议必须原样保留')

    def test_mark_done_refused_for_discharged_pet(self):
        """同样的形状要挡在「补记诊疗完成」上 —— 否则先补一个待出院再改建议。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        self.ok(self.post_json(
            f'{URL}{tid}/items/{self._first_item_id(detail)}/execute/', {}))
        Pet.objects.filter(pk=self.pet.pk).update(status='adopted')
        self.expect_fail(self.post_json(
            f'{URL}{tid}/done/', {'disposal_advised': 'released'}),
            message='动物已出院')
        self.assertIsNone(Treatment.objects.get(id=tid).done_at,
                         '拒绝不得把动物打回待出院')

    def test_non_hospital_cannot_edit_advice(self):
        tid = self._pending('released')
        self.login_as(self.shelter_user_a)
        self.assertEqual(self._advice(tid, 'pending_adopt').status_code, 403)


# ============================================================
# 看板 / 列表
# ============================================================
class BoardTest(FlowBase):
    """看板分组（第四十八轮：三态 → 五态）。

    旧口径只有 pending / treating / done 三个桶，把「待诊断」和「诊疗中」
    合并成 treating、把「待出院」和「已出院」合并成 done —— 界面上看不出
    「下一步该点哪个按钮」，也分不清「还在治」和「治完了等人接走」。
    """

    def test_board_lists_unopened_pets_as_checking(self):
        """还没开单的在院动物必须出现在「待接诊」—— 不出现 = 接不了诊。"""
        body = self.ok(self.get_json(URL + 'board/'))['data']
        codes = [r['petCode'] for r in body['checking']]
        self.assertIn(self.pet.code, codes)
        self.assertEqual(body['counts']['checking'], len(body['checking']))

    def test_board_groups_by_stage(self):
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)

        body = self.ok(self.get_json(URL + 'board/'))['data']
        self.assertEqual([r['petCode'] for r in body['treating']], [self.pet.code])
        self.assertNotIn(self.pet.code, [r['petCode'] for r in body['checking']])

    def test_board_marks_disposed(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        body = self.ok(self.get_json(URL + 'board/'))['data']
        row = [r for r in body['pending_discharge'] if r['petCode'] == self.pet.code][0]
        self.assertFalse(row['isDisposed'])

        self._dispose(tid, 'released')
        body = self.ok(self.get_json(URL + 'board/'))['data']
        row = [r for r in body['discharged'] if r['petCode'] == self.pet.code][0]
        self.assertTrue(row['isDisposed'])
        self.assertEqual(row['disposal'], 'released')
        # 办结后必须**离开**待出院桶，否则同一只动物同时挂在两个页签里
        self.assertNotIn(self.pet.code,
                         [r['petCode'] for r in body['pending_discharge']])

    def test_board_isolates_hospitals(self):
        """别院的动物不出现在本院看板上（双院同区县，靠机构隔离）。"""
        other = make_institution(type='hospital', district=self.district_a,
                                 name='甲区第二医院')
        make_pet(status='in_treatment', district=self.district_a, hospital=other)
        body = self.ok(self.get_json(URL + 'board/'))['data']
        codes = [r['petCode'] for r in body['checking']]
        self.assertIn(self.pet.code, codes)
        self.assertEqual(len(codes), 1)


class ListStageFilterTest(FlowBase):
    def test_stage_filter(self):
        detail = self._open()
        tid = detail['id']
        self._diagnosed(tid)
        body = self.ok(self.get_json(f'{URL}?stage=treating'))['data']
        self.assertEqual([r['id'] for r in body], [tid])
        body = self.ok(self.get_json(f'{URL}?stage=checking'))['data']
        self.assertEqual(body, [])

    def test_stage_filter_and_property_agree(self):
        """`?stage=` 的判据必须与 `Treatment.stage` 属性同源（`STAGE_FILTERS` 一张表）。

        两处各写一遍时间戳组合时，表现是「按阶段筛出 3 条，点开其中一条显示另一个阶段」。
        这里造四种时间戳组合各一张单，逐个阶段码比对「属性说的」与「接口筛出的」。
        """
        from django.utils import timezone
        now = timezone.now()
        combos = [{}, {'checked_at': now},
                  {'checked_at': now, 'diagnosed_at': now},
                  {'checked_at': now, 'diagnosed_at': now, 'done_at': now}]
        rows = []
        for ts in combos:
            pet = make_pet(status='in_treatment', district=self.district_a,
                           hospital=self.hospital_a, species='猫')
            t = Treatment.objects.get(id=self._open(pet=pet)['id'])
            for field, value in ts.items():
                setattr(t, field, value)
            t.save(update_fields=list(ts) or None)
            rows.append(t)

        for code in Treatment.STAGE_FILTERS:
            with self.subTest(stage=code):
                expect = sorted(t.id for t in rows if t.stage == code)
                got = sorted(r['id'] for r in
                             self.ok(self.get_json(f'{URL}?stage={code}'))['data'])
                self.assertEqual(
                    got, expect,
                    f'?stage={code} 接口筛出 {got}，但 Treatment.stage 属性说是 {expect}')

    def test_unknown_stage_rejected(self):
        """非法筛选值必须 4xx，**不能静默忽略**（静默忽略 = 用户以为筛了）。"""
        resp = self.get_json(f'{URL}?stage=whatever')
        self.expect_fail(resp, 400)


# ============================================================
# 权限 / 隔离
# ============================================================
class PermissionTest(FlowBase):
    def test_shelter_cannot_write(self):
        detail = self._open()
        tid = detail['id']
        self.login_as(self.shelter_user_a)
        resp = self.post_json(f'{URL}{tid}/check/',
                              {'temperature': 38.5, 'temperature_flag': 'normal'})
        self.assertEqual(resp.status_code, 403)

    def test_gov_cannot_write(self):
        detail = self._open()
        tid = detail['id']
        self.login_as(self.gov_a)
        resp = self.post_json(f'{URL}{tid}/diagnose/', {})
        self.assertEqual(resp.status_code, 403)

    def test_other_hospital_cannot_write(self):
        """同区县的第二家医院拿到诊疗单 id 也必须 404（不是 403）。"""
        detail = self._open()
        tid = detail['id']
        other = make_institution(type='hospital', district=self.district_a,
                                 name='甲区第二医院')
        other_user = make_user('hospital_second_t', role='hospital',
                               district=self.district_a, institution=other)
        self.login_as(other_user)
        resp = self.post_json(f'{URL}{tid}/diagnose/', {})
        self.expect_fail(resp, 404)

    def test_government_can_read_detail(self):
        detail = self._open()
        tid = detail['id']
        self.login_as(self.gov_a)
        body = self.ok(self.get_json(f'{URL}{tid}/'))['data']
        self.assertEqual(body['id'], tid)


class TreatmentPhotoTest(FlowBase):
    """诊疗后照片（B2）：第三步办结环节的当场留证。

    判据落在业务结果上：照片真落 `Pet.photo_treatment`、领养大厅**没有**
    被顺带创建 —— 若复用领养 edit-info 接口，给「放归 / 无害化」动物传
    照片会意外挂进领养大厅且不报错（见 `treatment_photo` 视图注释）。
    """

    def _png(self, name='after.png'):
        """一张真正能被 Pillow 解码的 PNG。"""
        buf = io.BytesIO()
        Image.new('RGB', (8, 8), (90, 130, 170)).save(buf, 'PNG')
        return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')

    def _fake(self, name='fake.png'):
        """扩展名伪装成 png 的文本：按文件名 / Content-Type 判断都会放行。"""
        return SimpleUploadedFile(name, b'definitely not an image',
                                  content_type='image/png')

    def test_upload_saves_photo_without_listing(self):
        """正常路径：照片落档案字段，且**不**创建领养展示位。"""
        detail = self._open()
        resp = self.client.post(f'{URL}{detail["id"]}/photo/',
                                {'photo': self._png()})
        body = self.ok(resp)
        self.pet.refresh_from_db()
        self.assertTrue(self.pet.photo_treatment)
        # 出参是整份 detail —— 前端照片回显靠 pet.photo_treatment。
        self.assertTrue(body['data']['pet']['photo_treatment'])
        # ⚠ 副作用防护：独立接口不得顺带把动物挂进领养大厅。
        self.assertEqual(
            AdoptionHallListing.objects.filter(pet=self.pet).count(), 0)

    def test_upload_replaces_photo(self):
        """再传一张 = 更换（覆盖同一字段），不是叠加。"""
        detail = self._open()
        self.ok(self.client.post(f'{URL}{detail["id"]}/photo/',
                                 {'photo': self._png('a.png')}))
        first = Pet.objects.get(id=self.pet.id).photo_treatment.name
        self.ok(self.client.post(f'{URL}{detail["id"]}/photo/',
                                 {'photo': self._png('b.png')}))
        second = Pet.objects.get(id=self.pet.id).photo_treatment.name
        self.assertNotEqual(first, second)

    def test_non_image_rejected_and_untouched(self):
        """假图片必须拦，且档案字段一个字节都不能动（先校验后落库）。"""
        detail = self._open()
        resp = self.client.post(f'{URL}{detail["id"]}/photo/',
                                {'photo': self._fake()})
        self.expect_fail(resp, 400, '不是有效的图片文件')
        self.pet.refresh_from_db()
        self.assertFalse(self.pet.photo_treatment)

    def test_missing_file_rejected(self):
        detail = self._open()
        resp = self.client.post(f'{URL}{detail["id"]}/photo/', {})
        self.expect_fail(resp, 400, '请选择')

    def test_other_hospital_gets_404(self):
        """他院拿到诊疗单 id 也必须 404（与 `_own_treatment` 口径一致，
        「存在但不属于本院」与「不存在」不可区分）。"""
        detail = self._open()
        other = make_institution(type='hospital', district=self.district_a,
                                 name='甲区照像测试医院')
        other_user = make_user('hospital_photo_t', role='hospital',
                               district=self.district_a, institution=other)
        self.login_as(other_user)
        resp = self.client.post(f'{URL}{detail["id"]}/photo/',
                                {'photo': self._png()})
        self.expect_fail(resp, 404)
        self.pet.refresh_from_db()
        self.assertFalse(self.pet.photo_treatment)

    def test_disposed_rejects_replacement(self):
        """办结后照片锁定（与检查单 `disposed_at` 拦截口径一致）。"""
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{detail["id"]}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{detail["id"]}/done/', {}))
        self._dispose(detail['id'], 'released')
        resp = self.client.post(f'{URL}{detail["id"]}/photo/',
                                {'photo': self._png()})
        self.expect_fail(resp, 400, '已办结')
        self.pet.refresh_from_db()
        self.assertFalse(self.pet.photo_treatment)


class ChipStockReconcileTest(FlowBase):
    def test_available_chip_count_matches_material_stock(self):
        """芯片执行后，「可用芯片数」与「芯片物料库存」必须**同向变化**。

        第四十六轮重构的目标就是这两者恒等 —— 只动一边（只扣库存不动芯片、
        或只动芯片不扣库存）都会让它们悄悄错开，而且不报错。
        """
        chip_material = make_material(category='chip', name='对账芯片',
                                      district=self.district_a)
        make_hospital_txn(chip_material, self.hospital_a, 'receive', 3)
        chips = [make_chip(number=f'CHIPRECON{i:04d}',
                           institution=self.hospital_a, material=chip_material)
                 for i in range(3)]

        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid, [{
            'key': 'chip', 'chip_no': chips[0].number,
            'planned_date': '2026-03-01'}])
        item_id = self._first_item_id(detail)

        before_chips = Chip.objects.filter(
            material=chip_material, institution=self.hospital_a,
            status='available').count()
        before_stock = self._stock(chip_material)
        self.assertEqual(before_chips, before_stock)

        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))

        after_chips = Chip.objects.filter(
            material=chip_material, institution=self.hospital_a,
            status='available').count()
        self.assertEqual(after_chips, before_chips - 1)
        self.assertEqual(self._stock(chip_material), before_stock - 1)
        self.assertEqual(after_chips, self._stock(chip_material))


# ============================================================
# 五态看板 + 办结不改动物状态（第四十八轮）
# ============================================================
class BoardStageTest(FlowBase):
    """诊疗五态：**全部由时间戳派生**，不存列。

    存列必然有一处写入路径忘了同步（比如「撤回诊断」只改了 `diagnosed_at`、
    忘了把状态列改回去），界面上的状态与真实进度脱节，而且不报错。
    这里把五个分支和优先级都钉住。
    """

    def test_stage_derived_from_timestamps(self):
        from django.utils import timezone
        from business import views_treatment

        now = timezone.now()
        cases = [
            ({}, 'checking', '无 checked_at → 待接诊'),
            ({'checked_at': now}, 'pending_diagnose', '有检查无诊断 → 待诊断'),
            ({'checked_at': now, 'diagnosed_at': now},
             'treating', '有诊断无完成 → 诊疗中'),
            ({'checked_at': now, 'diagnosed_at': now, 'done_at': now},
             'pending_discharge', '有完成无办结 → 待出院'),
            ({'checked_at': now, 'diagnosed_at': now, 'done_at': now,
              'disposed_at': now}, 'discharged', '有办结 → 已出院'),
        ]
        for ts, expect, why in cases:
            with self.subTest(why=why):
                self.assertEqual(
                    views_treatment._board_stage(Treatment(**ts)), expect, why)
        # 判据返回的五个取值必须就是 `BOARD_STAGES` 那五个 —— 少一个，
        # 看板就没有对应的桶（动物在界面上整列消失）
        self.assertEqual({c[1] for c in cases}, set(Treatment.BOARD_STAGES),
                         '五态判据用例与 Treatment.BOARD_STAGES 不同源')

    def test_board_buckets_follow_stage(self):
        """看板分组必须与 `_board_stage` 同口径 —— 两处各算一遍必然漂移。"""
        detail = self._open()
        self.ok(self._check(detail['id']))
        board = self.ok(self.get_json(URL + 'board/'))['data']
        codes = {k: [r['pet_code'] for r in v]
                 for k, v in board.items() if k != 'counts'}
        self.assertIn(self.pet.code, codes['pending_diagnose'])
        self.assertNotIn(self.pet.code, codes['checking'],
                         '已提交检查单的动物不能还留在「待接诊」')
        self.assertEqual(board['counts']['pending_diagnose'],
                         len(codes['pending_diagnose']))
        # 五个桶必须**同时存在**：少一个键，前端那个页签就永远空白
        for k in Treatment.BOARD_STAGES:
            self.assertIn(k, board, f'看板缺 {k} 桶')


class DisposeDeferStatusTest(FlowBase):
    """出院流程是「先开单据、再办结诊疗单」，动物状态必须由**单据**接管。

    先办结会立刻把动物改成终态，后面所有单据接口都会 400
    （它们都要求动物还在院）。所以办结时必须传 `defer_pet_status: true`。
    """

    def _to_done(self):
        detail = self._open()
        tid = detail['id']
        detail = self._diagnosed(tid)
        item_id = self._first_item_id(detail)
        self.ok(self.post_json(f'{URL}{tid}/items/{item_id}/execute/', {}))
        self.ok(self.post_json(f'{URL}{tid}/done/', {}))
        return tid

    def test_defer_keeps_pet_status(self):
        tid = self._to_done()
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'in_treatment', '前置条件：还在院')
        self._dispose(tid, 'pending_adopt', defer_pet_status=True)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'in_treatment',
                         'defer 时动物状态交给单据，办结不能改')

    def test_without_defer_writes_pet_status(self):
        """正向对照：不传 defer 时**确实**会改。

        没有这条，把 `if not defer_pet_status:` 整段删掉（永不写状态）
        也能让上面那条通过 —— 那就是假绿。
        """
        tid = self._to_done()
        self._dispose(tid, 'pending_adopt')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.status, 'pending_adopt')
