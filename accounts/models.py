from django.contrib.auth.models import AbstractUser
from django.db import models

from core.models import STATUS_ACTIVE


class User(AbstractUser):
    ROLE_CHOICES = [
        # ⚠ `platform_admin` 是**平台管理端**专用角色，与其它角色**不同端**：
        #   全局主数据的维护（区县 / 机构 / 账号 / 编号规则 / 公告）都在它名下，
        #   政府端（gov_city / gov_district）只保留查看。它不挂区县 ——
        #   可见范围恒为「全部」，见 core.scope.has_global_district_scope。
        ('platform_admin', '平台管理员'),
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
    status = models.CharField('状态', max_length=10, default=STATUS_ACTIVE)
    # R46 口径 2（**机制性口径，不随原型作废而失效**）：兽医签名 / 执业证号
    # **挂账号**，一次维护、每次自动带出。
    #
    # ⚠ 诊疗单落库时把它**快照**进 `Treatment.vet_license`，而不是每次实时读账号：
    #   证号是会变的（续期、换号），历史单据上的证号必须保持当时那一串 ——
    #   与 `Capture.operator_name` / `AuditLog.district_name` 是同一条约定。
    vet_license = models.CharField('兽医执业证号', max_length=100, blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    def __str__(self):
        return f'{self.username} ({self.get_role_display()})'

    @property
    def is_platform_admin(self):
        return self.role == 'platform_admin'

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
