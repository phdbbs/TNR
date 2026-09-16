"""黑名单视图测试。"""
from business.models import Blacklist
from business.tests.base import BusinessTestBase, make_user

URL = '/api/business/blacklist/'


class BlacklistCreateTest(BusinessTestBase):
    def test_create_success(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(f'{URL}create/', {
            'name': '老赖甲', 'phone': '13900000001',
            'id_card': '110102****5678', 'reason': '弃养领养宠物',
            'violation_date': '2026-01-15',
        }))
        record = Blacklist.objects.get(id=body['data']['id'])
        self.assertEqual(record.district, self.district_a)
        self.assertEqual(record.operator, self.shelter_user_a)

    def test_missing_name(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'reason': 'x'}),
                  message='姓名不能为空')

    def test_missing_reason(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'name': '某人'}),
                  message='拉黑原因不能为空')

    def test_user_without_district_rejected(self):
        user = make_user(role='gov_city')  # 无区县
        self.login_as(user)
        self.expect_fail(self.post_json(f'{URL}create/', {'name': 'x', 'reason': 'y'}),
                  message='缺少区县信息')

    def test_adopter_cannot_create(self):
        self.login_as(self.adopter)
        self.expect_fail(self.post_json(f'{URL}create/', {'name': 'x', 'reason': 'y'}),
                  status=403)


class BlacklistListTest(BusinessTestBase):
    def test_list_scoped_and_keyword(self):
        record = Blacklist.objects.create(name='张老赖', phone='13911112222',
                                          reason='弃养', district=self.district_a)
        Blacklist.objects.create(name='李他区', phone='13933334444',
                                 reason='弃养', district=self.district_b)
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(URL))['data']
        self.assertEqual([b['id'] for b in data], [record.id])
        data = self.ok(self.get_json(URL + '?keyword=张老赖'))['data']
        self.assertEqual([b['id'] for b in data], [record.id])


class BlacklistCheckTest(BusinessTestBase):
    def _add(self, **kw):
        return Blacklist.objects.create(
            name='被拉黑者', reason='弃养', district=self.district_a, **kw)

    def test_check_by_phone(self):
        self._add(phone='13900009999')
        self.login_as(self.adopter)
        data = self.ok(self.get_json(URL + 'check/?phone=13900009999'))['data']
        self.assertTrue(data['in_blacklist'])
        self.assertEqual(data['reason'], '弃养')

    def test_check_miss(self):
        self.login_as(self.adopter)
        data = self.ok(self.get_json(URL + 'check/?phone=10000000000'))['data']
        self.assertFalse(data['in_blacklist'])

    def test_check_id_card_masked(self):
        self._add(id_card='110102****5678')
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(
            URL + 'check/?id_card=110102199001015678&phone='))['data']
        self.assertTrue(data['in_blacklist'])

    def test_hospital_can_check(self):
        self.login_as(self.hospital_user_a)
        self.assertEqual(self.client.get(URL + 'check/').status_code, 200)


class BlacklistUpdateDeleteTest(BusinessTestBase):
    """编辑/移出黑名单：此前按钮无接口，点了没反应。"""

    def setUp(self):
        self.record = Blacklist.objects.create(
            name='待改人员', phone='13911110000', id_card='110102****5678',
            reason='弃养', district=self.district_a, violation_date='2026-01-01')
        self.base = f'{URL}{self.record.id}/'

    def test_update_success(self):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.post_json(self.base + 'update/', {
            'name': '改名后', 'phone': '13922220000',
            'reason': '虐待动物', 'violation_date': '2026-02-02',
        }))
        self.record.refresh_from_db()
        self.assertEqual(self.record.name, '改名后')
        self.assertEqual(self.record.phone, '13922220000')
        self.assertEqual(self.record.reason, '虐待动物')
        self.assertEqual(self.record.violation_date.isoformat(), '2026-02-02')
        self.assertEqual(body['data']['name'], '改名后')

    def test_update_requires_name_and_phone(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(self.base + 'update/', {'name': ' '}),
                         message='姓名不能为空')
        self.expect_fail(self.post_json(self.base + 'update/', {'phone': ''}),
                         message='电话不能为空')

    def test_update_cross_district_404(self):
        self.login_as(self.shelter_user_b)
        self.expect_fail(self.post_json(self.base + 'update/', {'name': 'x'}),
                         status=404, message='黑名单记录不存在')

    def test_delete_is_logical(self):
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.base + 'delete/'))
        self.record.refresh_from_db()
        self.assertTrue(self.record.is_deleted)
        self.assertIsNotNone(self.record.deleted_at)
        # 物理记录保留
        self.assertTrue(Blacklist.objects.filter(id=self.record.id).exists())

    def test_deleted_hidden_from_list(self):
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.base + 'delete/'))
        data = self.ok(self.get_json(URL))['data']
        self.assertNotIn(self.record.id, [b['id'] for b in data])

    def test_deleted_no_longer_blocks(self):
        """移出黑名单后不应再拦截。"""
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.base + 'delete/'))
        self.login_as(self.adopter)
        data = self.ok(self.get_json(URL + 'check/?phone=13911110000'))['data']
        self.assertFalse(data['in_blacklist'])

    def test_deleted_cannot_be_updated(self):
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.base + 'delete/'))
        self.expect_fail(self.post_json(self.base + 'update/', {'name': 'x'}),
                         message='已移出黑名单')

    def test_duplicate_delete_rejected(self):
        self.login_as(self.shelter_user_a)
        self.ok(self.post_json(self.base + 'delete/'))
        self.expect_fail(self.post_json(self.base + 'delete/'), message='请勿重复操作')

    def test_get_method_rejected(self):
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.get_json(self.base + 'delete/'), status=405,
                         message='仅支持 POST 请求')
