"""门户页面、门户辅助 API 与宠物全生命周期溯源测试。"""
import re
from datetime import datetime, timezone as dt_timezone

from business.models import (
    Adoption, AdoptionHallListing, Message, OwnerReturn, Release, Transfer,
    Treatment,
)
from business.tests.base import (
    BusinessTestBase, make_capture, make_institution, make_pet, make_user,
)


class PortalPageTest(BusinessTestBase):
    def test_portal_redirects_by_role(self):
        cases = [
            (self.shelter_user_a, '/shelter/'),
            (self.hospital_user_a, '/hospital/'),
            (self.adopter, '/adopter/'),
            (self.gov_city, '/gov/'),
        ]
        for user, url in cases:
            self.client.force_login(user)
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, f'{user.username} → {url}')
            self.assertIn('text/html', resp['Content-Type'])

    def test_wrong_role_redirected(self):
        self.client.force_login(self.adopter)
        resp = self.client.get('/gov/')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, '/')

    def test_anonymous_page_redirects_to_login(self):
        resp = self.client.get('/shelter/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)

    def test_portal_public_page(self):
        resp = self.client.get('/portal/')
        self.assertEqual(resp.status_code, 200)

    def test_adoption_hall_public_page_anonymous(self):
        resp = self.client.get('/adopter/hall/')
        self.assertEqual(resp.status_code, 200)

    def test_adoption_hall_public_page_adopter_gets_full_portal(self):
        self.client.force_login(self.adopter)
        resp = self.client.get('/adopter/hall/')
        self.assertEqual(resp.status_code, 200)


class PortalApiTest(BusinessTestBase):
    def test_hospital_pets_scoped(self):
        pet_a = make_pet(district=self.district_a, hospital=self.hospital_a)
        make_pet(district=self.district_b, hospital=self.hospital_b)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json('/api/business/pets/'))['data']
        self.assertEqual([p['id'] for p in data], [pet_a.id])

    def test_hospital_pets_status_filter(self):
        make_pet(district=self.district_a, hospital=self.hospital_a,
                 status='in_treatment')
        released = make_pet(district=self.district_a, hospital=self.hospital_a,
                            status='released')
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json('/api/business/pets/?status=released'))['data']
        self.assertEqual([p['id'] for p in data], [released.id])

    def test_hall_listings_adopter_sees_active_only(self):
        pet1 = make_pet(district=self.district_a, status='pending_adopt')
        pet2 = make_pet(district=self.district_a, status='pending_adopt')
        active = AdoptionHallListing.objects.create(pet=pet1, hospital=self.hospital_a)
        AdoptionHallListing.objects.create(pet=pet2, hospital=self.hospital_a,
                                           is_active=False)
        self.login_as(self.adopter)
        data = self.ok(self.get_json('/api/business/hall-listings/'))['data']
        self.assertEqual([l['id'] for l in data], [active.id])

    def test_hall_listings_hospital_sees_all_own(self):
        pet1 = make_pet(district=self.district_a, status='pending_adopt')
        pet2 = make_pet(district=self.district_a, status='pending_adopt')
        AdoptionHallListing.objects.create(pet=pet1, hospital=self.hospital_a)
        AdoptionHallListing.objects.create(pet=pet2, hospital=self.hospital_a,
                                           is_active=False)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json('/api/business/hall-listings/'))['data']
        self.assertEqual(len(data), 2, '医院端应包含已下架的上架信息')

    def test_my_adoptions(self):
        pet = make_pet(district=self.district_a, status='adopted')
        Adoption.objects.create(pet=pet, pet_code=pet.code, adopter=self.adopter,
                                adopter_name='领养人', adopter_phone='13800000001',
                                hospital=self.hospital_a, status='completed',
                                district=self.district_a)
        self.login_as(self.adopter)
        data = self.ok(self.get_json('/api/business/portal/adoptions/'))['data']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['pet']['id'], pet.id)

    def test_messages_and_mark_read(self):
        message = Message.objects.create(user=self.adopter, type='system',
                                         title='欢迎', content='内容')
        self.login_as(self.adopter)
        data = self.ok(self.get_json('/api/business/portal/messages/'))['data']
        self.assertEqual([m['id'] for m in data], [message.id])

        # GET 标记已读 → 405
        self.expect_fail(self.client.get(f'/api/business/portal/messages/{message.id}/read/'),
                  status=405)
        # 他人消息 → 404
        other = Message.objects.create(user=self.gov_city, type='system',
                                       title='x', content='y')
        self.expect_fail(self.post_json(f'/api/business/portal/messages/{other.id}/read/'),
                  status=404)
        # 正常标记
        self.ok(self.post_json(f'/api/business/portal/messages/{message.id}/read/'))
        message.refresh_from_db()
        self.assertTrue(message.is_read)


class PetLifecycleTest(BusinessTestBase):
    def _link_adopter(self, pet, ledger_no='ADP-LIFE-1'):
        """把宠物关联到本测试领养人（溯源接口按角色收敛可见范围）。"""
        return Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter=self.adopter,
            adopter_name='领养人', adopter_phone='13800009999',
            status='completed', ledger_no=ledger_no, district=pet.district)

    def _full_history_pet(self):
        capture = make_capture(district=self.district_a, shelter=self.shelter_a,
                               community=self.community_a,
                               pet_codes=['LIFE-PET-01'], ledger_no='CAP-LIFE-1')
        pet = make_pet(code='LIFE-PET-01', district=self.district_a,
                       shelter=self.shelter_a, hospital=self.hospital_a,
                       status='released', capture=capture)
        Transfer.objects.create(
            from_shelter=self.shelter_a, to_hospital=self.hospital_a,
            pet_codes=pet.code, pet_count=1, status='received',
            ledger_no='TRF-LIFE-1', district=self.district_a)
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital=self.hospital_a,
            items_sterilization=True, items_chip=True, chip_no='LIFECHIP01',
            status='completed', ledger_no='TRE-LIFE-1', district=self.district_a)
        Release.objects.create(
            pet=pet, pet_code=pet.code, community=self.community_a,
            community_name=self.community_a.name, status='released',
            ledger_no='REL-LIFE-1', district=self.district_a)
        return pet

    def test_lifecycle_events_ordered(self):
        pet = self._full_history_pet()
        self._link_adopter(pet)
        self.login_as(self.adopter)
        body = self.ok(self.get_json(
            f'/api/business/pets/{pet.id}/lifecycle/'))
        events = body['data']['events']
        types = [e['type'] for e in events]
        self.assertIn('capture', types)
        self.assertIn('transfer', types)
        self.assertIn('treatment', types)
        self.assertIn('release', types)
        # 事件按日期升序（捕捉最先产生）
        self.assertEqual(types[0], 'capture')
        capture_event = events[0]
        self.assertEqual(capture_event['ledger_no'], 'CAP-LIFE-1')
        self.assertEqual(capture_event['shelter_name'], self.shelter_a.name)
        treatment_event = next(e for e in events if e['type'] == 'treatment')
        self.assertEqual(treatment_event['items'], ['绝育', '芯片'])
        self.assertEqual(treatment_event['chip_no'], 'LIFECHIP01')

    def test_lifecycle_unknown_pet_404(self):
        self.login_as(self.adopter)
        self.expect_fail(self.get_json('/api/business/pets/999999/lifecycle/'),
                  status=404, message='宠物不存在')

    def test_lifecycle_minimal_pet_no_events(self):
        """无任何业务记录的动物返回空事件列表。"""
        pet = make_pet(district=self.district_a)
        self.login_as(self.gov_city)
        body = self.ok(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'))
        self.assertEqual(body['data']['events'], [])

    def test_lifecycle_adopter_sees_own_adoption_event(self):
        pet = make_pet(district=self.district_a)
        self._link_adopter(pet)
        self.login_as(self.adopter)
        body = self.ok(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'))
        self.assertEqual([e['type'] for e in body['data']['events']], ['adoption'])

    def test_lifecycle_other_peoples_pet_404(self):
        """领养人不得查看与自己无关的动物档案（防止遍历主键）。"""
        pet = make_pet(district=self.district_a)
        self.login_as(self.adopter)
        self.expect_fail(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'),
                         status=404, message='无权访问')

    def test_lifecycle_staff_scoped_by_district(self):
        """区级监管只能看本区动物。"""
        pet = make_pet(district=self.district_b)
        self.login_as(self.gov_a)
        self.expect_fail(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'),
                         status=404, message='无权访问')
        self.login_as(self.gov_b)
        self.ok(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'))

    def test_lifecycle_requires_login(self):
        pet = make_pet(district=self.district_a)
        self.expect_fail(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'),
                  status=401)


class PetLifecycleEventDateTest(BusinessTestBase):
    """宠物全生命周期 `events[].date` 一律是**本地日期**串（`YYYY-MM-DD`）。

    前端（捕捉端 timeline、领养人端 timeline）都直接把它当日期用。
    历史上 `owner_return` 分支写的是 `r.return_time.isoformat()` ——
    `return_time` 是 DateTimeField，`.isoformat()` 给的是 **UTC 带偏移**的时间串；
    前端 `.substring(0, 10)` 于是拿到 **UTC 日期**，本地 00:00–08:00 的回收记录
    会显示成**前一天**。同一字段「有时纯日期、有时 UTC 时间串」本身也是缺陷。

    本类锁死「一律纯本地日期」，与同文件其它 6 个分支
    （`timezone.localdate(...)` / `DateField.isoformat()`）的口径一致。
    """

    LOCAL_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

    def _pet_with_capture(self, code):
        cap = make_capture(district=self.district_a, shelter=self.shelter_a,
                           pet_codes=[code])
        return make_pet(code=code, district=self.district_a,
                        shelter=self.shelter_a, capture=cap)

    def _events(self, pet):
        self.login_as(self.shelter_user_a)
        body = self.ok(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'))
        return body['data']['events']

    def _make_owner_return(self, pet, ledger_no, **kw):
        return OwnerReturn.objects.create(
            pet=pet, ledger_no=ledger_no, owner_name='张三', reason='走失',
            district=self.district_a, **kw)

    def test_every_event_date_is_plain_local_date(self):
        pet = self._pet_with_capture('TNR-LC-1')
        self._make_owner_return(
            pet, 'OR-LC-1',
            return_time=datetime(2026, 9, 22, 20, 30, tzinfo=dt_timezone.utc))
        events = self._events(pet)
        self.assertTrue(events, '时间线不应为空')
        for ev in events:
            self.assertRegex(
                ev['date'], self.LOCAL_DATE_RE,
                f"{ev['type']} 的 date 不是纯日期：{ev['date']!r}")

    def test_owner_return_date_is_local_not_utc(self):
        """UTC 深夜（本地已跨日）：`date` 必须落在**本地**那一天。"""
        pet = self._pet_with_capture('TNR-LC-2')
        # 2026-09-22T20:30Z == 北京时间 2026-09-23 04:30
        self._make_owner_return(
            pet, 'OR-LC-2',
            return_time=datetime(2026, 9, 22, 20, 30, tzinfo=dt_timezone.utc))
        ev = next(e for e in self._events(pet) if e['type'] == 'owner_return')
        self.assertEqual(
            ev['date'], '2026-09-23',
            '`date` 用了 UTC 日期 —— 本地是 9/23，UTC 才是 9/22。')

    def test_owner_return_without_return_time_falls_back_to_created_at(self):
        """`return_time` 为空时退到 `created_at`，同样必须是本地日期。"""
        pet = self._pet_with_capture('TNR-LC-3')
        self._make_owner_return(pet, 'OR-LC-3')      # 不传 return_time
        ev = next(e for e in self._events(pet) if e['type'] == 'owner_return')
        self.assertRegex(ev['date'], self.LOCAL_DATE_RE,
                         f"退到 created_at 的 date 不是纯日期：{ev['date']!r}")
