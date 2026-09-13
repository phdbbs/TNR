"""诊疗视图测试：四项登记、库存联动、芯片绑定、状态机。"""
from django.db.models import Max

from business.models import Chip, MaterialTransaction, Treatment
from business.services import get_hospital_stock
from business.tests.base import (
    BusinessTestBase, make_chip, make_hospital_txn, make_material, make_pet,
    make_user,
)

URL = '/api/business/treatments/'


class TreatmentCreateTest(BusinessTestBase):
    def _pet(self, **kw):
        defaults = dict(district=self.district_a, hospital=self.hospital_a,
                        status='in_treatment')
        defaults.update(kw)
        return make_pet(**defaults)

    def _create(self, pet, payload=None):
        self.login_as(self.hospital_user_a)
        body = {'pet_id': pet.id}
        body.update(payload or {})
        return self.post_json(f'{URL}create/', body)

    def test_minimal_in_progress(self):
        pet = self._pet()
        body = self.ok(self._create(pet, {'items': {}}))
        treatment = Treatment.objects.get(id=body['data']['id'])
        self.assertEqual(treatment.status, 'in_progress')
        self.assertTrue(treatment.ledger_no.startswith('TRE-'))
        self.assertEqual(treatment.hospital, self.hospital_a)
        self.assertEqual(treatment.pet_code, pet.code)
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment')

    def test_sterilization_fields_saved(self):
        pet = self._pet()
        body = self.ok(self._create(pet, {
            'items': {'sterilization': True},
            'sterilization': {'surgery_date': '2026-03-01', 'surgeon': '赵医生',
                              'diagnosis': '健康', 'anesthesia': '呼吸麻醉',
                              'procedure': '常规绝育', 'recovery': '良好'},
        }))
        treatment = Treatment.objects.get(id=body['data']['id'])
        self.assertTrue(treatment.items_sterilization)
        self.assertEqual(treatment.sterilization_surgeon, '赵医生')
        self.assertEqual(str(treatment.sterilization_surgery_date), '2026-03-01')

    def test_vaccine_consumes_hospital_stock(self):
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 10)
        body = self.ok(self._create(pet, {
            'items': {'vaccine': True},
            'vaccine': {'type': material.name, 'material_id': material.id,
                        'batch_no': 'B1', 'date': '2026-03-01', 'quantity': 2},
        }))
        treatment = Treatment.objects.get(id=body['data']['id'])
        self.assertTrue(treatment.items_vaccine)
        self.assertEqual(treatment.vaccine_quantity, 2)
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 8)
        self.assertTrue(MaterialTransaction.objects.filter(
            material=material, hospital=self.hospital_a, type='consume').exists())

    def test_vaccine_out_of_stock_rejected(self):
        pet = self._pet()
        material = make_material(category='vaccine', district=self.district_a)
        resp = self._create(pet, {
            'items': {'vaccine': True},
            'vaccine': {'material_id': material.id, 'quantity': 1},
        })
        self.expect_fail(resp, message='疫苗库存不足')
        self.assertFalse(MaterialTransaction.objects.filter(
            material=material, hospital=self.hospital_a, type='consume').exists())

    def test_deworming_consumes_stock(self):
        pet = self._pet()
        material = make_material(category='dewormer', district=self.district_a)
        make_hospital_txn(material, self.hospital_a, 'receive', 5)
        self.ok(self._create(pet, {
            'items': {'deworming': True},
            'deworming': {'material_id': material.id, 'quantity': 1},
        }))
        self.assertEqual(get_hospital_stock(material, self.hospital_a), 4)

    def test_deworming_out_of_stock_rejected(self):
        pet = self._pet()
        material = make_material(category='dewormer', district=self.district_a)
        self.expect_fail(self._create(pet, {
            'items': {'deworming': True},
            'deworming': {'material_id': material.id, 'quantity': 3},
        }), message='驱虫药库存不足')

    def test_chip_binds_pet_and_consumes(self):
        pet = self._pet()
        chip = make_chip()
        chip_material = make_material(category='chip', district=self.district_a)
        make_hospital_txn(chip_material, self.hospital_a, 'receive', 5)
        self.ok(self._create(pet, {
            'items': {'chip': True},
            'chip': {'chip_no': chip.number, 'date': '2026-03-01'},
        }))
        chip.refresh_from_db()
        pet.refresh_from_db()
        self.assertEqual(chip.status, 'used')
        self.assertEqual(chip.pet, pet)
        self.assertEqual(pet.chip_no, chip.number)

    def test_reused_chip_rejected(self):
        pet = self._pet()
        chip = make_chip(status='used')
        resp = self._create(pet, {
            'items': {'chip': True},
            'chip': {'chip_no': chip.number},
        })
        self.expect_fail(resp, message='已被使用')

    def test_missing_chip_rejected(self):
        pet = self._pet()
        self.expect_fail(self._create(pet, {
            'items': {'chip': True},
            'chip': {'chip_no': 'GHOST-CHIP'},
        }), message='不存在')

    def test_pending_adopt_pet_can_be_treated(self):
        pet = self._pet(status='pending_adopt')
        self.ok(self._create(pet, {'items': {}}))

    def test_released_pet_rejected(self):
        pet = self._pet(status='released')
        self.expect_fail(self._create(pet, {'items': {}}), message='不可诊疗')

    def test_missing_pet_id(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {}), message='缺少宠物ID')

    def test_unknown_pet(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': 999999}),
                  message='宠物不存在')

    def test_shelter_cannot_create(self):
        pet = self._pet()
        self.login_as(self.shelter_user_a)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}), status=403)

    def test_hospital_quirk_no_hospital_info(self):
        """宠物与操作员都没有医院信息时应被拒绝。"""
        pet = make_pet(district=self.district_a, status='in_treatment', hospital=None)
        user = make_user(role='hospital', district=self.district_a)
        self.login_as(user)
        self.expect_fail(self.post_json(f'{URL}create/', {'pet_id': pet.id}),
                  message='缺少医院信息')


class TreatmentListTest(BusinessTestBase):
    def test_hospital_sees_own_only(self):
        pet_a = make_pet(district=self.district_a, hospital=self.hospital_a,
                         status='in_treatment')
        pet_b = make_pet(district=self.district_b, hospital=self.hospital_b,
                         status='in_treatment')
        Treatment.objects.create(pet=pet_a, pet_code=pet_a.code,
                                 hospital=self.hospital_a, district=self.district_a)
        Treatment.objects.create(pet=pet_b, pet_code=pet_b.code,
                                 hospital=self.hospital_b, district=self.district_b)
        self.login_as(self.hospital_user_a)
        data = self.ok(self.get_json(URL))['data']
        self.assertEqual({t['hospital_id'] for t in data}, {self.hospital_a.id})

    def test_status_filter(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        Treatment.objects.create(pet=pet, pet_code=pet.code,
                                 hospital=self.hospital_a, district=self.district_a)
        self.login_as(self.gov_city)
        data = self.ok(self.get_json(URL + '?status=in_progress'))['data']
        self.assertTrue(all(t['status'] == 'in_progress' for t in data))


class TreatmentDetailTest(BusinessTestBase):
    def test_detail_includes_pet(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        treatment = Treatment.objects.create(pet=pet, pet_code=pet.code,
                                             hospital=self.hospital_a,
                                             district=self.district_a)
        self.login_as(self.hospital_user_a)
        body = self.ok(self.get_json(f'{URL}{treatment.id}/'))
        self.assertEqual(body['data']['pet']['id'], pet.id)

    def test_unknown_404(self):
        self.login_as(self.hospital_user_a)
        self.expect_fail(self.get_json(f'{URL}999999/'), status=404, message='诊疗记录不存在')
