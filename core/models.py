from django.conf import settings
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
    # 稳定业务编号（I001/C001…），与 District.code 保持同一套约定。
    # 种子数据必须按 code 幂等匹配：机构名是会被人在界面上改的展示字段，
    # 早期按 name 做 get_or_create 去重，区县/机构被改名后每次部署都会
    # 重复插入一套同名机构（live 库里已出现孤儿重复数据）。
    code = models.CharField('机构编号', max_length=20, unique=True, null=True, blank=True)
    name = models.CharField('机构名称', max_length=100)
    type = models.CharField('机构类型', max_length=20, choices=TYPE_CHOICES)
    district = models.ForeignKey(District, on_delete=models.PROTECT, related_name='institutions', verbose_name='所属区县')
    address = models.CharField('地址', max_length=200, blank=True, default='')
    contact = models.CharField('联系人', max_length=50, blank=True, default='')
    phone = models.CharField('联系电话', max_length=20, blank=True, default='')
    status = models.CharField('状态', max_length=10, default='active')
    # 建档时间。写法与 `District.created_at` 保持一致（DateField + auto_now_add）。
    #
    # 为什么必须补：政府端「机构管理」列表要按起止时间筛选，而这个模型此前
    # **没有任何时间字段**。界面上的起止时间控件拿不到日期 → `inDateRange` 对
    # 「设了区间、但记录没有时间」返回 false → **一设日期就整列空表**，
    # 而且不报错、看起来就像「这个区间真的没有机构」。
    created_at = models.DateField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = '机构'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.name} ({self.get_type_display()})'


class AuditLog(models.Model):
    """业务操作审计日志。

    为什么不用 Django admin 的 ``LogEntry``：
    1. ``LogEntry`` **没有 district 字段**，政府端按区县隔离时只能退化成
       「按操作人的区县过滤」—— 而现场两个捕捉点操作员都挂在「全市（市级）」下，
       他们登记的捕捉单/转运单归属区县其实是襄城区/樊城区，按操作人过滤会让
       **本区县政府在自己的日志页里看不到本区的操作**。
       这里显式记录**业务记录的归属区县**（`district`），与捕捉单/转运单同一套口径。
    2. 业务接口从不经过 admin，``LogEntry`` 永远为空（实测 0 行 vs 业务记录 53 条）。
    3. 需要「模块 / 操作类型 / 对象 / 是否成功 / 来源IP」这些 admin 不关心的维度。

    ``district_name`` / ``user_name`` / ``role`` 是**写入时快照**：区县或账号被改名、
    停用后，历史日志仍应显示当时的名称（与 `Capture.operator_name` 同一约定）。
    """

    ACTION_ADD = 1
    ACTION_CHANGE = 2
    ACTION_DELETE = 3
    ACTION_CHOICES = [
        (ACTION_ADD, '新增'),
        (ACTION_CHANGE, '修改'),
        (ACTION_DELETE, '删除'),
    ]

    action_time = models.DateTimeField('操作时间', auto_now_add=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='audit_logs', verbose_name='操作人')
    user_name = models.CharField('操作人姓名', max_length=64, blank=True, default='')
    role = models.CharField('操作人角色', max_length=32, blank=True, default='')
    district = models.ForeignKey(
        District, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='audit_logs', verbose_name='归属区县')
    district_name = models.CharField('区县名称快照', max_length=50, blank=True, default='')
    module = models.CharField('业务模块', max_length=64, blank=True, default='')
    action_flag = models.PositiveSmallIntegerField(
        '操作类型', choices=ACTION_CHOICES, default=ACTION_CHANGE)
    object_type = models.CharField('对象类型', max_length=64, blank=True, default='')
    object_id = models.CharField('对象ID', max_length=64, blank=True, default='')
    object_repr = models.CharField('对象标识', max_length=200, blank=True, default='')
    summary = models.CharField('操作摘要', max_length=255, blank=True, default='')
    detail = models.TextField('操作详情', blank=True, default='')
    method = models.CharField('请求方法', max_length=8, blank=True, default='')
    path = models.CharField('请求路径', max_length=255, blank=True, default='')
    success = models.BooleanField('是否成功', default=True)
    ip = models.GenericIPAddressField('来源IP', null=True, blank=True)

    class Meta:
        ordering = ['-action_time', '-id']
        verbose_name = '操作日志'
        verbose_name_plural = verbose_name
        indexes = [
            models.Index(fields=['-action_time'], name='audit_time_idx'),
            models.Index(fields=['district', '-action_time'], name='audit_dist_time_idx'),
        ]

    def __str__(self):
        return f'[{self.action_time:%Y-%m-%d %H:%M}] {self.user_name} {self.summary}'
