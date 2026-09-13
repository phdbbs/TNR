"""
Django management command: seed_data
填充数据库种子数据，与前端原型 tnr-data.js 保持一致。

用法:
    python manage.py seed_data              # 填充数据（跳过已存在）
    python manage.py seed_data --flush      # 先清空再填充
"""
from datetime import date, datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import User
from business.models import (
    Pet, Capture, Transfer, Treatment, Material, MaterialTransaction,
    Chip, Release, Adoption, AdoptionHallListing, CheckIn, Blacklist,
    Euthanasia, Message,
)
from core.models import District, Institution


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
        data = [
            ('D000', '全市（市级）', 'CITY', True),
            ('D001', '朝阳区', 'CY', False),
            ('D002', '海淀区', 'HD', False),
            ('D003', '西城区', 'XC', False),
            ('D004', '东城区', 'DC', False),
        ]
        districts = {}
        for code_id, name, code, is_city in data:
            d, _ = District.objects.get_or_create(
                code=code,
                defaults={'name': name, 'is_city': is_city, 'status': 'active'}
            )
            districts[code_id] = d
        return districts

    # ============================================
    # 2. 机构
    # ============================================
    def _seed_institutions(self, districts):
        self.stdout.write('创建机构...')
        data = [
            # 捕捉点（捕捉点）
            ('I001', '朝阳区流浪动物捕捉点', 'shelter', 'D001', '朝阳区建国路88号', '王主任', '13800001001'),
            ('I002', '海淀区流浪动物捕捉点', 'shelter', 'D002', '海淀区中关村大街15号', '李主任', '13800001002'),
            # 医院
            ('I003', '爱心宠物医院', 'hospital', 'D001', '朝阳区三里屯路12号', '赵医生', '13800002001'),
            ('I004', '瑞鹏宠物医院', 'hospital', 'D001', '朝阳区望京SOHO旁', '钱医生', '13800002002'),
            ('I005', '芭比堂动物医院', 'hospital', 'D002', '海淀区五道口', '孙医生', '13800002003'),
            ('I006', '宠安宠物诊所', 'hospital', 'D003', '西城区西单', '周医生', '13800002004'),
            # 小区
            ('C001', '阳光花园小区', 'community', 'D001', '朝阳区阳光花园', '张物业', '13800003001'),
            ('C002', '翠湖天地小区', 'community', 'D001', '朝阳区翠湖天地', '刘物业', '13800003002'),
            ('C003', '中关村南区', 'community', 'D002', '海淀区中关村南区', '陈物业', '13800003003'),
            ('C004', '西单美居', 'community', 'D003', '西城区西单北大街', '杨物业', '13800003004'),
        ]
        institutions = {}
        for inst_id, name, inst_type, district_code, address, contact, phone in data:
            inst, _ = Institution.objects.get_or_create(
                name=name,
                defaults={
                    'type': inst_type,
                    'district': districts[district_code],
                    'address': address,
                    'contact': contact,
                    'phone': phone,
                    'status': 'active',
                }
            )
            institutions[inst_id] = inst
        return institutions

    # ============================================
    # 3. 用户
    # ============================================
    def _seed_users(self, districts, institutions):
        self.stdout.write('创建用户...')
        data = [
            # (username, name, role, district_code, inst_id, phone)
            ('admin', '市级管理员', 'gov_city', 'D000', None, '13800000001'),
            ('cy_gov', '朝阳区政府管理员', 'gov_district', 'D001', None, '13800000002'),
            ('hd_gov', '海淀区政府管理员', 'gov_district', 'D002', None, '13800000003'),
            ('cy_shelter', '朝阳捕捉点操作员', 'shelter', 'D000', 'I001', '13800000004'),
            ('hd_shelter', '海淀捕捉点操作员', 'shelter', 'D000', 'I002', '13800000005'),
            ('aixin_hosp', '爱心宠物医院', 'hospital', 'D001', 'I003', '13800000006'),
            ('ruipeng_hosp', '瑞鹏宠物医院', 'hospital', 'D001', 'I004', '13800000007'),
            ('babitang_hosp', '芭比堂动物医院', 'hospital', 'D002', 'I005', '13800000008'),
            ('adopter1', '王领养', 'adopter', None, None, '13800000009'),
        ]
        users = {}
        for username, name, role, district_code, inst_id, phone in data:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    'role': role,
                    'phone': phone,
                    'status': 'active',
                    'is_staff': role in ('gov_city', 'gov_district'),
                }
            )
            if created:
                user.set_password('123456')
                user.first_name = name
                if district_code:
                    user.district = districts[district_code]
                if inst_id:
                    user.institution = institutions[inst_id]
                user.save()
            users[username] = user
        return users

    # ============================================
    # 4. 物资
    # ============================================
    def _seed_materials(self, districts):
        self.stdout.write('创建物资...')
        data = [
            ('MAT001', '狂犬疫苗', 'vaccine', '支', '1ml/支', '国药集团', 'B20250101',
             120, 50, date(2025, 12, 31), '', '', 'D001'),
            ('MAT002', '猫三联疫苗', 'vaccine', '支', '1ml/支', '英特威', 'B20250102',
             80, 40, date(2025, 10, 31), '', '', 'D001'),
            ('MAT003', '体内外驱虫药', 'dewormer', '盒', '6片/盒', '拜耳', 'Q20250101',
             60, 30, date(2026, 6, 30), '', '', 'D001'),
            ('MAT004', '宠物芯片', 'chip', '个', '134.2kHz', '信码科技', 'C20250101',
             500, 200, None, '1000010001', '1000010500', 'D001'),
        ]
        materials = {}
        for mat_id, name, category, unit, spec, supplier, batch_no, stock, safety, expiry, chip_start, chip_end, district_code in data:
            m, _ = Material.objects.get_or_create(
                name=name,
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
                    'district': districts[district_code],
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
        existing = set(Chip.objects.values_list('number', flat=True))
        to_create = []
        for i in range(1, 501):
            number = f'1000010{i:04d}'
            if number not in existing:
                to_create.append(Chip(number=number))
        if to_create:
            Chip.objects.bulk_create(to_create, batch_size=200)

        # 标记前15个为已使用
        for i in range(1, 16):
            number = f'1000010{i:04d}'
            chip = Chip.objects.get(number=number)
            chip.status = 'used'
            chip.used_at = date(2025, 1, 20)
            chip.save(update_fields=['status', 'used_at'])
            chips[i] = chip
        # 其余芯片
        for i in range(16, 501):
            number = f'1000010{i:04d}'
            chips[i] = Chip.objects.get(number=number)
        return chips

    # ============================================
    # 6. 捕捉记录
    # ============================================
    def _seed_captures(self, districts, institutions, users):
        self.stdout.write('创建捕捉记录...')
        captures = {}
        data = [
            ('CAP001', 'D001', 'I001', '朝阳区流浪动物捕捉点', 'C001', '阳光花园小区',
             '朝阳区阳光花园3栋', '阳光物业', '张物业', '13800003001',
             3, 'TNR2501001,TNR2501002,TNR2501003', 'CAP-2025-0010-001', 'cy_shelter'),
            ('CAP002', 'D002', 'I002', '海淀区流浪动物捕捉点', 'C003', '中关村南区',
             '海淀区中关村南区5栋', '中关物业', '陈物业', '13800003003',
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
            ('TRF001', 'CAP001', 'I001', '朝阳区流浪动物捕捉点', 'I003', '爱心宠物医院',
             'TNR2501001,TNR2501002,TNR2501003', 3, 'received', date(2025, 1, 12),
             '', 'TRF-2025-0111-001', 'D001', 'cy_shelter'),
            ('TRF002', 'CAP002', 'I002', '海淀区流浪动物捕捉点', 'I005', '芭比堂动物医院',
             'TNR2502004', 1, 'received', date(2025, 1, 16),
             '', 'TRF-2025-0115-001', 'D002', 'hd_shelter'),
            ('TRF003', 'CAP002', 'I002', '海淀区流浪动物捕捉点', 'I005', '芭比堂动物医院',
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
             '', '朝阳区流浪动物捕捉点', 'I003', 'RVC-2025-0110-001', date(2025, 1, 10),
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
             '', '朝阳区流浪动物捕捉点', 'I003', 'RVC-2025-0107-001', date(2025, 1, 7),
             'D001', 'aixin_hosp', '签收下发物料（原单号：DIS-2025-0107-001）'),
        ]
        for row in data:
            (mtx_id, txn_type, mat_id, mat_name, qty, unit, batch_no,
             supplier, from_to, hosp_id, ledger_no, txn_date, dist, operator, note) = row
            MaterialTransaction.objects.get_or_create(
                ledger_no=ledger_no,
                defaults={
                    'type': txn_type,
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
             '王领养', '13800000009', '110105****1234', '朝阳区某某小区',
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
             'cy_shelter', '朝阳捕捉点操作员',
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
            Message.objects.get_or_create(
                title=title,
                content=content,
                defaults={
                    'user': users[user],
                    'type': msg_type,
                    'is_read': is_read,
                }
            )
