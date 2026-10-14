"""模型层测试：约束、默认值、级联与保护行为。"""
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from business.models import (
    Adoption, AdoptionApplication, AdoptionHallListing, Blacklist, Capture,
    CheckIn, Chip, Euthanasia, Material, MaterialTransaction, Message, OwnerReturn,
    Pet, Release, Transfer, Treatment,
)
from business.tests.base import (
    make_chip, make_district, make_institution, make_material, make_pet,
    make_user,
)
from core.models import Institution


class PetModelTest(TestCase):
    def test_default_status_in_transit(self):
        self.assertEqual(make_pet().status, 'in_transit')

    def test_code_unique(self):
        make_pet(code='DUP-1')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_pet(code='DUP-1')

    def test_species_choices(self):
        self.assertEqual(dict(Pet._meta.get_field('species').choices), {'猫': '猫', '狗': '狗'})

    def test_status_choices_complete(self):
        # `dead`（死亡/无害化）是第四十六轮从 `euthanized` 里拆出来的**独立状态**：
        # 原 `euthanized` 的 label 是「已死亡」，而 v5.1 原型的「办结去向」是
        # 两个选项（安乐死 / 死亡无害化），后续单据也不同（安乐死单 / 无害化单）。
        # 产生点：`services_treatment.dispose()` 按去向 key 直接赋给 `Pet.status`。
        expected = {'in_transit', 'in_treatment', 'pending_adopt', 'pending_claim',
                    'adopted', 'released', 'euthanized', 'dead', 'owner_returned'}
        self.assertEqual({c for c, _ in Pet.STATUS_CHOICES}, expected)

    def test_district_protect(self):
        pet = make_pet()
        with self.assertRaises(ProtectedError):
            pet.district.delete()

    def test_default_gender_blank_ok(self):
        pet = make_pet()
        self.assertEqual(pet.name, '')
        self.assertEqual(pet.chip_no, '')


class ChipModelTest(TestCase):
    def test_default_available(self):
        self.assertEqual(make_chip().status, 'available')

    def test_number_unique(self):
        make_chip(number='CHI-DUP')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_chip(number='CHI-DUP')


class MaterialModelTest(TestCase):
    def test_default_stock_zero(self):
        material = Material.objects.create(
            name='零库存物料', category='vaccine', unit='支', district=make_district())
        self.assertEqual(material.shelter_stock, 0)

    def test_category_choices(self):
        # `medicine`（药品）是第四十六轮加的：R46 口径 1 —— 处方药品走
        # **现有物料体系**（从已下发的药品里选、扣医院侧库存），
        # 不新建独立药品字典。
        # 产生点：采购入库 `views_material.purchase_create()` 的 `category` 参数；
        # 消费点：`services_treatment` 的 `RX_CATEGORIES`（可开处方的类别）。
        self.assertEqual({c for c, _ in Material.CATEGORY_CHOICES},
                         {'vaccine', 'dewormer', 'chip', 'medicine'})


class TransactionModelTest(TestCase):
    def test_type_choices(self):
        self.assertEqual({t for t, _ in MaterialTransaction.TYPE_CHOICES},
                         {'purchase', 'dispatch', 'receive', 'consume', 'adjustment'})


class TransferModelTest(TestCase):
    def test_default_pending(self):
        from business.models import Transfer
        transfer = Transfer.objects.create(
            from_shelter=make_institution(type='shelter'),
            to_hospital=make_institution(type='hospital'),
            pet_codes='X1', pet_count=1,
            district=make_district(),
        )
        self.assertEqual(transfer.status, 'pending')


class TreatmentModelTest(TestCase):
    def test_default_in_progress(self):
        treatment = Treatment.objects.create(pet=make_pet(), district=make_district())
        self.assertEqual(treatment.status, 'in_progress')


class DistrictModelTest(TestCase):
    def test_code_unique(self):
        make_district(code='UNIQ-01')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_district(code='UNIQ-01')


class InstitutionModelTest(TestCase):
    def test_type_choices(self):
        self.assertEqual({t for t, _ in Institution._meta.get_field('type').choices},
                         {'shelter', 'hospital', 'community'})


class MessageModelTest(TestCase):
    def test_default_unread(self):
        message = Message.objects.create(
            user=make_user(role='adopter'), type='system', title='标题', content='内容')
        self.assertFalse(message.is_read)


class AdoptionHallListingModelTest(TestCase):
    def test_default_active_and_one_to_one(self):
        pet = make_pet()
        listing = AdoptionHallListing.objects.create(pet=pet)
        self.assertTrue(listing.is_active)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AdoptionHallListing.objects.create(pet=pet)


class EuthanasiaModelTest(TestCase):
    def test_body_received_default_false(self):
        record = Euthanasia.objects.create(pet=make_pet(), reason='测试', district=make_district())
        self.assertFalse(record.body_received)
