"""第四十四轮：政府端「物料全局监管」（`/api/supervision/materials/`）。

## 这一轮钉住的不变量

政府端要按**机构**看库存，但数据模型是不对称的：

- `Material` 只有 `district`，**没有机构外键**，`shelter_stock` 天然就是「本区县捕捉点」的库存；
- 只有 `MaterialTransaction.hospital` 才是机构（医院），**医院库存只能由流水反推**。

所以后端补了 `materials[].hospital_stocks = {医院id: 库存}`（一条 GROUP BY 算出来，
不逐对调 `get_hospital_stock()`，否则是 物料×医院 次聚合）。
本文件把这个口径**钉死**，因为一旦它漂了，「物料全局监管」与「医院端实时库存」
就会各说各话，而两边都是 200、界面上都是个数字 —— 属于最难发现的一类不一致。

⚠ `hospital_stocks` 的对账断言必须有**正向对照**（库存非 0），
否则 `{}` 与 `{}`、`0` 与 `0` 也能"对得上"。
"""
from django.utils import timezone

from business.models import Material, MaterialTransaction
from business.services import get_hospital_stock
from business.tests.base import (
    BusinessTestBase, make_hospital_txn, make_material,
)

URL = '/api/supervision/materials/'


class HospitalStockReconciliationTest(BusinessTestBase):
    """`hospital_stocks` 必须与 `services.get_hospital_stock()` 逐对相等。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.material = make_material(
            name='对账疫苗', category='vaccine', district=cls.district_a,
            shelter_stock=5, safety_stock=50,
        )
        # 医院侧四种类型齐全 —— 只测 receive 的话，漏掉 adjustment 的符号
        # （`get_hospital_stock` 是 **减** adjustment）也照样"对得上"。
        make_hospital_txn(cls.material, cls.hospital_a, 'purchase', 10)
        make_hospital_txn(cls.material, cls.hospital_a, 'receive', 5)
        make_hospital_txn(cls.material, cls.hospital_a, 'consume', 3)
        make_hospital_txn(cls.material, cls.hospital_a, 'adjustment', 1)
        # dispatch 不计入医院库存（下发出库 ≠ 医院签收），拿它当**反向对照**：
        # 若实现里顺手把 dispatch 也加了，这里会变成 18 而不是 11。
        make_hospital_txn(cls.material, cls.hospital_a, 'dispatch', 7)

    def _payload(self):
        self.login_as(self.gov_city)
        return self.ok(self.get_json(URL))['data']

    def test_matches_get_hospital_stock(self):
        """每条物料的每个医院库存都与 `get_hospital_stock()` 相等，且**非零**。"""
        data = self._payload()
        item = next(m for m in data['materials'] if m['id'] == self.material.id)
        key = str(self.hospital_a.id)
        self.assertIn(key, item['hospital_stocks'], item['hospital_stocks'])
        got = item['hospital_stocks'][key]
        want = get_hospital_stock(self.material, self.hospital_a)
        self.assertEqual(got, want)
        # 正向对照：10 + 5 − 3 − 1 = 11（dispatch 7 不计入）
        self.assertEqual(got, 11, f'dispatch 不该计入医院库存；实得 {got}')

    def test_hospital_without_txn_is_absent(self):
        """该物料在这家医院**没有流水**时键缺失 —— 前端靠 `|| 0` 回落。

        这条不是可有可无：若实现改成"所有医院都补 0"，前端仍能工作，但
        `hospital_stocks` 的键集合就再也不能用来判断"这家医院有没有这个物料"。
        """
        data = self._payload()
        item = next(m for m in data['materials'] if m['id'] == self.material.id)
        self.assertNotIn(str(self.hospital_b.id), item['hospital_stocks'])

    def test_null_hospital_txn_not_in_hospital_stocks(self):
        """`hospital` 为空的流水（捕捉点侧）不能混进任何医院的库存。

        若实现里漏了 `hospital__isnull=False`，这 99 会加进某个 `None` 键、
        或（更糟）被并进第一家医院 —— 断言 `hospital_a` 仍是 11 就能抓到。
        """
        MaterialTransaction.objects.create(
            material=self.material, material_name=self.material.name,
            type='purchase', quantity=99, hospital=None,
            district=self.district_a, date=timezone.localdate(),
        )
        data = self._payload()
        item = next(m for m in data['materials'] if m['id'] == self.material.id)
        self.assertEqual(item['hospital_stocks'][str(self.hospital_a.id)], 11)
        self.assertNotIn('None', item['hospital_stocks'])
        self.assertNotIn('null', item['hospital_stocks'])


class TransactionInstitutionTest(BusinessTestBase):
    """流水行的机构名（台账标签的「机构」列靠它）。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.material = make_material(district=cls.district_a)
        make_hospital_txn(cls.material, cls.hospital_a, 'receive', 4)
        MaterialTransaction.objects.create(
            material=cls.material, material_name=cls.material.name,
            type='purchase', quantity=6, hospital=None,
            district=cls.district_a, date=timezone.localdate(),
        )

    def test_hospital_name_present_and_blank(self):
        self.login_as(self.gov_city)
        txns = self.ok(self.get_json(URL))['data']['transactions']
        by_qty = {t['quantity']: t for t in txns}
        # 医院侧流水 → 医院名
        self.assertEqual(by_qty[4]['hospital_name'], self.hospital_a.name)
        # 捕捉点侧流水（hospital 为空）→ 空串，前端回落显示该区县捕捉点名
        self.assertEqual(by_qty[6]['hospital_name'], '')

    def test_district_name_present_on_every_txn(self):
        """流水行也必须带 `district_name`（台账标签的「区县」列 + 关键字搜索都读它）。

        第四十四轮实测：这个字段**原先漏了**，于是「区县」整列安静地显示「—」、
        搜索区县名也搜不到 —— 接口 200、列头也在，只有内容空着。
        同一次响应里 `materials` 一直有这个字段，最容易「以为流水也有」。
        """
        self.login_as(self.gov_city)
        txns = self.ok(self.get_json(URL))['data']['transactions']
        self.assertTrue(txns, '夹具没造出流水，断言会空转')
        for t in txns:
            with self.subTest(ledger=t.get('ledger_no')):
                self.assertEqual(t['district_name'], self.district_a.name)
                self.assertEqual(t['districtName'], self.district_a.name)


class CamelTwinTest(BusinessTestBase):
    """手工聚合的响应必须递归补驼峰（`with_camel_keys`）。

    这个接口是「`serialize_instance` 给两套键 + 手拼字段只给 snake」的混搭，
    漏补的话前端读 `r.ledgerNo` / `m.hospitalStocks` 全是 undefined ——
    整列空白但接口仍 200，比 500 难查得多。
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.material = make_material(district=cls.district_a, shelter_stock=1, safety_stock=9)
        make_hospital_txn(cls.material, cls.hospital_a, 'receive', 2)

    def test_camel_twins_exist(self):
        self.login_as(self.gov_city)
        data = self.ok(self.get_json(URL))['data']

        m = next(x for x in data['materials'] if x['id'] == self.material.id)
        for k in ('hospitalStocks', 'categoryDisplay', 'districtName',
                  'safetyStock', 'shelterStock'):
            with self.subTest(key=k):
                self.assertIn(k, m)
        self.assertEqual(m['hospitalStocks'], m['hospital_stocks'])

        t = data['transactions'][0]
        for k in ('hospitalName', 'ledgerNo', 'materialName', 'operatorName',
                  'districtName', 'fromTo'):
            with self.subTest(key=k):
                self.assertIn(k, t)

        a = data['alerts'][0]
        for k in ('safetyStock', 'shelterStock', 'category'):
            with self.subTest(key=k):
                self.assertIn(k, a)

        for k in ('materialCount', 'transactionCount', 'alertCount',
                  'totalPurchased', 'totalDispatched', 'totalConsumed'):
            with self.subTest(key=k):
                self.assertIn(k, data['stats'])


class AlertAndStatsTest(BusinessTestBase):
    """异常数据的口径：`shelter_stock < safety_stock`，缺口 = 安全 − 当前。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.low = make_material(name='低库存', district=cls.district_a,
                                shelter_stock=3, safety_stock=10)
        # 相等**不算**异常（判据是严格小于）
        cls.equal = make_material(name='刚好持平', district=cls.district_a,
                                  shelter_stock=10, safety_stock=10)
        cls.enough = make_material(name='充足', district=cls.district_a,
                                   shelter_stock=99, safety_stock=10)
        cls.other_district = make_material(name='别区低库存', district=cls.district_b,
                                           shelter_stock=1, safety_stock=99)

    def test_alert_predicate_and_shortage(self):
        self.login_as(self.gov_city)
        data = self.ok(self.get_json(URL))['data']
        names = {a['name'] for a in data['alerts']}
        self.assertIn('低库存', names)
        self.assertNotIn('刚好持平', names, '相等不该算异常')
        self.assertNotIn('充足', names)
        a = next(a for a in data['alerts'] if a['name'] == '低库存')
        self.assertEqual(a['shortage'], 7)          # 10 − 3
        self.assertEqual(a['shelter_stock'], 3)
        self.assertEqual(a['safety_stock'], 10)

    def test_material_count_equals_returned_rows(self):
        """`stats.material_count` 用的是 `len(list)`，必须与 `materials` 长度一致。

        实现从 `.count()` 改成物化列表的 `len()` 是为了避免第二次查询，
        这里把它钉住 —— 两处若各算各的，筛选一变就会分叉。
        """
        self.login_as(self.gov_city)
        data = self.ok(self.get_json(URL))['data']
        self.assertEqual(data['stats']['material_count'], len(data['materials']))
        self.assertEqual(data['stats']['alert_count'], len(data['alerts']))

    def test_transaction_count_equals_returned_rows(self):
        self.login_as(self.gov_city)
        data = self.ok(self.get_json(URL))['data']
        self.assertEqual(data['stats']['transaction_count'], len(data['transactions']))


class DistrictScopeTest(BusinessTestBase):
    """区县隔离不能被「机构维度」带塌。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ma = make_material(name='甲区物料', district=cls.district_a)
        cls.mb = make_material(name='乙区物料', district=cls.district_b)
        make_hospital_txn(cls.ma, cls.hospital_a, 'receive', 5)
        make_hospital_txn(cls.mb, cls.hospital_b, 'receive', 7)

    def test_district_gov_sees_only_own_district(self):
        self.login_as(self.gov_a)
        data = self.ok(self.get_json(URL))['data']
        self.assertEqual([m['name'] for m in data['materials']], ['甲区物料'])
        self.assertEqual(len(data['transactions']), 1)
        self.assertEqual(data['transactions'][0]['quantity'], 5)
        # 医院库存里也不能出现别区县的医院
        self.assertNotIn(str(self.hospital_b.id),
                         data['materials'][0]['hospital_stocks'])

    def test_city_gov_sees_both(self):
        """正向对照：市级能看到两条 —— 否则上面那条「只看到一条」可能只是因为
        整个接口都返回空。"""
        self.login_as(self.gov_city)
        data = self.ok(self.get_json(URL))['data']
        self.assertEqual(sorted(m['name'] for m in data['materials']),
                         ['乙区物料', '甲区物料'])
        self.assertEqual(len(data['transactions']), 2)


class RoleGateTest(BusinessTestBase):
    """只有政府端角色可读；这是政府端页面，捕捉点/医院/领养人一律拒绝。"""

    def test_allowed_roles(self):
        for user in (self.gov_city, self.gov_a):
            with self.subTest(role=user.role):
                self.login_as(user)
                self.assertEqual(self.client.get(URL).status_code, 200)

    def test_denied_roles(self):
        for user in (self.shelter_user_a, self.hospital_user_a, self.adopter,
                     self.platform_admin):
            with self.subTest(role=user.role):
                self.login_as(user)
                self.assertEqual(self.client.get(URL).status_code, 403)


class MaterialModelShapeTest(BusinessTestBase):
    """把「机构维度只能由流水反推」这个**模型事实**钉住。

    本轮的整个后端改造都建立在它上面：一旦有人给 `Material` 加了机构外键，
    `hospital_stocks` 与 `materialView()` 的两套口径就该重新审视 ——
    这个断言会立刻变红，把人叫回来看注释。
    """

    def test_material_has_no_institution_fk(self):
        field_names = {f.name for f in Material._meta.get_fields()}
        self.assertNotIn('institution', field_names)
        self.assertIn('district', field_names)
        self.assertIn('shelter_stock', field_names)

    def test_hospital_fk_only_on_transaction(self):
        self.assertIn('hospital', {f.name for f in MaterialTransaction._meta.get_fields()})
