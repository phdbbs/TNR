"""
Django management command: seed_data
填充数据库种子数据，与前端原型 tnr-data.js 保持一致。

用法:
    python manage.py seed_data              # 填充数据（跳过已存在）
    python manage.py seed_data --flush      # 先清空再填充
"""
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from business.models import (
    Pet, Capture, Transfer, Treatment, Material, MaterialTransaction,
    Chip, Release, Adoption, AdoptionHallListing, CheckIn, Blacklist,
    Euthanasia, Message,
)
from core.models import District, Institution

# ============================================
# 区县（**单一来源**）
# ============================================
# 字段：(内部代号, 展示名, 实际 code, 是否市级)
#
# ⚠ 不要在别处再抄一份。下面两处都从这里**派生**：
#   1. `SEED_MATERIALS` 里的区县代号（'D001' 等）；
#   2. `SEED_MATERIAL_DISTRICT_CODES`（演示物料的区县范围）。
# `code` 是稳定主键，`name` 只是展示名：改名后重跑不会重复创建。
# 名称与项目实际（襄阳市）保持一致，避免「北京区县名 + 襄阳业务数据」的错配。
SEED_DISTRICTS = [
    ('D000', '全市（市级）', 'CITY', True),
    ('D001', '襄城区', 'CY', False),
    ('D002', '樊城区', 'HD', False),
    ('D003', '东津新区', 'XC', False),
    ('D004', '襄州区', 'DC', False),
]

# 内部代号 → 区县实际 code（供种子清单与其它命令换算，避免各抄一份）
SEED_DISTRICT_CODE_BY_INTERNAL = {
    internal: code for internal, _name, code, _is_city in SEED_DISTRICTS
}

# ============================================
# 演示物料清单（**单一来源**）
# ============================================
# 字段：(mat_id, 名称, 类别, 单位, 规格, 供应商, 批号, 捕捉点库存, 安全库存,
#        有效期距今天数, 芯片起始号, 芯片结束号, 区县代号)
#
# ⚠ 有效期用**距今天数**，不要写绝对日期。
# 本命令里其它日期（入库 2025-01-05、诊疗 2025-01-14 等）都是**历史事件**，
# 固定在过去是对的；但「有效期」是**面向未来**的属性 —— 写死绝对日期会让
# 任何时间点的全新部署一上线就「全部已过期」（实测 2026-09-21 部署时，
# 三件种子物料分别过期 83 / 264 / 325 天，演示时整列都是过期数据）。
# 偏移取 90~270 天，使三件物料呈现不同的到期紧迫度。
#
# ⚠ `refresh_demo_material_expiry` 命令也读这份清单来订正**存量库**
# （`get_or_create` 的 defaults 只在创建时生效，已导入的库修不回来）。
# 两处必须同源 —— 改这里就等于同时改了两边。
SEED_MATERIALS = [
    # --- 襄城区（演示区县之一，内部代号 D001） ---
    ('MAT001', '狂犬疫苗', 'vaccine', '支', '1ml/支', '国药集团', 'B20250101',
     120, 50, 270, '', '', 'D001'),
    ('MAT002', '猫三联疫苗', 'vaccine', '支', '1ml/支', '英特威', 'B20250102',
     80, 40, 180, '', '', 'D001'),
    ('MAT003', '体内外驱虫药', 'dewormer', '盒', '6片/盒', '拜耳', 'Q20250101',
     60, 30, 90, '', '', 'D001'),
    ('MAT004', '宠物芯片', 'chip', '个', '134.2kHz', '信码科技', 'C20250101',
     500, 200, None, '1000010001', '1000010500', 'D001'),
    # --- 樊城区（演示区县之二，内部代号 D002） ---
    #
    # ⚠ 为什么樊城区也必须有：生产实测时 4 条物料**全挂在襄城区**，
    # `hd_gov`（樊城区政府管理员）登录后「物料管理」是**一整页空白** ——
    # 区县级账号看不到任何本区县物料，演示时看不出这个模块存在。
    #
    # 名称与襄城区**刻意重名**：同一件耗材在多个区县各有一条库存是正常
    # 业务状态，也正好让 `(name, district)` 这个幂等键始终被真实覆盖。
    ('MAT101', '狂犬疫苗', 'vaccine', '支', '1ml/支', '国药集团', 'B20250201',
     90, 40, 300, '', '', 'D002'),
    ('MAT102', '猫三联疫苗', 'vaccine', '支', '1ml/支', '英特威', 'B20250202',
     60, 30, 200, '', '', 'D002'),
    ('MAT103', '体内外驱虫药', 'dewormer', '盒', '6片/盒', '拜耳', 'Q20250201',
     45, 20, 120, '', '', 'D002'),
    ('MAT104', '宠物芯片', 'chip', '个', '134.2kHz', '信码科技', 'C20250201',
     300, 150, None, '1000010501', '1000011000', 'D002'),
]

# 演示物料所属区县的**实际 code**。
#
# ⚠ `refresh_demo_material_expiry` 用它把订正范围**限定在演示区县内**。
# 别的区县里同名的物料（如现场 4 个区县各有一套「狂犬疫苗」）不是演示数据，
# 只按名称匹配会误伤它们 —— 那是真实业务数据。
#
# 这里**从 SEED_MATERIALS 派生**，不手工维护：手工列表一旦与清单漂移
# （加了物料却忘了加区县），那个区县的演示物料过期后就永远修不回来，
# 而命令不会报错 —— 只是"改得比预期少"。派生之后不可能漂移。
# 是**元组**而不是单个字符串：演示区县可以不止一个（襄城区 + 樊城区）。
SEED_MATERIAL_DISTRICT_CODES = tuple(sorted({
    SEED_DISTRICT_CODE_BY_INTERNAL[row[-1]] for row in SEED_MATERIALS
}))


class Command(BaseCommand):
    help = '填充 TNR 系统种子数据（区县/机构/用户/物资/宠物/业务记录等）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--flush',
            action='store_true',
            help='先清空现有业务数据再填充',
        )

    def handle(self, *args, **options):
        flush = options.get('flush', False)

        if flush:
            self._flush_data()

        with transaction.atomic():
            districts = self._seed_districts()
            institutions = self._seed_institutions(districts)
            users = self._seed_users(districts, institutions)
            materials = self._seed_materials(districts)
            chips = self._seed_chips()
            captures = self._seed_captures(districts, institutions, users)
            pets = self._seed_pets(districts, institutions, captures, chips)
            self._link_chips_to_pets(pets, chips)
            self._seed_transfers(districts, institutions, users, captures)
            self._seed_treatments(districts, institutions, users, pets)
            self._seed_material_txns(districts, institutions, users, materials)
            self._seed_releases(districts, institutions, users, pets)
            self._seed_adoptions(districts, institutions, users, pets)
            self._seed_hall_listings(institutions, pets)
            self._seed_checkins(users, pets)
            self._seed_blacklist(districts, users)
            self._seed_euthanasia(districts, institutions, users, pets)
            self._seed_messages(users)

        self.stdout.write(self.style.SUCCESS('种子数据填充完成！'))

    # ============================================
    # 清空数据
    # ============================================
    def _flush_data(self):
        self.stdout.write('清空现有数据...')
        models_to_clear = [
            Message, Euthanasia, Blacklist, CheckIn, Adoption, Release,
            MaterialTransaction, Treatment, Transfer, Pet, Capture,
            Chip, Material,
        ]
        for model in models_to_clear:
            model.objects.all().delete()
        Institution.objects.all().delete()
        District.objects.all().delete()
        User.objects.filter(is_superuser=False).delete()
        self.stdout.write('数据已清空')

    # ============================================
    # 1. 区县
    # ============================================
    def _seed_districts(self):
        self.stdout.write('创建区县...')
        # 清单提到模块级 `SEED_DISTRICTS`（单一来源）—— 演示物料的区县范围
        # 也由它派生，抄两份必然漂移。
        districts = {}
        for code_id, name, code, is_city in SEED_DISTRICTS:
            d, created = District.objects.get_or_create(
                code=code,
                defaults={'name': name, 'is_city': is_city, 'status': 'active'}
            )
            districts[code_id] = d
            if not created and d.name != name:
                self.stdout.write(f'  区县 {code} 保留现有名称「{d.name}」（种子名称为「{name}」）')
        return districts

    # ============================================
    # 2. 机构
    # ============================================
    def _seed_institutions(self, districts):
        self.stdout.write('创建机构...')
        data = [
            # (code, 名称, 类型, 区县, 地址, 联系人, 电话)
            # 捕捉点
            ('I001', '襄城流浪动物捕捉点', 'shelter', 'D001', '襄城区檀溪路88号', '王主任', '13800001001'),
            ('I002', '樊城流浪动物捕捉点', 'shelter', 'D002', '樊城区长虹路15号', '李主任', '13800001002'),
            # 医院
            ('I003', '爱心宠物医院', 'hospital', 'D001', '襄城区鼓楼路12号', '赵医生', '13800002001'),
            ('I004', '瑞鹏宠物医院', 'hospital', 'D001', '襄城区人民广场旁', '钱医生', '13800002002'),
            ('I005', '芭比堂动物医院', 'hospital', 'D002', '樊城区解放路', '孙医生', '13800002003'),
            ('I006', '宠安宠物诊所', 'hospital', 'D003', '东津新区和谐路8号', '周医生', '13800002004'),
            # 小区
            ('C001', '阳光花园小区', 'community', 'D001', '襄城区阳光花园', '张物业', '13800003001'),
            ('C002', '翠湖天地小区', 'community', 'D001', '襄城区翠湖天地', '刘物业', '13800003002'),
            ('C003', '樊城幸福里小区', 'community', 'D002', '樊城区幸福路12号', '陈物业', '13800003003'),
            ('C004', '开发区和谐家园', 'community', 'D003', '东津新区和谐路6号', '杨物业', '13800003004'),
        ]
        institutions = {}
        for code_id, name, inst_type, district_code, address, contact, phone in data:
            # 按 code 幂等：机构名是可在界面上修改的展示字段，不能作为去重键
            inst, _ = Institution.objects.get_or_create(
                code=code_id,
                defaults={
                    'name': name,
                    'type': inst_type,
                    'district': districts[district_code],
                    'address': address,
                    'contact': contact,
                    'phone': phone,
                    'status': 'active',
                }
            )
            institutions[code_id] = inst
        return institutions

    # ============================================
    # 3. 用户
    # ============================================
    def _seed_users(self, districts, institutions):
        self.stdout.write('创建用户...')
        data = [
            # (username, name, role, district_code, inst_id, phone)
            ('admin', '市级管理员', 'gov_city', 'D000', None, '13800000001'),
            ('cy_gov', '襄城区政府管理员', 'gov_district', 'D001', None, '13800000002'),
            ('hd_gov', '樊城区政府管理员', 'gov_district', 'D002', None, '13800000003'),
            ('cy_shelter', '襄城捕捉点操作员', 'shelter', 'D000', 'I001', '13800000004'),
            ('hd_shelter', '樊城捕捉点操作员', 'shelter', 'D000', 'I002', '13800000005'),
            ('aixin_hosp', '爱心宠物医院', 'hospital', 'D001', 'I003', '13800000006'),
            ('ruipeng_hosp', '瑞鹏宠物医院', 'hospital', 'D001', 'I004', '13800000007'),
            ('babitang_hosp', '芭比堂动物医院', 'hospital', 'D002', 'I005', '13800000008'),
            ('adopter1', '王领养', 'adopter', None, None, '13800000009'),
        ]
        users = {}
        repaired = []
        for username, name, role, district_code, inst_id, phone in data:
            district = districts[district_code] if district_code else None
            institution = institutions[inst_id] if inst_id else None
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    'role': role,
                    'phone': phone,
                    'status': 'active',
                    'is_staff': role in ('gov_city', 'gov_district'),
                }
            )

            # DEMO_ACCOUNTS.md 承诺「每次部署/调试启动后演示账号都可用」。
            # 原实现只在 created 分支写属性，账号一旦被人为停用或改了归属，
            # 重跑 seed_data 完全修不回来（现场就出现过 aixin_hosp、hd_shelter
            # 被停用后无法登录，而部署流程看起来是"成功"的）。
            # 因此这里对已存在账号也做一次校准。
            changed = []
            if user.role != role:
                user.role = role
                changed.append('role')
            if user.phone != phone:
                user.phone = phone
                changed.append('phone')
            if user.district_id != (district.id if district else None):
                user.district = district
                changed.append('district')
            if user.institution_id != (institution.id if institution else None):
                user.institution = institution
                changed.append('institution')
            if user.first_name != name:
                user.first_name = name
                changed.append('first_name')
            want_staff = role in ('gov_city', 'gov_district')
            if user.is_staff != want_staff:
                user.is_staff = want_staff
                changed.append('is_staff')
            if not user.is_active or user.status != 'active':
                user.is_active = True
                user.status = 'active'
                changed.append('is_active')
            if created:
                # 密码只在新建时设置。已存在账号的密码不自动重置——
                # 生产环境里静默覆盖人为修改过的凭据是危险的，
                # 改密按 DEMO_ACCOUNTS.md 的手工流程处理。
                user.set_password('123456')
                changed.append('password')
            if changed:
                user.save()
                if not created:
                    repaired.append(f'{username}({"/".join(changed)})')
            users[username] = user

        if repaired:
            self.stdout.write(self.style.WARNING(
                f'  已校准 {len(repaired)} 个演示账号：' + '、'.join(repaired)))
        return users

    # ============================================
    # 4. 物资
    # ============================================
    def _seed_materials(self, districts):
        self.stdout.write('创建物资...')
        today = timezone.localdate()
        materials = {}
        for mat_id, name, category, unit, spec, supplier, batch_no, stock, safety, expiry_offset, chip_start, chip_end, district_code in SEED_MATERIALS:
            # 有效期由**距今天数**换算（见 SEED_MATERIALS 的说明）；
            # `None` 表示该物料没有有效期（芯片）。
            expiry = None if expiry_offset is None else today + timedelta(days=expiry_offset)
            # ⚠ 幂等键必须是 **(name, district)**，不能只用 name。
            #
            # `Material.name` **没有唯一约束**（`models.py` 里就是普通 CharField）：
            # 同一件物资在多个区县各有一条是**正常业务状态**（本地与生产库都是
            # 4 个区县 × 4 件同名物资）。此时 `get_or_create(name=name)` 会在
            # `self.get(**kwargs)` 上抛 `MultipleObjectsReturned` ——
            # 而整个 `handle()` 包在 `transaction.atomic()` 里，异常会让**全部
            # 种子数据回滚**，`deploy.sh` 第 6 步随之失败。
            #
            # 种子定义的是「**演示区县**要有这 4 件物资」，所以自然键就是
            # 名称 + 区县；同名物资落在别的区县时应当新建一条，而不是复用。
            district = districts[district_code]
            m, _ = Material.objects.get_or_create(
                name=name,
                district=district,
                defaults={
                    'category': category,
                    'unit': unit,
                    'specification': spec,
                    'supplier': supplier,
                    'batch_no': batch_no,
                    'shelter_stock': stock,
                    'safety_stock': safety,
                    'expiry_date': expiry,
                    'chip_range_start': chip_start,
                    'chip_range_end': chip_end,
                }
            )
            materials[mat_id] = m
        return materials

    # ============================================
    # 5. 芯片
    # ============================================
    def _seed_chips(self):
        self.stdout.write('创建芯片（500个）...')
        chips = {}
        # 号段 1000010001-1000010500，共 10 位。
        # 注意补零宽度是 3 位（{i:03d}）：写成 {i:04d} 会得到 11 位的
        # 10000100001，与宠物档案里的 chip_no（1000010001）对不上，
        # 医院登记「芯片植入」时会报「芯片不存在」。
        existing = set(Chip.objects.values_list('number', flat=True))
        to_create = []
        for i in range(1, 501):
            number = f'1000010{i:03d}'
            if number not in existing:
                to_create.append(Chip(number=number))
        if to_create:
            Chip.objects.bulk_create(to_create, batch_size=200)

        # 标记前15个为已使用
        for i in range(1, 16):
            number = f'1000010{i:03d}'
            chip = Chip.objects.get(number=number)
            chip.status = 'used'
            chip.used_at = date(2025, 1, 20)
            chip.save(update_fields=['status', 'used_at'])
            chips[i] = chip
        # 其余芯片
        for i in range(16, 501):
            number = f'1000010{i:03d}'
            chips[i] = Chip.objects.get(number=number)
        return chips

    # ============================================
    # 6. 捕捉记录
    # ============================================
    def _seed_captures(self, districts, institutions, users):
        self.stdout.write('创建捕捉记录...')
        captures = {}
        data = [
            ('CAP001', 'D001', 'I001', '襄城流浪动物捕捉点', 'C001', '阳光花园小区',
             '襄城区阳光花园3栋', '阳光物业', '张物业', '13800003001',
             3, 'TNR2501001,TNR2501002,TNR2501003', 'CAP-2025-0010-001', 'cy_shelter'),
            ('CAP002', 'D002', 'I002', '樊城流浪动物捕捉点', 'C003', '樊城幸福里小区',
             '樊城区幸福路12号5栋', '幸福物业', '陈物业', '13800003003',
             2, 'TNR2502004,TNR2502005', 'CAP-2025-0115-001', 'hd_shelter'),
        ]
        for cap_id, dist_code, shelter_id, shelter_name, comm_id, comm_name, address, prop, contact, phone, pet_count, pet_codes, ledger_no, operator in data:
            cap, _ = Capture.objects.get_or_create(
                ledger_no=ledger_no,
                defaults={
                    'district': districts[dist_code],
                    'shelter': institutions[shelter_id],
                    'shelter_name': shelter_name,
                    'community': institutions[comm_id],
                    'community_name': comm_name,
                    'address': address,
                    'property_name': prop,
                    'contact_person': contact,
                    'contact_phone': phone,
                    'pet_count': pet_count,
                    'pet_codes': pet_codes,
                    'status': 'completed',
                    'operator': users[operator],
                    'operator_name': users[operator].get_full_name() or users[operator].username,
                }
            )
            # shelter_name / community_name 是机构名的冗余副本。get_or_create 只在
            # 新建时应用 defaults，机构后来改名后副本会一直停在旧值（现场 CAP001、
            # CAP002 的 shelter_name 至今仍是北京时期的「朝阳区/海淀区流浪动物捕捉点」）。
            # 种子捕捉单以机构当前名为准做一次同步，保证演示数据自洽。
            stale = []
            if cap.shelter and cap.shelter_name != cap.shelter.name:
                cap.shelter_name = cap.shelter.name
                stale.append('shelter_name')
            if cap.community and cap.community_name != cap.community.name:
                cap.community_name = cap.community.name
                stale.append('community_name')
            if stale:
                cap.save(update_fields=stale)
                self.stdout.write(f'  捕捉单 {cap.ledger_no} 同步机构名称：{"/".join(stale)}')
            captures[cap_id] = cap
        return captures

    # ============================================
    # 7. 宠物档案
    # ============================================
    def _seed_pets(self, districts, institutions, captures, chips):
        self.stdout.write('创建宠物档案...')
        data = [
            # (pet_id, code, name, species, breed, gender, age, color, weight, status, dist, capture_id, shelter_id, hospital_id, chip_no, desc)
            ('PET001', 'TNR2501001', '橘猫一号', '猫', '橘猫', '公', '约2岁', '橘色', '4.5kg',
             'adopted', 'D001', 'CAP001', 'I001', 'I003', '1000010001', '性格亲人，已绝育、免疫、驱虫、植入芯片'),
            ('PET002', 'TNR2501002', '狸花二号', '猫', '狸花猫', '母', '约1岁', '灰黑', '3.2kg',
             'released', 'D001', 'CAP001', 'I001', 'I003', '1000010006', '已绝育放养至原小区'),
            ('PET003', 'TNR2501003', '黑犬三号', '狗', '中华田园犬', '公', '约3岁', '黑色', '15kg',
             'pending_adopt', 'D001', 'CAP001', 'I001', 'I003', '1000010011', '性格温顺，适合家庭领养'),
            ('PET004', 'TNR2502004', '白猫四号', '猫', '白猫', '母', '约6月', '白色', '2.5kg',
             'in_treatment', 'D002', 'CAP002', 'I002', 'I005', '', '治疗中'),
            ('PET005', 'TNR2502005', '花猫五号', '猫', '三花猫', '母', '约1岁', '三花', '3.0kg',
             'in_transit', 'D002', 'CAP002', 'I002', None, '', '转运中'),
            ('PET006', 'TNR2501006', '黄犬六号', '狗', '中华田园犬', '公', '约2岁', '黄色', '12kg',
             'euthanized', 'D001', 'CAP001', 'I001', 'I004', '1000010016', '因病重安乐死'),
        ]
        pets = {}
        for pet_id, code, name, species, breed, gender, age, color, weight, status, dist, cap_id, shelter_id, hospital_id, chip_no, desc in data:
            pet, _ = Pet.objects.get_or_create(
                code=code,
                defaults={
                    'name': name,
                    'species': species,
                    'breed': breed,
                    'gender': gender,
                    'age': age,
                    'color': color,
                    'weight': weight,
                    'status': status,
                    'district': districts[dist],
                    'capture': captures[cap_id],
                    'shelter': institutions[shelter_id],
                    'hospital': institutions[hospital_id] if hospital_id else None,
                    'chip_no': chip_no,
                    'description': desc,
                }
            )
            pets[pet_id] = pet
        return pets

    # ============================================
    # 8. 关联芯片到宠物
    # ============================================
    def _link_chips_to_pets(self, pets, chips):
        self.stdout.write('关联芯片到宠物...')
        # 芯片1-5 -> PET001, 6-10 -> PET002, 11-15 -> PET003
        mapping = {
            'PET001': list(range(1, 6)),
            'PET002': list(range(6, 11)),
            'PET003': list(range(11, 16)),
        }
        for pet_id, chip_indices in mapping.items():
            pet = pets[pet_id]
            for idx in chip_indices:
                chip = chips[idx]
                chip.pet = pet
                chip.save(update_fields=['pet'])

    # ============================================
    # 9. 转运记录
    # ============================================
    def _seed_transfers(self, districts, institutions, users, captures):
        self.stdout.write('创建转运记录...')
        data = [
            ('TRF001', 'CAP001', 'I001', '襄城流浪动物捕捉点', 'I003', '爱心宠物医院',
             'TNR2501001,TNR2501002,TNR2501003', 3, 'received', date(2025, 1, 12),
             '', 'TRF-2025-0111-001', 'D001', 'cy_shelter'),
            ('TRF002', 'CAP002', 'I002', '樊城流浪动物捕捉点', 'I005', '芭比堂动物医院',
             'TNR2502004', 1, 'received', date(2025, 1, 16),
             '', 'TRF-2025-0115-001', 'D002', 'hd_shelter'),
            ('TRF003', 'CAP002', 'I002', '樊城流浪动物捕捉点', 'I005', '芭比堂动物医院',
             'TNR2502005', 1, 'pending', None,
             '', 'TRF-2025-0118-001', 'D002', 'hd_shelter'),
        ]
        for trf_id, cap_id, from_id, from_name, to_id, to_name, pet_codes, count, status, received_at, reject, ledger_no, dist, operator in data:
            Transfer.objects.get_or_create(
                ledger_no=ledger_no,
                defaults={
                    'capture': captures[cap_id],
                    'from_shelter': institutions[from_id],
                    'from_shelter_name': from_name,
                    'to_hospital': institutions[to_id],
                    'to_hospital_name': to_name,
                    'pet_codes': pet_codes,
                    'pet_count': count,
                    'status': status,
                    'received_at': received_at,
                    'reject_reason': reject,
                    'operator': users[operator],
                    'operator_name': users[operator].get_full_name() or users[operator].username,
                    'district': districts[dist],
                }
            )

    # ============================================
    # 10. 诊疗记录
    # ============================================
    def _seed_treatments(self, districts, institutions, users, pets):
        self.stdout.write('创建诊疗记录...')
        data = [
            ('TRE001', 'PET001', 'TNR2501001', 'I003', '爱心宠物医院',
             True, True, True, True,
             date(2025, 1, 14), '赵医生', '健康成年橘猫，适合绝育', '吸入麻醉', '常规绝育手术', '良好',
             '狂犬疫苗', 'B20250101', date(2025, 1, 13), 1,
             '体内外驱虫药', 'Q20250101', date(2025, 1, 13), 1,
             '1000010001', date(2025, 1, 15), 'completed', 'D001', 'aixin_hosp'),
            ('TRE002', 'PET002', 'TNR2501002', 'I003', '爱心宠物医院',
             True, True, True, True,
             date(2025, 1, 14), '赵医生', '健康狸花猫', '注射麻醉', '常规绝育手术', '良好',
             '猫三联疫苗', 'B20250102', date(2025, 1, 13), 1,
             '体内外驱虫药', 'Q20250101', date(2025, 1, 13), 1,
             '1000010006', date(2025, 1, 15), 'completed', 'D001', 'aixin_hosp'),
            ('TRE003', 'PET003', 'TNR2501003', 'I003', '爱心宠物医院',
             True, True, True, True,
             date(2025, 1, 16), '赵医生', '健康中华田园犬', '吸入麻醉', '常规绝育手术', '良好',
             '狂犬疫苗', 'B20250101', date(2025, 1, 15), 1,
             '体内外驱虫药', 'Q20250101', date(2025, 1, 15), 1,
             '1000010011', date(2025, 1, 17), 'completed', 'D001', 'aixin_hosp'),
            ('TRE004', 'PET004', 'TNR2502004', 'I005', '芭比堂动物医院',
             False, True, True, False,
             None, '', '', '', '', '',
             '猫三联疫苗', 'B20250102', date(2025, 1, 17), 1,
             '体内外驱虫药', 'Q20250101', date(2025, 1, 17), 1,
             '', None, 'in_progress', 'D002', 'babitang_hosp'),
        ]
        for row in data:
            (tre_id, pet_id, pet_code, hosp_id, hosp_name,
             ster, vac, dew, chip_item,
             surg_date, surgeon, diag, anesthesia, procedure, recovery,
             vac_type, vac_batch, vac_date, vac_qty,
             dew_type, dew_batch, dew_date, dew_qty,
             chip_no, chip_date, status, dist, operator) = row
            Treatment.objects.get_or_create(
                pet=pets[pet_id],
                defaults={
                    'pet_code': pet_code,
                    'hospital': institutions[hosp_id],
                    'hospital_name': hosp_name,
                    'items_sterilization': ster,
                    'items_vaccine': vac,
                    'items_deworming': dew,
                    'items_chip': chip_item,
                    'sterilization_surgery_date': surg_date,
                    'sterilization_surgeon': surgeon,
                    'sterilization_diagnosis': diag,
                    'sterilization_anesthesia': anesthesia,
                    'sterilization_procedure': procedure,
                    'sterilization_recovery': recovery,
                    'vaccine_type': vac_type,
                    'vaccine_batch_no': vac_batch,
                    'vaccine_date': vac_date,
                    'vaccine_quantity': vac_qty,
                    'deworming_type': dew_type,
                    'deworming_batch_no': dew_batch,
                    'deworming_date': dew_date,
                    'deworming_quantity': dew_qty,
                    'chip_no': chip_no,
                    'chip_date': chip_date,
                    'status': status,
                    'operator': users[operator],
                    'operator_name': users[operator].get_full_name() or users[operator].username,
                    'district': districts[dist],
                }
            )

    # ============================================
    # 11. 物资流水
    # ============================================
    def _seed_material_txns(self, districts, institutions, users, materials):
        self.stdout.write('创建物资流水...')
        data = [
            ('MTX001', 'purchase', 'MAT001', '狂犬疫苗', 100, '支', 'B20250101',
             '国药集团', '国药集团', None, 'PUR-2025-0105-001', date(2025, 1, 5),
             'D001', 'cy_shelter', '采购入库'),
            ('MTX002', 'dispatch', 'MAT001', '狂犬疫苗', 55, '支', 'B20250101',
             '', '爱心宠物医院/瑞鹏宠物医院', None, 'DIS-2025-0108-001', date(2025, 1, 8),
             'D001', 'cy_shelter', '下发至医院'),
            ('MTX003', 'consume', 'MAT001', '狂犬疫苗', 3, '支', 'B20250101',
             '', '诊疗消耗', 'I003', 'CON-2025-0113-001', date(2025, 1, 13),
             'D001', 'aixin_hosp', 'PET001,PET002疫苗接种'),
            ('MTX003R', 'receive', 'MAT001', '狂犬疫苗', 55, '支', 'B20250101',
             '', '襄城流浪动物捕捉点', 'I003', 'RVC-2025-0110-001', date(2025, 1, 10),
             'D001', 'aixin_hosp', '签收下发物料（原单号：DIS-2025-0108-001）'),
            ('MTX004', 'purchase', 'MAT004', '宠物芯片', 500, '个', 'C20250101',
             '信码科技', '信码科技', None, 'PUR-2025-0103-001', date(2025, 1, 3),
             'D001', 'cy_shelter', '芯片采购，号段1000010001-1000010500'),
            ('MTX005', 'dispatch', 'MAT004', '宠物芯片', 210, '个', 'C20250101',
             '', '多家医院', None, 'DIS-2025-0107-001', date(2025, 1, 7),
             'D001', 'cy_shelter', '下发至各医院'),
            ('MTX006', 'consume', 'MAT004', '宠物芯片', 3, '个', 'C20250101',
             '', '诊疗消耗', 'I003', 'CON-2025-0115-001', date(2025, 1, 15),
             'D001', 'aixin_hosp', 'PET001,PET002,PET003芯片植入'),
            ('MTX006R', 'receive', 'MAT004', '宠物芯片', 210, '个', 'C20250101',
             '', '襄城流浪动物捕捉点', 'I003', 'RVC-2025-0107-001', date(2025, 1, 7),
             'D001', 'aixin_hosp', '签收下发物料（原单号：DIS-2025-0107-001）'),
            # --- 樊城区（D002）---
            #
            # 只补物料不补流水，`hd_gov` 的「库存流水」页仍是空的 ——
            # 物料台账的意义就在流水上，所以这里配两条：
            # 一条采购入库（形成库存），一条下发医院且**尚未签收**
            # （正好演示「待签收」这个中间态）。
            ('MTX101', 'purchase', 'MAT101', '狂犬疫苗', 90, '支', 'B20250201',
             '国药集团', '国药集团', None, 'PUR-2026-0210-001', date(2026, 2, 10),
             'D002', 'hd_shelter', '采购入库'),
            ('MTX102', 'dispatch', 'MAT101', '狂犬疫苗', 30, '支', 'B20250201',
             '', '芭比堂动物医院', 'I005', 'DIS-2026-0212-001', date(2026, 2, 12),
             'D002', 'hd_shelter', '下发至医院（待签收）'),
        ]
        for row in data:
            (mtx_id, txn_type, mat_id, mat_name, qty, unit, batch_no,
             supplier, from_to, hosp_id, ledger_no, txn_date, dist, operator, note) = row
            # ⚠ 幂等键是 **(ledger_no, type)**，不能只用 `ledger_no`。
            #
            # `ledger_no` **本身就不唯一** —— 这是业务设计，不是缺陷：
            # 「下发」与「签收」是同一张单的两条台账，运行时两边共用同一个
            # `DIS-` 单号（签收行的 note 里写着「原单号：DIS-…」）。
            # 本地库实测：按 `ledger_no` 分组有 **21 组**重复，加上 `type`
            # 之后只剩 **1 组**（`''` × 66，那是 `consume` 消耗记录本来
            # 就没有台账编号）—— 21 组里绝大多数正是这种下发/签收配对。
            #
            # 只按 `ledger_no` 取键，将来种子里真的配一对同号的下发/签收，
            # 就会**静默少建一条**（第二条被当成"已存在"跳过），而且不报错。
            # 加上 `type` 之后，键与业务身份一致：同号不同侧是两条。
            MaterialTransaction.objects.get_or_create(
                ledger_no=ledger_no,
                type=txn_type,
                defaults={
                    'material': materials[mat_id],
                    'material_name': mat_name,
                    'quantity': qty,
                    'unit': unit,
                    'batch_no': batch_no,
                    'supplier': supplier,
                    'from_to': from_to,
                    'hospital': institutions[hosp_id] if hosp_id else None,
                    'operator': users[operator],
                    'operator_name': users[operator].get_full_name() or users[operator].username,
                    'date': txn_date,
                    'district': districts[dist],
                    'note': note,
                }
            )

    # ============================================
    # 12. 放养记录
    # ============================================
    def _seed_releases(self, districts, institutions, users, pets):
        self.stdout.write('创建放养记录...')
        data = [
            ('REL001', 'PET002', 'TNR2501002', 'C001', '阳光花园小区',
             '张物业', '13800003001', 'released', date(2025, 1, 25),
             'REL-2025-0124-001', 'D001', 'cy_shelter'),
        ]
        for row in data:
            (rel_id, pet_id, pet_code, comm_id, comm_name,
             receiver, phone, status, released_at, ledger_no, dist, operator) = row
            Release.objects.get_or_create(
                ledger_no=ledger_no,
                defaults={
                    'pet': pets[pet_id],
                    'pet_code': pet_code,
                    'community': institutions[comm_id],
                    'community_name': comm_name,
                    'receiver_name': receiver,
                    'receiver_phone': phone,
                    'status': status,
                    'released_at': released_at,
                    'operator': users[operator],
                    'operator_name': users[operator].get_full_name() or users[operator].username,
                    'district': districts[dist],
                }
            )

    # ============================================
    # 13. 领养记录
    # ============================================
    def _seed_adoptions(self, districts, institutions, users, pets):
        self.stdout.write('创建领养记录...')
        data = [
            ('ADP001', 'PET001', 'TNR2501001',
             '王领养', '13800000009', '420602****1234', '襄城区某某小区',
             '有稳定住所，有养宠经验', '已签署', '线下已签',
             'I003', '爱心宠物医院', 'completed', date(2025, 2, 1),
             'ADP-2025-0128-001', 'D001', 'cy_shelter'),
        ]
        for row in data:
            (adp_id, pet_id, pet_code, adopter_name, adopter_phone, adopter_id, adopter_addr,
             qual, commit, agreement, hosp_id, hosp_name, status, adopted_at,
             ledger_no, dist, operator) = row
            Adoption.objects.get_or_create(
                ledger_no=ledger_no,
                defaults={
                    'pet': pets[pet_id],
                    'pet_code': pet_code,
                    'adopter': users['adopter1'],
                    'adopter_name': adopter_name,
                    'adopter_phone': adopter_phone,
                    'adopter_id_card': adopter_id,
                    'adopter_address': adopter_addr,
                    'qualification': qual,
                    'commitment_letter': commit,
                    'adoption_agreement': agreement,
                    'hospital': institutions[hosp_id],
                    'hospital_name': hosp_name,
                    'status': status,
                    'adopted_at': adopted_at,
                    'operator': users[operator],
                    'operator_name': users[operator].get_full_name() or users[operator].username,
                    'district': districts[dist],
                }
            )

    # ============================================
    # 13.1 领养大厅上架信息
    # ============================================
    def _seed_hall_listings(self, institutions, pets):
        self.stdout.write('创建领养大厅上架信息...')
        # PET003 黑犬三号（待领养）上架展示，保证公开领养大厅有可看内容
        AdoptionHallListing.objects.get_or_create(
            pet=pets['PET003'],
            defaults={
                'hospital': institutions['I003'],
                'hospital_name': '爱心宠物医院',
                'intro': '性格沉稳亲人，已完成绝育和疫苗接种，已植入芯片。适合有固定住所的家庭领养。',
                'personality': '安静温顺，对人友好，与其他狗狗相处融洽',
                'body_condition': '体况良好，已驱虫，疫苗齐全',
                'flow_doc': '提交领养申请 → 机构审核 → 签署领养协议 → 医院确认领出 → 每月回访打卡',
                'is_active': True,
                'published_at': date(2025, 1, 20),
            }
        )

    # ============================================
    # 14. 回访打卡
    # ============================================
    def _seed_checkins(self, users, pets):
        self.stdout.write('创建回访打卡...')
        data = [
            ('CHK001', 'PET001', 'TNR2501001', 'adopter1', '王领养',
             '2025-02', '猫咪适应良好，食欲正常', 'approved'),
            ('CHK002', 'PET001', 'TNR2501001', 'adopter1', '王领养',
             '2025-03', '一切正常，体重增长', 'approved'),
        ]
        for row in data:
            (chk_id, pet_id, pet_code, adopter, adopter_name, month, note, status) = row
            CheckIn.objects.get_or_create(
                pet=pets[pet_id],
                month=month,
                defaults={
                    'pet_code': pet_code,
                    'adopter': users[adopter],
                    'adopter_name': adopter_name,
                    'note': note,
                    'status': status,
                }
            )

    # ============================================
    # 15. 黑名单
    # ============================================
    def _seed_blacklist(self, districts, users):
        self.stdout.write('创建黑名单...')
        data = [
            ('BLK001', '李某某', '110102****5678', '13900000001', '弃养领养宠物',
             date(2024, 12, 15), 'D001', 'cy_shelter'),
        ]
        for row in data:
            (blk_id, name, id_card, phone, reason, violation_date, dist, operator) = row
            Blacklist.objects.get_or_create(
                name=name,
                phone=phone,
                defaults={
                    'id_card': id_card,
                    'reason': reason,
                    'violation_date': violation_date,
                    'operator': users[operator],
                    'operator_name': users[operator].get_full_name() or users[operator].username,
                    'district': districts[dist],
                }
            )

    # ============================================
    # 16. 安乐死记录
    # ============================================
    def _seed_euthanasia(self, districts, institutions, users, pets):
        self.stdout.write('创建安乐死记录...')
        data = [
            ('EUT001', 'PET006', 'TNR2501006', 'I004', '瑞鹏宠物医院',
             '严重外伤感染，无法救治', '后腿骨折感染，多处伤口化脓',
             date(2025, 1, 20), True, date(2025, 1, 21),
             'cy_shelter', '襄城捕捉点操作员',
             'EUT-2025-0120-001', 'D001', 'ruipeng_hosp'),
        ]
        for row in data:
            (eut_id, pet_id, pet_code, hosp_id, hosp_name, reason, condition,
             euthanized_at, body_received, body_received_at,
             body_received_by, body_received_name, ledger_no, dist, operator) = row
            Euthanasia.objects.get_or_create(
                ledger_no=ledger_no,
                defaults={
                    'pet': pets[pet_id],
                    'pet_code': pet_code,
                    'hospital': institutions[hosp_id],
                    'hospital_name': hosp_name,
                    'reason': reason,
                    'condition': condition,
                    'euthanized_at': euthanized_at,
                    'body_received': body_received,
                    'body_received_at': body_received_at,
                    'body_received_by': users[body_received_by],
                    'body_received_by_name': body_received_name,
                    'operator': users[operator],
                    'operator_name': users[operator].get_full_name() or users[operator].username,
                    'district': districts[dist],
                }
            )

    # ============================================
    # 17. 消息通知
    # ============================================
    def _seed_messages(self, users):
        self.stdout.write('创建消息通知...')
        data = [
            ('MSG001', 'adopter1', 'approval', '领养审核通过',
             '您的领养申请已通过审核，PET001已成功领养。', False),
            ('MSG002', 'adopter1', 'checkin_reminder', '月度打卡提醒',
             '请于本月完成PET001的回访打卡。', True),
            ('MSG003', 'adopter1', 'checkin_reminder', '3月打卡提醒',
             '请于3月完成PET001的月度回访打卡。', False),
        ]
        for msg_id, user, msg_type, title, content, is_read in data:
            # ⚠ 幂等键必须**带上收件人**，不能只用 (title, content)。
            #
            # `Message` 是「一条通知发给一个人」的流水表：同一个 (title, content)
            # 本来就会有多行（同一只宠物的审核/领养完成通知反复产生、
            # 一条公告发给 N 个领养人）。本地库实测按 (title, content) 分组
            # 有 **9 组**重复 —— 那是真实使用产生的**正常数据，不是脏数据，
            # 无需清理**（已核对：同一用户同标题同正文的多行创建时间相隔 8~9
            # 分钟，是反复实测留下的，不是一次请求写重）。
            #
            # 但 (title, content) 不是这个表的业务身份，用它取键会**认领别人的行**：
            # 库里只要已有一条同标题同内容、但收件人不是 `adopter1` 的真实通知，
            # `get_or_create` 就认为"已存在"并跳过 —— 演示账号**静默收不到**
            # 这条演示消息，而命令照样报成功。
            # 业务身份 = (收件人, 类型, 标题, 正文)，四者齐备才是同一条。
            #
            # ⚠ 这是**潜在**缺陷，不是活缺陷：种子清单里三条消息的键本来就
            # 互不相同，所以今天撞不上、修完也看不出可见差异 —— 但键与业务
            # 身份不一致这件事本身，迟早会撞，而且撞了不报错。
            # `msg_id` 只是清单里的可读标号（`Message` 没有这一列），不参与取键。
            Message.objects.get_or_create(
                user=users[user],
                type=msg_type,
                title=title,
                content=content,
                defaults={
                    'is_read': is_read,
                }
            )
