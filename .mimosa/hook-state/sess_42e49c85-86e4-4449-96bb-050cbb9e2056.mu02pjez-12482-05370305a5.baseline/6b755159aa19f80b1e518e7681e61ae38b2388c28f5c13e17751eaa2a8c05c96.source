"""定时任务测试：诊疗完成 5 天自动转待领养。"""
from datetime import timedelta

from django.utils import timezone

from business.models import AdoptionHallListing, Treatment
from business.tasks import auto_promote_to_adoptable
from business.tests.base import BusinessTestBase, make_institution, make_pet


def _completed_treatment(pet, days_ago=None):
    treatment = Treatment.objects.create(
        pet=pet, pet_code=pet.code, hospital=pet.hospital,
        status='completed', district=pet.district)
    if days_ago is not None:
        Treatment.objects.filter(id=treatment.id).update(
            created_at=timezone.now() - timedelta(days=days_ago))
    return treatment


class AutoPromoteTest(BusinessTestBase):
    def test_promotes_after_5_days(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=6)
        result = auto_promote_to_adoptable()
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')
        listing = AdoptionHallListing.objects.get(pet=pet)
        self.assertTrue(listing.is_active)
        self.assertEqual(listing.hospital, self.hospital_a)
        self.assertIn('Promoted 1', result)

    def test_not_promoted_before_5_days(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=4)
        auto_promote_to_adoptable()
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'in_treatment')

    def test_non_treatment_pet_untouched(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='adopted')
        _completed_treatment(pet, days_ago=10)
        auto_promote_to_adoptable()
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'adopted')

    def test_force_promotes_all_completed(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=0)
        auto_promote_to_adoptable(force=True)
        pet.refresh_from_db()
        self.assertEqual(pet.status, 'pending_adopt')

    def test_no_duplicate_listing(self):
        pet = make_pet(district=self.district_a, hospital=self.hospital_a,
                       status='in_treatment')
        _completed_treatment(pet, days_ago=6)
        auto_promote_to_adoptable()
        auto_promote_to_adoptable()
        self.assertEqual(AdoptionHallListing.objects.filter(pet=pet).count(), 1)
