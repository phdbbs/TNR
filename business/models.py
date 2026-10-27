from django.db import models


# ============================================
# 流浪动物档案
# ============================================
class Pet(models.Model):
    STATUS_CHOICES = [
        ('in_transit', '在途'),
        ('in_treatment', '待诊疗'),
        ('pending_adopt', '待领养'),
        # 医院 → 捕捉点的反向运输（第五十四轮出院重组）：
        # 运回单开出后动物进入 `return_pending`，捕捉点确认接收后进入
        # `pending_release`，由捕捉点再开「放归单」与物业交接。
        # 不加这两个中间态，动物在运回单未确认时仍可被领养/再次放归，
        # 两张单据会同时承诺同一只动物。
        ('return_pending', '待运回捕捉点'),
        ('pending_release', '待放归'),
        ('pending_claim', '待领出'),
        ('adopted', '已领养'),
        ('released', '已放归'),
        # ⚠ 第四十六轮把这两态拆开（R46 §四「办结去向」缺口）：
        #   原 `euthanized` 的 label 是「已死亡」，而原型「办结去向」是**两个**
        #   选项 —— 🕯️ 安乐死（需另开安乐死单）与 ⚰️ 死亡 / 无害化（需另开
        #   无害化单）。用同一个状态表达两件事的后果是：**统计口径永久合并**
        #   （「本区安乐死了几只」与「本区收尸了几只」是两张要分开报的表），
        #   且事后无法从数据里拆开 —— 旧行一律落在 `euthanized`，
        #   回填只能靠猜。label 改名本身不动数据，`dead` 是纯新增。
        ('euthanized', '已安乐死'),
        ('dead', '已死亡/无害化'),
        ('owner_returned', '主人领回'),
    ]
    #: 已出库的终态：到了这些状态，动物就不在任何在办流程里了。
    #: 回退类操作（驳回/撤回转运单）必须排除它们，否则一张作废的转运单
    #: 能把「已放归/已领养」的档案打回「待转运」，与已归档的单据互相矛盾。
    TERMINAL_STATUSES = ('adopted', 'released', 'euthanized', 'dead', 'owner_returned')
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
    # 冗余「归属机构」—— 见 `Transfer.institution` 的说明（第四十八轮统一加）。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='归属机构')
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
    # 单号是单据的**业务键**：打印页按号反查、芯片追溯、监管台账对账都靠它，
    # 因此本文件 8 张单据表的 `ledger_no` 一律建索引（此前只有外键自动索引，
    # 按号查等于全表扫）。
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='',
                                 db_index=True)
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
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='',
                                 db_index=True)
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='owner_returns', verbose_name='所属区县')
    # 冗余「归属机构」—— 见 `Transfer.institution` 的说明。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='归属机构')
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
        # 部分签收（第四十八轮）：医院只签收了本单的一部分动物。剩余动物
        # 仍在待签收状态，可继续签收或整单驳回；`received_codes` 记录已签收
        # 的子集。**不要**用「pending + received_codes 非空」表达同一件事 ——
        # 那样列表筛选、看板统计、捕捉单推导都要各自再判一次，必然有一处漏。
        ('partial', '部分签收'),
        ('received', '已签收'),
        ('rejected', '已驳回'),
        # 捕捉点主动撤回（医院尚未签收）。注意：void 既不计入
        # ACTIVE_TRANSFER_STATUSES，也不是 rejected，因此不占用捕捉单、
        # 也不影响捕捉单状态推导，宠物回退为 in_transit 可再次被选择转运。
        ('void', '已撤回'),
    ]
    # 「还没签完」= 医院仍可继续签收或驳回 = `pending` ∪ `partial`。
    # 这条口径此前在 `views_transfer.py` 的签收与驳回两处各写一遍字面量，
    # 前端另有六处（待办计数、标签页、行内按钮、可选动物、看板、领养人时间线）。
    # 少算 `partial` 的后果不是显示难看，而是**部分签收的单子从此没人再签**：
    # 待办列表里没有它、行内没有「去签收」、捕捉点还会把这几只动物当成在途占用。
    # 与 `ACTIVE_TRANSFER_STATUSES`（services.py）不同：那张表算的是「占用捕捉单」，
    # 已签收也占；本表只问「本院还有没有要签的」。
    OPEN_STATUSES = ('pending', 'partial')
    capture = models.ForeignKey('business.Capture', on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers', verbose_name='捕捉记录')
    from_shelter = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_transfers', verbose_name='发出捕捉点')
    from_shelter_name = models.CharField('发出捕捉点名称', max_length=100, blank=True, default='')
    to_hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='received_transfers', verbose_name='接收医院')
    to_hospital_name = models.CharField('接收医院名称', max_length=100, blank=True, default='')
    # 冗余「归属机构」—— 这张单据**业务上归谁**（不是操作员归谁）。
    #
    # 为什么需要它：打印页原先只按 `district` 收敛，而诊疗单/安乐死单的
    # `district` 是**动物**的区县。跨区县送医时，做手术的医院在另一个区县，
    # 它点「打印诊疗单」→ 404「单据不存在或无权查看」——单据就在眼前却打不出来。
    #
    # ⚠ 写入时必须从**业务对象**推导（capture→shelter、treatment→hospital、
    #   transfer→from_shelter…），**不能**取 `request.user.institution`：
    #   平台/市级账号没有机构，取操作者会让单据归属漂移，而这条字段一旦
    #   漂移就是「打印权限放错人」，不报错、只有对账才发现。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='归属机构')
    pet_codes = models.TextField('动物编号', blank=True, default='', help_text='逗号分隔')
    # 已签收的动物编号（逗号分隔）。空串 = 一个都没签收。
    # 只有 `status in ('partial', 'received')` 时才有意义；驳回时保留下来，
    # 用来区分「已签收的」与「被退回的」，重算捕捉单状态也依赖它。
    received_codes = models.TextField('已签收动物编号', blank=True, default='',
                                      help_text='逗号分隔，部分签收时记录已收子集')
    pet_count = models.IntegerField('动物数量', default=0)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    received_at = models.DateField('签收日期', null=True, blank=True)
    reject_reason = models.TextField('驳回原因', blank=True, default='')
    # 备注：前端「新建转运单」一直有这个输入框、接口文档也声明接受 note，
    # 但模型缺这一列 → 用户填的备注被静默丢弃、详情抽屉的「备注」恒为 `—`。
    note = models.TextField('备注', blank=True, default='')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='',
                                 db_index=True)
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
    """诊疗单 —— 一次收治后在该医院的完整诊疗过程（v5.1 原型三步流程）。

    ⚠ 本模型有**两代字段**并存，这不是历史遗留没清干净，而是刻意保留：

    * **旧代**：`items_sterilization` / `items_vaccine` / `items_deworming` /
      `items_chip` 四个布尔，以及 `sterilization_*` / `vaccine_*` /
      `deworming_*` / `chip_*` 明细列。它们是第四十六轮之前的口径，
      现在仍被 **打印页**（`views_print.TREATMENT_FIELDS`）、**监管端统计**
      （`supervision/views.py`）、**宠物档案时间轴**（`views_portal.py`）、
      **政府端 / 医院端列表**读取。删掉它们 = 这几处一起断。
    * **新代**：体检 / 医嘱 / 签名 / 办结字段 + `TreatmentItem` / `TreatmentRx`
      两张子表，承载 v5.1 原型的三步流程。

    写入侧的分工（**只有一处写**，避免双真源漂移）：
    新流程一律写子表；`items_*` 四个布尔作为「本单做了哪些项目」的**派生索引**
    由 `services_treatment.sync_legacy_item_flags()` 在同一事务内回写，
    供上面那几处既有页面继续工作。旧的明细列（`sterilization_surgeon` 等）
    **新流程不再写** —— 新流程的明细在子表里，打印页按「有子表读子表」兜底。
    """

    STATUS_CHOICES = [
        ('in_progress', '进行中'),
        ('completed', '已完成'),
    ]
    # 办结去向 —— key 与 `Pet.STATUS_CHOICES` 一一对齐（v5.1 原型 DISPOSALS）。
    # 「安乐死」与「死亡/无害化」是两个选项，对应 Pet 的两个状态（见 Pet 的注释）。
    DISPOSAL_CHOICES = [
        ('pending_adopt', '领养'),
        ('released', '放回原小区'),
        ('owner_returned', '主人领回'),
        ('euthanized', '安乐死'),
        ('dead', '死亡 / 无害化'),
    ]
    TEMP_FLAG_CHOICES = [
        ('normal', '正常'),
        ('fever', '发烧'),
    ]
    # 三步流程的阶段 —— 由三个时间戳**派生**，不单独存列。
    # 存列的话就会出现「stage 说已办结、disposed_at 是空的」这种自相矛盾的行，
    # 而且必然有一处写入路径忘了同步（本项目已栽过多次「双真源」）。
    STAGE_CHECKING = 'checking'
    STAGE_TREATING = 'treating'
    STAGE_DONE = 'done'
    STAGE_CHOICES = [
        (STAGE_CHECKING, '待接诊'),
        (STAGE_TREATING, '诊疗中'),
        (STAGE_DONE, '诊疗完毕'),
    ]
    # 三态的**筛选口径**：`?stage=` 与 `Treatment.stage` 属性必须读同一张表。
    # 以前列表视图里手写 `checked_at__isnull=False, done_at__isnull=True`，
    # 属性里再 `if self.done_at ...` 推一遍 —— 同一件事两份判据，
    # 调属性判据（比如把 已办结 也算进 done）时筛选必然先漂。
    STAGE_FILTERS = {
        STAGE_CHECKING: (('checked_at__isnull', True),),
        STAGE_TREATING: (('checked_at__isnull', False), ('done_at__isnull', True)),
        STAGE_DONE: (('done_at__isnull', False),),
    }
    # 医院看板的**五态**（流程顺序）：比三态多看 `diagnosed_at`（下诊断）和
    # `disposed_at`（办结出院），判据在 `views_treatment._board_stage()`。
    # 分桶字典由它生成 —— 以前桶键在「空数据兜底」和「正常分桶」两处各写一遍字面量，
    # 加一态要改两处，漏一处的表现是那一态的动物**整列消失**（页面看着像没数据）。
    BOARD_STAGES = ('checking', 'pending_diagnose', 'treating',
                    'pending_discharge', 'discharged')
    # 办结去向是**单选**，且只在「诊疗完成」之后才可选（原型：未诊断完成时
    # 办结去向整块显示「请先完成医嘱与处方」）。
    DISPOSED_STATUSES = ('pending_adopt', 'released', 'owner_returned',
                         'euthanized', 'dead')

    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='treatments', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='treatments', verbose_name='医院')
    hospital_name = models.CharField('医院名称', max_length=100, blank=True, default='')
    # 冗余「归属机构」—— 见 `Transfer.institution` 的说明。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='归属机构')
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

    # ---------- 第一步：诊疗前检查 ----------
    # 体温存**数值**（R46 §五.1 建议）：只有数值才能做异常预警与趋势，
    # 存「38.5℃」这种字符串的话，将来想画个体温曲线就得把历史数据全部正则解析。
    temperature = models.FloatField('体温(℃)', null=True, blank=True)
    temperature_flag = models.CharField('体温判定', max_length=10, choices=TEMP_FLAG_CHOICES,
                                        blank=True, default='')
    # 检查项一律存**文本快照**（同 R46 §五.1）：原型上的 chips 选项以后会变，
    # 存 key 的话「历史单据」会跟着今天的选项一起变，那就不是留痕了。
    common_findings = models.TextField('常见病检查', blank=True, default='',
                                       help_text='多选，顿号分隔')
    common_other = models.CharField('常见病其他', max_length=200, blank=True, default='')
    # 传染病检查三态（未检 / 阴性 / 阳性），按物种给不同的项。
    # 存 JSON 文本而不是若干列布尔：物种不同则项不同（猫 FPV/FHV/FCV/狂犬/弓形虫、
    # 狗 CDV/CPV/CCV/狂犬），加一个病种就要加一列，且「未检」与「阴性」
    # 必须能区分 —— 用 BooleanField 的话两者都是 False，分不开。
    # 清单真源是 `services_treatment.INFECTIOUS_TESTS`（**允许全空**，
    # 服务端不加「检完才能办结」的门禁，理由见那份表旁边的注释）。
    infectious_tests = models.TextField('传染病检查', blank=True, default='',
                                        help_text='JSON: {"fpv": "neg", ...}')
    skin_mucosa = models.CharField('皮肤黏膜检查', max_length=50, blank=True, default='')
    skin_other = models.CharField('皮肤黏膜其他', max_length=200, blank=True, default='')
    check_note = models.TextField('其它检查情况', blank=True, default='')
    checked_at = models.DateTimeField('检查单提交时间', null=True, blank=True)

    # ---------- 第二步：医嘱与处方 ----------
    advice = models.TextField('医嘱', blank=True, default='')
    diagnosed_at = models.DateTimeField('诊断完成时间', null=True, blank=True)

    # ---------- 签名与电子章（R46 口径 2：挂账号，落库时快照）----------
    vet_name = models.CharField('兽医姓名', max_length=50, blank=True, default='')
    vet_license = models.CharField('兽医执业证号', max_length=100, blank=True, default='')
    hospital_seal = models.CharField('机构电子章', max_length=100, blank=True, default='')

    # ---------- 第三步：执行与办结 ----------
    done_at = models.DateTimeField('诊疗完成时间', null=True, blank=True)
    # 「去向建议」在**诊疗完成的那一刻**就记下（`mark_done`），
    # 动物此时还在院、所有出院单据都能开；「最终去向」`disposal` 是
    # **办结**时才写（`dispose`），与 `disposed_at` 同时落。
    #
    # 为什么不复用 `disposal`：它所有消费点都以「已办结」为前提 ——
    # 诊疗单上那一行就叫「办结去向」。把建议也塞进去，会印出
    # 「办结去向：领养」而动物明明还在院（界面上承诺的事没发生）。
    disposal_advised = models.CharField('去向建议', max_length=20, choices=DISPOSAL_CHOICES,
                                        blank=True, default='')
    disposal = models.CharField('办结去向', max_length=20, choices=DISPOSAL_CHOICES,
                                blank=True, default='')
    disposed_at = models.DateTimeField('办结时间', null=True, blank=True)

    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='in_progress')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='treatments', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='',
                                 db_index=True)
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='treatments', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '诊疗记录'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.pet_code} - {self.hospital_name}'

    @property
    def stage(self):
        """三步流程的阶段 —— **派生值**，见 `STAGE_CHOICES` 上方的注释。"""
        if self.done_at:
            return self.STAGE_DONE
        if self.checked_at:
            return self.STAGE_TREATING
        return self.STAGE_CHECKING

    @property
    def is_disposed(self):
        """是否已选办结去向（「待出院」与「已办结」的分界，原型 `_disposed`）。"""
        return bool(self.disposed_at)


# ============================================
# 诊疗单 · 处置项目明细（v5.1 原型第二步的每个项目）
# ============================================
class TreatmentItem(models.Model):
    """一张诊疗单里的**处置项目**：绝育 / 驱虫 / 免疫 / 芯片 / 其他。

    ⚠ 「展开、选了处方」= 该项目**必须执行**（原型口径：`must = hasRx`）。
    执行阶段不把它做完，就不允许「诊疗完成」。所以 `is_done` 是办结的前置闸门，
    判据见 `services_treatment.must_items_not_done()`。
    """

    ITEM_CHOICES = [
        ('sterilization', '绝育'),
        ('deworming', '驱虫'),
        ('vaccine', '免疫'),
        ('chip', '芯片'),
        ('other', '其他'),
    ]

    # 本模型没有 `district` 外键（区县随诊疗单走），必须声明派生路径：
    # 缺了它，区县账号一旦对这张表调用 `get_district_filtered_queryset()`
    # 就会抛 `FieldError` → 接口 500，而市级账号走 `return all()` 绕过 ——
    # 只测市级账号永远发现不了（`Chip` 就是这么栽的，见 business/models.py 上方）。
    DISTRICT_LOOKUP = 'treatment__district'

    treatment = models.ForeignKey('business.Treatment', on_delete=models.CASCADE,
                                  related_name='items', verbose_name='诊疗单')
    item_key = models.CharField('项目', max_length=20, choices=ITEM_CHOICES)
    item_name = models.CharField('项目名称', max_length=50, blank=True, default='')
    planned_date = models.DateField('预计执行时间', null=True, blank=True)
    # 芯片号只对 `item_key='chip'` 有意义，但对其他项目留空即可 ——
    # 拆成另一张表会让「一张单的项目列表」要 join 三张表才拼得出来。
    chip_no = models.CharField('芯片号', max_length=30, blank=True, default='')
    notes = models.CharField('注意事项', max_length=500, blank=True, default='')
    # 手写处方：原型明确「仅记录、不统计库存」（`rxOther`），与下面从库存选的
    # `TreatmentRx` 是两回事，**不能合并** —— 合并会让手写药名也去扣库存。
    rx_other = models.CharField('手写处方', max_length=500, blank=True, default='')
    # 操作人快照（原型每个项目可单独指定兽医，默认当前账号）
    vet_name = models.CharField('操作人', max_length=50, blank=True, default='')
    vet_license = models.CharField('操作人执业证号', max_length=100, blank=True, default='')
    is_done = models.BooleanField('已执行', default=False)
    done_at = models.DateTimeField('执行时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['id']
        # 一张单里每个项目只应有一行 —— 原型是「每个 key 一份草稿」，
        # 没有约束的话重复提交会静默多出几行，而界面上只显示一行，
        # 「已执行 3/5 项」这类计数从此永远对不上。
        constraints = [
            models.UniqueConstraint(fields=['treatment', 'item_key'],
                                    name='uniq_treatment_item_key'),
        ]
        verbose_name = '诊疗处置项目'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.treatment_id} - {self.get_item_key_display()}'


# ============================================
# 诊疗单 · 处方明细（从本院库存药品里选，R46 口径 1）
# ============================================
class TreatmentRx(models.Model):
    """处置项目下的一条处方 —— **必须指向真实物料**。

    R46 口径 1：药品从**已下发的药品**里选、扣**医院侧库存**，
    不新建独立药品字典。所以这里存的是 `Material` 外键，而不是药名字符串 ——
    存字符串的话「扣了哪条物料的库存」就无从谈起，只能整单瞎扣。
    """

    # 与 `TreatmentItem` 同理：没有 `district` 外键，区县随诊疗单走。
    DISTRICT_LOOKUP = 'item__treatment__district'

    item = models.ForeignKey('business.TreatmentItem', on_delete=models.CASCADE,
                             related_name='rx_rows', verbose_name='处置项目')
    # PROTECT：被处方引用过的物料不允许删 —— 与 `MaterialTransaction.material`
    # 同一约定。否则历史处方会指向一个不存在的药品，打印出来是个空名字。
    material = models.ForeignKey('business.Material', on_delete=models.PROTECT,
                                 related_name='treatment_rx', verbose_name='药品')
    material_name = models.CharField('药品名称快照', max_length=100, blank=True, default='')
    quantity = models.IntegerField('数量', default=1)
    unit = models.CharField('单位', max_length=20, blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = '诊疗处方'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.material_name} x{self.quantity}'


# ============================================
# 物资
# ============================================
class Material(models.Model):
    # 库存只有三类（第六十四轮）：疫苗 / 驱虫药 / 芯片。
    # 药品（含麻药）不再进库存 —— 绝育用药写在诊疗单的用药记录里，
    # 处方也只能从疫苗 / 驱虫药里开（`services_treatment.RX_CATEGORIES`）。
    CATEGORY_CHOICES = [
        ('vaccine', '疫苗'),
        ('dewormer', '驱虫药'),
        ('chip', '芯片'),
    ]
    # 历史数据的类别标签：`medicine` 已从选项里移除，但库里的旧行还在，
    # 台账 / 监管导出不能显示成裸的 `medicine`。
    LEGACY_CATEGORY_LABELS = {'medicine': '药品'}
    name = models.CharField('物资名称', max_length=100)
    category = models.CharField('类别', max_length=20, choices=CATEGORY_CHOICES)
    unit = models.CharField('单位', max_length=20)
    specification = models.CharField('规格', max_length=100, blank=True, default='')
    supplier = models.CharField('供应商', max_length=100, blank=True, default='')
    batch_no = models.CharField('批号', max_length=50, blank=True, default='')
    shelter_stock = models.IntegerField('捕捉点库存', default=0)
    safety_stock = models.IntegerField('安全库存', default=0)
    expiry_date = models.DateField('过期日期', null=True, blank=True)
    # ⚠ 第四十六轮删除了 `chip_range_start` / `chip_range_end`：
    #   那两个字段只记号段字符串、与实际建出的 `Chip` 行**数量不校验**，
    #   是「双真源」（号段说 100 个、实际 30 行，界面照样显示 100 且不报错）。
    #   现在芯片一律以 `Chip` 表逐号为准，入库时按「首号 + 数量」生成号码。
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='materials', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '物资'
        verbose_name_plural = verbose_name

    def get_category_display(self):
        return (dict(self.CATEGORY_CHOICES).get(self.category)
                or self.LEGACY_CATEGORY_LABELS.get(self.category, self.category))

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
        ('outbound', '出库'),
    ]
    # 出库原因：只对 `outbound` 类型有意义，其他类型一律留空。
    #   为什么不像 `adjustment` 那样自由文本：出库是**主动申报**的动作，
    #   报废 / 退回 / 调拨 / 盘点 四种去向要能按类别汇总，
    #   而「自由文本写原因」= 同一个原因有 N 种写法，统计必然对不上。
    #   「出库数量可以为负」表示**盘盈 / 退回入库**，此时 reason 仍记申报的那一个。
    OUTBOUND_REASON_CHOICES = [
        ('scrap', '报废'),
        ('return', '退回'),
        ('transfer', '调拨'),
        ('inventory', '盘点'),
    ]
    # 异动原因（第五十轮）：异动是「纠正账实差异」，原因也必须可汇总 ——
    # 自由文本会让「过期 / 盘亏」这类统计永远对不上。
    # 方向约定（判据见 `services_stock.validate_adjustment_reason()`）：
    # 过期 / 失效 / 盘亏 = 减少（数量为正），盘盈 / 退回 = 增加（数量为负），
    # 其他两者皆可。
    ADJUSTMENT_REASON_CHOICES = [
        ('expired', '过期'),
        ('invalid', '失效'),
        ('gain', '盘盈'),
        ('loss', '盘亏'),
        ('return', '退回'),
        ('other', '其他'),
    ]
    # `reason` 列由出库与异动**共用**，字段 choices 是两套白名单的并集
    # （`return` 两边同义，只留一份）。各自的白名单仍在 services_stock 里分开校验。
    REASON_CHOICES = OUTBOUND_REASON_CHOICES + [
        ('expired', '过期'),
        ('invalid', '失效'),
        ('gain', '盘盈'),
        ('loss', '盘亏'),
        ('other', '其他'),
    ]
    type = models.CharField('类型', max_length=20, choices=TYPE_CHOICES)
    # 原因：`outbound` 记出库白名单，`adjustment` 记异动白名单，其余类型一律 '' ——
    # 判据见 `services_stock.validate_outbound_reason()` / `validate_adjustment_reason()`。
    reason = models.CharField(
        '原因', max_length=20, choices=REASON_CHOICES, blank=True, default='')
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
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='',
                                 db_index=True)
    # 关联单据号：接收单指向其签收的下发单号；其他单据留空。
    # 旧实现「接收单复用下发单号」无法区分两张单，也无法单独打印归档。
    ref_no = models.CharField('关联单据号', max_length=50, blank=True, default='')
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


class MaterialBatch(models.Model):
    """物料批次子表（B5）—— 「一行 = 一个机构 × 一种物料 × 一个批号」。

    为什么要有这张表
    ----------------
    在这之前，批号 / 效期挂在 `Material` **主表**上，而主表那一对字段每次采购
    都会被**覆盖写回**（见 `views_material.purchase_create` 的快照更新）——
    于是「上一批还剩 60 支、效期 10 月」的信息随第二次采购**凭空消失**，
    连带着：

    - 「过期拦截」按主表单值判：老批次过期、新批次明明有效，诊疗却被一刀切
      拒掉（假拦截）；反过来新采购把效期顶新，老批次的存货照样能用（漏拦截）；
    - 「扣的是哪一批」在数据里**根本不存在** —— 出库流水只带调用方随手
      填的批号文本，既不参与扣减、也与库存无关。

    这张表把批次变成**可记账的库存维度**：

    - 入库（采购 / 签收）：按批号 `get_or_create` 并累加（同批号再次入库
      视为同一批货，效期随之更新）；
    - 出库（下发 / 消耗 / 异动）：FEFO —— 近效期先出、同效期按入库先后，
      跨批次自动拆多笔流水（同 `ledger_no`，每笔 `batch_no` = 所扣批次）；
    - 对账：`MaterialStock.quantity == Σ 该机构该物料批次.quantity`
      （精确等式，见 `reconcile_material_stock`）。

    ⚠ `quantity` 可为 0（批次耗尽）：批次的**档案**（批号/效期）要留着 ——
    签收方回填效期、追溯「这批货去哪了」都靠它；删行会把历史变成孤儿。

    ⚠ 效期 `expiry_date` 可为空：药品/疫苗有效期，猫粮/耗材没有 —— 空 =
    「无效期概念」，FEFO 排序时垫底（不会挡在有过期风险的批次前面）。
    """
    material = models.ForeignKey(
        'business.Material', on_delete=models.CASCADE,
        related_name='batches', verbose_name='物资')
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.CASCADE,
        related_name='material_batches', verbose_name='持有机构')
    batch_no = models.CharField('批号', max_length=50, db_index=True)
    expiry_date = models.DateField('有效期至', null=True, blank=True)
    # 批次当前余量 —— 与 MaterialStock 同样用「已算好的结果」口径，
    # 增量维护（F() 自增），不现算。
    quantity = models.IntegerField('批次余量', default=0)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    # 同 MaterialStock：不加冗余 district 外键，区县隔离沿机构派生。
    # 不声明的话区县角色一访问就 FieldError 500（市级账号测不出来）。
    DISTRICT_LOOKUP = 'institution__district'

    class Meta:
        ordering = ['expiry_date', 'id']
        verbose_name = '物料批次'
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=['material', 'institution', 'batch_no'],
                name='uniq_material_institution_batch'),
        ]

    def __str__(self):
        return f'{self.institution.name} - {self.material.name} [{self.batch_no}] x{self.quantity}'


# ============================================
# 芯片
# ============================================
class Chip(models.Model):
    """芯片 —— 「一行 = 一个号码」，按**每一个号码单独管理**（第四十六轮重构）。

    为什么必须逐号建行
    ------------------
    芯片与疫苗/驱虫药不同：**每一颗芯片的号码都要能单独追溯到动物**。
    「号段」这种表示法能算出数量，但答不出「042060107000177 这颗现在在谁手上、
    植给了哪只猫」—— 而这恰恰是芯片存在的意义。

    重构前的双轨（第四十六轮之前）
    ------------------------------
    老实现同时维护两套、且**互不校验**：

    - `Chip` 表逐号一行，但**没有机构归属** —— 号码是全局的，
      查不出「这批货在谁手上」，医院端也无从「从本院库存里挑芯片」；
    - `Material.chip_range_start/end` 只记两个字符串，与实际建出来的
      `Chip` 行**数量不校验** —— 号段写着 100 个、实际只建了 30 行，
      界面照样按号段显示 100，全程没有任何报错。

    重构后：**`Chip` 表是唯一真源**，`Material` 上的号段字段已删除。
    持有机构、所属物料、入库批次都记在这一行上。

    归属怎么表达
    ------------
    `institution` 走 `DISTRICT_LOOKUP` 派生区县，**故意不加冗余
    `district` 外键** —— 与 `MaterialStock` 同一条理由：区县恒等于
    `institution.district`，加一列只是把同一个事实存两遍、凭空多一个漂移面。

    状态机（四态）
    --------------
    - `available`  可用：在本机构手上，没植出去；
    - `in_transit` 在途：随下发单出去了、对方**还没签收**。
      为什么要有这一态：物资流转的口径是「建单时发起方就出库、签收时接收方
      才入库，中间两边都不算」。芯片必须跟着同一条口径走，否则
      「芯片数」与「库存数」在在途期间必然对不上（发起方库存已经 -N，
      芯片却还挂在他名下）。
    - `used`       已使用：植进动物体内了。**仍留在原机构名下**（追溯用），
      但不再计入库存 —— 消耗那一步已经让 `MaterialStock` 减过了。
    - `void`       已作废：损坏 / 丢失，随「库存异动」一起报废。
      为什么必须有这一态：库存异动会让 `MaterialStock` 减掉 N，
      若芯片行不动，`available` 计数就比库存多出 N 且**不报错**。
      作废的号码**保留在表里**（不删行）—— 「这颗哪去了」本身就是要能答的问题。

    ⚠ 存量行（重构前 seed 出来的）`institution` / `material` 一律为 NULL，
    **不回填**：号段与捕捉点的对应关系在数据里已经丢了，同一区县可能有两个
    捕捉点，猜错就是把芯片记到别的机构名下（与 `MaterialTransaction.institution`
    历史行同一条处理）。读取侧把 NULL 当作「未归属」；写入侧
    **只拦新引用、不拦存量** —— 否则历史芯片会一颗都用不出去。
    """

    STATUS_CHOICES = [
        ('available', '可用'),
        ('in_transit', '在途'),
        ('used', '已使用'),
        ('void', '已作废'),
    ]
    number = models.CharField('芯片号', max_length=30, unique=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='available')
    pet = models.ForeignKey('business.Pet', on_delete=models.SET_NULL, null=True, blank=True, related_name='chips', verbose_name='关联宠物')
    used_at = models.DateField('使用日期', null=True, blank=True)
    # 「持有机构」—— 这颗芯片现在在谁手上。NULL = 重构前的存量行（未归属）。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='chips', verbose_name='持有机构')
    # 这颗芯片是从哪条物料进来的（chip 类别的 Material）。NULL = 存量行。
    material = models.ForeignKey(
        'business.Material', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='chips', verbose_name='所属物料')
    # 入库批次号 = 采购单号（如 PUR202610130001）。用于回答「这颗是哪一批进来的」。
    batch_no = models.CharField('入库批次号', max_length=50, blank=True, default='')
    # 「在途下发单」—— 仅 `status='in_transit'` 时有值，指向把它发出去的那张
    # 下发流水。签收时靠它反查「这批要收的是哪些号码」，清空后置 NULL。
    dispatch_txn = models.ForeignKey(
        'business.MaterialTransaction', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transit_chips', verbose_name='在途下发单')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    # 本模型没有 `district` 外键，区县隔离沿机构派生 —— 不声明的话
    # 区县角色一访问就 `FieldError` 500（市级账号走 `return all()` 绕过该分支，
    # 只测市级账号**永远发现不了**，同 `CheckIn` / `AdoptionHallListing`）。
    DISTRICT_LOOKUP = 'institution__district'

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
        ('pending', '待转运回小区'),
        # 第四十八轮：放归拆成两段 —— 「转运回小区」与「放归回捕捉地」。
        # 原先只有 pending → released 一步，等于把「送回小区」和「放归」
        # 当成同一件事，小区接收人还没签字、动物还在车上，状态就已经
        # 变成「已放归」了。`transferred` = 已回小区、等小区确认放归。
        ('transferred', '已回小区待放归'),
        ('released', '已放归'),
    ]
    # 「待放归」= 放归流程**未办结**：两段（`release_transfer` → `release_confirm`）
    # 里的第一段与第二段都算。判据只有一处定义，后端取单/互斥校验与前端
    # `TNR_UI.OPEN_RELEASE_STATUS` 都引它（闸门逐条比对）。
    # 漏掉 `transferred` 会让中间态的单子既查不到、又被当成「可以再开一张」。
    OPEN_STATUSES = ('pending', 'transferred')
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='releases', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    community = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='releases', verbose_name='小区')
    community_name = models.CharField('小区名称', max_length=100, blank=True, default='')
    # 冗余「归属机构」—— 见 `Transfer.institution` 的说明。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='归属机构')
    receiver_name = models.CharField('接收人姓名', max_length=50, blank=True, default='')
    receiver_phone = models.CharField('接收人电话', max_length=20, blank=True, default='')
    signature = models.TextField('签字', blank=True, default='')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    transferred_at = models.DateTimeField('转运回小区时间', null=True, blank=True)
    released_at = models.DateField('放归日期', null=True, blank=True)
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='releases', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='',
                                 db_index=True)
    district = models.ForeignKey('core.District', on_delete=models.PROTECT, related_name='releases', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '放归记录'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.pet_code} - {self.community_name}'


# ============================================
# 运回单（医院 → 捕捉点）
# ============================================
class ReturnOrder(models.Model):
    """医院把诊疗完成的动物成批运回捕捉点（放归的反向前半段）。

    与 `Release` 的分工：
      * `ReturnOrder` —— 医院 → 捕捉点，**多只动物一张单**，捕捉点确认接收；
      * `Release`     —— 捕捉点 → 原小区，单只动物，物业签字放归。
    两者串起来才是完整放归：医院运回 → 捕捉点接收 → 捕捉点再放归。
    此前只有 `Release` 一段，医院直接把动物「转运回小区」，捕捉点
    既没有接收凭证，也不知道院里还有哪些动物等着接。
    """
    STATUS_CHOICES = [
        ('pending', '待确认'),
        ('received', '已确认出库'),
        ('rejected', '已驳回'),
        ('withdrawn', '已撤回'),
    ]
    OPEN_STATUSES = ('pending',)
    shelter = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='received_return_orders', verbose_name='接收捕捉点')
    shelter_name = models.CharField('接收捕捉点名称', max_length=100, blank=True, default='')
    hospital = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sent_return_orders', verbose_name='发出医院')
    hospital_name = models.CharField('发出医院名称', max_length=100, blank=True, default='')
    # 冗余「归属机构」= 发出医院（与转运单的 from_shelter 同一口径）。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='归属机构')
    pet_codes = models.TextField('动物编号', blank=True, default='', help_text='逗号分隔')
    pet_count = models.IntegerField('动物数量', default=0)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    received_at = models.DateField('接收日期', null=True, blank=True)
    receiver_name = models.CharField('接收人', max_length=50, blank=True, default='')
    receiver_phone = models.CharField('接收人电话', max_length=20, blank=True, default='')
    signature = models.TextField('接收人签字', blank=True, default='')
    reject_reason = models.TextField('驳回原因', blank=True, default='')
    note = models.TextField('备注', blank=True, default='')
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='return_orders', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='',
                                 db_index=True)
    district = models.ForeignKey('core.District', on_delete=models.PROTECT,
                                 related_name='return_orders', verbose_name='所属区县')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '运回单'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.hospital_name} -> {self.shelter_name} ({self.pet_count})'


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
    # 领养出库 / 领养人交接（第五十四轮出院重组）。
    # `pending_claim` 只是「捕捉点已批准、动物还在院」，医院真正交动物时
    # 必须留下出库日期、医院交接人与领养人签字，否则纸面上无法证明
    # 动物是哪天由谁交给领养人的。
    outbound_at = models.DateField('出库日期', null=True, blank=True)
    handover_name = models.CharField('交接人', max_length=50, blank=True, default='')
    signature = models.TextField('领养人签字', blank=True, default='')
    handover_photo = models.ImageField('交接照片', upload_to='adoptions/', null=True, blank=True)
    # 冗余「归属机构」—— 见 `Transfer.institution` 的说明。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='归属机构')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='completed')
    adopted_at = models.DateField('领养日期', null=True, blank=True)
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='adoptions_operated', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='',
                                 db_index=True)
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
    # 审核意见。驳回**必填**：领养人只看到「已驳回」时不知道要补什么，
    # 而回访驳回的常见原因就是「照片看不清 / 近况写得太少」——这类
    # 一句话就能改的问题，没有原因就等于让领养人猜。
    # 与 `AdoptionApplication.review_note` 同一口径（字段名、语义、下发方式都对齐）。
    review_note = models.TextField('审核意见', blank=True, default='')
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
    # 处置方式（第四十八轮）：出院模块把「死亡」与「安乐死」合并成一个
    # 子模块（「死亡（含安乐死）」），但台账必须分得开 —— 两者一个是
    # 主动终止生命、一个是自然/伤重死亡后的无害化处理，监管口径完全不同。
    # 合成一张表靠 `kind` 区分，而不是建两个模型：打印、关联链、统计
    # 都已经按 Euthanasia 写好了，拆表会让这些地方各分叉一次。
    KIND_CHOICES = [
        ('euthanasia', '安乐死'),
        ('dead', '死亡 / 无害化'),
    ]
    pet = models.ForeignKey('business.Pet', on_delete=models.CASCADE, related_name='euthanasia_records', verbose_name='宠物')
    pet_code = models.CharField('宠物编号', max_length=30, blank=True, default='')
    kind = models.CharField('处置方式', max_length=20, choices=KIND_CHOICES,
                            default='euthanasia')
    hospital = models.ForeignKey('core.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='euthanasia_records', verbose_name='医院')
    hospital_name = models.CharField('医院名称', max_length=100, blank=True, default='')
    # 冗余「归属机构」—— 见 `Transfer.institution` 的说明。
    institution = models.ForeignKey(
        'core.Institution', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='归属机构')
    reason = models.TextField('安乐死原因')
    condition = models.TextField('动物状况', blank=True, default='')
    euthanized_at = models.DateField('安乐死日期', null=True, blank=True)
    body_received = models.BooleanField('遗体已领取', default=False)
    body_received_at = models.DateField('遗体领取日期', null=True, blank=True)
    body_received_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='received_bodies', verbose_name='遗体领取人')
    body_received_by_name = models.CharField('遗体领取人姓名', max_length=50, blank=True, default='')
    # 死亡出库单（第五十四轮）：医院把遗体交给无害化处理厂时补录。
    # 原先只有捕捉点「领取遗体」一个动作，医院侧没有出库凭证 ——
    # 谁交接、交给哪家处理厂、接收人是谁、签字在哪，纸上全是空的。
    outbound_at = models.DateField('出库日期', null=True, blank=True)
    handover_name = models.CharField('交接人', max_length=50, blank=True, default='')
    receiving_org = models.CharField('接收机构（无害化处理厂）', max_length=100, blank=True, default='')
    receiver_name = models.CharField('接收人', max_length=50, blank=True, default='')
    # 旧版出库单与「接收机构」重复的字段，已不再写入与展示，仅保留历史数据。
    receiver_unit = models.CharField('接收单位', max_length=100, blank=True, default='')
    signature = models.TextField('接收人签字', blank=True, default='')
    photo = models.ImageField('出库照片', upload_to='euthanasia/', null=True, blank=True)
    operator = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='euthanasia_operated', verbose_name='操作员')
    operator_name = models.CharField('操作员姓名', max_length=50, blank=True, default='')
    ledger_no = models.CharField('台账编号', max_length=50, blank=True, default='',
                                 db_index=True)
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
    # 捕捉/诊疗环节已经产生的照片，上架时默认全部展示；医院可逐张隐藏。
    # 存字段名列表（如 ["photo_before"]），不存 URL —— 换图后设置仍然有效。
    hidden_photos = models.JSONField('大厅隐藏照片', default=list, blank=True)
    is_active = models.BooleanField('已上架', default=True)
    published_at = models.DateField('上架日期', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        ordering = ['-id']
        verbose_name = '领养大厅上架'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'上架 - {self.pet.code}'


class AdoptionPhoto(models.Model):
    """医院在领养资料里补拍的照片（不再重复上传捕捉/诊疗环节已有的照片）。"""
    listing = models.ForeignKey(
        AdoptionHallListing, on_delete=models.CASCADE,
        related_name='extra_photos', verbose_name='领养上架')
    image = models.ImageField('补充照片', upload_to='photos/adoption/')
    created_at = models.DateTimeField('上传时间', auto_now_add=True)

    class Meta:
        ordering = ['id']
        verbose_name = '领养补充照片'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'补充照片 - {self.listing.pet.code}'


# ============================================
# 单据号流水计数器
# ============================================
class DocumentSequence(models.Model):
    """单据号计数器：每「类型码 + 年份」一支流水，全市统一递增。

    单据号格式 `{类型码}{YY}{5位流水}`（如 CAP2600001），由
    `business.services.generate_doc_no()` 在事务内取号 —— 本表只做
    「发号到几号」的记账，不承载业务含义；单据号本身仍存在各业务单的
    `ledger_no` 字段上。

    ⚠ 与旧编号（`CAP-260925-0142` 日期+随机）的区别：新号是**顺序流水**，
    打印归档时人眼可数「第几张单」，且取号在行锁内完成、不可能重号。
    旧 `generate_ledger_no()` 保留仅为历史兼容，新代码一律用 `generate_doc_no()`。
    """
    prefix = models.CharField('单据类型码', max_length=8)
    year = models.PositiveSmallIntegerField('年份（后两位）')
    last_no = models.IntegerField('已发流水号', default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['prefix', 'year'], name='uniq_docseq_prefix_year'),
        ]
        verbose_name = '单据号计数器'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f'{self.prefix}{self.year:02d} → {self.last_no:05d}'
