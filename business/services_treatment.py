"""诊疗流程服务层 —— v5.1 原型的三步流程（第四十六轮新建）。

流程（与原型 `STEPS` 一一对应）
================================

1. **诊疗前检查** —— 基础信息写回档案 + 体检项；「提交」落 `checked_at`，
   阶段从「待接诊」变「诊疗中」。
2. **医嘱与处方** —— 整单医嘱 + 逐处置项目（绝育 / 驱虫 / 免疫 / 芯片 / 其他）的
   预计执行时间、处方、注意事项、操作人。「诊断完成」落 `diagnosed_at`。
3. **执行与办结** —— 逐项「执行完成」（**此时才扣库存**），全部必执行项目完成后
   「诊疗完成」落 `done_at`（动物进入「待出院」），医生选的去向只是
   `disposal_advised`（建议）；真正的 `disposed_at` 由**出院单据完成时**
   落（见 `dispose_for_discharge`）。在诊疗页点「完成」不算出院。

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

from django.utils import timezone

from .models import (
    AdoptionHallListing, Chip, Material, Pet, Treatment,
    TreatmentItem, TreatmentRx,
)
from .services import (
    adjust_stock, expired_material_error, generate_doc_no, get_hospital_stock,
    parse_int_param, use_chip,
)

# ============================================
# 选项表 —— **只有这一份真源**
# ============================================
# 前端不另写一份：原型把 TESTS / ITEMS / DISPOSALS 硬编码在页面里，
# 那样「后端改了病种、前端还在用旧的」会静默漂移 —— 两边都能跑，
# 只是列表里少一项、而且没人发现。这里由 `check_options()` 下发。
INFECTIOUS_TESTS = {
    '猫': [('fpv', '猫瘟 FPV'), ('fhv', '猫鼻支 FHV'), ('fcv', '猫杯状 FCV'),
           ('rabies', '狂犬病'), ('toxoplasma', '弓形虫')],
    '狗': [('cdv', '犬瘟热 CDV'), ('cpv', '犬细小 CPV'), ('ccv', '犬冠状病毒 CCV'),
           ('rabies', '狂犬病')],
}
# 狂犬病是**人畜共患、法定报告**疫病（《动物防疫法》名录），猫狗都必须给项；
# 弓形虫的终末宿主是猫，所以只在猫的清单里出现 —— 给狗列它等于让医生
# 面对一个临床上无意义的三态选择，反而降低整张检查单的可信度。
#
# ⚠ 口径（业务 2026-10-19 确认）：这些项目**允许全空**，服务端不加
# 「必须检完才能办结」的硬门禁。原因是 TNR 现场是群体绝育、不做逐只隔离观察，
# 加门禁的结果不会是「都检了」，而是「都填阴性」—— 用假数据换一张合规外观的表，
# 比空着更危险。三态里的 `none`（未检）就是为此保留的**诚实档位**。
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
#
# `needs_record` = 该项目还要填一份**绝育手术病历**（手术日期 / 主刀 / 麻醉 /
# 诊断 / 手术过程 / 恢复）。第四十七轮从旧「快速登记」表单并入：
# 这几个字段只在 sterilization 卡片里出现，服务端 `save_sterilization()`
# 校验「勾了绝育就必须填全」。
ITEM_META = [
    {'key': 'sterilization', 'icon': '✂️', 'name': '绝育',
     'hint': '母猫卵巢子宫摘除 / 公猫去势',
     'needs_record': True,
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

# ============================================
# 绝育手术病历（第四十七轮：从旧「快速登记」表单并入三步流程）
# ============================================
# 六个「旧口径」列。⚠ 不新建平行字段：`views_print.TREATMENT_FIELDS`、
# 监管统计、宠物档案时间轴、政府端 / 医院端列表**都在读这几列**，
# 新建字段 = 那几处一起印出空白，而且不报错。
STERILIZATION_FIELDS = (
    ('surgery_date', 'sterilization_surgery_date'),
    ('surgeon', 'sterilization_surgeon'),
    ('anesthesia', 'sterilization_anesthesia'),
    ('diagnosis', 'sterilization_diagnosis'),
    ('procedure', 'sterilization_procedure'),
    ('recovery', 'sterilization_recovery'),
)
# 给用户看的字段名（报错时要点名是哪一栏）
STERILIZATION_LABELS = {
    'surgery_date': '手术日期', 'surgeon': '主刀医生',
    'anesthesia': '麻醉方式', 'diagnosis': '诊断',
    'procedure': '手术过程', 'recovery': '恢复情况',
}


def sterilization_draft_filled(ster):
    """这份病历草稿**填了没有**（任一栏有值即算）。

    「0 栏 = 没填」与「1~5 栏 = 填了一半」在两条规则里意义不同：
    计划阶段允许 0 栏（手术还没做），但 1~5 栏要拒绝（半途病历会印成
    「做了手术、日期空着」的归档单）。判据只有这一份，
    `save_plan()` 的 `record_keys` 与 `save_sterilization()` 的放行都用它。
    """
    if not isinstance(ster, dict):
        return False
    return any(str(ster.get(k) or '').strip()
               for k in STERILIZATION_REQUIRED + ('surgery_date',))


def sterilization_record_missing(treatment):
    """诊疗单上**已落库**的病历缺哪几栏（返回中文栏名列表，齐全时为空）。

    执行「绝育」项目时的闸门用它 —— 判据只看这六列（真源），不看前端草稿，
    否则「界面说填了、库里没有」会放行。
    """
    missing = []
    for key, col in STERILIZATION_FIELDS:
        if key == 'surgery_date':
            empty = getattr(treatment, col) is None
        else:
            empty = not (getattr(treatment, col) or '').strip()
        if empty:
            missing.append(STERILIZATION_LABELS[key])
    return missing
ANESTHESIA_CHOICES = ('吸入麻醉', '注射麻醉', '复合麻醉')
# 病历必填项（手术日期走 `_parse_date()` 另做格式校验，不在此列）
STERILIZATION_REQUIRED = ('surgeon', 'diagnosis', 'procedure', 'recovery')
STERILIZATION_TEXT_MAX = 500      # 单栏文本上限，与 `notes` 同量级


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
        # 归属机构 = 接诊医院（业务对象推导，不取操作员机构）
        institution=hospital,
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
    # 第四十八轮：检查单一经提交（`checked_at` 落库）就**锁定**，
    # 之后返回第 1 步只能查看。理由：检查结果是后续医嘱 / 诊断的依据，
    # 允许事后静默改写会让「医嘱基于什么体检结论下的」失去凭据；
    # 而且界面上「保存修改并下一步」与「提交并下一步」两个按钮只差两个字，
    # 用户点完回来改一笔、单据号与打印件已经发出去的情况很难追溯。
    #
    # ⚠ 前端已经把这步整页置灰，但**服务端必须自己也拦** ——
    #   「前端收窄选项而服务端不校验 = 洞」，直调 API 一样能改。
    if treatment.checked_at:
        raise ValueError('检查单已提交，不能再修改；如需修正请先撤回诊断')

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
        # 提交门禁**只卡体温**这两项，其它检查项（常见病 / 传染病 / 皮肤黏膜）
        # 允许全空 —— 业务 2026-10-19 确认的口径。
        # 为什么不给它们加「必须检完才能办结」：加门禁的结果不会是「都检了」，
        # 而是「都填阴性」—— 假阴性比空白更糟，它会进入疫病监测的统计。
        # 体温不一样：数值是客观读数、后续（发烧判定、趋势）都要用它，
        # 空着会让下游算错，所以只有这一项卡死。
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

    # 绝育项目**没有处方也没有芯片号**，`has_rx` 永远是假 —— 按「三样全空就跳过」
    # 的规则它会被静默丢掉，而 `save_sterilization()` 随即以 `has_item=False`
    # 把刚填好的六列清空。表现为「病历填完、保存成功、再打开是空的」，
    # 打印页也印不出手术信息（第四十七轮实测）。所以「病历填了」本身要算「要做」。
    ster_body = body.get('sterilization')
    record_keys = {'sterilization'} if sterilization_draft_filled(ster_body) else set()

    if items:
        _replace_items(treatment, items, hospital, record_keys=record_keys)

    # 绝育病历：必须在 `_replace_items()` **之后**判 —— 「本单有没有勾绝育」
    # 要以替换后的子表为准，否则先撤勾再保存会漏清空旧病历。
    has_ster = treatment.items.filter(item_key='sterilization').exists()
    updated.extend(save_sterilization(treatment, has_ster, body,
                                      allow_blank=True))

    if updated:
        treatment.save(update_fields=sorted(set(updated)))
    sync_legacy_item_flags(treatment)
    return updated


def save_sterilization(treatment, has_item, body, allow_blank=False):
    """把绝育手术病历写进诊疗单的六个旧口径列。

    ⚠ 第四十七轮起这是**唯一**的写入路径：旧的「快速登记」整块删掉了，
    病历入口并到了三步流程的「绝育」项目卡片里。

    三条规则，都不报错的那种错最需要注意：
    1. **交了病历就必须交全**（0/6 放行、6/6 放行、3/6 拒绝）—— 半途病历
      落库等于「绝育手术：是、手术日期：—」的归档单据。
    2. **没勾绝育就清空病历**。留着的话会出现「没勾绝育却印得出手术日期」
       的单子，而打印页与政府端详情都在读这几列。
    3. `allow_blank=True`（第五十一轮，只给「医嘱与处方」这一步用）：
       病历**整体为空**时直接放过、六列**原样不动**。
       理由是一条时间顺序问题 —— 医生在「医嘱与处方」阶段只**安排**手术，
       主刀/麻醉/手术过程/术后恢复这四栏在**手术做完之前根本不存在**，
       硬要当时填全就只能编一份假病历（而假病历比空白更糟：它进了归档、
       进了政府端统计）。真正的「必须填全」挪到**执行绝育项目**那一刻
       （见 `execute_item()` 的 `needs_record` 闸门）—— 那时手术已完成，
       六栏都填得出来，而「诊疗完成」又必然要求该项目已执行。

    :param has_item: 本单是否勾选了「绝育」处置项目（按替换后的子表判）
    :param allow_blank: 见规则 3；默认 False = 交了就须交全且不能整体为空
    :return: 被更新的**列名**列表（可直接并入 `update_fields`）
    """
    ster = body.get('sterilization')
    if ster is None:
        ster = {}
    if not isinstance(ster, dict):
        raise ValueError('绝育病历格式不正确')

    if not has_item:
        cleared = []
        for _, col in STERILIZATION_FIELDS:
            old = getattr(treatment, col)
            if col.endswith('_date'):
                blank = None
            else:
                blank = ''
            if old != blank:
                setattr(treatment, col, blank)
                cleared.append(col)
        return cleared

    if allow_blank and not sterilization_draft_filled(ster):
        return []

    values = {}
    for key in STERILIZATION_REQUIRED:
        val = str(ster.get(key) or '').strip()
        if not val:
            raise ValueError(f'绝育病历「{STERILIZATION_LABELS[key]}」不能为空')
        if len(val) > STERILIZATION_TEXT_MAX:
            raise ValueError(
                f'绝育病历「{STERILIZATION_LABELS[key]}」不能超过 '
                f'{STERILIZATION_TEXT_MAX} 字')
        values[key] = val

    anesthesia = str(ster.get('anesthesia') or '').strip()
    if anesthesia not in ANESTHESIA_CHOICES:
        raise ValueError('麻醉方式取值不正确')
    values['anesthesia'] = anesthesia

    raw_date = str(ster.get('surgery_date') or '').strip()
    if not raw_date:
        raise ValueError('绝育病历「手术日期」不能为空')
    values['surgery_date'] = _parse_date(raw_date)

    updated = []
    for key, col in STERILIZATION_FIELDS:
        val = values[key]
        if getattr(treatment, col) != val:
            setattr(treatment, col, val)
            updated.append(col)
    return updated


def submit_sterilization(treatment, body):
    """单独提交绝育手术病历 —— 术后补录 / 执行阶段填写的入口。

    ⚠ 为什么必须有这个入口（第五十一轮）：`save_plan()` 在**已诊断**时一律拒绝
    （否则前端漏禁用就能改处方、而执行阶段按处方扣库存，账实不符且不报错）。
    病历的填写时机恰恰在手术之后 = 已诊断之后，所以「医嘱与处方」那条路
    对它来说是关着的 —— 界面上按钮写着「查看 / 修改病历」，点完却提交不了，
    等于「手术做完了也没法把病历记进系统」，只能撤回诊断（会把已执行项目一起打乱）。

    校验一律走 `save_sterilization()` 那一份判据，不在这里再写一遍。

    :param body: **就是病历本身**（六栏平铺），不再套一层 `sterilization` ——
        这条接口的整个请求体只可能是病历，两种形状只会让前端发错包。
    """
    if treatment is None:
        raise ValueError('诊疗单不存在')
    if treatment.disposed_at:
        raise ValueError('本单已办结，不能再补录手术病历')
    if not treatment.items.filter(item_key='sterilization').exists():
        raise ValueError('本单没有「绝育」处置项目，不需要手术病历')

    updated = save_sterilization(treatment, True, {'sterilization': body})
    if updated:
        treatment.save(update_fields=sorted(set(updated)))
    return updated


def _replace_items(treatment, items, hospital, record_keys=()):
    """全量替换该诊疗单的处置项目与处方（草稿语义：每次保存覆盖）。

    :param record_keys: 「靠病历而不是处方表达」的项目 key（当前只有绝育）。
        这些项目只要病历填了就该落库 —— 它们没有处方，按 `has_rx` 判会被
        静默跳过，随后 `save_sterilization()` 又会把病历清空。

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
        # ⚠ 元素形态同样要守卫（与旧「快速登记」的 `material_id` 同族）：
        # `{"rx": [[1,2,3]]}` / `{"rx": ["abc"]}` 会让
        # `Material.objects.filter(pk=<值>)` 抛 `TypeError`/`ValueError`
        # → 500；超长数字串则会带着 MySQL "Out of range" 一路炸到 500。
        # 旧接口那条测试（`test_nested_material_id_malformed_is_rejected`）
        # 随接口一起下线，判据必须搬到这里 —— 否则同类坑在新路径上复活。
        # 判据不另写一份：直接走 `parse_int_param()`（三态语义同源）。
        for pos, mid in enumerate(rx_ids, 1):
            value, err = parse_int_param(mid, label='处方药品ID')
            if err or value is None:
                raise ValueError(err or '处方药品ID格式不正确')
        rx_other = str(raw.get('rx_other') or '').strip()[:500]
        chip_no = str(raw.get('chip_no') or '').strip()
        if len(rx_ids) > MAX_RX_ITEMS:
            raise ValueError(f'单个处置项目的处方最多 {MAX_RX_ITEMS} 条')

        planned_date = _parse_date(raw.get('planned_date'))
        has_rx = bool(rx_ids) or bool(rx_other) or bool(chip_no)
        # 「病历类」项目（当前只有绝育）没有处方、也没有芯片号，
        # 「要做」有两种表达（第五十一轮）：
        #  ① 填了手术病历 —— 旧口径，留着给 admin / 历史导入的半成品；
        #  ② 只排了预计执行时间 —— 新口径，计划阶段的标准用法：
        #     医生此刻决定的是「这台手术要做」，病历要等术后才写得出来。
        # 少了 ②，「勾绝育」在计划阶段就只剩「当场编一份病历」这一条路。
        has_record = key in record_keys or (
            planned_date is not None
            and _item_meta(key).get('needs_record'))

        # 芯片项目：**填了预计执行时间就必须填芯片号**。
        # 只填时间不填号的话 `has_rx` 为假、整项会被下面静默跳过 ——
        # 用户以为在植芯片，实际什么都没记，而界面上不会报任何错。
        if key == 'chip' and planned_date is not None and not chip_no:
            raise ValueError('芯片处置必须填写芯片号')

        if not has_rx and not has_record:
            continue

        # 原型 `validatePlan()`：**有处方但没日期** = 待补全，拦住不让往下走。
        # 这是「时间与处方互为必填」，不是「日期必填」—— 没处方就不要求日期；
        # 只有病历（绝育）的项目也不要求「预计执行时间」，手术日期在病历里。
        if has_rx and planned_date is None:
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

    # 病历类项目（绝育）：**手术做完了才允许确认执行**，所以病历必须已经落库。
    # 第五十一轮把这道闸从「医嘱与处方」挪到这里 —— 计划阶段医生只是在排手术，
    # 主刀 / 麻醉 / 过程 / 恢复那时候根本还不存在；原先在那一步硬要求填全，
    # 实际结果只能是术前编一份假病历（假病历会进归档、进政府端统计，
    # 而空白不会 —— 它一眼就看得出没填）。
    # 「诊疗完成」不必再加一遍：它本来就要求所有必执行项目先执行完。
    if _item_meta(item.item_key).get('needs_record'):
        missing = sterilization_record_missing(treatment)
        if missing:
            raise ValueError('绝育手术病历不完整，请补全：' + '、'.join(missing))

    # 芯片项目：号码必须有，且**在这里就把行取出来** —— 落库段不再做
    # 可能失败的查询（「写库后还可能 raise」会留半截子）。
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
        # 有效期判据（第四十七轮补）：芯片走「芯片号」而不是处方，开方阶段
        # 看不到它的物料，所以这道闸只能落在执行时。旧「快速登记」在落库前
        # 一次性判过 `chip_material` 的有效期，接口下线后判据必须搬到这里 ——
        # 否则「过期芯片物料照植」在新路径上复活，且不报错。
        err = expired_material_error(chip_row.material, '用于诊疗',
                                     institution=hospital)
        if err:
            raise ValueError(err)
        chip_stock = get_hospital_stock(chip_row.material, hospital)
        if chip_stock < 1:
            raise ValueError(
                f'芯片物料「{chip_row.material.name}」库存不足（当前 {chip_stock}）')

    # 芯片的四种状态（可用 / 在途 / 已使用 / 已作废）与归属校验都在
    # `use_chip()` 里且**只有那一份**，这里再写一遍就是孪生判据。

    # ---- 第二段：落库 ----
    # ⚠ 先抢项目，再扣库存（第五十二轮）。上面那句 `if item.is_done` 是**读**：
    #   两个人同开一张单、或手机端双击/卡顿重试「治疗完成」时，两笔请求都从
    #   `is_done=False` 出发、双双通过校验，于是**同一张处方扣两遍库存、生成两条
    #   CON 流水，两次都返回成功**。账实不符只能等月末对账才发现 —— 这类缺陷
    #   不报错，所以判据必须写在同一条 UPDATE 的 WHERE 里：只有把 `is_done`
    #   从 False 抢成 True 的那一方继续往下走，抢输的一方 raise 让整块回滚
    #   （调用方 `views_treatment.py:471` 已经把本函数包在 `atomic()` 里）。
    #   不用 `select_for_update()`：开发/测试库是 sqlite，Django 在该后端会
    #   **静默丢弃** FOR UPDATE 子句（`features.has_select_for_update` 为 False），
    #   锁只在生产 MySQL 生效、用例一行都覆盖不到（与 `use_chip()` 同族论证）。
    done_at = timezone.now()
    if not TreatmentItem.objects.filter(
            pk=item.pk, is_done=False).update(is_done=True, done_at=done_at):
        raise ValueError(
            f'{item.get_item_key_display()}刚刚被他人执行，请刷新后查看')
    item.is_done = True
    item.done_at = done_at

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

    # `is_done` / `done_at` 已在第二段开头由那条 CAS 写库，这里**不再 save**：
    # 再写一次等于用一个新的 `done_at` 覆盖刚抢到的那一版。
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

    # ⚠ 与 `execute_item()` 对称的并发闸门（第五十二轮）：上面那句 `if not
    #   item.is_done` 是**读**。两人同点「撤销」（或撤销与执行交错）时，两笔都
    #   通过判据、各自把药品数量**加回一遍** —— 库存凭空翻倍，且两次都返回成功。
    #   判据压在条件更新上：抢下「已执行 → 未执行」这一变的人才有资格退库存，
    #   抢输的一方 raise，整块回滚（调用方 `views_treatment.py:495` 的 `atomic()`）。
    if not TreatmentItem.objects.filter(
            pk=item.pk, is_done=True).update(is_done=False, done_at=None):
        raise ValueError(
            f'{item.get_item_key_display()}刚刚被他人处理，请刷新后重试')
    item.is_done = False
    item.done_at = None

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

    # `is_done` / `done_at` 已由开头那条 CAS 写库，这里不再 save。
    return True


def _release_chip(chip_no, pet):
    """把芯片退回「可用」（撤销植入时用）。

    ⚠ 只在「这颗芯片确实挂在本宠物名下」时退回：若中途被别的诊疗单
    重新用掉（`chip.pet` 已经不是它了），退回会把别人正在用的芯片
    变成「可用」，随后被第三只动物植走 —— 一颗芯片两条档案，且不报错。

    ⚠ 这道判据必须写在**同一条 UPDATE 的 WHERE 里**（第五十一轮）。
    原先「读出来判一下、再 `chip.save()`」在撤销与另一笔植入并发时，
    两半都会通过各自的判据、后写的把先写的覆盖掉，于是上面那段注释
    描述的事故照样发生 —— 判据只是**看起来**存在。
    `pet_id` 一并进 WHERE 而不是只判 `status`：存量行的 `pet` 可能是 NULL
    （宠物档案被删过，`on_delete=SET_NULL`），那种仍然允许退回，与旧行为一致。
    """
    chip = Chip.objects.filter(number=chip_no).first()
    if chip is None:
        return False
    if chip.pet_id is not None and chip.pet_id != pet.id:
        raise ValueError(f'芯片 {chip_no} 已绑定其它动物档案，不能退回')

    released = Chip.objects.filter(
        number=chip_no, status=chip.status, pet_id=chip.pet_id).update(
        status='available', pet=None, used_at=None)
    if not released:
        raise ValueError(f'芯片 {chip_no} 刚刚被他人改动，请刷新后重试')

    if pet.chip_no == chip_no:
        pet.chip_no = ''
        pet.save(update_fields=['chip_no'])
    return True


def mark_done(treatment, disposal_advised=''):
    """「诊疗完成」—— 落 `done_at`，宠物进入「待出院」。

    :param disposal_advised: 去向**建议**（`Treatment.DISPOSAL_CHOICES` 的 key）。
        可空 —— 医生没拿准时可以先不选，待出院弹窗会按此建议预选去向。
        它只写 `disposal_advised`：此刻动物还在院，六个出院单据都开得出来，
        一旦顺手写了 `disposal`（最终去向 = 已办结），单据全部会 400。
    :raises ValueError: 还有必执行项目没做完 / 建议值不合法
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

    advised = (disposal_advised or '').strip()
    if advised and advised not in dict(Treatment.DISPOSAL_CHOICES):
        raise ValueError(f'去向建议「{advised}」不合法')

    treatment.done_at = timezone.now()
    treatment.disposal_advised = advised
    treatment.save(update_fields=['done_at', 'disposal_advised'])
    return treatment


def dispose(treatment, disposal, user, defer_pet_status=False, promote_hall=True):
    """办结 —— 记录去向并改动物状态。

    :param promote_hall: 是否为「领养」把动物上架领养大厅。出院单据接管时
        置 False —— 领养出库完成时动物已 `adopted`，再按 `pending_adopt`
        激活大厅，会把已领养的动物重新挂回大厅。

    :param defer_pet_status: 由**出院单据**接管动物状态时置 True。
        放归是两段（转运回小区 → 放归回捕捉地），办结时动物还在本院待转运，
        把 `Pet.status` 直接写成 `released` 会让「还在车上」的动物在台账上
        已经「已放归」，而且出院单据的后续确认会找不到可操作的对象。
        其余去向（领养 / 死亡 / 安乐死 / 领回）单据本身就是终态，不必 defer。

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
    if not defer_pet_status:
        pet.status = disposal
        pet.save(update_fields=['status'])

    # 「领养」必须**当场**进领养大厅（第四十七轮）。
    # 旧口径靠 `auto_promote_to_adoptable()` 在 5 天后补这一步，而它的判据是
    # 「`Treatment.status='completed'` 且 `Pet.status='in_treatment'`」——
    # 新流程这两件事是**同一条 UPDATE 里同时发生**的，那个中间态再也不存在，
    # 定时任务永远命中不到。不在这里补，动物会变成「状态待领养、大厅里查不到」
    # 的幽灵（界面上办结提示还写着「信息进入领养大厅」，承诺与事实不符）。
    if promote_hall and disposal == 'pending_adopt' and not pet.is_deleted:
        listing = AdoptionHallListing.objects.filter(pet=pet).first()
        if listing is None:
            AdoptionHallListing.objects.create(
                pet=pet,
                hospital=pet.hospital,
                hospital_name=pet.hospital.name if pet.hospital else '',
                is_active=True,
                published_at=timezone.localdate(),
            )
        else:
            # 已有记录时**显式置回 is_active**：领养完成/撤销时记录会被下架，
            # 单靠 `get_or_create` 不会重新激活它。
            listing.is_active = True
            if pet.hospital_id and listing.hospital_id != pet.hospital_id:
                listing.hospital = pet.hospital
                listing.hospital_name = pet.hospital.name
            listing.save(update_fields=['is_active', 'hospital', 'hospital_name'])
    return treatment


def dispose_for_discharge(pet, disposal, user):
    """出院单据**真正完成**时，办结该动物所有「诊疗完成、尚未办结」的诊疗单。

    第五十六轮口径：诊疗页选的是「建议去向」，只有出院管理里的终态动作
    （领养出库 / 死亡出库单提交 / 运回单对方签收 / 主人领回）才说明动物
    离院。那些接口在写完自己的单据后调用本函数，`disposed_at` 才落。

    与 `dispose()` 直接办结的区别：**不改宠物状态、不进领养大厅** ——
    两者都由出院单据自己负责，这里只补诊疗单的办结时间与去向。
    """
    if pet is None:
        return []
    treatments = list(Treatment.objects.filter(
        pet=pet, done_at__isnull=False, disposed_at__isnull=True,
    ))
    for treatment in treatments:
        dispose(treatment, disposal, user,
                defer_pet_status=True, promote_hall=False)
    return treatments


# ============================================
# 派生 / 辅助
# ============================================
def item_is_must(item):
    """该项目是否「必执行」—— 有处方 / 手写处方 / 芯片号 就是。

    ⚠ 落库时 `_replace_items()` 已经把「三样全空」的项目过滤掉了，
    所以理论上表里的每一行都是必执行的。这个函数仍然显式判一遍：
    数据可能来自 admin 或历史导入，判据不该依赖「写入侧已经过滤过」。

    第四十七轮补充：**病历类项目（绝育）没有处方也是必执行**。它的「要做」
    由病历表达 —— 勾了绝育并填完病历，就必须在执行阶段确认做完，
    否则「诊疗完成」时这台手术既没执行记录、也没扣任何库存，
    单子却已经办结。前端 `txItemState().must` 同口径。
    """
    if item is None:
        return False
    if _item_meta(item.item_key).get('needs_record'):
        return True
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
