"""第四十五轮：市级捕捉点 + 机构库存（`MaterialStock`）回归套件。

本文件盯四件事，每件都对应一条**已确认口径**：

1. **二级下发正向链路**（Q4）：市级捕捉点采购 → 下发给区县级捕捉点 →
   区县级签收 → 区县级再下发给本区县医院 → 医院签收。
   每一步都断言**库存真的动了**，而不是只看状态码 ——
   「接口 200 但两边库存都没变」是本项目实测过的一类缺陷。
2. **市级跨区直发**（Q5 正向对照）：市级捕捉点可以直接把药发到**任何**区县的医院。
3. **两条反向对照**：
   - Q6：区县级捕捉点**不能**跨区县下发（必须 400），但本区县可以（正向对照）；
   - Q2：市级捕捉点登记的捕捉单，归属区县 = **实际捕捉区县**，不是「全市」。
4. **对账**：`MaterialStock.quantity == opening_quantity + 按机构归属的流水合计`，
   逐对相等；且 `check_data_integrity` 对这批正常数据**不误报**。

⚠ 夹具形态必须与生产一致：生产里**所有捕捉点操作员的 `district` 都是
「全市（市级）」**（`seed_data` 就是这么建的）。`BusinessTestBase` 的
`shelter_user_a` 挂的是具体区县 —— 那是生产上不存在的形态，用它测
「市级捕捉点」会把整条市级分支漏掉。所以本文件自带一套夹具。
"""
from io import StringIO

from django.core.management import call_command

from business.models import MaterialStock, MaterialTransaction
from business.services import (
    get_institution_stock, get_shelter_stock, is_city_shelter, stock_delta,
)
from business.tests.base import (
    BusinessTestBase, make_institution, make_material, make_user,
)

URL = '/api/business/materials/'


class MaterialStockTestBase(BusinessTestBase):
    """在公共夹具之上加「市级捕捉点」这一套（第四十五轮的主角）。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # 市级捕捉点：机构挂在「全市（市级）」下 —— `is_city_shelter()` 的**唯一判据**
        cls.city_shelter = make_institution(
            type='shelter', district=cls.city, name='市级捕捉点')
        # 操作员区县同样是市级，与生产一致（账号区县**不是**判据，这里只是照实还原）
        cls.city_shelter_user = make_user(
            'city_shelter_t', role='shelter', district=cls.city,
            institution=cls.city_shelter)

    # ---------- 共用断言 ----------

    def assert_stock_matches_ledger(self, msg=''):
        """机构库存 == 期初 + 该机构名下流水合计（逐行，不留任何一行例外）。

        这条等式是「库存表」与「流水表」两个真源的**唯一**粘合剂：
        只断言某一笔业务后的数字，漏掉的是「两半里有一半没写」这类缺陷
        （界面数字对得上，但对账永远差一点）。
        """
        checked = 0
        for row in MaterialStock.objects.select_related('material', 'institution'):
            # ⚠ 必须**按类型逐条**求和（`stock_delta` 带方向），不能写
            # `aggregate(Sum('quantity'))` 再判方向：同一机构同一物料可能既有进
            # 又有出，SQL 直接 Sum 会把「进 10、出 4」算成 14。
            # `stock_delta` 是方向真源（进/出各一个常量元组），这里不另写一份
            # `if type in (...)` —— 那就是孪生漂移的起点。
            signed = 0
            for t in MaterialTransaction.objects.filter(
                    material=row.material, institution=row.institution):
                signed += stock_delta(t.type, t.quantity)
            self.assertEqual(
                row.quantity, row.opening_quantity + signed,
                f'{msg}机构库存与流水对不上：{row.institution.name} / '
                f'{row.material.name} 行库存 {row.quantity}，'
                f'期初 {row.opening_quantity} + 流水 {signed}')
            checked += 1
        self.assertGreater(checked, 0, '一行机构库存都没有 —— 用例没跑到被测路径')

    def _receive(self, txn_id):
        return self.post_json(f'{URL}{txn_id}/receive/', {})


class IsCityShelterTest(MaterialStockTestBase):
    """判据本身：`is_city_shelter()` 只看机构类型 + 机构区县是否市级。"""

    def test_only_city_level_shelter_counts(self):
        self.assertTrue(is_city_shelter(self.city_shelter))
        self.assertFalse(is_city_shelter(self.shelter_a))
        self.assertFalse(is_city_shelter(self.hospital_a))
        self.assertFalse(is_city_shelter(None))

    def test_account_district_is_not_the_judgement(self):
        """⚠ 反例守卫：账号区县是市级**不等于**市级捕捉点。

        判据若写成「操作员区县是不是市级」，生产里**每一个**捕捉点操作员
        （区县级的也是）都会被判成市级，于是区县级捕捉点也能跨区县下发 ——
        Q6 直接被击穿，且界面上看不出任何区别。
        """
        fake = make_user('fake_city_scope_t', role='shelter',
                         district=self.city, institution=self.shelter_a)
        self.assertTrue(fake.district.is_city)          # 账号区县确实是市级
        self.assertFalse(is_city_shelter(fake.institution))   # 但它不是市级捕捉点


class TwoLevelDispatchTest(MaterialStockTestBase):
    """Q4：市级 → 区县级捕捉点 → 本区县医院，两级下发全程。"""

    def test_full_two_level_chain(self):
        material = make_material(district=self.city, shelter_stock=0,
                                 name='市级采购疫苗')

        # ---- 1) 市级捕捉点采购 100 ----
        self.login_as(self.city_shelter_user)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 100, 'supplier': '国药',
        }))
        self.assertEqual(get_institution_stock(material, self.city_shelter), 100)

        # ---- 2) 下发给甲区捕捉点 40：发起方**建单即减**，接收方**签收才加** ----
        body = self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'quantity': 40,
            'to_shelter_id': self.shelter_a.id,
        }))
        txn_id = body['data']['id']
        self.assertEqual(get_institution_stock(material, self.city_shelter), 60)
        self.assertEqual(
            get_institution_stock(material, self.shelter_a), 0,
            '接收方**签收前**不该有库存 —— 否则这批货会同时算在两边（一进一出）')

        # ---- 3) 甲区捕捉点签收 40 ----
        self.login_as(self.shelter_user_a)
        self.ok(self._receive(txn_id))
        self.assertEqual(get_institution_stock(material, self.shelter_a), 40)
        self.assertEqual(
            get_institution_stock(material, self.city_shelter), 60,
            '签收不该动发起方的库存（那 40 已经在上一步扣走了）')

        # ---- 4) 甲区捕捉点二级下发 10 给本区县医院 ----
        body = self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'quantity': 10,
            'hospital_id': self.hospital_a.id,
        }))
        self.assertEqual(get_institution_stock(material, self.shelter_a), 30)

        # ---- 5) 医院签收 10 ----
        self.login_as(self.hospital_user_a)
        self.ok(self._receive(body['data']['id']))
        self.assertEqual(get_institution_stock(material, self.hospital_a), 10)

        # 每一步都对账：两个真源必须严丝合缝
        self.assert_stock_matches_ledger('二级下发链路：')

    def test_pending_dispatch_visible_to_receiver_only(self):
        """待签收可见性：`hospital` 记的是**接收方**，发起方不该在自己的待签收里看到它。"""
        material = make_material(district=self.city, shelter_stock=0)
        self.login_as(self.city_shelter_user)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 50}))
        body = self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'quantity': 20,
            'to_shelter_id': self.shelter_a.id}))

        # 接收方（甲区捕捉点）能看到这条待签收单
        self.login_as(self.shelter_user_a)
        rows = self.ok(self.get_json(f'{URL}transactions/'))['data']
        mine = [t for t in rows
                if t['id'] == body['data']['id'] and t.get('hospital') == self.shelter_a.id]
        self.assertEqual(len(mine), 1, '接收方看不到发给自己的待签收单 —— 货到了签不了收')
        self.assertNotIn('[已签收]', mine[0].get('note') or '')

        # 发起方那边它是一条**自己发出的**单（`institution` = 发起方）
        self.login_as(self.city_shelter_user)
        rows = self.ok(self.get_json(f'{URL}transactions/'))['data']
        sent = next(t for t in rows if t['id'] == body['data']['id'])
        self.assertEqual(sent.get('institution'), self.city_shelter.id)

    def test_signed_dispatch_cannot_be_received_twice(self):
        """签收是幂等的反面：同一条单不能签两次（否则库存凭空翻倍）。"""
        material = make_material(district=self.city, shelter_stock=0)
        self.login_as(self.city_shelter_user)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 30}))
        body = self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'quantity': 10,
            'to_shelter_id': self.shelter_a.id}))

        self.login_as(self.shelter_user_a)
        self.ok(self._receive(body['data']['id']))
        self.assertEqual(get_institution_stock(material, self.shelter_a), 10)
        # 第二次必须失败，且库存不变
        self.expect_fail(self._receive(body['data']['id']))
        self.assertEqual(get_institution_stock(material, self.shelter_a), 10)
        self.assert_stock_matches_ledger('重复签收：')


class CityShelterCrossDistrictTest(MaterialStockTestBase):
    """Q5 正向 / Q6 反向，成对出现。"""

    def test_city_shelter_can_dispatch_to_any_district_hospital(self):
        material = make_material(district=self.city, shelter_stock=0)
        self.login_as(self.city_shelter_user)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 50}))

        for hospital in (self.hospital_a, self.hospital_b):
            self.ok(self.post_json(f'{URL}dispatch/', {
                'material_id': material.id, 'quantity': 5,
                'hospital_id': hospital.id}))
        self.assertEqual(get_institution_stock(material, self.city_shelter), 40)

    def test_city_shelter_can_dispatch_to_any_district_shelter(self):
        material = make_material(district=self.city, shelter_stock=0)
        self.login_as(self.city_shelter_user)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 50}))
        self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'quantity': 5,
            'to_shelter_id': self.shelter_b.id}))
        self.assertEqual(get_institution_stock(material, self.city_shelter), 45)

    def test_district_shelter_cannot_cross_district(self):
        """Q6 反向：区县级捕捉点只能发本区县；跨区县必须 400。

        ⚠ 两条断言必须**成对**：只测「跨区县被拒」的话，把判据写成
        「一律拒绝」也能通过 —— 那会把 Q4 的二级下发一起掐死。
        """
        material = make_material(district=self.district_a, shelter_stock=100)
        self.login_as(self.shelter_user_a)

        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'quantity': 1,
            'hospital_id': self.hospital_b.id}), message='只能下发到本区县医院')
        self.expect_fail(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'quantity': 1,
            'to_shelter_id': self.shelter_b.id}), message='只能下发到本区县捕捉点')

        # 正向对照：本区县医院**可以**（否则「一律禁止」也能骗过上面两条）
        self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'quantity': 1,
            'hospital_id': self.hospital_a.id}))

    def test_dispatch_target_options_follow_sender_level(self):
        """下拉数据源（`dispatch-targets`）必须与 `dispatch_create` 同一判据。

        这条闸门盯的是 C8：前端自己拼下拉 → 用户**选得到、发不出去**。
        所以这里直接比「接口给的选项」与「服务端会不会接受」。
        """
        material = make_material(district=self.district_a, shelter_stock=100)

        # 区县级：只给本区县，且不含自己
        self.login_as(self.shelter_user_a)
        ids = {t['id'] for t in self.ok(
            self.get_json(f'{URL}dispatch-targets/?material_id={material.id}'))['data']}
        self.assertIn(self.hospital_a.id, ids)
        self.assertNotIn(self.hospital_b.id, ids, '区县级不该看到外区县医院')
        self.assertNotIn(self.shelter_a.id, ids, '不该把自己列成可下发的接收方')

        # 市级：跨区县
        city_material = make_material(district=self.city, shelter_stock=100)
        self.login_as(self.city_shelter_user)
        ids = {t['id'] for t in self.ok(
            self.get_json(f'{URL}dispatch-targets/?material_id={city_material.id}'))['data']}
        self.assertIn(self.hospital_b.id, ids, '市级捕捉点应能给全市任何医院')

    def test_dispatch_target_options_reject_bad_param(self):
        """非法 `material_id` 必须 400 —— 不能 500（查询参数契约）。"""
        self.login_as(self.shelter_user_a)
        for bad in ('abc', '9' * 24):
            resp = self.get_json(f'{URL}dispatch-targets/?material_id={bad}')
            self.assertEqual(resp.status_code, 400, bad)


class MaterialOwnershipTest(MaterialStockTestBase):
    """Q2：归属必须来自**业务对象**，不能来自操作者（本项目第 4 次踩同一条）。"""

    def test_city_shelter_capture_uses_actual_district(self):
        """市级捕捉点登记到甲区的动物，归属必须是**甲区**。

        归属若跟着操作者走（市级），这张单会落到「全市」→ 甲区政府在区县隔离下
        **看不到本区数据**，而界面上一切正常。
        """
        from business.models import Capture
        self.login_as(self.city_shelter_user)
        body = self.ok(self.post_json('/api/business/captures/create/', {
            'shelter_id': self.city_shelter.id,
            'district_id': self.district_a.id,
            'property_name': '甲区某小区',
            'community_name': '甲区某小区',
            'address': '甲区某路 1 号',
            'contact_person': '物业张',
            'contact_phone': '13800000000',
            'pet_count': 1,
        }))
        # `capture_create` 的响应是 `{capture, pet_codes}`，不是裸记录
        capture = Capture.objects.get(id=body['data']['capture']['id'])
        self.assertEqual(capture.district_id, self.district_a.id)
        self.assertEqual(capture.shelter_id, self.city_shelter.id)

        # 正向可见性：甲区政府看得到它
        self.login_as(self.gov_a)
        rows = self.ok(self.get_json('/api/business/captures/'))['data']
        self.assertIn(capture.id, [r['id'] for r in rows])

    def test_city_shelter_capture_without_district_is_rejected(self):
        """市级捕捉点**必须**显式指定归属区县 —— 它自己的区县是市级，不能当归属。

        反向对照：这条若放行，记录会落到「全市（市级）」，区县隔离直接被击穿。
        """
        self.login_as(self.city_shelter_user)
        self.expect_fail(self.post_json('/api/business/captures/create/', {
            'shelter_id': self.city_shelter.id,
            'property_name': '某小区',
            'community_name': '某小区',
            'address': '某路 1 号',
            'contact_person': '物业张',
            'contact_phone': '13800000000',
            'pet_count': 1,
        }), message='市级')


class ShelterStockFieldCompatTest(MaterialStockTestBase):
    """兼容字段 `Material.shelter_stock` 必须与机构库存合计同步。"""

    def test_field_tracks_institution_rows(self):
        material = make_material(district=self.district_a, shelter_stock=0)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 25}))
        material.refresh_from_db()
        self.assertEqual(material.shelter_stock, 25)
        self.assertEqual(get_shelter_stock(material), 25)

    def test_multi_shelter_district_sums_both(self):
        """同一区县两个捕捉点：合计 = 两个机构之和，不是「谁先建行谁独占」。"""
        second = make_institution(type='shelter', district=self.district_a,
                                  name='甲区第二捕捉点')
        material = make_material(district=self.district_a, shelter_stock=0)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 30}))

        second_user = make_user('shelter_a2_t', role='shelter',
                                district=self.district_a, institution=second)
        self.login_as(second_user)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 7}))

        self.assertEqual(get_institution_stock(material, self.shelter_a), 30)
        self.assertEqual(get_institution_stock(material, second), 7)
        self.assertEqual(get_shelter_stock(material, self.district_a), 37)


class MaterialStockIntegrityTest(MaterialStockTestBase):
    """巡检不误报 + 对账规则真的在跑。"""

    def _run(self):
        out = StringIO()
        try:
            call_command('check_data_integrity', stdout=out)
        except SystemExit as exc:
            return out.getvalue(), exc.code
        return out.getvalue(), 0

    def test_normal_data_is_not_reported(self):
        """跑完一整条二级下发链路后，巡检必须**全绿**。

        误报比漏报更致命：一旦运维习惯性忽略输出，真正的差异就再也报不出来。
        市级捕捉点那三条既有规则（捕捉点/医院归属）本轮加了 `is_city` 豁免 ——
        这条用例就是那个豁免的守卫（没豁免的话市级捕捉点会被全量误报）。
        """
        material = make_material(district=self.city, shelter_stock=0)
        self.login_as(self.city_shelter_user)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 100}))
        body = self.ok(self.post_json(f'{URL}dispatch/', {
            'material_id': material.id, 'quantity': 40,
            'to_shelter_id': self.shelter_a.id}))
        self.login_as(self.shelter_user_a)
        self.ok(self._receive(body['data']['id']))

        output, code = self._run()
        self.assertEqual(code, 0, output)
        self.assertIn('未发现一致性问题', output)
        # 三条新对账规则必须**真的跑了**（`✓` 前缀 = 该规则零命中）——
        # 只断言「未发现一致性问题」的话，把三条规则整个删掉也照样通过。
        self.assertIn('✓ 机构库存与流水合计不一致', output)
        self.assertIn('✓ 医院机构库存与现算口径不一致', output)
        self.assertIn('✓ 捕捉点库存字段与机构库存合计不一致', output)

    def test_deliberate_mismatch_is_reported(self):
        """变异对照：人为把一行库存改错，巡检**必须**报出来。

        只测「干净库全绿」的话，把整条对账规则删掉也照样通过
        （假绿闸门：断言「没报错」抓不到「逻辑被删」）。
        """
        material = make_material(district=self.district_a, shelter_stock=0)
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(f'{URL}purchase/', {
            'material_id': material.id, 'quantity': 25}))

        row = MaterialStock.objects.get(material=material,
                                        institution=self.shelter_a)
        MaterialStock.objects.filter(pk=row.pk).update(quantity=999)

        output, code = self._run()
        self.assertEqual(code, 1, output)
        self.assertIn(material.name, output)
