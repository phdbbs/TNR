from django.db import models


class SystemConfig(models.Model):
    """系统配置（键值对存储）"""
    key = models.CharField('配置键', max_length=50, unique=True)
    value = models.TextField('配置值', blank=True, default='')
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '系统配置'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.key} = {self.value}'
