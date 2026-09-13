"""
Task 6: 诊疗与物料库存联动
- 诊疗列表/创建/详情
- 疫苗/驱虫/芯片消耗自动扣减医院库存
- 诊疗完成时通过 django-q2 调度5天自动转待领养
"""
from datetime import datetime, date as date_type

from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Treatment, Pet, Material, Chip
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    generate_ledger_no, get_district_filtered_queryset,
    adjust_stock, use_chip, get_hospital_stock,
)


@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def treatment_list(request):
    """诊疗列表"""
    user = request.user

    if user.role == 'hospital':
        # 医院只看本院诊疗记录（不按区县过滤，因为宠物可能跨区县转运）
        if user.institution_id:
            qs = Treatment.objects.filter(hospital_id=user.institution_id)
        else:
            qs = Treatment.objects.none()
    else:
        qs = get_district_filtered_queryset(Treatment, user)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    data = [serialize_instance(t) for t in qs]
    return json_ok(data)


@csrf_exempt
@role_required('hospital', 'gov_city', 'gov_district')
@login_required
def treatment_create(request):
    """创建诊疗记录

    请求体示例:
    {
        "pet_id": 1,
        "items": {"sterilization": true, "vaccine": true, "deworming": true, "chip": true},
        "sterilization": {"surgery_date": "2025-01-14", "surgeon": "赵医生", ...},
        "vaccine": {"type": "狂犬疫苗", "material_id": 1, "batch_no": "B001", "date": "2025-01-13"},
        "deworming": {"type": "体内外驱虫药", "material_id": 3, "batch_no": "Q001", "date": "2025-01-13"},
        "chip": {"chip_no": "1000010001", "date": "2025-01-15"},
        "status": "in_progress"
    }
    """
    data = parse_json_body(request)
    user = request.user

    pet_id = data.get('pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    try:
        pet = Pet.objects.get(id=pet_id)
    except Pet.DoesNotExist:
        return json_fail('宠物不存在')

    if pet.status not in ('in_treatment', 'pending_adopt'):
        return json_fail(f'宠物当前状态({pet.status})不可诊疗')

    items = data.get('items', {})
    hospital = pet.hospital or getattr(user, 'institution', None)
    if not hospital:
        return json_fail('缺少医院信息')

    district_id = pet.district_id or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    status = data.get('status', 'in_progress')

    treatment = Treatment.objects.create(
        pet=pet,
        pet_code=pet.code,
        hospital=hospital,
        hospital_name=hospital.name,
        items_sterilization=items.get('sterilization', False),
        items_vaccine=items.get('vaccine', False),
        items_deworming=items.get('deworming', False),
        items_chip=items.get('chip', False),
        status=status,
        operator=user,
        operator_name=user.get_full_name() or user.username,
        ledger_no=generate_ledger_no('TRE'),
        district_id=district_id,
    )

    # 绝育信息
    ster = data.get('sterilization', {})
    if ster:
        treatment.sterilization_surgeon = ster.get('surgeon', '')
        treatment.sterilization_diagnosis = ster.get('diagnosis', '')
        treatment.sterilization_anesthesia = ster.get('anesthesia', '')
        treatment.sterilization_procedure = ster.get('procedure', '')
        treatment.sterilization_recovery = ster.get('recovery', '')
        surgery_date = ster.get('surgery_date')
        if surgery_date:
            treatment.sterilization_surgery_date = _parse_date(surgery_date)

    # 疫苗 - 消耗库存
    vac = data.get('vaccine', {})
    if items.get('vaccine') and vac:
        treatment.vaccine_type = vac.get('type', '')
        treatment.vaccine_batch_no = vac.get('batch_no', '')
        vaccine_date = vac.get('date')
        if vaccine_date:
            treatment.vaccine_date = _parse_date(vaccine_date)
        treatment.vaccine_quantity = int(vac.get('quantity', 1))

        material_id = vac.get('material_id')
        if material_id:
            try:
                material = Material.objects.get(id=material_id, category='vaccine')
                if get_hospital_stock(material, hospital) < treatment.vaccine_quantity:
                    return json_fail(f'疫苗库存不足（{material.name}），请先补充库存')
                adjust_stock(
                    material=material,
                    hospital=hospital,
                    quantity=treatment.vaccine_quantity,
                    txn_type='consume',
                    operator=user,
                    operator_name=user.get_full_name() or user.username,
                    from_to='诊疗消耗',
                    note=f'{pet.code} 疫苗接种',
                )
            except Material.DoesNotExist:
                pass

    # 驱虫 - 消耗库存
    dew = data.get('deworming', {})
    if items.get('deworming') and dew:
        treatment.deworming_type = dew.get('type', '')
        treatment.deworming_batch_no = dew.get('batch_no', '')
        dew_date = dew.get('date')
        if dew_date:
            treatment.deworming_date = _parse_date(dew_date)
        treatment.deworming_quantity = int(dew.get('quantity', 1))

        material_id = dew.get('material_id')
        if material_id:
            try:
                material = Material.objects.get(id=material_id, category='dewormer')
                if get_hospital_stock(material, hospital) < treatment.deworming_quantity:
                    return json_fail(f'驱虫药库存不足（{material.name}），请先补充库存')
                adjust_stock(
                    material=material,
                    hospital=hospital,
                    quantity=treatment.deworming_quantity,
                    txn_type='consume',
                    operator=user,
                    operator_name=user.get_full_name() or user.username,
                    from_to='诊疗消耗',
                    note=f'{pet.code} 驱虫',
                )
            except Material.DoesNotExist:
                pass

    # 芯片 - 使用芯片并消耗库存
    chip_data = data.get('chip', {})
    if items.get('chip') and chip_data:
        chip_no = chip_data.get('chip_no', '')
        chip_date = chip_data.get('date')
        if chip_date:
            treatment.chip_date = _parse_date(chip_date)

        if chip_no:
            try:
                use_chip(chip_no, pet)
                treatment.chip_no = chip_no

                # 消耗芯片物料库存
                chip_material = Material.objects.filter(
                    category='chip', district_id=district_id
                ).first()
                if chip_material:
                    adjust_stock(
                        material=chip_material,
                        hospital=hospital,
                        quantity=1,
                        txn_type='consume',
                        operator=user,
                        operator_name=user.get_full_name() or user.username,
                        from_to='诊疗消耗',
                        note=f'{pet.code} 芯片植入 {chip_no}',
                    )
            except ValueError as e:
                treatment.save()
                return json_fail(str(e), data=serialize_instance(treatment))

    treatment.save()

    # 更新宠物状态
    if pet.status != 'in_treatment':
        pet.status = 'in_treatment'
        pet.save(update_fields=['status'])

    # 诊疗完成时调度5天自动转待领养
    if status == 'completed':
        _schedule_auto_promote()

    return json_ok(serialize_instance(treatment), message='诊疗记录创建成功')


@csrf_exempt
@role_required('hospital', 'gov_city', 'gov_district')
@login_required
def treatment_detail(request, pk):
    """诊疗详情"""
    try:
        treatment = Treatment.objects.get(id=pk)
    except Treatment.DoesNotExist:
        return json_fail('诊疗记录不存在', status=404)

    data = serialize_instance(treatment)
    data['pet'] = serialize_instance(treatment.pet)
    return json_ok(data)


# ============================================
# 辅助函数
# ============================================
def _parse_date(date_str):
    """解析日期字符串，支持 YYYY-MM-DD 格式"""
    if not date_str:
        return None
    if isinstance(date_str, date_type):
        return date_str
    try:
        return datetime.strptime(str(date_str)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _schedule_auto_promote():
    """通过 django-q2 调度自动转待领养任务"""
    try:
        from django_q.tasks import async_task
        async_task('business.tasks.auto_promote_to_adoptable')
    except Exception:
        # django-q2 未运行或未配置，不影响主流程
        # 部署时配置定时任务即可
        pass
