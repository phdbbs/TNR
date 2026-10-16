"""诊疗流程服务层 —— v5.1 原型的三步流程（第四十六轮新建）。

流程（与原型 `STEPS` 一一对应）
================================

1. **诊疗前检查** —— 基础信息写回档案 + 体检项；「提交」落 `checked_at`，
   阶段从「待接诊」变「诊疗中」。
2. **医嘱与处方** —— 整单医嘱 + 逐处置项目（绝育 / 驱虫 / 免疫 / 芯片 / 其他）的
   预计执行时间、处方、注意事项、操作人。「诊断完成」落 `diagnosed_at`。
3. **执行与办结** —— 逐项「执行完成」（**此时才扣库存**），全部必执行项目完成后
   「诊疗完成」落 `done_at`，最后选办结去向落 `disposed_at` 并改 `Pet.status`。

为什么扣库存放在「执行」而不是「诊断完成」
------------------------------------------
原型 `exec-done` 的提示原文就是「治疗完成，已扣减库存」。业务上也是这个道理：
医嘱开完还能改（原型专门有「撤回诊断」），但药是**真的打进去了**才算消耗。
放在诊断完成那一刻扣的话，「开完医嘱又改处方」会让库存与真实用药对不上，
而且**不会报错** —— 库存少了、账上看不出来。

⚠ 本模块所有写函数都**必须在调用方的 `transaction.atomic()` 里**调用：
它们与 `adjust_stock()` 写的是同一笔业务的两半，一半成功一半失败会留下
「库存扣了、项目没执行完」的半截状态，且不报错。
"""

import json

from django.db import transaction
from django.utils import timezone

from .models import (
    Chip, Material, MaterialStock, Pet, Treatment, TreatmentItem, TreatmentRx,
)
from .services import (
    adjust_stock, expired_material_error, generate_doc_no, get_hospital_stock,
    use_chip,
)

# ============================================
# 选项表 —— **只有这一份真源**
# ============================================
# 前端不另写一份：原型把 TESTS / ITEMS / DISPOSALS 硬编码在页面里，
# 那样「后端改了病种、前端还在用旧的」会静默漂移 —— 两边都能跑，
# 只是列表里少一项、而且没人发现。这里由 `check_options()` 下发。
INFECTIOUS_TESTS = {
    '猫': [('fpv', '猫瘟 FPV'), ('fhv', '猫鼻支 FHV'), ('fcv', '猫杯状 FCV')],
    '狗': [('cdv', '犬瘟热 CDV'), ('cpv', '犬细小 CPV'), ('ccv', '犬冠状病毒 CCV')],
}
# 三态必须分开存：「未检」与「阴性」在 BooleanField 里都是 False，
# 而它们是完全不同的临床事实（没做 vs 做了没病）。
TEST_RESULTS = [('none', '未检'), ('neg', '阴性'), ('pos', '阳性')]
COMMON_FINDINGS = ['无异常', '体表寄生虫', '有跳蚤']
SKIN_MUCOSA = ['无异常', '眼角有脓性分泌物']
ADVICE_TEMPLATES = [
    '术前禁食禁水 12 小时',
    '术后 24 小时观察精神与食欲',
    '保持术部干燥清洁',
    '一周后复查',
    '若出现呕吐、食欲废绝请及时复诊',
]

# 处置项目的展示元数据。⚠ 与 `TreatmentItem.ITEM_CHOICES` 的 key 集合
# **必须完全一致** —— 判据 `test_item_meta_covers_every_choice` 盯着这件事：
# 少一个 key = 那个处置项目在界面上永远没有入口（枚举值存在、产生点缺失）。
ITEM_META = [
    {'key': 'sterilization', 'icon': '✂️', 'name': '绝育',
     'hint': '母猫卵巢子宫摘除 / 公猫去势',
     'note_tpl': ['术前禁食 12h、禁水 4h', '术后 7 日复查拆线', '术后戴伊丽莎白圈 7 日']},
    {'key': 'deworming', 'icon': '💊', 'name': '驱虫', 'hint': '体内外驱虫',
     'note_tpl': ['驱虫后 7 日内避免洗澡', '空腹口服效果更佳']},
    {'key': 'vaccine', 'icon': '💉', 'name': '免疫', 'hint': '猫三联 / 犬四联免疫接种',
     'note_tpl': ['接种后留观 30 分钟', '与三联间隔 7 日']},
    {'key': 'chip', 'icon': '📡', 'name': '芯片', 'hint': '颈部皮下植入并绑定档案',
     'needs_chip': True, 'note_tpl': ['植入后扫码核对芯片号', '同步录入管理平台']},
    {'key': 'other', 'icon': '📝', 'name': '其他', 'hint': '其他处置项目，处方手写记录',
     'note_tpl': []},
]

# 每个处置项目对应的「旧代」布尔列（打印页 / 监管统计 / 政府端在读）。
# `other` 没有对应的旧列 —— 它本来就是第四十六轮新加的自由项。
LEGACY_ITEM_FLAGS = {
    'sterilization': 'items_sterilization',
    'vaccine': 'items_vaccine',
    'deworming': 'items_deworming',
    'chip': 'items_chip',
}

# 可以开进处方的物料类别。**药品是第四十六轮新加的**（R46 口径 1）：
# 处方药品走现有物料体系，不新建独立药品字典。
RX_CATEGORIES = ('medicine', 'vaccine', 'dewormer')

MAX_RX_ITEMS = 30          # 单个项目的处方条目上限（防误输）
MAX_ITEM_COUNT = len(ITEM_META)

# 体温判定的发烧线：≥ 此值判「发烧」。口径按猫的正常上限（约 38.0~39.5℃）
# 取 39.5 —— 本系统以猫为主，狗的正常上限略低（约 39.0℃），混养时统一按
# 猫的上限是保守选择：最多把个别偏高的狗多判一次「发烧」（看一眼就排除），
# 不会漏掉真发烧（漏判会绕过疫病监测）。将来要分物种设阈值，只改这一处；
# 前端 `syncTempFlag()` 与本函数必须同口径，阈值随 `check_options()` 下发，
# 前端不另抄一份。
FEVER_THRESHOLD = 39.5


def fever_judge(value):
    """体温数值 → 判定（normal / fever）。交叉校验与前端自动判定共用这一条线。"""
    return 'fever' if value >= FEVER_THRESHOLD else 'normal'


def check_options(species):
    """下发给前端的选项表（按物种给不同的传染病项）。"""
    return {
        'common_findings': COMMON_FINDINGS,
        'skin_mucosa': SKIN_MUCOSA,
        'infectious_tests': [{'key': k, 'label': l}
                             for k, l in INFECTIOUS_TESTS.get(species or '', [])],
        'test_results': [{'key': k, 'label': l} for k, l in TEST_RESULTS],
        'items': ITEM_META,
        'disposals': [{'key': k, 'label': l} for k, l in Treatment.DISPOSAL_CHOICES],
        'advice_templates': ADVICE_TEMPLATES,
        # 发烧线下发给前端做「手改数值 → 自动判定」，前端不另抄一份。
        'fever_threshold': FEVER_THRESHOLD,
    }


# ============================================
# 取单 / 开单
# ============================================
def open_treatment(pet, hospital, user, district_id):
    """取该宠物在本院**进行中**的诊疗单，没有就开一张（原型：点动物进流程）。

    ⚠ 不做成「点一下列表就建单」：那样「待接诊」列表点开又退出就会留下一张
    空诊疗单，`Treatment` 表里全是没内容的行，台账/统计都得再加一层过滤。
    只有真正开始填检查单时才建。

    ⚠ **归属收口**：动物挂在别院名下时返回 None，视图据此回一个与「不存在」
    **完全相同**的 404 —— 否则「存在但不属于我」与「不存在」可区分，
    就成了存在性预言机（枚举 id 就能问出别院有哪些动物）。
    `pet.hospital` 为空的历史行放行（「只拦新引用、不拦存量」）。

    ⚠ 状态不允许诊疗时**抛异常**而不是返回 None：那时动物本来就归本院，
    「状态不对」是本院操作员需要看到的信息，藏起来只会让人以为是系统坏了。
    """
    if pet is None or hospital is None:
        return None
    if pet.hospital_id is not None and pet.hospital_id != hospital.id:
        return None
    if pet.status not in ('in_treatment', 'pending_adopt'):
        raise ValueError(f'动物当前状态（{pet.get_status_display()}）不可诊疗')

    treatment = Treatment.objects.filter(
        pet=pet, hospital=hospital, status='in_progress').order_by('-id').first()
    if treatment is not None:
        return treatment

    return Treatment.objects.create(
        pet=pet,
        pet_code=pet.code,
        hospital=hospital,
        hospital_name=hospital.name,
        status='in_progress',
        operator=user,
        operator_name=user.get_full_name() or user.username,
        ledger_no=generate_doc_no('TRE'),
        district_id=district_id,
        # 签名与电子章：R46 口径 2「挂账号，一次维护、每次自动带出」。
        # 落库时**快照** —— 证号与机构名以后改了，历史单据不能跟着变。
        vet_name=user.get_full_name() or user.username,
        vet_license=getattr(user, 'vet_license', '') or '',
        hospital_seal=(getattr(hospital, 'seal_text', '') or hospital.name),
    )


# ============================================
# 第一步：诊疗前检查
# ============================================
def apply_check(treatment, body, submit=False):
    """保存 / 提交检查单。

    :param submit: False = 暂存（原型「暂存」），True = 提交并进入第二步
    :raises ValueError: 校验不通过
    :return: 被更新的字段名列表

    ⚠ 「暂存」与「提交」的区别**只在体温是否必填**：暂存允许填一半，
    提交必须完整。这是原型的口径（`saveCheck(isDraft)`）。
    """
    if treatment is None:
        raise ValueError('诊疗单不存在')
    if treatment.disposed_at:
        raise ValueError('本单已办结，不能再修改检查单')

    pet = treatment.pet
    updated = []

    # ---- 基础信息：写回动物档案（原型「转运档案带出 · 可修改」）----
    pet_fields = []
    species = body.get('species')
    if species:
        if species not in dict(Pet.SPECIES_CHOICES):
            raise ValueError(f'物种「{species}」不合法')
        if species != pet.species:
            pet.species = species
            pet_fields.append('species')
    for field, label in (('gender', '性别'), ('breed', '品种'),
                         ('color', '毛色'), ('weight', '体重')):
        if field in body:
            value = str(body.get(field) or '').strip()
            if getattr(pet, field) != value:
                setattr(pet, field, value)
                pet_fields.append(field)
    if pet_fields:
        pet.save(update_fields=pet_fields)

    # ---- 体检项 ----
    # 体温：数值 + 判定。原型点「正常 38.5℃」自动填数值、可手改，两者独立存。
    if 'temperature' in body:
        raw = body.get('temperature')
        value = None
        if raw not in (None, ''):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise ValueError('体温必须是数字')
            # 40.5℃ 的手改空间是有的，但 405 这种明显是小数点打漏了；
            # 拦在 30~45 之外 —— 存进去之后没人会发现，只会污染体温趋势。
            if not (30 <= value <= 45):
                raise ValueError(f'体温 {value}℃ 超出合理范围（30~45）')
        treatment.temperature = value
        updated.append('temperature')

    if 'temperature_flag' in body:
        flag = str(body.get('temperature_flag') or '').strip()
        valid = [k for k, _ in Treatment.TEMP_FLAG_CHOICES]
        if flag and flag not in valid:
            raise ValueError(f'体温判定「{flag}」不合法')
        treatment.temperature_flag = flag
        updated.append('temperature_flag')

    # 数值与判定的交叉校验：两者都有值时必须落在发烧线（`FEVER_THRESHOLD`）
    # 的同侧 —— 「40.2℃ + 正常」一旦落库，打印单、体温趋势、疫病监测会
    # 互相矛盾，而且没人会去核对。前端手改数值时已自动点亮判定，这里兜底
    # 拦 API 直调 / 前端漏拦的矛盾组合。⚠ 对**最终组合**校验而不是「本次
    # 请求是否传了体温」：暂存分两次填（先数值后判定）不会误拦，而库里
    # 已是矛盾组合的旧单，任何一次保存都会被拦下来逼着修正 —— 渐进清洗。
    if treatment.temperature is not None and treatment.temperature_flag:
        expect = fever_judge(treatment.temperature)
        if treatment.temperature_flag != expect:
            labels = dict(Treatment.TEMP_FLAG_CHOICES)
            expect_label = '发烧' if expect == 'fever' else '正常'
            picked_label = labels.get(treatment.temperature_flag,
                                      treatment.temperature_flag)
            raise ValueError(
                f'体温 {treatment.temperature}℃ 与判定「{picked_label}」矛盾：'
                f'按发烧线 {FEVER_THRESHOLD}℃ 应判「{expect_label}」，请修正')

    if submit:
        if treatment.temperature is None:
            raise ValueError('请先完成「体温检测」再提交')
        if not treatment.temperature_flag:
            raise ValueError('请选择体温判定（正常 / 发烧）再提交')

    # 常见病检查：多选，顿号分隔的**文本快照**（选项以后会变，历史单据不能跟着变）
    if 'common_findings' in body:
        raw = body.get('common_findings')
        if raw is None:
            raw = []
        if not isinstance(raw, (list, tuple)):
            raise ValueError('常见病检查必须是数组')
        picked = [str(v) for v in raw]
        unknown = [v for v in picked if v not in COMMON_FINDINGS]
        if unknown:
            raise ValueError(f'常见病检查含未知选项：{"、".join(unknown)}')
        # 「无异常」与其它项互斥 —— 原型的 chips 交互就是这么做的，
        # 但服务端也必须拦：前端漏拦时「无异常 + 有跳蚤」会一起存进去，
        # 打印出来自相矛盾，而且没人会去核对。
        if '无异常' in picked and len(picked) > 1:
            raise ValueError('「无异常」不能与其它常见病检查项同时选择')
        treatment.common_findings = '、'.join(picked)
        updated.append('common_findings')

    if 'common_other' in body:
        treatment.common_other = str(body.get('common_other') or '').strip()[:200]
        updated.append('common_other')

    # 传染病检查：三态，按物种给项。存 JSON 文本快照。
    if 'infectious_tests' in body:
        raw = body.get('infectious_tests') or {}
        if not isinstance(raw, dict):
            raise ValueError('传染病检查格式不正确')
        allowed = dict(INFECTIOUS_TESTS.get(pet.species or '', []))
        valid_results = {k for k, _ in TEST_RESULTS}
        cleaned = {}
        for key, value in raw.items():
            if key not in allowed:
                raise ValueError(f'传染病检查项「{key}」不适用于{pet.species or "该物种"}')
            value = str(value or 'none')
            if value not in valid_results:
                raise ValueError(f'传染病检查结果「{value}」不合法')
            cleaned[key] = value
        # 未传的项补 `none`：让「没做」这件事在数据里显式存在，
        # 而不是靠「键缺失」暗示 —— 两者在读取侧的含义完全不同。
        for key in allowed:
            cleaned.setdefault(key, 'none')
        treatment.infectious_tests = json.dumps(cleaned, ensure_ascii=False)
        updated.append('infectious_tests')

    if 'skin_mucosa' in body:
        value = str(body.get('skin_mucosa') or '').strip()
        if value and value not in SKIN_MUCOSA and value != '其他':
            raise ValueError(f'皮肤黏膜检查「{value}」不合法')
        treatment.skin_mucosa = value
        updated.append('skin_mucosa')

    if 'skin_other' in body:
        treatment.skin_other = str(body.get('skin_other') or '').strip()[:200]
        updated.append('skin_other')

    if 'check_note' in body:
        treatment.check_note = str(body.get('check_note') or '').strip()
        updated.append('check_note')

    # 提交才落 `checked_at`（阶段从「待接诊」变「诊疗中」）。
    # 暂存**不动**它 —— 否则「暂存一下就变成诊疗中了」，
    # 待接诊列表会莫名其妙清空。
    if submit and treatment.checked_at is None:
        treatment.checked_at = timezone.now()
        updated.append('checked_at')

    if updated:
        treatment.save(update_fields=sorted(set(updated)))
    return updated


# ============================================
# 第二步：医嘱与处方
# ============================================
def save_plan(treatment, body):
    """保存医嘱与处置项目（原型「暂存」/「诊断完成」前的落库）。

    :raises ValueError: 校验不通过

    ⚠ 已诊断（`diagnosed_at` 非空）时**一律拒绝**：原型把整块表单 `disabled`，
    并要求先点「撤回诊断」。服务端不拦的话，前端漏禁用就能在「已诊断」状态下
    改处方 —— 而执行阶段是按处方扣库存的，改了之后账实不符且无痕迹。
    """
    if treatment is None:
        raise ValueError('诊疗单不存在')
    if treatment.disposed_at:
        raise ValueError('本单已办结，不能再修改医嘱与处方')
    if treatment.diagnosed_at is not None:
        raise ValueError('诊断已完成，请先「撤回诊断」再修改医嘱与处方')

    hospital = treatment.hospital
    if hospital is None:
        raise ValueError('诊疗单缺少医院信息')

    updated = []
    if 'advice' in body:
        treatment.advice = str(body.get('advice') or '').strip()
        updated.append('advice')

    items = body.get('items')
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError('处置项目必须是数组')
    if len(items) > MAX_ITEM_COUNT:
        raise ValueError(f'处置项目最多 {MAX_ITEM_COUNT} 项')
    if items:
        _replace_items(treatment, items, hospital)

    if updated:
        treatment.save(update_fields=sorted(set(updated)))
    sync_legacy_item_flags(treatment)
    return updated


def _replace_items(treatment, items, hospital):
    """全量替换该诊疗单的处置项目与处方（草稿语义：每次保存覆盖）。

    ⚠ 先**全校验**再落库：任何一项不合法就整体拒绝，不产生半截子表。
    """
    valid_keys = {k for k, _ in TreatmentItem.ITEM_CHOICES}
    seen = set()
    cleaned = []

    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError('处置项目格式不正确')
        key = str(raw.get('key') or '').strip()
        if key not in valid_keys:
            raise ValueError(f'处置项目「{key}」不合法')
        if key in seen:
            raise ValueError(f'处置项目「{key}」重复提交')
        seen.add(key)

        # 原型口径：选了处方（或填了芯片号 / 手写处方）才算「要做」。
        # 三样都空的项目 = 跳过，不落库 —— 落库了它就成了「必执行」，
        # 会把「诊疗完成」永久卡住（原型 `must` 就是 hasRx）。
        rx_ids = raw.get('rx') or []
        if not isinstance(rx_ids, list):
            raise ValueError('处方必须是数组')
        rx_ids = [i for i in rx_ids if i not in (None, '')]
        rx_other = str(raw.get('rx_other') or '').strip()[:500]
        chip_no = str(raw.get('chip_no') or '').strip()
        if len(rx_ids) > MAX_RX_ITEMS:
            raise ValueError(f'单个处置项目的处方最多 {MAX_RX_ITEMS} 条')

        planned_date = _parse_date(raw.get('planned_date'))
        has_rx = bool(rx_ids) or bool(rx_other) or bool(chip_no)

        # 芯片项目：**填了预计执行时间就必须填芯片号**。
        # 只填时间不填号的话 `has_rx` 为假、整项会被下面静默跳过 ——
        # 用户以为在植芯片，实际什么都没记，而界面上不会报任何错。
        if key == 'chip' and planned_date is not None and not chip_no:
            raise ValueError('芯片处置必须填写芯片号')

        if not has_rx:
            continue

        # 原型 `validatePlan()`：**有处方但没日期** = 待补全，拦住不让往下走。
        # 这是「时间与处方互为必填」，不是「日期必填」—— 没处方就不要求日期。
        if planned_date is None:
            meta = _item_meta(key)
            raise ValueError(f'{meta["name"]}：已选处方但未填预计执行时间')

        materials = []
        for mid in rx_ids:
            material = Material.objects.filter(pk=mid).first()
            if material is None:
                raise ValueError(f'药品不存在（id={mid}）')
            if material.category not in RX_CATEGORIES:
                raise ValueError(f'「{material.name}」不是可开处方的物资（当前类别：{material.get_category_display()}）')
            # B5：批次级过期判据（传 institution=hospital）—— 开方时只看
            # 「本院是否还有未过期批次」，真正扣哪批由执行时的 FEFO 决定。
            err = expired_material_error(material, '用于处方', institution=hospital)
            if err:
                raise ValueError(err)
            # 原型「无库存 = 不可选」。这里提前告知，真正的闸门在执行时
            # （执行时库存可能已被别的单用掉，那时还会再拦一次）。
            if get_hospital_stock(material, hospital) <= 0:
                raise ValueError(f'「{material.name}」本院库存为 0，无法开入处方')
            if material.pk in [m.pk for m in materials]:
                raise ValueError(f'「{material.name}」在同一项目中重复开方')
            materials.append(material)

        cleaned.append({
            'key': key,
            'planned_date': planned_date,
            'chip_no': chip_no,
            'notes': str(raw.get('notes') or '').strip()[:500],
            'rx_other': rx_other,
            'vet_name': str(raw.get('vet_name') or '').strip()[:50],
            'vet_license': str(raw.get('vet_license') or '').strip()[:100],
            'materials': materials,
        })

    # ---- 校验全过，开始落库 ----
    treatment.items.all().delete()
    for row in cleaned:
        item = TreatmentItem.objects.create(
            treatment=treatment,
            item_key=row['key'],
            item_name=_item_meta(row['key'])['name'],
            planned_date=row['planned_date'],
            chip_no=row['chip_no'],
            notes=row['notes'],
            rx_other=row['rx_other'],
            vet_name=row['vet_name'] or treatment.vet_name,
            vet_license=row['vet_license'] or treatment.vet_license,
        )
        TreatmentRx.objects.bulk_create([
            TreatmentRx(item=item, material=m, material_name=m.name,
                        quantity=1, unit=m.unit)
            for m in row['materials']
        ], batch_size=100)


def mark_diagnosed(treatment):
    """「诊断完成」—— 落 `diagnosed_at`，进入执行阶段。

    :raises ValueError: 医嘱与处置项目全空
    """
    if treatment is None:
        raise ValueError('诊疗单不存在')
    if treatment.disposed_at:
        raise ValueError('本单已办结')
    if treatment.checked_at is None:
        raise ValueError('请先提交诊疗前检查单')
    if treatment.diagnosed_at is not None:
        raise ValueError('诊断已完成，无需重复提交')

    # 原型：`if (!advice.trim() && !mustItems().length)` → 拦住。
    # 全空就点「诊断完成」的话，会得到一张什么都没做的单子进入执行阶段，
    # 而执行阶段的所有项目都「已完成」（因为没有项目），可以直接办结。
    has_items = treatment.items.exists()
    if not (treatment.advice or '').strip() and not has_items:
        raise ValueError('请至少填写医嘱，或为某个处置项目填写时间与处方')

    treatment.diagnosed_at = timezone.now()
    treatment.save(update_fields=['diagnosed_at'])
    return treatment


def undiagnose(treatment):
    """「撤回诊断」—— 清 `diagnosed_at`，回到可编辑状态。

    ⚠ 有任何项目**已执行**时拒绝：执行已经扣过库存、植过芯片，
    撤回诊断会让「已执行的项目」处在「诊断未完成」的状态里，
    再改处方就出现「按新处方重扣一次」的双重扣减。
    """
    if treatment is None:
        raise ValueError('诊疗单不存在')
    if treatment.disposed_at:
        raise ValueError('本单已办结，不能撤回诊断')
    if treatment.diagnosed_at is None:
        raise ValueError('本单尚未完成诊断')
    executed = treatment.items.filter(is_done=True)
    if executed.exists():
        raise ValueError('已有处置项目执行完成，请先撤销执行再撤回诊断')

    treatment.diagnosed_at = None
    treatment.save(update_fields=['diagnosed_at'])
    return treatment


# ============================================
# 第三步：执行与办结
# ============================================
def execute_item(treatment, item, user):
    """执行一个处置项目 —— **库存在这里扣**。

    :raises ValueError: 前置不满足 / 库存不足 / 芯片不可用
    :return: 消耗的流水条数

    ⚠ 两段式：先把**所有**处方药品的库存校验一遍，再统一落库。
    边校验边落库的话，第 2 条药库存不足时会留下「第 1 条已扣、项目未完成」
    的半截状态 —— 而项目 `is_done` 还是 False，用户可以再点一次执行，
    于是第 1 条药被扣两次。
    """
    if treatment is None or item is None:
        raise ValueError('处置项目不存在')
    if item.treatment_id != treatment.id:
        raise ValueError('处置项目不属于本诊疗单')
    if treatment.disposed_at:
        raise ValueError('本单已办结，不能再执行')
    if treatment.diagnosed_at is None:
        raise ValueError('请先完成「医嘱与处方」并点击诊断完成')
    if item.is_done:
        raise ValueError(f'{item.get_item_key_display()}已执行完成，无需重复执行')

    hospital = treatment.hospital
    if hospital is None:
        raise ValueError('诊疗单缺少医院信息')

    rx_rows = list(item.rx_rows.select_related('material'))

    # 芯片项目：号码必须有，且**在这里就把行取出来** —— 落库段不再做
    # 可能失败的查询（「写库后还可能 raise」会留半截行）。
    chip_row = None
    if item.item_key == 'chip':
        chip_no = (item.chip_no or '').strip()
        if not chip_no:
            raise ValueError('芯片处置未填写芯片号')
        chip_row = Chip.objects.filter(number=chip_no).first()
        if chip_row is None:
            raise ValueError(f'芯片 {chip_no} 不存在')

    # ---- 第一段：全校验 ----
    for rx in rx_rows:
        material = rx.material
        if material is None:
            raise ValueError('处方药品已被删除，无法执行')
        # B5 起传 institution=hospital 走批次级判据（与 views_treatment 同口径）
        err = expired_material_error(material, '用于诊疗', institution=hospital)
        if err:
            raise ValueError(err)
        # ⚠ 医院侧 `adjust_stock` **故意不判余额**（见 services_stock 的长注释），
        #   所以这道判据必须在这里自己写 —— 少了它，医院能把库存扣成负数。
        stock = get_hospital_stock(material, hospital)
        if stock < rx.quantity:
            raise ValueError(f'「{material.name}」库存不足（当前 {stock}，需 {rx.quantity}）')

    # 芯片植入也要扣**芯片物料**的库存 —— 「可用芯片数」与「芯片物料库存」
    # 必须恒等（第四十六轮重构的目标）。不扣这一笔，可用芯片数就比库存多 1，
    # 而**没有任何报错**，只有把两边的账摊开才看得出来。
    if chip_row is not None and chip_row.material_id:
        chip_stock = get_hospital_stock(chip_row.material, hospital)
        if chip_stock < 1:
            raise ValueError(
                f'芯片物料「{chip_row.material.name}」库存不足（当前 {chip_stock}）')

    # 芯片的四种状态（可用 / 在途 / 已使用 / 已作废）与归属校验都在
    # `use_chip()` 里且**只有那一份**，这里再写一遍就是孪生判据。

    # ---- 第二段：落库 ----
    for rx in rx_rows:
        adjust_stock(
            material=rx.material,
            hospital=hospital,
            quantity=rx.quantity,
            txn_type='consume',
            institution=hospital,
            operator=user,
            operator_name=user.get_full_name() or user.username,
            from_to='诊疗消耗',
            note=f'{treatment.pet_code} {item.item_name}',
            ledger_no=generate_doc_no('CON'),
        )

    if item.item_key == 'chip':
        # 异常**故意不包 try**：让它传播出 `atomic()` 整块回滚，
        # 不会留下「库存扣了、芯片没植」的半截行。
        # 在事务里 `return` / 吞异常反而会**提交**半截数据。
        use_chip(chip_row.number, treatment.pet, institution=hospital)
        treatment.chip_no = chip_row.number
        treatment.chip_date = timezone.localdate()
        treatment.save(update_fields=['chip_no', 'chip_date'])
        if chip_row.material_id:
            adjust_stock(
                material=chip_row.material,
                hospital=hospital,
                quantity=1,
                txn_type='consume',
                institution=hospital,
                operator=user,
                operator_name=user.get_full_name() or user.username,
                from_to='诊疗消耗',
                note=f'{treatment.pet_code} 芯片植入 {chip_row.number}',
                ledger_no=generate_doc_no('CON'),
            )

    item.is_done = True
    item.done_at = timezone.now()
    item.save(update_fields=['is_done', 'done_at'])
    return len(rx_rows)


def undo_item(treatment, item, user):
    """撤销一个已执行的项目 —— 库存退回、芯片退回「可用」。

    :raises ValueError: 前置不满足

    ⚠ 退回写的是 **`adjustment` 类型、数量为负** 的流水，而不是删掉原流水：
    本项目禁物理删除，且「撤回过一次」本身就是要能查的事实。

    ⚠ 为什么负数能加回库存：`stock_delta()` 对 `adjustment` 返回 `-quantity`，
    传 `-1` 就得到 `+1`。**不要**用 `purchase` 类型去加回 ——
    那会让「采购入库合计」凭空变大，采购台账直接错。
    """
    if treatment is None or item is None:
        raise ValueError('处置项目不存在')
    if item.treatment_id != treatment.id:
        raise ValueError('处置项目不属于本诊疗单')
    if treatment.disposed_at:
        raise ValueError('本单已办结，不能再撤销执行')
    if not item.is_done:
        raise ValueError(f'{item.get_item_key_display()}尚未执行，无需撤销')

    hospital = treatment.hospital
    if hospital is None:
        raise ValueError('诊疗单缺少医院信息')

    for rx in item.rx_rows.select_related('material'):
        if rx.material is None:
            continue
        adjust_stock(
            material=rx.material,
            hospital=hospital,
            quantity=-rx.quantity,
            txn_type='adjustment',
            institution=hospital,
            operator=user,
            operator_name=user.get_full_name() or user.username,
            from_to='撤销诊疗消耗',
            note=f'{treatment.pet_code} 撤销{item.item_name}',
            ledger_no=generate_doc_no('ADJ'),
        )

    if item.item_key == 'chip' and (item.chip_no or '').strip():
        chip_row = Chip.objects.filter(number=item.chip_no.strip()).first()
        # 先退芯片（可能因「已绑定别的动物」而 raise），再退库存 ——
        # 反过来的话，芯片没退成、库存已经加回去了。
        _release_chip(item.chip_no.strip(), treatment.pet)
        if chip_row is not None and chip_row.material_id:
            adjust_stock(
                material=chip_row.material,
                hospital=hospital,
                quantity=-1,
                txn_type='adjustment',
                institution=hospital,
                operator=user,
                operator_name=user.get_full_name() or user.username,
                from_to='撤销诊疗消耗',
                note=f'{treatment.pet_code} 撤销芯片植入 {chip_row.number}',
                ledger_no=generate_doc_no('ADJ'),
            )

    if item.item_key == 'chip':
        # 撤销执行 = 植入结果一并作废（`execute_item` 写入的对称清理）。
        # 不清的话，诊疗单/打印页仍显示「已植入 xxx」，而动物档案那侧
        # （`_release_chip`）已经清空 —— 单据与档案自相矛盾。
        treatment.chip_no = ''
        treatment.chip_date = None
        treatment.save(update_fields=['chip_no', 'chip_date'])

    item.is_done = False
    item.done_at = None
    item.save(update_fields=['is_done', 'done_at'])
    return True


def _release_chip(chip_no, pet):
    """把芯片退回「可用」（撤销植入时用）。

    ⚠ 只在「这颗芯片确实挂在本宠物名下」时退回：若中途被别的诊疗单
    重新用掉（`chip.pet` 已经不是它了），退回会把别人正在用的芯片
    变成「可用」，随后被第三只动物植走 —— 一颗芯片两条档案，且不报错。
    """
    chip = Chip.objects.filter(number=chip_no).first()
    if chip is None:
        return False
    if chip.pet_id is not None and chip.pet_id != pet.id:
        raise ValueError(f'芯片 {chip_no} 已绑定其它动物档案，不能退回')
    chip.status = 'available'
    chip.pet = None
    chip.used_at = None
    chip.save(update_fields=['status', 'pet', 'used_at'])
    if pet.chip_no == chip_no:
        pet.chip_no = ''
        pet.save(update_fields=['chip_no'])
    return True


def mark_done(treatment):
    """「诊疗完成」—— 落 `done_at`，宠物进入「待出院」。

    :raises ValueError: 还有必执行项目没做完
    """
    if treatment is None:
        raise ValueError('诊疗单不存在')
    if treatment.disposed_at:
        raise ValueError('本单已办结')
    if treatment.diagnosed_at is None:
        raise ValueError('请先完成「医嘱与处方」并点击诊断完成')
    if treatment.done_at is not None:
        raise ValueError('本单已完成诊疗')

    left = must_items_not_done(treatment)
    if left:
        names = '、'.join(i.item_name or i.get_item_key_display() for i in left)
        raise ValueError(f'还有 {len(left)} 项未完成：{names}')

    treatment.done_at = timezone.now()
    treatment.save(update_fields=['done_at'])
    return treatment


def dispose(treatment, disposal, user):
    """办结 —— 记录去向并改动物状态。

    :raises ValueError: 未诊疗完成 / 去向不合法

    ⚠ 本步**只改 `Pet.status` 并记去向**，不代开具体单据（领养 / 放归 /
    领回 / 安乐死 / 无害化）—— 那些单据各有自己的必填项与流程
    （如安乐死单要填原因、无害化要登记遗体），在这里代开会造出
    一张字段全空的单据，看起来「流程走完了」，实际是空壳。
    """
    if treatment is None:
        raise ValueError('诊疗单不存在')
    if treatment.disposed_at:
        raise ValueError('本单已办结')
    if treatment.done_at is None:
        raise ValueError('请先点击「诊疗完成」再选择办结去向')

    valid = dict(Treatment.DISPOSAL_CHOICES)
    if disposal not in valid:
        raise ValueError(f'办结去向「{disposal}」不合法')

    pet = treatment.pet
    if pet is None:
        raise ValueError('诊疗单缺少动物档案')

    treatment.disposal = disposal
    treatment.disposed_at = timezone.now()
    treatment.status = 'completed'
    treatment.save(update_fields=['disposal', 'disposed_at', 'status'])

    # 去向的 key 与 `Pet.STATUS_CHOICES` 一一对齐（见 `Treatment.DISPOSAL_CHOICES`），
    # 所以直接赋值即可。用一张映射表反而多一处会漏改的地方。
    pet.status = disposal
    pet.save(update_fields=['status'])
    return treatment


# ============================================
# 派生 / 辅助
# ============================================
def item_is_must(item):
    """该项目是否「必执行」—— 有处方 / 手写处方 / 芯片号 就是。

    ⚠ 落库时 `_replace_items()` 已经把「三样全空」的项目过滤掉了，
    所以理论上表里的每一行都是必执行的。这个函数仍然显式判一遍：
    数据可能来自 admin 或历史导入，判据不该依赖「写入侧已经过滤过」。
    """
    if item is None:
        return False
    return bool(item.rx_rows.exists() or (item.rx_other or '').strip()
                or (item.chip_no or '').strip())


def must_items_not_done(treatment):
    """还没执行完的必执行项目列表（`mark_done` 的前置闸门）。"""
    if treatment is None:
        return []
    return [i for i in treatment.items.all()
            if item_is_must(i) and not i.is_done]


def sync_legacy_item_flags(treatment):
    """把子表的项目同步到旧代 `items_*` 四个布尔列。

    为什么要同步：打印页（`views_print.TREATMENT_FIELDS`）、监管端统计
    （`supervision/views.py`）、宠物档案时间轴（`views_portal.py`）、
    政府端 / 医院端列表都在读这四个布尔。不同步的话，新流程开的单
    在那些页面上显示成「什么都没做」。

    ⚠ 写入点**只有这一处**（`save_plan` / 项目增删时调），否则就是双真源。
    """
    if treatment is None:
        return
    keys = set(treatment.items.values_list('item_key', flat=True))
    updates = {col: (key in keys) for key, col in LEGACY_ITEM_FLAGS.items()}
    if not updates:
        return
    Treatment.objects.filter(pk=treatment.pk).update(**updates)
    for field, value in updates.items():
        setattr(treatment, field, value)


def _item_meta(key):
    for meta in ITEM_META:
        if meta['key'] == key:
            return meta
    return {'key': key, 'name': key, 'icon': '', 'hint': '', 'note_tpl': []}


def _parse_date(value):
    """宽松解析 `YYYY-MM-DD`；空值返回 None，非法值返回 None（由调用方判空报错）。"""
    from datetime import date as date_type
    from datetime import datetime
    if not value:
        return None
    if isinstance(value, date_type):
        return value
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None
