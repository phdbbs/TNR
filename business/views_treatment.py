"""
诊疗与物料库存联动（Task 6 + 第四十六轮 v5.1 三步流程）

**两代接口并存，各有各的用途：**

- **旧口径**（Task 6）：`treatment_create` —— 一次请求提交整张单
  （绝育 / 疫苗 / 驱虫 / 芯片 + 各自明细），**当场扣库存**。
  保留原因：政府端与监管统计在用它，历史数据也是按它建的。
- **新口径**（v5.1 原型三步流程）：`open` → `check` → `plan` → `diagnose`
  → `items/<id>/execute` → `done` → `dispose`，**库存在「执行」那一刻才扣**。

两者写的是同一批 `items_*` 布尔列（新流程由
`services_treatment.sync_legacy_item_flags()` 回写），语义一致 ——
所以打印页 / 监管统计 / 政府端列表对两种单子都能读，不需要分叉。

⚠ 写接口一律 `@role_required('hospital')` + 按**本机构**取单
（`_own_treatment()`）。政府端 / 捕捉点只读 —— 第二十九轮起政府端
写接口整体收口为只读（全局设置搬平台端），诊疗写接口同样不能例外。
"""
import json
from datetime import datetime, date as date_type

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Treatment, TreatmentItem, Pet, Material, Chip, Transfer
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_int, body_dict, body_str,
    generate_doc_no, get_district_filtered_queryset,
    adjust_stock, use_chip, get_hospital_stock,
    get_active_pet, get_scoped_object, expired_material_error,
    find_hospital_chip_material, validate_uploaded_images,
)
from business import services_treatment as st


# ============================================
# 取单辅助
# ============================================
def _own_treatment(pk, user):
    """按**本机构**取诊疗单；不存在或不属于本院一律返回 None。

    医院端写接口必须走它，不要裸写 `Treatment.objects.get(id=pk)` ——
    裸取会让「存在但不属于本院」与「不存在」返回**不同**结果，
    形成存在性预言机（本项目已在转运 / 机构 / 芯片三处踩过同一个坑）。
    """
    if not getattr(user, 'institution_id', None):
        return None
    return Treatment.objects.filter(pk=pk, hospital_id=user.institution_id).first()


def _code_in_csv(code):
    """`Transfer.pet_codes` 是逗号分隔的字符串，精确匹配其中一个编号。

    ⚠ 不能写 `pet_codes__contains=code`：`TNR2510001` 会命中 `TNR25100010`，
    于是看板上会出现**别的动物**的入院信息，而且看起来完全正常。
    """
    return (Q(pet_codes=code) | Q(pet_codes__startswith=code + ',')
            | Q(pet_codes__endswith=',' + code) | Q(pet_codes__contains=',' + code + ','))


def _intake_info(pet):
    """该动物的入院信息（最近一张**已签收**的转运单）。"""
    transfer = (Transfer.objects.filter(_code_in_csv(pet.code), status='received')
                .order_by('-id').first())
    if transfer is None:
        return {'transfer_no': '', 'receive_date': '', 'shelter': ''}
    return {
        'transfer_no': transfer.ledger_no or '',
        'receive_date': transfer.received_at.isoformat() if transfer.received_at else '',
        'shelter': transfer.from_shelter_name or '',
    }


# ============================================
# 序列化
# ============================================
def _item_data(item):
    """一个处置项目的出参（含处方明细）。"""
    return {
        'id': item.id,
        'item_key': item.item_key,
        'item_name': item.item_name or item.get_item_key_display(),
        'planned_date': item.planned_date.isoformat() if item.planned_date else '',
        'chip_no': item.chip_no,
        'notes': item.notes,
        'rx_other': item.rx_other,
        'vet_name': item.vet_name,
        'vet_license': item.vet_license,
        'is_done': item.is_done,
        'done_at': item.done_at.isoformat() if item.done_at else '',
        'is_must': st.item_is_must(item),
        'rx': [{
            'id': rx.id,
            'material_id': rx.material_id,
            'material_name': rx.material_name,
            'quantity': rx.quantity,
            'unit': rx.unit,
        } for rx in item.rx_rows.all()],
    }


def _treatment_detail_data(treatment):
    """诊疗单完整出参：三步流程需要的一切（前端一次拿全，不再逐项请求）。"""
    pet = treatment.pet
    data = serialize_instance(treatment)
    data['stage'] = treatment.stage
    data['is_disposed'] = treatment.is_disposed
    # 顿号分隔的文本快照 → 数组，供前端 chips 回显
    data['common_findings_list'] = [v for v in (treatment.common_findings or '').split('、') if v]
    data['infectious'] = _load_infectious(treatment)
    data['items'] = [_item_data(i) for i in treatment.items.all()]
    data['must_not_done'] = [
        {'id': i.id, 'item_name': i.item_name or i.get_item_key_display()}
        for i in st.must_items_not_done(treatment)]
    if pet is not None:
        data['pet'] = serialize_instance(pet)
        data['options'] = st.check_options(pet.species)
        data['intake'] = _intake_info(pet)
    else:
        data['options'] = st.check_options('')
        data['intake'] = {}
    return data


def _load_infectious(treatment):
    """把 `infectious_tests` 的 JSON 文本解析成 dict（坏数据返回空 dict）。"""
    raw = (treatment.infectious_tests or '').strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        # 坏 JSON 不该让整个详情接口 500 —— 检查单其余部分仍然可读可改。
        return {}
    return value if isinstance(value, dict) else {}


# ============================================
# 看板（v5.1 原型的三标签列表）
# ============================================
@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def treatment_board(request):
    """诊疗看板：待接诊 / 诊疗中 / 诊疗完毕。

    「待接诊」= 本院待诊疗的动物（**不管有没有开单**）—— 原型里点动物
    才进流程，所以「还没开单」是常态，不能只列已有诊疗单的动物，
    否则新收治的动物在看板上根本不出现（看不到 = 接不了诊）。
    """
    user = request.user
    if user.role == 'hospital':
        if not user.institution_id:
            return json_ok({'pending': [], 'treating': [], 'done': [], 'counts': {}})
        treatments = Treatment.objects.filter(hospital_id=user.institution_id)
        pets = Pet.objects.filter(hospital_id=user.institution_id, is_deleted=False)
    else:
        # 政府端 / 捕捉点只读：按区县范围看全部
        treatments = get_district_filtered_queryset(Treatment, user)
        pets = get_district_filtered_queryset(Pet, user).filter(is_deleted=False)

    treatments = treatments.select_related('pet').order_by('-id')

    pending, treating, done = [], [], []
    seen_pet_ids = set()

    for t in treatments:
        row = _board_row(t)
        seen_pet_ids.add(t.pet_id)
        if t.done_at is not None:
            done.append(row)
        elif t.checked_at is not None:
            treating.append(row)
        else:
            pending.append(row)

    # 还没开单、但已在院待诊疗的动物 —— 它们是「待接诊」的主体。
    # ⚠ `status='in_treatment'` 是唯一口径：`pending_adopt` 的动物虽然也允许
    #   开单（复查），但它们已经出院了，不该出现在待接诊列表里。
    orphan_pets = pets.filter(status='in_treatment').exclude(id__in=seen_pet_ids)
    for pet in orphan_pets.order_by('-id'):
        pending.append(_pet_board_row(pet))

    counts = {'pending': len(pending), 'treating': len(treating), 'done': len(done)}
    return json_ok({'pending': pending, 'treating': treating, 'done': done,
                    'counts': counts})


def _board_row(treatment):
    pet = treatment.pet
    info = _intake_info(pet) if pet is not None else {}
    return {
        'treatment_id': treatment.id,
        'pet_id': treatment.pet_id,
        'pet_code': treatment.pet_code,
        'pet_name': pet.name if pet else '',
        'species': pet.species if pet else '',
        'gender': pet.gender if pet else '',
        'breed': pet.breed if pet else '',
        'color': pet.color if pet else '',
        'hospital_name': treatment.hospital_name,
        'stage': treatment.stage,
        'is_disposed': treatment.is_disposed,
        'disposal': treatment.disposal,
        'checked_at': treatment.checked_at.isoformat() if treatment.checked_at else '',
        'done_at': treatment.done_at.isoformat() if treatment.done_at else '',
        'transfer_no': info.get('transfer_no', ''),
        'receive_date': info.get('receive_date', ''),
        'shelter': info.get('shelter', ''),
    }


def _pet_board_row(pet):
    info = _intake_info(pet)
    return {
        'treatment_id': None,
        'pet_id': pet.id,
        'pet_code': pet.code,
        'pet_name': pet.name,
        'species': pet.species,
        'gender': pet.gender,
        'breed': pet.breed,
        'color': pet.color,
        'hospital_name': getattr(pet.hospital, 'name', ''),
        'stage': 'checking',
        'is_disposed': False,
        'disposal': '',
        'checked_at': '',
        'done_at': '',
        'transfer_no': info.get('transfer_no', ''),
        'receive_date': info.get('receive_date', ''),
        'shelter': info.get('shelter', ''),
    }


# ============================================
# 开单 / 取单
# ============================================
@csrf_exempt
@role_required('hospital')
@login_required
def treatment_open(request):
    """进入诊疗流程：取该动物在本院进行中的诊疗单，没有就开一张。"""
    data = parse_json_body(request)
    user = request.user

    pet_id = body_int(data, 'pet_id')
    if not pet_id:
        return json_fail('缺少宠物ID')

    pet = get_active_pet(pet_id, user)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

    hospital = getattr(user, 'institution', None)
    if hospital is None:
        return json_fail('当前账号未绑定医院')

    district_id = pet.district_id or getattr(user, 'district_id', None)
    if not district_id:
        return json_fail('缺少区县信息')

    try:
        with transaction.atomic():
            treatment = st.open_treatment(pet, hospital, user, district_id)
    except ValueError as e:
        return json_fail(str(e))

    if treatment is None:
        # 与上面「宠物不存在或无权访问」**同一个状态码 + 同一句文案**：
        # `open_treatment()` 对「挂在别院名下」也返回 None，两者必须不可区分。
        return json_fail('宠物不存在或无权访问', status=404)
    return json_ok(_treatment_detail_data(treatment), message='已进入诊疗流程')


# ============================================
# 第一步：诊疗前检查
# ============================================
@csrf_exempt
@role_required('hospital')
@login_required
def treatment_check(request, pk):
    """保存 / 提交检查单（`draft=true` 为暂存）。"""
    data = parse_json_body(request)
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    submit = not _truthy(data.get('draft'))
    try:
        with transaction.atomic():
            st.apply_check(treatment, data, submit=submit)
    except ValueError as e:
        return json_fail(str(e))
    return json_ok(_treatment_detail_data(treatment),
                   message='检查单已提交' if submit else '检查单已暂存')


# ============================================
# 第二步：医嘱与处方
# ============================================
@csrf_exempt
@role_required('hospital')
@login_required
def treatment_plan(request, pk):
    """保存医嘱与处置项目。"""
    data = parse_json_body(request)
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    try:
        with transaction.atomic():
            st.save_plan(treatment, data)
    except ValueError as e:
        return json_fail(str(e))
    return json_ok(_treatment_detail_data(treatment), message='医嘱与处方已保存')


@csrf_exempt
@role_required('hospital')
@login_required
def treatment_diagnose(request, pk):
    """诊断完成 —— 进入执行阶段。"""
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    try:
        with transaction.atomic():
            st.mark_diagnosed(treatment)
    except ValueError as e:
        return json_fail(str(e))
    return json_ok(_treatment_detail_data(treatment), message='诊断完成，进入执行阶段')


@csrf_exempt
@role_required('hospital')
@login_required
def treatment_undiagnose(request, pk):
    """撤回诊断 —— 回到可编辑状态（已执行的项目会拦住它）。"""
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    try:
        with transaction.atomic():
            st.undiagnose(treatment)
    except ValueError as e:
        return json_fail(str(e))
    return json_ok(_treatment_detail_data(treatment), message='已撤回诊断，可继续修改医嘱与处方')


# ============================================
# 第三步：执行与办结
# ============================================
@csrf_exempt
@role_required('hospital')
@login_required
def treatment_item_execute(request, pk, item_id):
    """执行一个处置项目 —— **库存在这里扣**。"""
    user = request.user
    treatment = _own_treatment(pk, user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    item = TreatmentItem.objects.filter(pk=item_id, treatment=treatment).first()
    if item is None:
        return json_fail('处置项目不存在或无权访问', status=404)

    try:
        with transaction.atomic():
            st.execute_item(treatment, item, user)
    except ValueError as e:
        return json_fail(str(e))
    treatment.refresh_from_db()
    return json_ok(_treatment_detail_data(treatment),
                   message=f'{item.item_name or item.get_item_key_display()}：治疗完成，已扣减库存')


@csrf_exempt
@role_required('hospital')
@login_required
def treatment_item_undo(request, pk, item_id):
    """撤销一个已执行的项目 —— 库存退回、芯片退回「可用」。"""
    user = request.user
    treatment = _own_treatment(pk, user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    item = TreatmentItem.objects.filter(pk=item_id, treatment=treatment).first()
    if item is None:
        return json_fail('处置项目不存在或无权访问', status=404)

    try:
        with transaction.atomic():
            st.undo_item(treatment, item, user)
    except ValueError as e:
        return json_fail(str(e))
    treatment.refresh_from_db()
    return json_ok(_treatment_detail_data(treatment),
                   message=f'{item.item_name or item.get_item_key_display()}：已撤销执行')


@csrf_exempt
@role_required('hospital')
@login_required
def treatment_done(request, pk):
    """诊疗完成 —— 全部必执行项目都做完才能点。"""
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    try:
        with transaction.atomic():
            st.mark_done(treatment)
    except ValueError as e:
        return json_fail(str(e))
    return json_ok(_treatment_detail_data(treatment), message='诊疗完成，请选择办结去向')


@csrf_exempt
@role_required('hospital')
@login_required
def treatment_dispose(request, pk):
    """办结 —— 记录去向并变更动物状态。"""
    data = parse_json_body(request)
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    disposal = str(body_str(data, 'disposal') or '').strip()
    if not disposal:
        return json_fail('请选择办结去向')

    try:
        with transaction.atomic():
            st.dispose(treatment, disposal, request.user)
    except ValueError as e:
        return json_fail(str(e))
    treatment.refresh_from_db()
    return json_ok(_treatment_detail_data(treatment), message='已办结')


@csrf_exempt
@role_required('hospital')
@login_required
def treatment_photo(request, pk):
    """上传 / 更换「诊疗后照片」（B2：第三步办结环节的当场留证）。

    落 ``Pet.photo_treatment`` —— 领养大厅「诊疗后」与领养资料页读的就是
    这一个字段，一张照片两处展示，不需要再传第二遍。

    ⚠ **必须走本独立接口，不能复用领养资料的 ``adoption_edit_info``**：
    那个接口传照片会 ``get_or_create`` 领养展示位并把 ``published_at``
    置为今天 —— 办结去向是「放归 / 无害化」的动物若借它传照片，
    会被**意外挂进领养大厅**，而且不报错（领养人能看到一只已经
    安乐死的猫）。本接口只写宠物档案的照片字段，不碰领养业务。
    """
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    pet = treatment.pet
    if pet is None:
        return json_fail('该诊疗单没有关联的动物档案')

    if treatment.is_disposed:
        return json_fail('本单已办结，诊疗后照片不能再更换')

    if request.FILES.get('photo') is None:
        return json_fail('请选择要上传的照片')

    # 先校验后落库（两段式）：图片非法时档案字段一个字节都不动。
    err = validate_uploaded_images(request.FILES, {'photo': '诊疗后照片'})
    if err:
        return json_fail(err)

    pet.photo_treatment = request.FILES['photo']
    pet.save(update_fields=['photo_treatment'])

    # 审计用上下文（与 adoption_edit_info 同口径：照片落在宠物档案上）
    request.audit_district = pet.district
    return json_ok(_treatment_detail_data(treatment), message='诊疗后照片已保存')


# ============================================
# 列表 / 详情（Task 6 起，第四十六轮扩展）
# ============================================
@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def treatment_list(request):
    """诊疗列表。

    支持 `stage` 过滤（v5.1 三步流程）：`checking` / `treating` / `done`。
    阶段是**派生值**（由 `checked_at` / `done_at` 推导），所以这里翻译成
    查询条件，而不是去 filter 一个不存在的列。
    """
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

    stage = request.GET.get('stage')
    if stage:
        if stage == 'checking':
            qs = qs.filter(checked_at__isnull=True)
        elif stage == 'treating':
            qs = qs.filter(checked_at__isnull=False, done_at__isnull=True)
        elif stage == 'done':
            qs = qs.filter(done_at__isnull=False)
        else:
            return json_fail(f'未知的诊疗阶段：{stage}')

    # 列表**不**返回完整详情：`_treatment_detail_data()` 会为每行带上
    # 处置项目、处方、选项表、入院信息 —— 在列表上就是 N+1 查询
    # （而且选项表每行都一样）。列表只要阶段与去向，详情由 detail 接口给。
    data = []
    for t in qs.select_related('pet'):
        row = serialize_instance(t)
        row['stage'] = t.stage
        row['is_disposed'] = t.is_disposed
        row['pet_name'] = t.pet.name if t.pet else ''
        row['species'] = t.pet.species if t.pet else ''
        data.append(row)
    return json_ok(data)


@csrf_exempt
@role_required('hospital', 'gov_city', 'gov_district')
@login_required
def treatment_create(request):
    """创建诊疗记录（**Task 6 旧口径**，一次请求提交整张单并当场扣库存）。

    v5.1 三步流程请走 `treatment_open` + `check` + `plan` + `diagnose` +
    `items/<id>/execute` + `done` + `dispose`。

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
        # B5 起传 institution=hospital 走批次级判据：老批次过期、新批次有效
        # 时放行（FEFO 会扣有效批次），而不是按主表快照一刀切。
        err = expired_material_error(vaccine_material, '用于诊疗', institution=hospital)
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
        err = expired_material_error(deworming_material, '用于诊疗', institution=hospital)
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
            # 在途芯片不能植（第四十六轮）：库存已从发起方扣掉、对方还没签收，
            # 此时它不属于任何一方 —— 谁都不该能用。重构前只有两态，漏了这一态。
            if chip.status == 'in_transit':
                return json_fail(f'芯片 {chip_no} 在途（尚未签收），不能使用')
            # 归属校验（第四十六轮）：**只拦新引用、不拦存量** ——
            # 重构前的 `Chip` 行没有机构归属（`institution` 为 NULL），
            # 归属信息在数据里已经丢了，一律放行，否则历史芯片一颗都植不出去。
            # 新入库的芯片一定带机构，所以「甲院的芯片被乙院植掉」会被拦住。
            if (chip.institution_id is not None
                    and hospital is not None
                    and chip.institution_id != hospital.id):
                return json_fail(f'芯片 {chip_no} 不属于 {hospital.name}，不能使用')
            # 芯片物料在这里**一并取好并校验**，保持「先全校验 → 再落库」的两段式。
            # 原先它在下面的事务块内才查询 —— 一次可能失败的 DB 查询落在落库之后，
            # 且过期判据会漏掉这条路径。
            #
            # ⚠ 取法必须是「**该医院实际有库存**的芯片物料」（第四十五轮 C7）。
            #   原先按 `district_id`（宠物区县）取，跨区县送医时会取到**别的
            #   区县**的芯片物料，本院那条一颗都不减、外区那条被凭空扣掉，
            #   而且不报错。
            chip_material = find_hospital_chip_material(hospital, district_id)
            err = expired_material_error(chip_material, '用于诊疗', institution=hospital)
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
            ledger_no=generate_doc_no('TRE'),
            district_id=district_id,
            # 签名与电子章快照（R46 口径 2），与 `open_treatment()` 同一口径
            vet_name=user.get_full_name() or user.username,
            vet_license=getattr(user, 'vet_license', '') or '',
            hospital_seal=(getattr(hospital, 'seal_text', '') or hospital.name),
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
                    institution=hospital,
                    operator=user,
                    operator_name=user.get_full_name() or user.username,
                    from_to='诊疗消耗',
                    note=f'{pet.code} 疫苗接种',
                    ledger_no=generate_doc_no('CON'),
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
                    institution=hospital,
                    operator=user,
                    operator_name=user.get_full_name() or user.username,
                    from_to='诊疗消耗',
                    note=f'{pet.code} 驱虫',
                    ledger_no=generate_doc_no('CON'),
                )

        # 芯片 - 绑定芯片并消耗芯片物料库存
        if items.get('chip') and chip_data:
            if chip_data.get('date'):
                treatment.chip_date = _parse_date(chip_data['date'])
            if chip_no:
                # 归属校验（第四十六轮）：传 `institution=hospital` 让
                # `use_chip()` 复核一次「这颗芯片是不是本院的」。
                # 上面第一步已做过同一判据，这里是**落库前的最后一道闸** ——
                # 万一中间有人改了芯片归属，也不会把别人的芯片植掉。
                # ⚠ 故意**不包 try**：异常传播出 `atomic()` 会整块回滚，
                #   不会留下「Treatment 建了、芯片没植」的半截行；
                #   在事务内 `return json_fail()` 反而会**提交**半截数据。
                use_chip(chip_no, pet, institution=hospital)
                treatment.chip_no = chip_no
                # chip_material 已在第一步取好并校验过（见上），这里不再查询
                if chip_material:
                    adjust_stock(
                        material=chip_material,
                        hospital=hospital,
                        quantity=1,
                        txn_type='consume',
                        institution=hospital,
                        operator=user,
                        operator_name=user.get_full_name() or user.username,
                        from_to='诊疗消耗',
                        note=f'{pet.code} 芯片植入 {chip_no}',
                        ledger_no=generate_doc_no('CON'),
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
        treatment = _own_treatment(pk, user)
    else:
        treatment = get_scoped_object(Treatment, pk, user)

    if treatment is None:
        return json_fail('诊疗记录不存在或无权访问', status=404)

    return json_ok(_treatment_detail_data(treatment))


# ============================================
# 辅助函数
# ============================================
def _truthy(value):
    """宽松判真：`true` / `1` / `'true'` / `'1'` 都算真。

    前端可能用 JSON 布尔，也可能用表单字符串 —— 只认 `is True` 的话
    `draft='true'` 会被当成「不是草稿」，于是「暂存」按钮悄悄变成了「提交」，
    而界面上的提示还是「已暂存」。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


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
