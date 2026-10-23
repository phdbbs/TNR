"""
诊疗与物料库存联动（Task 6 起，第四十六轮 v5.1 三步流程）

**写接口只剩三步流程一条路径**（第四十七轮收口）：

`open` → `check` → `plan` → `diagnose` → `items/<id>/execute` → `done` →
`dispose`，**库存在「执行」那一刻才扣**。

第四十七轮删掉了 Task 6 旧口径的 `treatment_create`（一次请求提交整张单、
当场扣库存）。它唯一的界面入口是医院端「快速登记」标签，删除前已把唯一
独有价值（绝育手术病历）并入三步流程的「绝育」项目卡片
（`services_treatment.save_sterilization()`）。旧口径还有两处不适合留下：
①「当场扣库存」与三步流程「执行时才扣」是两套会漂移的口径，读写同一批
`items_*` 列；
②「诊疗完成即时投递自动转待领养」的快路径只挂在它身上
（`_schedule_auto_promote()` 随之下线）—— 新流程 `dispose()` 同步落
`Treatment.status='completed'` 与 `Pet.status=去向`，正常办结不再出现
「诊疗已完成、动物还在院」的中间态；**但放归两段用
`defer_pet_status=True` 会刻意造出这个中间态**，所以周期调度
（`0016` 迁移，每天 03:00）的判据必须按 `disposal` 排除已定非领养去向的动物，
详见 `business/tasks.py::auto_promote_to_adoptable`。

三步流程由 `services_treatment.sync_legacy_item_flags()` 回写
`items_*` 布尔列，语义与旧口径一致 —— 所以打印页 / 监管统计 /
政府端列表对两种单子都能读，不需要分叉。

⚠ 写接口一律 `@role_required('hospital')` + 按**本机构**取单
（`_own_treatment()`）。政府端 / 捕捉点只读 —— 第二十九轮起政府端
写接口整体收口为只读（全局设置搬平台端），诊疗写接口同样不能例外。
"""
import json
from datetime import datetime, date as date_type

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Treatment, TreatmentItem, Pet, Transfer
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    body_int, body_str,
    get_district_filtered_queryset,
    get_scoped_object,
    get_hospital_scoped_pet,
    validate_uploaded_images,
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
    """该动物的入院信息（最近一张**已签收**的转运单）。

    ⚠ 第四十八轮起转运单可以「部分签收」（`status='partial'`）——
    这类单里已经签收的动物同样要带出转运单号与来源捕捉点，否则医院
    开了诊疗单、详情页的「转运单号 / 来源捕捉点」全是 `—`，看起来像
    这只动物是凭空出现的。所以状态要认 `received` **和** `partial`，
    并确认该动物确实在已签收集合里。
    """
    for t in (Transfer.objects
              .filter(_code_in_csv(pet.code), status__in=('received', 'partial'))
              .order_by('-id')):
        if t.status == 'received' or pet.code in [
                c.strip() for c in (t.received_codes or '').split(',') if c.strip()]:
            return {
                'transfer_no': t.ledger_no or '',
                'receive_date': t.received_at.isoformat() if t.received_at else '',
                'shelter': t.from_shelter_name or '',
            }
    return {'transfer_no': '', 'receive_date': '', 'shelter': ''}


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


def _disposal_label(key):
    """去向 key → 中文。空 key 返回空串（前端据此判断「有没有建议」）。"""
    return dict(Treatment.DISPOSAL_CHOICES).get(key, '') if key else ''


def _treatment_detail_data(treatment):
    """诊疗单完整出参：三步流程需要的一切（前端一次拿全，不再逐项请求）。"""
    pet = treatment.pet
    data = serialize_instance(treatment)
    data['stage'] = treatment.stage
    data['is_disposed'] = treatment.is_disposed
    data['disposal_label'] = _disposal_label(treatment.disposal)
    data['disposal_advised_label'] = _disposal_label(treatment.disposal_advised)
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
    """诊疗看板：待接诊 / 待诊断 / 诊疗中 / 待出院 / 已出院（第四十八轮五态）。

    五态**全部由时间戳派生**，不存列（存列必然有一处写入路径忘了同步）：

    | 状态 | 判据 |
    |---|---|
    | 待接诊 | `checked_at` 为空（还没提交检查单） |
    | 待诊断 | `checked_at` 有、`diagnosed_at` 为空 |
    | 诊疗中 | `diagnosed_at` 有、`done_at` 为空 |
    | 待出院 | `done_at` 有、`disposed_at` 为空 |
    | 已出院 | `disposed_at` 有 |

    原先只有三态（pending / treating / done），把「待诊断」和「诊疗中」合并
    成 treating、把「待出院」和「已出院」合并成 done —— 界面上看不出
    「下一步该点哪个按钮」，也分不清「还在治」和「治完了等人接走」。

    「待接诊」的主体是**本院待诊疗的动物**（不管有没有开单）—— 原型里点动物
    才进流程，所以「还没开单」是常态，不能只列已有诊疗单的动物，
    否则新收治的动物在看板上根本不出现（看不到 = 接不了诊）。

    取值顺序见 `Treatment.BOARD_STAGES`，界面文案见 `TNR_UI.BOARD_STAGE`。
    """
    user = request.user
    if user.role == 'hospital':
        if not user.institution_id:
            return json_ok({**_board_buckets(), 'counts': {}})
        treatments = Treatment.objects.filter(hospital_id=user.institution_id)
        pets = Pet.objects.filter(hospital_id=user.institution_id, is_deleted=False)
    else:
        # 政府端 / 捕捉点只读：按区县范围看全部
        treatments = get_district_filtered_queryset(Treatment, user)
        pets = get_district_filtered_queryset(Pet, user).filter(is_deleted=False)

    treatments = treatments.select_related('pet').order_by('-id')

    buckets = _board_buckets()
    seen_pet_ids = set()

    for t in treatments:
        row = _board_row(t)
        seen_pet_ids.add(t.pet_id)
        buckets[_board_stage(t)].append(row)

    # 还没开单、但已在院待诊疗的动物 —— 它们是「待接诊」桶的主体。
    # ⚠ `status='in_treatment'` 是唯一口径：`pending_adopt` 的动物虽然也允许
    #   开单（复查），但它们已经出院了，不该出现在待接诊列表里。
    orphan_pets = pets.filter(status='in_treatment').exclude(id__in=seen_pet_ids)
    for pet in orphan_pets.order_by('-id'):
        buckets['checking'].append(_pet_board_row(pet))

    counts = {k: len(v) for k, v in buckets.items()}
    buckets['counts'] = counts
    return json_ok(buckets)


def _board_buckets():
    """看板五态的空桶。桶名唯一取自 `Treatment.BOARD_STAGES`。

    以前空桶字面量在「账号没绑机构」的兜底和正常分桶两处各写一遍，加一态要改两处；
    漏一处的表现是那一态在看板上**永远空白**，前端页签也点不出数据。
    """
    return {k: [] for k in Treatment.BOARD_STAGES}


def _board_stage(treatment):
    """五态判据（唯一真源）。前端只做展示，不在 JS 里重算一遍。"""
    if treatment.disposed_at:
        return 'discharged'
    if treatment.done_at:
        return 'pending_discharge'
    if treatment.diagnosed_at:
        return 'treating'
    if treatment.checked_at:
        return 'pending_diagnose'
    return 'checking'


def _board_row(treatment):
    pet = treatment.pet
    info = _intake_info(pet) if pet is not None else {}
    return {
        'treatment_id': treatment.id,
        'pet_id': treatment.pet_id,
        'pet_code': treatment.pet_code,
        # 动物档案状态：看板五态由**诊疗单时间戳**派生，与 `Pet.status` 是两回事。
        # 出院候选要按「动物现在归谁管」筛（已出院 / 已被单据占用的不能再来一张），
        # 前端手上没有档案列表时（已出院桶、别的桶）必须能直接读到，
        # 否则只能靠 `pet_id` 回查，回查失败的行会静默变成可选。
        'pet_status': pet.status if pet else '',
        'pet_status_display': pet.get_status_display() if pet else '',
        'pet_name': pet.name if pet else '',
        'species': pet.species if pet else '',
        'gender': pet.gender if pet else '',
        'breed': pet.breed if pet else '',
        'color': pet.color if pet else '',
        'hospital_name': treatment.hospital_name,
        'stage': treatment.stage,
        # 五态判据由后端给出（`_board_stage` 是唯一真源），前端只做展示
        'board_stage': _board_stage(treatment),
        'is_disposed': treatment.is_disposed,
        'disposal': treatment.disposal,
        'disposal_label': _disposal_label(treatment.disposal),
        # 去向建议：诊疗完成那一刻选的，待出院时按它预选出院单据
        'disposal_advised': treatment.disposal_advised,
        'disposal_advised_label': _disposal_label(treatment.disposal_advised),
        'checked_at': treatment.checked_at.isoformat() if treatment.checked_at else '',
        'diagnosed_at': treatment.diagnosed_at.isoformat() if treatment.diagnosed_at else '',
        'done_at': treatment.done_at.isoformat() if treatment.done_at else '',
        'disposed_at': treatment.disposed_at.isoformat() if treatment.disposed_at else '',
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
        'pet_status': pet.status,
        'pet_status_display': pet.get_status_display(),
        'pet_name': pet.name,
        'species': pet.species,
        'gender': pet.gender,
        'breed': pet.breed,
        'color': pet.color,
        'hospital_name': getattr(pet.hospital, 'name', ''),
        'stage': 'checking',
        'board_stage': 'checking',
        'is_disposed': False,
        'disposal': '',
        'disposal_label': '',
        'disposal_advised': '',
        'disposal_advised_label': '',
        'checked_at': '',
        'diagnosed_at': '',
        'done_at': '',
        'disposed_at': '',
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

    # ⚠ 「未绑定医院」必须**前置**到取单之前：取单口径已按机构收口
    # （`get_hospital_scoped_pet` 在无机构时返回 None），若先取单，
    # 账号没绑机构会被报成 404「宠物不存在或无权访问」—— 把**账号配置问题**
    # 伪装成「数据不存在」，运维照 404 去查数据永远查不到。
    hospital = getattr(user, 'institution', None)
    if hospital is None:
        return json_fail('当前账号未绑定医院')

    # 按「本院在治 ∪ 本院诊疗过」取单（与看板同口径）。不能用按区县收敛的
    # `get_active_pet`：跨区县送医的动物会「看板看得见、点开 404」。
    pet = get_hospital_scoped_pet(pet_id, user)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)

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
def treatment_sterilization(request, pk):
    """提交绝育手术病历 —— 与「医嘱与处方」分开的独立入口。

    病历要等手术做完才写得出来，而 `save_plan()` 在已诊断后一律拒绝
    （改处方会让按处方扣掉的库存账实不符），所以这一步必须有自己的写入口；
    判据仍然只有 `services_treatment.save_sterilization()` 那一份。
    """
    data = parse_json_body(request)
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    try:
        with transaction.atomic():
            st.submit_sterilization(treatment, data)
    except ValueError as e:
        return json_fail(str(e))
    return json_ok(_treatment_detail_data(treatment), message='绝育手术病历已保存')


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
    """诊疗完成 —— 全部必执行项目都做完才能点。

    可带 `disposal_advised`（去向**建议**，可空）：此刻记下医生判断的出院方向，
    待出院弹窗按它预选单据。只写 `disposal_advised`，**不写 `disposal`** ——
    后者与 `disposed_at` 同时落，一落动物就进终态，六个出院单据全部 400。
    """
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    data = parse_json_body(request)
    advised = str(body_str(data, 'disposal_advised') or '').strip()
    try:
        with transaction.atomic():
            st.mark_done(treatment, disposal_advised=advised)
    except ValueError as e:
        return json_fail(str(e))
    treatment.refresh_from_db()
    return json_ok(_treatment_detail_data(treatment),
                   message='诊疗完成，动物进入待出院')


@csrf_exempt
@role_required('hospital')
@login_required
def treatment_advice(request, pk):
    """待出院阶段修改「建议去向」（第五十七轮）。

    出院四个去向能选到哪些动物，完全由这张诊疗单的 `disposal_advised`
    决定（判据见 `services_discharge.DISCHARGE_ADVICE`）。所以业务方要的
    「要改去向就先把建议去向改了，再回去院选择」只能落在这里 ——
    原先 `mark_done()` 之后界面上的建议是只读的（`adviceLocked`），
    后端也没有第二个写入口，选错去向只能把整张单作废重来。

    动物一旦被出院单据接管（已出库 / 已登记死亡 / 已在运回单上…）
    就**不再允许**修改：改了不会动任何单据，只会让单据与建议各说一套。
    """
    treatment = _own_treatment(pk, request.user)
    if treatment is None:
        return json_fail('诊疗单不存在或无权访问', status=404)

    data = parse_json_body(request)
    advised = str(body_str(data, 'disposal_advised') or '').strip()
    try:
        with transaction.atomic():
            st.set_advice(treatment, advised)
    except ValueError as e:
        return json_fail(str(e))
    treatment.refresh_from_db()
    label = _disposal_label(treatment.disposal_advised)
    return json_ok(_treatment_detail_data(treatment),
                   message=f'建议去向已改为「{label}」，可到出院管理办理')


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
        # 判据与 `Treatment.stage` 属性同源（`Treatment.STAGE_FILTERS`）：以前这里手写
        # 一份时间戳 isnull 组合、属性里再推一遍，两处一漂就出现
        # 「按阶段筛出 3 条，点开其中一条却显示另一个阶段」。
        stage_kwargs = Treatment.STAGE_FILTERS.get(stage)
        if stage_kwargs is None:
            return json_fail(f'未知的诊疗阶段：{stage}')
        qs = qs.filter(**dict(stage_kwargs))

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
