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
        ('released', '已放归'),
        ('euthanized', '已死亡'),
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
    photo_capture = models.ImageField('捕捉照片', upload_to='photos/', null=True, blank=True)
    photo_group = models.ImageField('合照', upload_to='photos/', null=True, blank=True)
    photo_before = models.ImageField('术前照片', upload_to='photos/', null=True, blank=True)
    photo_after = models.ImageField('术后照片', upload_to='photos/', null=True, blank=True)
    photo_treatment = models.ImageField('诊疗照片', upload_to='photos/', null=True, blank=True)
    # 逻辑删除：随所属捕捉记录一并作废，不做物理删除
    is_deleted = models.BooleanField('已删除', default=False, db_index=True)
    deleted_at = models.DateTimeField('删除时间', null=True, blank=True)
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
    """捕捉记录。

    状态由系统按「本单宠物的转运情况」自动推导，不允许人工随意填写：
    - pending   待转运：本单还没有提交任何转运单
    - partial   部分转运：部分宠物已提交转运单（尚未全部退回）
    - completed 已完成：全部宠物均已提交转运单
    - void      已作废：记录被逻辑删除
    """
    STATUS_CHOICES = [
        ('pending', '待转运'),
        ('partial', '部分转运'),
        ('completed', '已完成'),
        ('void', '已作废'),
    ]
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='captures', verbose_name='所属区县')
    shelter = models.ForeignKey('core.Institution', on_delete=models.PROTECT, related_name='captures', verbose_name='捕捉点')
    shelter_name = models.CharField('捕捉点名称', max_length=100, blank=True, default='')
    community = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='community_captures', verbose_name='小区')
    community_name = models.CharField('小区名称', max_length=100, blank=True, default='')
    address = models.CharField('捕捉地址', max_length=200, blank=True, default='')
    latitude = models.FloatField('纬度', null=True, blank=True)
    longitude = models.FloatField('经度', null=True, blank=True)
    geo_address = models.CharField('定位地址', max_length=255, blank=True, default='', help_text='由定位经纬度逆地理编码得到的地址名称')
    property_name = models.CharField('物业名称', max_length=100, blank=True, default='')
    contact_person = models.CharField('联系人', max_length=50, blank=True, default='')
    contact_phone = models.CharField('联系电话', max_length=20, blank=True, default='')
    pet_count = models.IntegerField('动物数量', default=0)
    pet_codes = models.TextField('动物编号', blank=True, default='', help_text='逗号分隔')
    group_photo = models.ImageField('合照', upload_to='photos/', null=True, blank=True)
    signature = models.TextField('签字', blank=True, default='', help_text='base64')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='captures', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    # 逻辑删除：删除只做标记，数据库记录保留，便于追溯与监管核查
    is_deleted = models.BooleanField('已删除', default=False, db_index=True)
    deleted_at = models.DateTimeField('删除时间', null=True, blank=True)
    deleted_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='deleted_captures', verbose_name='删除人')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

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
    # 需求：回收单需记录住址与回收时间（此前只落 owner_name/phone，住址与时间被静默丢弃）
    owner_address = models.CharField('主人住址', max_length=200, blank=True, default='')
    return_time = models.DateTimeField('回收时间', null=True, blank=True)
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
        # 捕捉点主动撤回（医院尚未签收）。注意：void 既不计入
        # ACTIVE_TRANSFER_STATUSES，也不是 rejected，因此不占用捕捉单、
        # 也不影响捕捉单状态推导，宠物回退为 in_transit 可再次被选择转运。
        ('void', '已撤回'),
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
    # 备注：前端「新建转运单」一直有这个输入框、接口文档也声明接受 note，
    # 但模型缺这一列 → 用户填的备注被静默丢弃、详情抽屉的「备注」恒为 `—`。
    note = models.TextField('备注', blank=True, default='')
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
        ('receive', '签收'),
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
    # ⚠ `hospital` 的语义是**这笔流水的「对方机构」**，历史上只可能是医院，所以叫了这个名。
    #   第四十五轮起捕捉点之间也能下发（市级 → 区县级），于是它也会指向捕捉点。
    #   「本笔流水让**哪个机构**的库存发生变化」是另一件事，见下面的 `institution`。
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='material_txns', verbose_name='对方机构')
    # 「库存归属机构」——本笔流水**动的是哪个机构的库存**。
    #   捕捉点侧采购/异动/下发 → 发起捕捉点；医院侧签收/消耗/异动 → 该医院。
    #   为什么必须单独加一列：`hospital=None` 曾同时表示「捕捉点侧」和「没有对方机构」，
    #   于是**发起捕捉点根本没有被记下来** —— 同一区县有两个捕捉点时，
    #   台账里分不出这批货是从哪个点出去的（第四十五轮 C3 的根因）。
    #   ⚠ 历史行这一列为 NULL，**不允许**回填猜测（同一区县可能有两个捕捉点，
    #   猜错就是把库存记到别的机构名下）；读取侧一律把 NULL 当作「未归属」。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='material_stock_txns', verbose_name='库存归属机构')
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


class MaterialStock(models.Model):
    """机构库存 —— 「一行 = 一个机构 + 一种物料的**当前库存**」。

    为什么要有这张表（第四十五轮 Q3 方案 2）
    ----------------------------------------
    在这之前，库存只有 `Material.shelter_stock` **一个数**，含义是
    「这个区县（的捕捉点）一共有多少」：同一区县有两个捕捉点时，
    **分不出是哪个点的**；而医院侧的库存是**每次现算**的
    （`get_hospital_stock()` = 4 次聚合），也就是「每次盘点都全量计算」。

    磊哥的要求：区县级捕捉点也要有像医院那样**按机构**看得到的库存，
    且**不要每次全量累加，要有已经算好的结果**。

    于是这张表就是那个「已经算好的结果」：
    - 写入侧：采购 / 下发 / 签收 / 消耗 / 异动，在**同一个事务里**
      把对应机构那一行加或减（见 `services.apply_stock_delta()`）；
    - 读取侧：看库存 = **直接读一行**（`services.get_institution_stock()`），
      不做任何累加；
    - 流水（`MaterialTransaction`）照旧逐笔完整记录 → 两者必须能互相验证，
      对账规则见 `check_data_integrity` 的「机构库存与流水合计不一致」。

    ⚠ 与 `Material.shelter_stock` 的关系：后者是**存量兼容字段**，
    语义保持「该区县捕捉点合计」不变（界面不用改）。新写入会同时维护两者；
    对账规则只在「该物料已有机构库存行」时才比较，避免把纯存量数据误报成脏数据。

    区县归属：**故意不加冗余 `district` 外键** —— 这一行的归属恒等于
    `institution.district`，加一列只是把同一个事实存两遍，反而制造漂移面
    （与 `Message` 那种「归属要靠别的表反查、反查还会算错」的情形不同：
    这里反查只有一跳且恒等）。因此走 `DISTRICT_LOOKUP` 声明式收敛。
    """
    material = models.ForeignKey(
        'business.Material', on_delete=models.CASCADE, related_name='stocks', verbose_name='物资')
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.CASCADE, related_name='material_stocks', verbose_name='持有机构')
    quantity = models.IntegerField('当前库存', default=0)
    # 期初库存 —— 「这张表诞生之前就已经存在的量」。
    #
    # 为什么必须有这一列：`Material.shelter_stock` 是**种子数据直接写进去**的
    # （`seed_data` 设字段、不写流水），所以「机构库存 == 该机构流水合计」这条
    # 对账判据从第一天起就**恒不成立**（实测 16 行全不一致，流水侧是 0）。
    # 把期初显式存下来，判据就能写成**精确等式**：
    #
    #     quantity == opening_quantity + 按机构归属的流水合计
    #
    # 否则只剩两个选择，都不好：① 造一条假的「期初采购」流水 → 台账里凭空
    # 多出一笔没人下过的单；② 放宽判据（「只在有流水时比较」）→ 判据自己
    # 变成一笔糊涂账，真正的漏记反而漏检。
    opening_quantity = models.IntegerField('期初库存', default=0)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    # 本模型没有 `district` 外键，区县隔离必须沿机构派生 —— 不声明的话
    # 区县角色一访问就 `FieldError` 500（市级账号走 `return all()` 绕过该分支，
    # 只测市级账号**永远发现不了**，同 `CheckIn` / `AdoptionHallListing`）。
    DISTRICT_LOOKUP = 'institution__district'

    class Meta:
        ordering = ['-id']
        verbose_name = '机构库存'
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=['material', 'institution'], name='uniq_material_institution_stock'),
        ]

    def __str__(self):
        return f'{self.institution.name} - {self.material.name} x{self.quantity}'


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
# 放归记录
# ============================================
class Release(models.Model):
    STATUS_CHOICES = [
        ('pending', '待放归'),
        ('released', '已放归'),
    ]
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='releases', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    community = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='releases', verbose_name='小区')
    community_name = models.CharField('小区名称', max_length=100, blank=True, default='')
    receiver_name = models.CharField('接收人姓名', max_length=50, blank=True, default='')
    receiver_phone = models.CharField('接收人电话', max_length=20, blank=True, default='')
    signature = models.TextField('签字', blank=True, default='')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    released_at = models.DateField('放归日期', null=True, blank=True)
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='releases', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='')
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='releases', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '放归记录'
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
    # 本模型没有 district 字段：回访打卡随领养走，区县**由所属宠物派生**。
    # get_district_filtered_queryset() 会读这个属性来决定过滤路径，
    # 缺少它时该函数会直接 filter(district_id=...) 而抛 FieldError。
    DISTRICT_LOOKUP = 'pet__district'

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
    # 逻辑删除：移出黑名单只打标记，保留历史记录便于追溯
    is_deleted = models.BooleanField('已移出', default=False, db_index=True)
    deleted_at = models.DateTimeField('移出时间', null=True, blank=True)
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
    # ⚠⚠ 公告（`type='notice'`）**必须自带区县**，不能靠接收人的 `User.district` 推导。
    #
    # 生产实测：领养人账号的 `district` **恒为空**（1/1 为空）—— 注册时就没有区县。
    # 按 `User.district` 收敛的直接后果是**区级 gov 的收件人集合永远为空**
    # （襄城区 0 人）→ `notice_publish` 一律回 400「该范围内没有可接收公告的用户」，
    # 而角色闸门却明确允许 `gov_district` 调用 —— 典型「功能实现了、但到不了」。
    #
    # 归属必须从**业务对象**推导（领养人的归属 = 他领养的动物属于哪个区县），
    # 这与本项目既有的三次同口径教训（捕捉单区县 / 操作日志 / 账号区县↔机构区县）
    # 是同一条纪律。
    #
    # 点对点消息（`approval` / `checkin_reminder` / `system`）走 `my_messages`
    # 按接收人取，不需要区县，故留空。
    district = models.ForeignKey('core.District', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='messages', verbose_name='所属区县')
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
    # 与 CheckIn 同理：上架记录本身没有 district 字段，区县由所属宠物派生。
    DISTRICT_LOOKUP = 'pet__district'

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
