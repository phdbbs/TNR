"""core 应用测试：区县与机构模型。"""
from django.db import IntegrityError, transaction
from django.test import TestCase

from business.tests.base import make_district, make_institution, make_user
from core.models import District, Institution


class DistrictModelTest(TestCase):
    def test_create_defaults(self):
        district = make_district(name='测试区', code='X001')
        self.assertEqual(district.status, 'active')
        self.assertFalse(district.is_city)

    def test_code_unique(self):
        make_district(code='SAME01')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_district(code='SAME01')

    def test_city_district_flag(self):
        district = make_district(is_city=True)
        self.assertTrue(district.is_city)


class InstitutionModelTest(TestCase):
    def test_create_with_type(self):
        district = make_district()
        shelter = make_institution(type='shelter', district=district)
        self.assertEqual(shelter.type, 'shelter')
        self.assertEqual(shelter.status, 'active')

    def test_types(self):
        for t in ('shelter', 'hospital', 'community'):
            self.assertEqual(make_institution(type=t).type, t)

    def test_district_protect(self):
        institution = make_institution()
        with self.assertRaises(Exception):
            institution.district.delete()

    def test_institution_delete_sets_user_institution_null(self):
        institution = make_institution(type='hospital')
        user = make_user(role='hospital', institution=institution)
        institution.delete()
        user.refresh_from_db()
        self.assertIsNone(user.institution)
