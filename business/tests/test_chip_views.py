"""芯片接口测试（第四十六轮）。

芯片从「物料上的两个号段字符串」改成「逐号一行、带持有机构」之后，
列表与挑选都走新接口。这里盯的是**业务结果**：

- 「本院挑得到自己的芯片」与「挑不到别家的」必须**成对**出现 ——
  只测一边的话，把判据写成「一律返回全部」同样能通过；
- 区县隔离必须用**区县**账号测 —— 芯片没有 `district` 外键，
  少了 `DISTRICT_LOOKUP` 声明，区县角色一访问就 `FieldError` 500，
  而市级账号走 `return all()` 绕过该分支，**只测市级永远发现不了**。
"""
from business.tests.base import BusinessTestBase, make_chip, make_material

URL = '/api/business/chips/'


class ChipListTest(BusinessTestBase):
    def _chip(self, institution, material, number, status='available'):
        return make_chip(number=number, status=status, institution=institution,
                         material=material)

    def test_district_scope_hides_other_district(self):
        """区县政府账号只看**本区县**的芯片。

        这条是 `DISTRICT_LOOKUP = 'institution__district'` 的活体判据：
        芯片自己没有 `district` 外键，派生路径写错了就是 500。
        """
        m = make_material(category='chip', district=self.district_a)
        self._chip(self.shelter_a, m, 'CHIP-A-001')
        self._chip(self.hospital_a, m, 'CHIP-A-002')
        self._chip(self.shelter_b, m, 'CHIP-B-001')
        self.login_as(self.gov_a)
        body = self.ok(self.get_json(URL))
        self.assertEqual(sorted(r['number'] for r in body['data']['items']),
                         ['CHIP-A-001', 'CHIP-A-002'])

    def test_city_gov_sees_all(self):
        """市级政府账号看全部 —— 上面那条的**正向对照**。

        没有它的话，把区县过滤写成「一律返回空」也能让上一条通过。
        """
        m = make_material(category='chip', district=self.district_a)
        self._chip(self.shelter_a, m, 'CHIP-A-001')
        self._chip(self.shelter_b, m, 'CHIP-B-001')
        self.login_as(self.gov_city)
        body = self.ok(self.get_json(URL))
        self.assertEqual(sorted(r['number'] for r in body['data']['items']),
                         ['CHIP-A-001', 'CHIP-B-001'])

    def test_institution_account_sees_only_own(self):
        """捕捉点账号只看**本机构**的，同区县的医院持有的也不看。

        这是**界面聚焦**（不是安全边界）：少了它，捕捉点操作员会看到
        同区县别家医院的芯片，误以为是自己手上的货。
        """
        m = make_material(category='chip', district=self.district_a)
        self._chip(self.shelter_a, m, 'CHIP-A-001')
        self._chip(self.hospital_a, m, 'CHIP-A-002')
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(URL))
        self.assertEqual([r['number'] for r in body['data']['items']],
                         ['CHIP-A-001'])

    def test_status_filter_and_total(self):
        m = make_material(category='chip', district=self.district_a)
        self._chip(self.shelter_a, m, 'CHIP-A-001')
        self._chip(self.shelter_a, m, 'CHIP-A-002', status='used')
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(URL + '?status=used'))
        self.assertEqual(body['data']['total'], 1)
        self.assertEqual([r['number'] for r in body['data']['items']],
                         ['CHIP-A-002'])

    def test_invalid_status_is_400_not_silently_ignored(self):
        """非法 `status` 必须 400。

        静默忽略的话，前端把参数写错会看到「全部芯片」——
        看起来像「筛选没生效」，而不是「参数写错了」。
        """
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.get_json(URL + '?status=NOPE'), message='芯片状态无效')

    def test_invalid_material_id_is_400_not_500(self):
        """`material_id=abc` 必须 400，不能 `ValueError` 打成 500。"""
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.get_json(URL + '?material_id=abc'))

    def test_keyword_matches_number(self):
        m = make_material(category='chip', district=self.district_a)
        self._chip(self.shelter_a, m, '042060107000177')
        self._chip(self.shelter_a, m, '042060107000178')
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(URL + '?keyword=000177'))
        self.assertEqual([r['number'] for r in body['data']['items']],
                         ['042060107000177'])

    def test_row_carries_institution_and_material(self):
        """出参必须带机构名与物料名 —— 列表要显示「这颗现在在谁手上」。"""
        m = make_material(category='chip', district=self.district_a)
        self._chip(self.shelter_a, m, 'CHIP-A-001')
        self.login_as(self.shelter_user_a)
        row = self.ok(self.get_json(URL))['data']['items'][0]
        self.assertEqual(row['institutionName'], '甲区捕捉点')
        self.assertEqual(row['materialName'], m.name)
        self.assertEqual(row['statusDisplay'], '可用')

    def test_used_chip_carries_pet(self):
        """已使用的芯片要能反查到动物 —— 这正是「逐号管理」的意义。

        号段式管理答不出「042060107000177 这颗给了哪只猫」，
        而芯片存在的意义恰恰是能答这个。
        """
        from business.tests.base import make_pet
        m = make_material(category='chip', district=self.district_a)
        pet = make_pet(code='TNRX0001', district=self.district_a)
        make_chip(number='CHIP-A-001', status='used', institution=self.shelter_a,
                  material=m, pet=pet)
        self.login_as(self.shelter_user_a)
        row = self.ok(self.get_json(URL))['data']['items'][0]
        self.assertEqual(row['petCode'], 'TNRX0001')


class ChipAvailableTest(BusinessTestBase):
    """医院端「从本院库存里挑芯片」—— v5.1 原型的核心交互。"""

    def test_returns_only_own_available(self):
        m = make_material(category='chip', district=self.district_a)
        make_chip(number='H-001', material=m, institution=self.hospital_a)
        make_chip(number='H-002', status='used', material=m,
                  institution=self.hospital_a)
        make_chip(number='H-003', status='in_transit', material=m,
                  institution=self.hospital_a)
        make_chip(number='S-001', material=m, institution=self.shelter_a)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.get_json(URL + 'available/'))
        # 只本院、只可用：已使用的、在途的、别家的都不该出现。
        self.assertEqual([r['number'] for r in body['data']], ['H-001'])

    def test_empty_when_hospital_has_none(self):
        """本院没有芯片时返回空列表（不是 500、也不能拿别家的顶上）。"""
        m = make_material(category='chip', district=self.district_a)
        make_chip(number='S-001', material=m, institution=self.shelter_a)
        self.login_as(self.hospital_user_a)
        self.assertEqual(self.ok(self.get_json(URL + 'available/'))['data'], [])

    def test_material_filter(self):
        m1 = make_material(category='chip', district=self.district_a, name='芯片甲')
        m2 = make_material(category='chip', district=self.district_a, name='芯片乙')
        make_chip(number='H-001', material=m1, institution=self.hospital_a)
        make_chip(number='H-002', material=m2, institution=self.hospital_a)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.get_json(f'{URL}available/?material_id={m2.id}'))
        self.assertEqual([r['number'] for r in body['data']], ['H-002'])

    def test_cross_district_material_404(self):
        """别区县的物料 → 404（与下发/消耗共用「可操作物料」判据）。

        不共用的话会出现「下拉里选得到、挑芯片时 404」的孪生漂移。
        """
        m = make_material(category='chip', district=self.district_b)
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.get_json(f'{URL}available/?material_id={m.id}'),
                         status=404)

    def test_shelter_also_allowed(self):
        """捕捉点也要能挑（区县级捕捉点自己也会植芯片）。"""
        m = make_material(category='chip', district=self.district_a)
        make_chip(number='S-001', material=m, institution=self.shelter_a)
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(URL + 'available/'))
        self.assertEqual([r['number'] for r in body['data']], ['S-001'])

    def test_gov_rejected(self):
        """政府端是**只读业务**（第四十二轮）—— 不能挑芯片。"""
        self.login_as(self.gov_a)
        self.expect_fail(self.get_json(URL + 'available/'), status=403)
