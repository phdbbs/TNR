"""公告发布（第三十七轮）。

`Message.TYPE_CHOICES` 里的 ``notice``（公告）在第三十七轮之前**没有任何
真实产生点** —— 只有 `seed_data` 造过演示数据。领养人端「消息中心」
早就支持 `notice` 的图标与文案映射，但没有任何接口能产生一条。

这一组用例钉住三件事：

1. **能产生** —— 发布接口真的写出 `type='notice'` 的消息；
2. **发对人** —— 区级管理员只能发本区县，不能向全市广播；
3. **失败不留痕** —— 批量写接口的校验必须全部前置，否则会留下半截广播。
"""
from business.models import Message
from business.tests.base import BusinessTestBase, make_user


class NoticePublishTest(BusinessTestBase):
    PUBLISH = '/api/supervision/notices/publish/'
    LIST = '/api/supervision/notices/'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # 两个区县各一名领养人（带区县，用于验证区级发布的范围收敛）
        cls.adopter_a = make_user(role='adopter', district=cls.district_a)
        cls.adopter_b = make_user(role='adopter', district=cls.district_b)

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
        self.assertGreaterEqual(sent, 3)   # 基础夹具 1 人 + 本类 2 人
        for adopter in (self.adopter, self.adopter_a, self.adopter_b):
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
        # 把甲区领养人停用，制造「本区县无人可发」
        self.adopter_a.is_active = False
        self.adopter_a.save(update_fields=['is_active'])
        self.adopter.district = self.district_b
        self.adopter.save(update_fields=['district'])
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
