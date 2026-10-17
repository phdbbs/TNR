"""全生命周期端到端集成测试：捕捉 → 转运 → 签收 → 诊疗（含库存与芯片）→
自动转待领养 → 领养大厅 → 在线申请与线下登记 → 确认领出 / 放归闭环。"""
from business.models import (
    Adoption, AdoptionApplication, AdoptionHallListing, Capture, Message, Pet, Release,
)
from business.services import get_hospital_stock
from business.tests.base import BusinessTestBase, make_pet
API = '/api/business'


class FullLifecycleTest(BusinessTestBase):
    """两条支线：
    宠物A：诊疗完成 → 待领养 → 在线申请 → 审核通过；
    宠物B：放归闭环（小区确认）。
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.vaccine = None  # 在 setUp 中准备

    def setUp(self):
        super().setUp()
        # 医院备好疫苗与驱虫药库存
        self.vaccine = self._make_material('vaccine')
        self.dewormer = self._make_material('dewormer')

    # ---------- 工具 ----------
    def _login(self, user):
        self.client.force_login(user)

    def _post(self, url, payload):
        import json
        return self.client.post(url, data=json.dumps(payload),
                                content_type='application/json')

    def _make_material(self, category):
        from business.models import Material, MaterialTransaction
        from django.utils import timezone
        material = Material.objects.create(
            name=f'集成测试-{category}', category=category, unit='支',
            shelter_stock=999, district=self.district_a)
        MaterialTransaction.objects.create(
            material=material, material_name=material.name, quantity=50,
            type='receive', hospital=self.hospital_a,
            date=timezone.localdate(), district=self.district_a)
        return material

    def _available_chip(self):
        from business.models import Chip, Material
        if not Material.objects.filter(category='chip', district=self.district_a).exists():
            Material.objects.create(name='集成测试-芯片', category='chip', unit='个',
                                    shelter_stock=1, district=self.district_a)
        return Chip.objects.create(number='INTL-CHIP-000001')

    # ---------- 用例 ----------
    def test_full_lifecycle(self):
        # 1. 捕捉点批量登记 2 只猫
        self._login(self.shelter_user_a)
        body = self.ok(self.post_json(f'{API}/captures/create/', {
            'property_name': '集成测试物业',
            'community_id': self.community_a.id,
            'community_name': self.community_a.name,
            'address': '集成测试地址', 'pet_count': 2, 'species': '猫',
            'contact_person': '物业李四', 'contact_phone': '13800005678',
        }))
        pet_codes = body['data']['pet_codes']
        capture_id = body['data']['capture']['id']
        self.assertEqual(Pet.objects.filter(code__in=pet_codes).count(), 2)
        self.assertEqual(Capture.objects.get(id=capture_id).status, 'pending')

        # 2. 转运至甲区医院
        body = self.ok(self.post_json(f'{API}/transfers/create/', {
            'capture_id': capture_id,
            'to_hospital_id': self.hospital_a.id,
            'pet_codes': pet_codes,
        }))
        transfer_id = body['data'][0]['id']

        # 3. 医院签收
        self._login(self.hospital_user_a)
        self.ok(self.post_json(f'{API}/transfers/{transfer_id}/receive/'))
        pets = list(Pet.objects.filter(code__in=pet_codes).order_by('id'))
        self.assertTrue(all(p.status == 'in_treatment' for p in pets))

        # 4. 诊疗（三步流程）：绝育（含手术病历）+ 疫苗 + 驱虫 + 芯片
        #    第四十七轮起只有这一条写路径（旧「快速登记」接口已删），
        #    库存在「执行」那一刻才扣。
        chip = self._available_chip()
        pet_a, pet_b = pets
        stock_before = get_hospital_stock(self.vaccine, self.hospital_a)
        tid = self.ok(self._post(f'{API}/treatments/open/',
                                 {'pet_id': pet_a.id}))['data']['id']
        self.ok(self._post(f'{API}/treatments/{tid}/check/', {
            'temperature': 38.6, 'temperature_flag': 'normal'}))
        self.ok(self._post(f'{API}/treatments/{tid}/plan/', {
            'advice': '术后 24 小时观察精神与食欲',
            'items': [
                {'key': 'sterilization', 'notes': '常规绝育'},
                {'key': 'vaccine', 'planned_date': '2026-03-01',
                 'rx': [self.vaccine.id]},
                {'key': 'deworming', 'planned_date': '2026-03-01',
                 'rx': [self.dewormer.id]},
                {'key': 'chip', 'planned_date': '2026-03-01',
                 'chip_no': chip.number},
            ],
            'sterilization': {
                'surgery_date': '2026-03-01', 'surgeon': '集成医生',
                'diagnosis': '健康', 'anesthesia': '吸入麻醉',
                'procedure': '常规绝育', 'recovery': '良好',
            },
        }))
        detail = self.ok(self._post(f'{API}/treatments/{tid}/diagnose/', {}))['data']
        for item in detail['items']:
            self.ok(self._post(
                f'{API}/treatments/{tid}/items/{item["id"]}/execute/', {}))
        self.ok(self._post(f'{API}/treatments/{tid}/done/', {}))
        self.ok(self._post(f'{API}/treatments/{tid}/dispose/',
                           {'disposal': 'pending_adopt'}))

        self.assertEqual(get_hospital_stock(self.vaccine, self.hospital_a),
                         stock_before - 1)
        chip.refresh_from_db()
        self.assertEqual(chip.status, 'used')

        # 5. 办结去向「领养」必须**当场**进领养大厅（第四十七轮）。
        #    旧口径靠 `auto_promote_to_adoptable()` 在 5 天后补这一步；
        #    新流程 `dispose()` 同时落「诊疗单已完成」与「动物待领养」，
        #    那个中间态不存在了，定时任务（`test_tasks.py` 覆盖存量行）命中不到。
        pet_a.refresh_from_db()
        self.assertEqual(pet_a.status, 'pending_adopt')
        self.assertTrue(AdoptionHallListing.objects.get(pet=pet_a).is_active)

        # 6a. 在线申请支线：领养人申请 → 医院审核通过
        self._login(self.adopter)
        body = self.ok(self._post(f'{API}/adoptions/apply/', {
            'pet_id': pet_a.id, 'applicant_name': '在线领养人',
            'applicant_phone': '13877778888', 'reason': '有爱心有条件',
        }))
        application_id = body['data']['id']
        self._login(self.hospital_user_a)
        self.ok(self._post(
            f'{API}/adoptions/applications/{application_id}/review/',
            {'action': 'approve', 'review_note': '符合条件'}))
        self.assertEqual(
            AdoptionApplication.objects.get(id=application_id).status, 'approved')

        # 6b. 线下登记支线：宠物B 由捕捉点登记领养（走 pending_claim）
        #     先完成诊疗再把医院确认领出串起来
        #     ⚠ 这里只做到「诊疗完成」**不办结**：办结去向「放回原小区」会直接
        #     把 `Pet.status` 置成 released，而 `releases/create` 只接受
        #     `in_treatment` / `pending_adopt` —— 先办结就再也开不出放归单了。
        #     （「医院直接结案」与「需小区确认的放归单」是两条路径，本用例覆盖后者。）
        self._login(self.hospital_user_a)
        tid = self.ok(self._post(f'{API}/treatments/open/',
                                 {'pet_id': pet_b.id}))['data']['id']
        self.ok(self._post(f'{API}/treatments/{tid}/check/', {
            'temperature': 38.4, 'temperature_flag': 'normal'}))
        self.ok(self._post(f'{API}/treatments/{tid}/plan/', {
            'advice': '观察 24 小时', 'items': []}))
        self.ok(self._post(f'{API}/treatments/{tid}/diagnose/', {}))
        self.ok(self._post(f'{API}/treatments/{tid}/done/', {}))

        # 7. 放归闭环（宠物B 走放归而不是领养）
        self._login(self.shelter_user_a)
        body = self.ok(self._post(f'{API}/releases/create/', {'pet_id': pet_b.id}))
        release_id = body['data']['id']
        self.ok(self._post(f'{API}/releases/{release_id}/confirm/', {
            'receiver_name': '物业确认人', 'signature': 'sig'}))
        pet_b.refresh_from_db()
        self.assertEqual(pet_b.status, 'released')

        # 8. 溯源：宠物B 应有 捕捉→转运→诊疗→放归 完整事件链
        body = self.ok(self.client.get(f'{API}/pets/{pet_b.id}/lifecycle/'))
        types = [e['type'] for e in body['data']['events']]
        self.assertEqual(types, ['capture', 'transfer', 'treatment', 'release'])

    def test_offline_adoption_to_completed(self):
        """线下领养登记 → 医院确认领出 → 领养完成 + 消息通知。"""
        pet = make_pet(district=self.district_a, shelter=self.shelter_a,
                       hospital=self.hospital_a, status='pending_adopt')
        self._login(self.shelter_user_a)
        body = self.ok(self._post(f'{API}/adoptions/register/', {
            'pet_id': pet.id, 'adopter_name': '线下领养人',
            'adopter_phone': '13899990000',
        }))
        adoption_id = body['data']['id']
        adoption = Adoption.objects.get(id=adoption_id)
        adopter = adoption.adopter

        self._login(self.hospital_user_a)
        self.ok(self._post(f'{API}/adoptions/{adoption_id}/confirm-claim/', {}))
        pet.refresh_from_db()
        adoption.refresh_from_db()
        self.assertEqual(pet.status, 'adopted')
        self.assertEqual(adoption.status, 'completed')
        self.assertTrue(Message.objects.filter(user=adopter, title='领养完成').exists())
