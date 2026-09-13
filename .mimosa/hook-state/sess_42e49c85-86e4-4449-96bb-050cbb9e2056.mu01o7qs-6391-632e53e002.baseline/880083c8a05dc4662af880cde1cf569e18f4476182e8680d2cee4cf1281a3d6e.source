from django.db import models


class District(models.Model):
    name = models.CharField('区县名称', max_length=50)
    code = models.CharField('区县代码', max_length=20, unique=True)
    is_city = models.BooleanField('是否市级', default=False, help_text='市级区域用于市管理员/捕捉点操作员归属')
    status = models.CharField('状态', max_length=10, default='active')
    created_at = models.DateField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = '区县'
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.name


class Institution(models.Model):
    TYPE_CHOICES = [
        ('shelter', '捕捉点'),
        ('hospital', '医院'),
        ('community', '小区'),
    ]
    name = models.CharField('机构名称', max_length=100)
    type = models.CharField('机构类型', max_length=20, choices=TYPE_CHOICES)
    district = models.ForeignKey(District, on_delete=models.PROTECT, related_name='institutions', verbose_name='所属区县')
    address = models.CharField('地址', max_length=200, blank=True, default='')
    contact = models.CharField('联系人', max_length=50, blank=True, default='')
    phone = models.CharField('联系电话', max_length=20, blank=True, default='')
    status = models.CharField('状态', max_length=10, default='active')

    class Meta:
        ordering = ['id']
        verbose_name = '机构'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.name} ({self.get_type_display()})'
