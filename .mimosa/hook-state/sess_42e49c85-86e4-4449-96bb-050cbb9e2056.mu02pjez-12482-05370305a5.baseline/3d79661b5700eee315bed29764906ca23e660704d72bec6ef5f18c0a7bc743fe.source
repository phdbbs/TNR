from django.db import models


# ============================================
# 流浪动物档案
# ============================================
class Pet(models.Model):
    STATUS_CHOICES = [
        ('in_transit', '在途'),
        ('in_treatment', '待诊疗'),
        ('pending_adopt', '待领养'),
        ('pending_claim', '待领出'),
        ('adopted', '已领养'),
        ('released', '已放养'),
        ('euthanized', '已安乐死'),
        ('owner_returned', '主人领回'),
    ]
    SPECIES_CHOICES = [
        ('猫', '猫'),
        ('狗', '狗'),
    ]
    GENDER_CHOICES = [
        ('公', '公'),
        ('母', '母'),
    ]
    code = models.CharField('档案编号', max_length=30, unique=True, help_text='TNR+年月日+序号, 如 TNR2501001')
    name = models.CharField('名称', max_length=50, blank=True, default='')
    species = models.CharField('物种', max_length=10, choices=SPECIES_CHOICES)
    breed = models.CharField('品种', max_length=50, blank=True, default='')
    gender = models.CharField('性别', max_length=10, choices=GENDER_CHOICES, blank=True, default='')
    age = models.CharField('年龄', max_length=20, blank=True, default='')
    color = models.CharField('毛色', max_length=30, blank=True, default='')
    weight = models.CharField('体重', max_length=20, blank=True, default='')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='in_transit')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='pets', verbose_name='所属区县')
    capture = models.ForeignKey('business.Capture', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='捕捉记录')
    shelter = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='pets', verbose_name='捕捉点')
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='hospital_pets', verbose_name='医院')
    chip_no = models.CharField('芯片号', max_length=30, blank=True, default='')
    description = models.TextField('描述', blank=True, default='')
    photo_group = models.ImageField('合照', upload_to='photos/', null=True, blank=True)
    photo_before = models.ImageField('术前照片', upload_to='photos/', null=True, blank=True)
    photo_after = models.ImageField('术后照片', upload_to='photos/', null=True, blank=True)
    photo_treatment = models.ImageField('诊疗照片', upload_to='photos/', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '宠物档案'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.code} ({self.name})'


# ============================================
# 捕捉记录
# ============================================
class Capture(models.Model):
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='captures', verbose_name='所属区县')
    shelter = models.ForeignKey('core.Institution', on_delete=models.PROTECT, related_name='captures', verbose_name='捕捉点')
    shelter_name = models.CharField('捕捉点名称', max_length=100, blank=True, default='')
    community = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='community_captures', verbose_name='小区')
    community_name = models.CharField('小区名称', max_length=100, blank=True, default='')
    address = models.CharField('捕捉地址', max_length=200, blank=True, default='')
    property_name = models.CharField('物业名称', max_length=100, blank=True, default='')
    contact_person = models.CharField('联系人', max_length=50, blank=True, default='')
    contact_phone = models.CharField('联系电话', max_length=20, blank=True, default='')
    pet_count = models.IntegerField('动物数量', default=0)
    pet_codes = models.TextField('动物编号', blank=True, default='', help_text='逗号分隔')
    group_photo = models.ImageField('合照', upload_to='photos/', null=True, blank=True)
    signature = models.TextField('签字', blank=True, default='', help_text='base64')
    status = models.CharField('状态', max_length=20, default='completed')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='captures', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '捕捉记录'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.ledger_no or self.id} - {self.shelter_name}'


# ============================================
# 主人领回
# ============================================
class OwnerReturn(models.Model):
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='owner_returns', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    owner_name = models.CharField('主人姓名', max_length=50)
    owner_phone = models.CharField('主人电话', max_length=20, blank=True, default='')
    owner_id_card = models.CharField('主人身份证', max_length=30, blank=True, default='')
    reason = models.TextField('领回原因')
    signature = models.TextField('签字', blank=True, default='')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='owner_returns', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='owner_returns', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '主人领回'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.pet_code} - {self.owner_name}'


# ============================================
# 转运记录
# ============================================
class Transfer(models.Model):
    STATUS_CHOICES = [
        ('pending', '待签收'),
        ('received', '已签收'),
        ('rejected', '已驳回'),
    ]
    capture = models.ForeignKey('business.Capture', on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers', verbose_name='捕捉记录')
    from_shelter = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_transfers', verbose_name='发出捕捉点')
    from_shelter_name = models.CharField('发出捕捉点名称', max_length=100, blank=True, default='')
    to_hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='received_transfers', verbose_name='接收医院')
    to_hospital_name = models.CharField('接收医院名称', max_length=100, blank=True, default='')
    pet_codes = models.TextField('动物编号', blank=True, default='', help_text='逗号分隔')
    pet_count = models.IntegerField('动物数量', default=0)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    received_at = models.DateField('签收日期', null=True, blank=True)
    reject_reason = models.TextField('驳回原因', blank=True, default='')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='transfers', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '转运记录'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.from_shelter_name} -> {self.to_hospital_name}'


# ============================================
# 诊疗记录
# ============================================
class Treatment(models.Model):
    STATUS_CHOICES = [
        ('in_progress', '进行中'),
        ('completed', '已完成'),
    ]
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='treatments', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='treatments', verbose_name='医院')
    hospital_name = models.CharField('医院名称', max_length=100, blank=True, default='')
    items_sterilization = models.BooleanField('绝育', default=False)
    items_vaccine = models.BooleanField('疫苗', default=False)
    items_deworming = models.BooleanField('驱虫', default=False)
    items_chip = models.BooleanField('芯片', default=False)
    sterilization_surgery_date = models.DateField('绝育手术日期', null=True, blank=True)
    sterilization_surgeon = models.CharField('主刀医生', max_length=50, blank=True, default='')
    sterilization_diagnosis = models.TextField('诊断', blank=True, default='')
    sterilization_anesthesia = models.CharField('麻醉方式', max_length=50, blank=True, default='')
    sterilization_procedure = models.TextField('手术过程', blank=True, default='')
    sterilization_recovery = models.TextField('术后恢复', blank=True, default='')
    vaccine_type = models.CharField('疫苗类型', max_length=50, blank=True, default='')
    vaccine_batch_no = models.CharField('疫苗批号', max_length=50, blank=True, default='')
    vaccine_date = models.DateField('疫苗日期', null=True, blank=True)
    vaccine_quantity = models.IntegerField('疫苗数量', default=0)
    deworming_type = models.CharField('驱虫类型', max_length=50, blank=True, default='')
    deworming_batch_no = models.CharField('驱虫批号', max_length=50, blank=True, default='')
    deworming_date = models.DateField('驱虫日期', null=True, blank=True)
    deworming_quantity = models.IntegerField('驱虫数量', default=0)
    chip_no = models.CharField('芯片号', max_length=30, blank=True, default='')
    chip_date = models.DateField('植入日期', null=True, blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='in_progress')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='treatments', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='treatments', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '诊疗记录'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.pet_code} - {self.hospital_name}'


# ============================================
# 物资
# ============================================
class Material(models.Model):
    CATEGORY_CHOICES = [
        ('vaccine', '疫苗'),
        ('dewormer', '驱虫药'),
        ('chip', '芯片'),
    ]
    name = models.CharField('物资名称', max_length=100)
    category = models.CharField('类别', max_length=20, choices=CATEGORY_CHOICES)
    unit = models.CharField('单位', max_length=20)
    specification = models.CharField('规格', max_length=100, blank=True, default='')
    supplier = models.CharField('供应商', max_length=100, blank=True, default='')
    batch_no = models.CharField('批号', max_length=50, blank=True, default='')
    shelter_stock = models.IntegerField('捕捉点库存', default=0)
    safety_stock = models.IntegerField('安全库存', default=0)
    expiry_date = models.DateField('过期日期', null=True, blank=True)
    chip_range_start = models.CharField('芯片起始号', max_length=30, blank=True, default='')
    chip_range_end = models.CharField('芯片结束号', max_length=30, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='materials', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '物资'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.name} ({self.get_category_display()})'


# ============================================
# 物资流水
# ============================================
class MaterialTransaction(models.Model):
    TYPE_CHOICES = [
        ('purchase', '采购入库'),
        ('dispatch', '下发'),
        ('receive', '医院签收'),
        ('consume', '消耗'),
        ('adjustment', '异动'),
    ]
    type = models.CharField('类型', max_length=20, choices=TYPE_CHOICES)
    material = models.ForeignKey('business.Material', on_delete=models.PROTECT, related_name='transactions', verbose_name='物资')
    material_name = models.CharField('物资名称', max_length=100, blank=True, default='')
    quantity = models.IntegerField('数量')
    unit = models.CharField('单位', max_length=20, blank=True, default='')
    batch_no = models.CharField('批号', max_length=50, blank=True, default='')
    supplier = models.CharField('供应商', max_length=100, blank=True, default='')
    from_to = models.CharField('来往方', max_length=100, blank=True, default='', help_text='如"爱心宠物医院"或"国药集团"')
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='material_txns', verbose_name='医院')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='material_txns', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    date = models.DateField('日期')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='material_txns', verbose_name='所属区县')
    note = models.TextField('备注', blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '物资流水'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.get_type_display()} - {self.material_name} x{self.quantity}'


# ============================================
# 芯片
# ============================================
class Chip(models.Model):
    STATUS_CHOICES = [
        ('available', '可用'),
        ('used', '已使用'),
    ]
    number = models.CharField('芯片号', max_length=30, unique=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='available')
    pet = models.ForeignKey('business.Pet', on_delete=models.SET_NULL, null=True, blank=True, related_name='chips', verbose_name='关联宠物')
    used_at = models.DateField('使用日期', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '芯片'
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.number


# ============================================
# 放养记录
# ============================================
class Release(models.Model):
    STATUS_CHOICES = [
        ('pending', '待放养'),
        ('released', '已放养'),
    ]
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='releases', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    community = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='releases', verbose_name='小区')
    community_name = models.CharField('小区名称', max_length=100, blank=True, default='')
    receiver_name = models.CharField('接收人姓名', max_length=50, blank=True, default='')
    receiver_phone = models.CharField('接收人电话', max_length=20, blank=True, default='')
    signature = models.TextField('签字', blank=True, default='')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    released_at = models.DateField('放养日期', null=True, blank=True)
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='releases', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='releases', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '放养记录'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.pet_code} - {self.community_name}'


# ============================================
# 领养记录
# ============================================
class Adoption(models.Model):
    STATUS_CHOICES = [
        ('pending_claim', '待领出'),
        ('completed', '已完成'),
        ('cancelled', '已取消'),
    ]
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='adoptions', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    adopter = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='adoptions', verbose_name='领养人')
    adopter_name = models.CharField('领养人姓名', max_length=50, blank=True, default='')
    adopter_phone = models.CharField('领养人电话', max_length=20, blank=True, default='')
    adopter_id_card = models.CharField('领养人身份证', max_length=30, blank=True, default='')
    adopter_address = models.CharField('领养人地址', max_length=200, blank=True, default='')
    qualification = models.TextField('资质证明', blank=True, default='')
    commitment_letter = models.CharField('承诺书', max_length=200, blank=True, default='')
    adoption_agreement = models.CharField('领养协议', max_length=200, blank=True, default='')
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='adoptions', verbose_name='医院')
    hospital_name = models.CharField('医院名称', max_length=100, blank=True, default='')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='completed')
    adopted_at = models.DateField('领养日期', null=True, blank=True)
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='adoptions_operated', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='adoptions', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '领养记录'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.pet_code} -> {self.adopter_name}'


# ============================================
# 在线领养申请单
# ============================================
class AdoptionApplication(models.Model):
    """领养人在线提交的领养申请单。

    流程：领养人在领养大厅在线提交申请 → 机构审核（通过/拒绝） → 通过后转入线下领养登记。
    """
    STATUS_CHOICES = [
        ('pending', '待审核'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
        ('cancelled', '已取消'),
    ]
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='adoption_applications', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    applicant = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='adoption_applications', verbose_name='申请人')
    applicant_name = models.CharField('申请人姓名', max_length=50, blank=True, default='')
    applicant_phone = models.CharField('联系电话', max_length=20, blank=True, default='')
    applicant_id_card = models.CharField('身份证号', max_length=30, blank=True, default='')
    applicant_address = models.CharField('居住地址', max_length=200, blank=True, default='')
    qualification = models.TextField('领养资质说明', blank=True, default='')
    reason = models.TextField('领养理由', blank=True, default='')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='adoption_applications', verbose_name='受理机构')
    hospital_name = models.CharField('受理机构名称', max_length=100, blank=True, default='')
    review_note = models.TextField('审核意见', blank=True, default='')
    reviewed_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='adoption_applications_reviewed', verbose_name='审核人')
    reviewed_at = models.DateTimeField('审核时间', null=True, blank=True)
    applied_at = models.DateTimeField('申请时间', auto_now_add=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '领养申请单'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.pet_code} - {self.applicant_name} ({self.get_status_display()})'


# ============================================
# 领养后回访打卡
# ============================================
class CheckIn(models.Model):
    STATUS_CHOICES = [
        ('pending', '待审核'),
        ('approved', '已通过'),
        ('rejected', '已驳回'),
    ]
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='checkins', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    adopter = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='checkins', verbose_name='领养人')
    adopter_name = models.CharField('领养人姓名', max_length=50, blank=True, default='')
    month = models.CharField('回访月份', max_length=20, help_text='如 2025-02')
    photo = models.ImageField('回访照片', upload_to='checkins/', null=True, blank=True)
    note = models.TextField('备注', blank=True, default='')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='checkins_reviewed', verbose_name='操作员')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '回访打卡'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.pet_code} - {self.month}'


# ============================================
# 黑名单
# ============================================
class Blacklist(models.Model):
    name = models.CharField('姓名', max_length=50)
    id_card = models.CharField('身份证号', max_length=30, blank=True, default='')
    phone = models.CharField('电话', max_length=20, blank=True, default='')
    reason = models.TextField('拉黑原因')
    violation_date = models.DateField('违规日期', null=True, blank=True)
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='blacklist_added', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='blacklist', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '黑名单'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.name} ({self.phone})'


# ============================================
# 安乐死记录
# ============================================
class Euthanasia(models.Model):
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='euthanasia_records', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='euthanasia_records', verbose_name='医院')
    hospital_name = models.CharField('医院名称', max_length=100, blank=True, default='')
    reason = models.TextField('安乐死原因')
    condition = models.TextField('动物状况', blank=True, default='')
    euthanized_at = models.DateField('安乐死日期', null=True, blank=True)
    body_received = models.BooleanField('遗体已领取', default=False)
    body_received_at = models.DateField('遗体领取日期', null=True, blank=True)
    body_received_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='received_bodies', verbose_name='遗体领取人')
    body_received_by_name = models.CharField('遗体领取人姓名', max_length=50, blank=True, default='')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='euthanasia_operated', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='euthanasia_records', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '安乐死记录'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.pet_code} - {self.hospital_name}'


# ============================================
# 消息通知
# ============================================
class Message(models.Model):
    TYPE_CHOICES = [
        ('approval', '审批通知'),
        ('checkin_reminder', '回访提醒'),
        ('system', '系统消息'),
        ('notice', '公告'),
    ]
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='messages', verbose_name='接收用户')
    type = models.CharField('类型', max_length=20, choices=TYPE_CHOICES)
    title = models.CharField('标题', max_length=200)
    content = models.TextField('内容')
    is_read = models.BooleanField('已读', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '消息'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.get_type_display()} - {self.title}'


# ============================================
# 领养大厅上架
# ============================================
class AdoptionHallListing(models.Model):
    pet = models.OneToOneField('business.Pet', on_delete=models.CASCADE, related_name='hall_listing', verbose_name='宠物')
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='hall_listings', verbose_name='医院')
    hospital_name = models.CharField('医院名称', max_length=100, blank=True, default='')
    intro = models.TextField('简介', blank=True, default='')
    personality = models.CharField('性格', max_length=100, blank=True, default='')
    body_condition = models.CharField('身体状况', max_length=100, blank=True, default='')
    flow_doc = models.TextField('流程文档', blank=True, default='')
    is_active = models.BooleanField('已上架', default=True)
    published_at = models.DateField('上架日期', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '领养大厅上架'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'上架 - {self.pet.code}'
