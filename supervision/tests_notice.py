"""公告发布（第三十七轮，第四十二轮迁到平台端）。

`Message.TYPE_CHOICES` 里的 ``notice``（公告）在第三十七轮之前**没有任何
真实产生点** —— 只有 `seed_data` 造过演示数据。领养人端「消息中心」
早就支持 `notice` 的图标与文案映射，但没有任何接口能产生一条。

第四十二轮起，公告发布随「全局设置」一起从政府端迁到**平台端**
（`templates/portal/platform/portal.html` 的系统配置页），角色白名单收成
`platform_admin`。平台管理员是**全局角色、本身不挂区县**，所以「发给哪个
区县」改由请求**显式指定** `district_id`：不传 = 全市公告。

这一组用例钉住六件事：

1. **能产生** —— 发布接口真的写出 `type='notice'` 的消息；
2. **发对人** —— 指定 `district_id` 时只发给该区县，不指定时发给全市；
3. **归属从业务对象推导** —— 领养人的区县看 `Adoption.district`，
   **不看** `User.district`（生产上领养人该字段恒为空，按它收敛会让
   区县公告的收件人恒为空集，功能 100% 不可用）；
4. **失败不留痕** —— 批量写接口的校验必须全部前置，否则会留下半截广播；
5. **平台端独有** —— `gov_city` / `gov_district` 一律 403（「前后端一起收口」，
   只把 UI 藏起来不算收口）；
6. **非法目标区县必须 400** —— 不能因为 `body_int` 对非法值返回 `None`
   就把 `{"district_id": "abc"}` 静默降级成一次**全市广播**。
"""
from business.models import Message
from business.tests.base import (
    BusinessTestBase, make_adoption, make_district, make_pet, make_user,
)
from core.scope import EMPTY_DISTRICT_SCOPE
from supervision.views import _notice_recipients


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

    def _publish(self, user=None, **payload):
        """以平台管理员身份发布公告。`district_id` 不传 = 全市公告。"""
        body = {'title': '测试公告', 'content': '正文内容'}
        body.update(payload)
        self.login_as(user or self.platform_admin)
        return self.post_json(self.PUBLISH, body)

    # ---------- 正常路径 ----------

    def test_city_publishes_to_every_adopter(self):
        """不传 `district_id` = 全市公告，全市领养人都是受众。"""
        resp = self._publish()
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
        """指定 `district_id` = 只发给该区县 —— 不能溢出成全市广播。"""
        self.ok(self._publish(district_id=self.district_a.id))
        self.assertTrue(
            Message.objects.filter(user=self.adopter_a, type='notice').exists())
        self.assertFalse(
            Message.objects.filter(user=self.adopter_b, type='notice').exists(),
            '乙区领养人不该收到甲区公告')

    def test_city_level_district_normalizes_to_city_notice(self):
        """显式选了「市级」区划 → 归一成**全市公告**（`district_id=None`）。

        否则公告会带上 `district_id=<市级>`，`notice_list` 的区县收敛就多出
        一个「只有挂在市级的人看得到」的第三种状态，而收件人其实包含了
        全部区县的领养人 —— 列表显示的归属与真实受众对不上。
        """
        self.ok(self._publish(district_id=self.city.id, title='市级公告'))
        self.assertEqual(
            set(Message.objects.filter(title='市级公告')
                .values_list('district_id', flat=True)),
            {None})
        self.assertTrue(
            Message.objects.filter(user=self.adopter_b, type='notice').exists(),
            '市级 = 全市，乙区领养人也要收到')

    # ---------- ⚠⚠ 归属推导（第三十七轮缺陷的回归）----------

    def test_district_recipients_derive_from_adoption_not_user_district(self):
        """⚠⚠ 回归：领养人**账号没有区县**，归属必须从**领养记录**推导。

        旧实现走 `_scope_filter(..., field='district')`，读的是 `User.district`。
        生产实测该字段对领养人**恒为空**：

        | 收敛口径 | 襄城区可触达领养人 |
        |---|---|
        | `User.district`（旧） | **0 人** |
        | `Adoption.district`（新） | **1 人** |

        后果是区县公告一律 400「该范围内没有可接收公告的用户」，
        而角色闸门却明确允许调用 —— 功能实现了、但到不了。
        """
        # 前提检查：本用例的领养人账号确实没有区县，否则就没在测东西
        self.assertIsNone(self.adopter_a.district_id)
        self.assertIsNone(self.adopter_b.district_id)
        self.ok(self._publish(district_id=self.district_a.id))
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
        self.ok(self._publish(district_id=self.district_a.id))
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
        resp = self.ok(self._publish(district_id=self.district_a.id))
        self.assertEqual(resp['data']['sent'], 1, '收件人必须去重')
        self.assertEqual(
            Message.objects.filter(user=self.adopter_a, type='notice').count(), 1)

    def test_notice_carries_publisher_district(self):
        """公告自带目标区县：指定区县 = 该区县，不指定 = None（全市）。"""
        self.ok(self._publish(district_id=self.district_a.id, title='甲区公告'))
        self.assertEqual(
            set(Message.objects.filter(title='甲区公告')
                .values_list('district_id', flat=True)),
            {self.district_a.id})
        self.ok(self._publish(title='全市公告'))
        self.assertEqual(
            set(Message.objects.filter(title='全市公告')
                .values_list('district_id', flat=True)),
            {None}, '全市公告不带区县')

    def test_message_is_notice_type_and_unread(self):
        self.ok(self._publish(title='防疫通知', content='请按时接种疫苗'))
        msg = Message.objects.filter(user=self.adopter, type='notice').get()
        self.assertEqual(msg.title, '防疫通知')
        self.assertEqual(msg.content, '请按时接种疫苗')
        self.assertFalse(msg.is_read)

    # ---------- 收件人收敛的哨兵语义（直接单测纯函数）----------

    def test_empty_scope_sentinel_yields_no_recipients(self):
        """哨兵 `EMPTY_DISTRICT_SCOPE` 是「看不到任何区县」，必须回空集。

        不能拿哨兵（-1）去 `filter(adoptions__district_id=-1)` —— 它不是真实
        区县 id，写进外键会撞约束、用来过滤则语义含糊。这里直接锁住
        `_notice_recipients()` 的三态：`None` = 全市 / 区县 id = 该区县 /
        哨兵 = 空集。
        """
        self.assertEqual(_notice_recipients('adopter', EMPTY_DISTRICT_SCOPE).count(), 0)
        self.assertGreater(_notice_recipients('adopter', None).count(), 0)
        self.assertEqual(
            _notice_recipients('adopter', self.district_a.id).count(), 1)

    # ---------- 输入校验 ----------

    def test_empty_title_rejected(self):
        self.expect_fail(self._publish(title='   '),
                         message='请填写公告标题')
        self.assertFalse(Message.objects.filter(type='notice').exists(),
                         '校验失败不能留下任何消息')

    def test_empty_content_rejected(self):
        self.expect_fail(self._publish(content=''),
                         message='请填写公告内容')
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_overlong_title_rejected(self):
        from supervision.views import NOTICE_TITLE_MAX
        self.expect_fail(
            self._publish(title='长' * (NOTICE_TITLE_MAX + 1)),
            message='不能超过')
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_overlong_content_rejected(self):
        from supervision.views import NOTICE_CONTENT_MAX
        self.expect_fail(
            self._publish(content='长' * (NOTICE_CONTENT_MAX + 1)),
            message='不能超过')
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_unknown_target_role_rejected(self):
        self.expect_fail(self._publish(target_role='hospital'),
                         message='不支持的目标角色')

    def test_non_string_fields_rejected(self):
        """`body_str` 必须挡住非字符串 —— 否则 `str([1,2])` 会静默变成正文。"""
        for payload in ({'title': [1, 2]}, {'content': {'a': 1}},
                        {'title': 123}):
            with self.subTest(payload=payload):
                self.expect_fail(self._publish(**payload))
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_unknown_district_id_rejected(self):
        self.expect_fail(self._publish(district_id=99999999),
                         message='所选区县不存在')
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_inactive_district_rejected(self):
        """停用区县不能作为公告目标（与机构/账号引用的停用判据同一条）。"""
        dead = make_district(name='停用区', status='inactive')
        self.expect_fail(self._publish(district_id=dead.id),
                         message='所选区县已停用')
        self.assertFalse(Message.objects.filter(type='notice').exists())

    def test_non_numeric_district_id_rejected_not_silently_citywide(self):
        """⚠⚠ `{"district_id": "abc"}` 必须 400，**不能静默降级成全市广播**。

        `body_int` 对非法值返回 `None`，与「未提供」不可区分。若直接写
        `if district_id:`，一次「本想发给甲区」的误操作会变成**发给全市**，
        而广播不可撤回、界面上还显示成功。宁可 400。
        """
        for bad in ('abc', [1, 2], {'a': 1}, '9' * 24):
            with self.subTest(district_id=bad):
                self.expect_fail(self._publish(district_id=bad),
                                 message='所选区县无效')
        self.assertFalse(Message.objects.filter(type='notice').exists(),
                         '非法区县不能留下任何消息（更不能是全市广播）')

    def test_no_recipients_is_rejected_not_silently_ok(self):
        """范围内没人可发 → 必须报错，不能回「已发送 0 人」当成功。

        「成功但 0 人」与「成功」在界面上无法区分，发布方会以为公告发出去了。
        """
        Message.objects.filter(type='notice').delete()
        # 甲区唯一的领养记录持有者停用 → 本区县无人可发。
        # （`adopter_only_district` 只有账号区县、没有领养记录，
        #   按正确口径本来就不是收件人 —— 这条同时印证了口径。）
        self.adopter_a.is_active = False
        self.adopter_a.save(update_fields=['is_active'])
        self.expect_fail(self._publish(district_id=self.district_a.id),
                         message='没有可接收公告的用户')

    # ---------- 权限（第四十二轮：政府端全部收口）----------

    def test_non_platform_role_forbidden(self):
        """⚠ 政府端**读也要收口** —— 公告页已从政府端门户移除，
        只留接口白名单等于留了一条无 UI 的后门。

        这里同时钉住「只把前端菜单删掉、服务端没改」这种半截收口：
        若 `role_required` 里还留着 `gov_city`，本用例会红。
        """
        for user in (self.gov_city, self.gov_a, self.gov_b,
                     self.shelter_user_a, self.hospital_user_a, self.adopter):
            with self.subTest(role=user.role, username=user.username):
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
        self.ok(self._publish(title='聚合测试'))
        rows = self.ok(self.client.get(self.LIST))['data']
        matched = [r for r in rows if r['title'] == '聚合测试']
        self.assertEqual(len(matched), 1, '同一公告不应在列表里出现多行')
        self.assertEqual(matched[0]['sent_count'],
                         Message.objects.filter(title='聚合测试').count())
        # 驼峰孪生必须同时存在（前端读 sentCount）
        self.assertIn('sentCount', matched[0])

    def test_list_empty_before_any_publish(self):
        self.login_as(self.platform_admin)
        self.assertEqual(self.ok(self.client.get(self.LIST))['data'], [])

    def test_list_requires_platform_role(self):
        for user in (self.adopter, self.gov_city, self.gov_a):
            with self.subTest(role=user.role):
                self.login_as(user)
                self.expect_fail(self.client.get(self.LIST), status=403)

    def test_list_sees_every_district(self):
        """平台管理员是全局角色 → 各区的公告都要看得到（含全市公告）。"""
        self.ok(self._publish(title='甲区公告', district_id=self.district_a.id))
        self.ok(self._publish(title='乙区公告', district_id=self.district_b.id))
        self.ok(self._publish(title='全市公告'))
        titles = [r['title'] for r in self.ok(self.client.get(self.LIST))['data']]
        for t in ('甲区公告', '乙区公告', '全市公告'):
            self.assertIn(t, titles)

    def test_list_keeps_same_title_for_different_districts_separate(self):
        """⚠ 聚合键必须带 `district_id`：同名同文、不同受众 = **两条**公告。

        平台端可以分别给甲区、乙区发一模一样的公告。只按 `title + content`
        分组会把两条**不同受众**的公告并成一行，`sent_count` 变成两区之和 ——
        运营看到的触达人数与任何一次实际发布都对不上。
        """
        self.ok(self._publish(title='同名公告', district_id=self.district_a.id))
        self.ok(self._publish(title='同名公告', district_id=self.district_b.id))
        rows = [r for r in self.ok(self.client.get(self.LIST))['data']
                if r['title'] == '同名公告']
        self.assertEqual(len(rows), 2, '不同受众的同名公告不能被并成一行')
        self.assertEqual({r['districtId'] for r in rows},
                         {self.district_a.id, self.district_b.id})
        self.assertEqual([r['sentCount'] for r in rows], [1, 1])
