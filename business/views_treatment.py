"""
Task 6: 诊疗与物料库存联动
- 诊疗列表/创建/详情
- 疫苗/驱虫/芯片消耗自动扣减医院库存
- 诊疗完成时通过 django-q2 调度5天自动转待领养
"""
from datetime import datetime, date as date_type

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Treatment, Pet, Material, Chip
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_int, body_dict,
    generate_ledger_no, get_district_filtered_queryset,
    adjust_stock, use_chip, get_hospital_stock,
    get_active_pet, get_scoped_object, expired_material_error,
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

    pet_id = body_int(data, 'pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    # 按区县范围取宠物，并排除已逻辑删除的档案
    pet = get_active_pet(pet_id, user)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

    if pet.status not in ('in_treatment', 'pending_adopt'):
        return json_fail(f'宠物当前状态({pet.get_status_display()})不可诊疗')

    # ⚠ 这几个都是**嵌套 dict** 参数，必须走 `body_dict`（第三十五轮）：
    # `data.get('sterilization', {}) or {}` 挡不住 `{"sterilization": "abc"}`
    # ——`"abc"` 是 truthy，`or {}` 不生效 → 下游 `ster.get(...)` 炸 500。
    items = body_dict(data, 'items')
    hospital = pet.hospital or getattr(user, 'institution', None)
    if not hospital:
        return json_fail('缺少医院信息')

    district_id = pet.district_id or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    status = data.get('status', 'in_progress')

    ster = body_dict(data, 'sterilization')
    vac = body_dict(data, 'vaccine')
    dew = body_dict(data, 'deworming')
    chip_data = body_dict(data, 'chip')

    try:
        # 注意不能写 `vac.get('quantity', 1) or 1`——那会把显式传入的 0 变成 1，
        # 使「数量必须大于 0」的校验形同虚设。
        vaccine_qty = int(vac.get('quantity', 1))
        deworming_qty = int(dew.get('quantity', 1))
    except (TypeError, ValueError):
        return json_fail('疫苗/驱虫数量必须为整数')
    if vaccine_qty <= 0 or deworming_qty <= 0:
        return json_fail('疫苗/驱虫数量必须大于 0')

    # ---- 第一步：把所有前置校验做完，任一不通过就整体拒绝，不产生任何副作用 ----
    vaccine_material = None
    if items.get('vaccine') and vac and vac.get('material_id'):
        # ⚠ `vac['material_id']` 是**嵌套 dict 里的值**，同样必须走解析器：
        # 传 `{"vaccine": {"material_id": [1,2,3]}}` 会抛
        # `ValueError: Field 'id' expected a number but got [1, 2, 3]` → 500。
        # 解析失败返回 None → `filter(id=None)` 不匹配 → 走下面的「物料不存在」400。
        vaccine_material = Material.objects.filter(
            id=body_int(vac, 'material_id'), category='vaccine').first()
        if vaccine_material is None:
            return json_fail('疫苗物料不存在')
        # 过期疫苗不能用于诊疗。判据在 services 里只有一份，与其它接口共用。
        err = expired_material_error(vaccine_material, '用于诊疗')
        if err:
            return json_fail(err)
        if get_hospital_stock(vaccine_material, hospital) < vaccine_qty:
            return json_fail(f'疫苗库存不足（{vaccine_material.name}），请先补充库存')

    deworming_material = None
    if items.get('deworming') and dew and dew.get('material_id'):
        deworming_material = Material.objects.filter(
            id=body_int(dew, 'material_id'), category='dewormer').first()
        if deworming_material is None:
            return json_fail('驱虫药物料不存在')
        err = expired_material_error(deworming_material, '用于诊疗')
        if err:
            return json_fail(err)
        if get_hospital_stock(deworming_material, hospital) < deworming_qty:
            return json_fail(f'驱虫药库存不足（{deworming_material.name}），请先补充库存')

    chip_no = ''
    chip_material = None
    if items.get('chip') and chip_data:
        chip_no = (chip_data.get('chip_no') or '').strip()
        if chip_no:
            chip = Chip.objects.filter(number=chip_no).first()
            if chip is None:
                return json_fail(f'芯片 {chip_no} 不存在')
            if chip.status == 'used':
                return json_fail(f'芯片 {chip_no} 已被使用')
            # 芯片物料在这里**一并取好并校验**，保持「先全校验 → 再落库」的两段式。
            # 原先它在下面的事务块内才查询 —— 一次可能失败的 DB 查询落在落库之后，
            # 且过期判据会漏掉这条路径。
            chip_material = Material.objects.filter(
                category='chip', district_id=district_id).first()
            err = expired_material_error(chip_material, '用于诊疗')
            if err:
                return json_fail(err)

    # ---- 第二步：校验全部通过后才落库；整体放进事务，避免「库存已扣但记录没存」 ----
    with transaction.atomic():
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
        if ster:
            treatment.sterilization_surgeon = ster.get('surgeon', '')
            treatment.sterilization_diagnosis = ster.get('diagnosis', '')
            treatment.sterilization_anesthesia = ster.get('anesthesia', '')
            treatment.sterilization_procedure = ster.get('procedure', '')
            treatment.sterilization_recovery = ster.get('recovery', '')
            if ster.get('surgery_date'):
                treatment.sterilization_surgery_date = _parse_date(ster['surgery_date'])

        # 疫苗 - 消耗库存
        if items.get('vaccine') and vac:
            treatment.vaccine_type = vac.get('type', '')
            treatment.vaccine_batch_no = vac.get('batch_no', '')
            if vac.get('date'):
                treatment.vaccine_date = _parse_date(vac['date'])
            treatment.vaccine_quantity = vaccine_qty
            if vaccine_material is not None:
                adjust_stock(
                    material=vaccine_material,
                    hospital=hospital,
                    quantity=vaccine_qty,
                    txn_type='consume',
                    operator=user,
                    operator_name=user.get_full_name() or user.username,
                    from_to='诊疗消耗',
                    note=f'{pet.code} 疫苗接种',
                )

        # 驱虫 - 消耗库存
        if items.get('deworming') and dew:
            treatment.deworming_type = dew.get('type', '')
            treatment.deworming_batch_no = dew.get('batch_no', '')
            if dew.get('date'):
                treatment.deworming_date = _parse_date(dew['date'])
            treatment.deworming_quantity = deworming_qty
            if deworming_material is not None:
                adjust_stock(
                    material=deworming_material,
                    hospital=hospital,
                    quantity=deworming_qty,
                    txn_type='consume',
                    operator=user,
                    operator_name=user.get_full_name() or user.username,
                    from_to='诊疗消耗',
                    note=f'{pet.code} 驱虫',
                )

        # 芯片 - 绑定芯片并消耗芯片物料库存
        if items.get('chip') and chip_data:
            if chip_data.get('date'):
                treatment.chip_date = _parse_date(chip_data['date'])
            if chip_no:
                use_chip(chip_no, pet)
                treatment.chip_no = chip_no
                # chip_material 已在第一步取好并校验过（见上），这里不再查询
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
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def treatment_detail(request, pk):
    """诊疗详情

    按区县范围取数：医院仅能查看本院记录，其余角色按所属区县过滤，
    避免只凭主键即可跨区县读取他人诊疗数据。
    """
    user = request.user

    if user.role == 'hospital':
        if user.institution_id:
            treatment = Treatment.objects.filter(id=pk, hospital_id=user.institution_id).first()
        else:
            treatment = None
    else:
        treatment = get_scoped_object(Treatment, pk, user)

    if treatment is None:
        return json_fail('诊疗记录不存在或无权访问', status=404)

    data = serialize_instance(treatment)
    if treatment.pet_id:
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
    """诊疗完成时**立即**投递一次「自动转待领养」，让结果尽快生效。

    ⚠ 这只是**快路径**，不是周期调度。真正的定时执行由
    `business/migrations/0016_register_auto_promote_schedule.py` 注册的
    每天 03:00 那条 `Schedule` 负责。

    这里原先的注释写的是「部署时配置定时任务即可」—— 把周期注册当成运维的
    手工动作，结果**从未被做过**，该任务长期只靠启动补偿和这条快路径触发
    （详见 `DEPLOY.md` §5.2）。现已固化进数据迁移，注释同步更正。
    """
    try:
        from django_q.tasks import async_task
        async_task('business.tasks.auto_promote_to_adoptable')
    except Exception:
        # 有意静默降级：投递失败不影响「诊疗记录创建成功」这个主流程，
        # 且周期调度（0016）会在当天凌晨兜底。这里不向用户呈现错误是刻意的，
        # 不是漏了错误处理 —— 别按「死 catch」当缺陷修。
        pass
