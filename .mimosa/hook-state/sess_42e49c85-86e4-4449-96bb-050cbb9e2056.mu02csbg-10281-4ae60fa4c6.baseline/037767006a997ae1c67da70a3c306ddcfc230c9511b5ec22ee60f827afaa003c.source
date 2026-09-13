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
        expected = {'in_transit', 'in_treatment', 'pending_adopt', 'pending_claim',
                    'adopted', 'released', 'euthanized', 'owner_returned'}
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
        self.assertEqual({c for c, _ in Material.CATEGORY_CHOICES},
                         {'vaccine', 'dewormer', 'chip'})


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
