from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    ROLE_CHOICES = [
        ('gov_city', '市级政府管理员'),
        ('gov_district', '区级政府管理员'),
        ('shelter', '捕捉点操作员'),
        ('hospital', '医院操作员'),
        ('adopter', '领养人'),
    ]
    role = models.CharField('角色', max_length=20, choices=ROLE_CHOICES, default='adopter')
    district = models.ForeignKey('core.District', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='所属区县')
    institution = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='所属机构')
    phone = models.CharField('电话', max_length=20, blank=True, default='')
    status = models.CharField('状态', max_length=10, default='active')  # active/inactive
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    def __str__(self):
        return f'{self.username} ({self.get_role_display()})'

    @property
    def is_gov_city(self):
        return self.role == 'gov_city'

    @property
    def is_gov_district(self):
        return self.role == 'gov_district'

    @property
    def is_shelter(self):
        return self.role == 'shelter'

    @property
    def is_hospital(self):
        return self.role == 'hospital'

    @property
    def is_adopter(self):
        return self.role == 'adopter'
