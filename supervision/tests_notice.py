"""公告发布（第三十七轮）。

`Message.TYPE_CHOICES` 里的 ``notice``（公告）在第三十七轮之前**没有任何
真实产生点** —— 只有 `seed_data` 造过演示数据。领养人端「消息中心」
早就支持 `notice` 的图标与文案映射，但没有任何接口能产生一条。

这一组用例钉住四件事：

1. **能产生** —— 发布接口真的写出 `type='notice'` 的消息；
2. **发对人** —— 区级管理员只能发本区县，不能向全市广播；
3. **归属从业务对象推导** —— 领养人的区县看 `Adoption.district`，
   **不看** `User.district`（生产上领养人该字段恒为空，按它收敛会让
   区级公告的收件人恒为空集，功能 100% 不可用）；
4. **失败不留痕** —— 批量写接口的校验必须全部前置，否则会留下半截广播。
"""
from business.models import Message
from business.tests.base import (
    BusinessTestBase, make_adoption, make_pet, make_user,
)


class NoticePublishTest(BusinessTestBase):
    PUBLISH = '/api/supervision/notices/publish/'
    LIST = '/api/supervision/notices/'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # ⚠ 领养人账号**没有区县**（基类的 `cls.adopter` 就是这样，与生产一致）。
        # 他们的区县归属由**领养记录**表达：甲区一只、乙区一只。
        cls.adopter_a = make_user(role='adopter')
        cls.adopter_b = make_user(role='adopter')
        cls.pet_a = make_pet(district=cls.district_a)
        cls.pet_b = make_pet(district=cls.district_b)
        cls.adoption_a = make_adoption(cls.adopter_a, district=cls.district_a,
                                       pet=cls.pet_a)
        cls.adoption_b = make_adoption(cls.adopter_b, district=cls.district_b,
                                       pet=cls.pet_b)
        # 干扰项：账号上**有**区县、但**没有**领养记录。
        # 旧实现（读 `User.district`）会把它当成甲区收件人；新实现不该。
        cls.adopter_only_district = make_user(role='adopter',
                                              district=cls.district_a)

    def _publish(self, user, **payload):
        body = {'title': '测试公告', 'content': '正文内容'}
        body.update(payload)
        self.login_as(user)
        return self.post_json(self.PUBLISH, body)

    # ---------- 正常路径 ----------

    def test_city_publishes_to_every_adopter(self):
        resp = self._publish(self.gov_city)
        body = self.ok(resp)
        sent = body['data']['sent']
        self.assertEqual(
            sent, Message.objects.filter(type='notice').count())
        self.assertGreaterEqual(sent, 4)   # 基础夹具 1 人 + 本类 3 人
        for adopter in (self.adopter, self.adopter_a, self.adopter_b,
                        self.adopter_only_district):
            self.assertTrue(
                Message.objects.filter(user=adopter, type='notice').exists(),
                f'{adopter.username} 应收到公告')

    def test_district_publishes_only_within_its_district(self):
        """区级管理员**只能发给本区县** —— 否则就成了全市广播。"""
        self.ok(self._publish(self.gov_a))
        self.assertTrue(
            Message.objects.filter(user=self.adopter_a, type='notice').exists())
        self.assertFalse(
            Message.objects.filter(user=self.adopter_b, type='notice').exists(),
            '乙区领养人不该收到甲区管理员发的公告')

    # ---------- ⚠⚠ 归属推导（本轮缺陷的回归）----------

    def test_district_recipients_derive_from_adoption_not_user_district(self):
        """⚠⚠ 回归：领养人**账号没有区县**，归属必须从**领养记录**推导。

        旧实现走 `_scope_filter(..., field='district')`，读的是 `User.district`。
        生产实测该字段对领养人**恒为空**：

        | 收敛口径 | 襄城区可触达领养人 |
        |---|---|
        | `User.district`（旧） | **0 人** |
        | `Adoption.district`（新） | **1 人** |

        后果是 `gov_district` 发公告一律 400「该范围内没有可接收公告的用户」，
        而角色闸门却明确允许它调用 —— 功能实现了、但到不了。
        """
        # 前提检查：本用例的领养人账号确实没有区县，否则就没在测东西
        self.assertIsNone(self.adopter_a.district_id)
        self.assertIsNone(self.adopter_b.district_id)
        self.ok(self._publish(self.gov_a))
        self.assertTrue(
            Message.objects.filter(user=self.adopter_a, type='notice').exists(),
            '有甲区领养记录、账号无区县的领养人，必须收到甲区公告')
        self.assertFalse(
            Message.objects.filter(user=self.adopter_b, type='notice').exists(),
            '乙区领养人不该收到甲区公告')

    def test_user_district_alone_does_not_make_a_recipient(self):
        """反向：**只有账号区县、没有领养记录**的人不该收到区级公告。

        这条与上一条成对 —— 只钉住一个方向的话，把实现改回读 `User.district`
        只会让上一条通过（因为那时账号区县为空、收件人为 0，上一条会红），
        但这条能独立说明「归属口径不对」。
        """
        self.assertEqual(self.adopter_only_district.district_id,
                         self.district_a.id)
        self.ok(self._publish(self.gov_a))
        self.assertFalse(
            Message.objects.filter(user=self.adopter_only_district,
                                   type='notice').exists(),
            '归属不能读 User.district —— 没有领养记录就不是本区县的受众')

    def test_adopter_with_two_adoptions_gets_one_notice(self):
        """同一区县有两条领养记录 → 只收**一条**公告（收件人必须去重）。

        `_notice_recipients()` 走 `adoptions__district_id` 的 join，
        少了 `.distinct()` 就是「一条领养一行」，同一个人会收到 N 条
        一模一样的公告 —— 界面上表现为消息中心里同一公告刷屏。
        """
        make_adoption(self.adopter_a, district=self.district_a)
        resp = self.ok(self._publish(self.gov_a))
        self.assertEqual(resp['data']['sent'], 1, '收件人必须去重')
        self.assertEqual(
            Message.objects.filter(user=self.adopter_a, type='notice').count(), 1)

    def test_notice_carries_publisher_district(self):
        """公告自带发布方区县：区级 = 本区县，市级 = None。"""
        self.ok(self._publish(self.gov_a, title='甲区公告'))
        self.assertEqual(
            set(Message.objects.filter(title='甲区公告')
                .values_list('district_id', flat=True)),
            {self.district_a.id})
        self.ok(self._publish(self.gov_city, title='全市公告'))
        self.assertEqual(
            set(Message.objects.filter(title='全市公告')
                .values_list('district_id', flat=True)),
            {None}, '市级公告是全市公告，不带区县')

    def test_message_is_notice_type_and_unread(self):
        self.ok(self._publish(self.gov_city, title='防疫通知',
                              content='请按时接种疫苗'))
        msg = Message.objects.filter(user=self.adopter, type='notice').get()
        self.assertEqual(msg.title, '防疫通知')
        self.assertEqual(msg.content, '请按时接种疫苗')
        self.assertFalse(msg.is_read)

    # ---------- 输入校验 ----------

    def test_empty_title_rejected(self):
        self.expect_fail(self._publish(self.gov_city, title='   '),
                         message='请填写公告标题')
        self.assertFalse(Message.objects.filter(type='notice').exists(),
                         '校验失败不能留下任何消息')

    def test_empty_content_rejected(self):
        self.expect_fail(self._publish(self.gov_city, content=''),
                         message='请填写公告内容')
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_overlong_title_rejected(self):
        from supervision.views import NOTICE_TITLE_MAX
        self.expect_fail(
            self._publish(self.gov_city, title='长' * (NOTICE_TITLE_MAX + 1)),
            message='不能超过')
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_overlong_content_rejected(self):
        from supervision.views import NOTICE_CONTENT_MAX
        self.expect_fail(
            self._publish(self.gov_city, content='长' * (NOTICE_CONTENT_MAX + 1)),
            message='不能超过')
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_unknown_target_role_rejected(self):
        self.expect_fail(self._publish(self.gov_city, target_role='hospital'),
                         message='不支持的目标角色')

    def test_non_string_fields_rejected(self):
        """`body_str` 必须挡住非字符串 —— 否则 `str([1,2])` 会静默变成正文。"""
        for payload in ({'title': [1, 2]}, {'content': {'a': 1}},
                        {'title': 123}):
            with self.subTest(payload=payload):
                self.expect_fail(self._publish(self.gov_city, **payload))
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_no_recipients_is_rejected_not_silently_ok(self):
        """范围内没人可发 → 必须报错，不能回「已发送 0 人」当成功。

        「成功但 0 人」与「成功」在界面上无法区分，发布方会以为公告发出去了。
        """
        Message.objects.filter(type='notice').delete()
        self.login_as(self.gov_a)
        # 甲区唯一的领养记录持有者停用 → 本区县无人可发。
        # （`adopter_only_district` 只有账号区县、没有领养记录，
        #   按正确口径本来就不是收件人 —— 这条同时印证了口径。）
        self.adopter_a.is_active = False
        self.adopter_a.save(update_fields=['is_active'])
        resp = self.post_json(self.PUBLISH, {'title': 't', 'content': 'c'})
        self.expect_fail(resp, message='没有可接收公告的用户')

    # ---------- 权限 ----------

    def test_non_gov_role_forbidden(self):
        for user in (self.shelter_user_a, self.hospital_user_a, self.adopter):
            with self.subTest(role=user.role):
                self.login_as(user)
                resp = self.post_json(
                    self.PUBLISH, {'title': 't', 'content': 'c'})
                self.expect_fail(resp, status=403)

    def test_anonymous_gets_401_json(self):
        resp = self.post_json(self.PUBLISH, {'title': 't', 'content': 'c'})
        self.assertEqual(resp.status_code, 401)
        self.assertIn('json', resp['Content-Type'])
        self.assertFalse(Message.objects.filter(type='notice').exists())

    # ---------- 已发列表 ----------

    def test_list_aggregates_by_title(self):
        """一条公告 = N 行 Message，列表必须按标题聚合成一行。"""
        self.ok(self._publish(self.gov_city, title='聚合测试'))
        self.login_as(self.gov_city)
        rows = self.ok(self.client.get(self.LIST))['data']
        matched = [r for r in rows if r['title'] == '聚合测试']
        self.assertEqual(len(matched), 1, '同一公告不应在列表里出现多行')
        self.assertEqual(matched[0]['sent_count'],
                         Message.objects.filter(title='聚合测试').count())
        # 驼峰孪生必须同时存在（前端读 sentCount）
        self.assertIn('sentCount', matched[0])

    def test_list_empty_before_any_publish(self):
        self.login_as(self.gov_city)
        self.assertEqual(self.ok(self.client.get(self.LIST))['data'], [])

    def test_list_requires_gov_role(self):
        self.login_as(self.adopter)
        resp = self.client.get(self.LIST)
        self.expect_fail(resp, status=403)

    # ---------- ⚠⚠ 列表的区县收敛 ----------

    def test_district_list_hides_other_districts_notices(self):
        """区级列表不得出现**其他区县**发布的公告（区县隔离）。

        收敛按**发布方**（`Message.district`）判定，不按接收人反查 ——
        反查会引入多表 join 扇出，且领养记录变动会让历史公告的归属漂移。
        """
        self.ok(self._publish(self.gov_a, title='甲区公告'))
        self.ok(self._publish(self.gov_b, title='乙区公告'))
        self.login_as(self.gov_a)
        titles = [r['title'] for r in self.ok(self.client.get(self.LIST))['data']]
        self.assertIn('甲区公告', titles)
        self.assertNotIn('乙区公告', titles, '区级不该看到别区县发布的公告')

    def test_district_list_shows_city_notices(self):
        """市级公告对区级可见 —— 它本来就发给了本区县的领养人。

        对区级隐藏只会让「已发布公告」列表与实际触达情况不一致。
        """
        self.ok(self._publish(self.gov_city, title='全市公告'))
        self.login_as(self.gov_a)
        titles = [r['title'] for r in self.ok(self.client.get(self.LIST))['data']]
        self.assertIn('全市公告', titles)

    def test_city_list_shows_all_notices(self):
        self.ok(self._publish(self.gov_a, title='甲区公告'))
        self.ok(self._publish(self.gov_b, title='乙区公告'))
        self.login_as(self.gov_city)
        titles = [r['title'] for r in self.ok(self.client.get(self.LIST))['data']]
        self.assertIn('甲区公告', titles)
        self.assertIn('乙区公告', titles)
