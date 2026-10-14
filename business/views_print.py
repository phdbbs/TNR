"""统一单据打印（第四十六轮：全局单据化）。

每类业务单据提供 A4 打印视图 `/print/<doc>/<pk>/`：
- 顶部机构抬头 + 单据名称 + **单据号大字**（CAP2600001 式）+ 溯源二维码；
- 中部按各单据字段清单排版的正文；
- 底部**关联单据链**（捕捉单 → 转运单 → 领回/放归单 …，纸质归档时单据互见）
  + 经办人签字留白栏（打印后手签）+ 打印时间 / 打印人。

⚠ **路径不在 `/api/` 下**（第四十六轮末修正）。这个视图返回的是**整页 HTML**，
而 `/api/` 的契约是「一律 JSON 信封」（见 `MEMORY.md`：`/api/` 下必须用 `json_ok`；
`core/tests.py` 的 `AllApiRoutesContractTest` 会**枚举**每条 `/api/` 路由并断言
`Content-Type: application/json`）。挂在 `/api/business/print/` 下时，
那条闸门立刻报 `print/1/1/ 404 text/html` —— **闸门报的是真问题**：
路由违反了自己命名空间的契约。所以改的是路由，不是闸门。

权限：页面语义 —— 未登录走 `login_required` 的 302 跳登录页（而不是 JSON 401），
角色限 `shelter / hospital / gov_city / gov_district`（有打印入口的四端）。
取数按区县隔离，**跨区县与不存在完全不可区分**（同一个 404，不暴露存在性）。
"""
import qrcode
import base64
import io
import json
from datetime import date, datetime

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from business.models import (
    Capture, Transfer, OwnerReturn, Release, Adoption, Euthanasia,
    Treatment, MaterialTransaction,
)
from business.services import get_scoped_object
from business import services_treatment as st
from accounts.decorators import role_required

# 纸张来源提示：统一 A4 竖版
# ------------------------------------------------------------------
# 字段清单：(标签, 取值函数)。取值函数收到单据对象，返回字符串。


def _v(attr, fmt=None):
    """取值函数：空值统一 `—`，日期时间统一**本地时区 + 无微秒**。

    ⚠ 直接 `str(value)` 会把 `created_at` 打成
    `2026-09-29 08:51:28.922381+00:00` —— 打印到纸面上就是
    「UTC 时间 + 6 位微秒」，既看不懂也不合规。日期字段同理，
    统一走 `YYYY-MM-DD`，与系统其它台账口径一致。
    """
    def get(obj):
        val = getattr(obj, attr, None)
        if val in (None, ''):
            return '—'
        if fmt == 'bool':
            return '是' if val else '否'
        if isinstance(val, datetime):
            return timezone.localtime(val).strftime('%Y-%m-%d %H:%M')
        if isinstance(val, date):
            return val.strftime('%Y-%m-%d')
        return str(val)
    return get


def _district_name(obj):
    try:
        return obj.district.name
    except Exception:
        return '—'


def _choice(attr):
    def get(obj):
        val = getattr(obj, attr, None)
        if val in (None, ''):
            return '—'
        getter = getattr(obj, f'get_{attr}_display', None)
        return getter() if getter else str(val)
    return get


CAPTURE_FIELDS = [
    ('单据号', _v('ledger_no')), ('所属区县', _district_name),
    ('捕捉点', _v('shelter_name')), ('小区名称', _v('community_name')),
    ('捕捉地址', _v('address')), ('动物数量', _v('pet_count')),
    ('动物编号', _v('pet_codes')), ('联系人', _v('contact_person')),
    ('联系电话', _v('contact_phone')), ('操作员', _v('operator_name')),
    ('状态', _choice('status')), ('登记时间', _v('created_at')),
]
TRANSFER_FIELDS = [
    ('单据号', _v('ledger_no')), ('所属区县', _district_name),
    ('发出捕捉点', _v('from_shelter_name')), ('接收医院', _v('to_hospital_name')),
    ('动物数量', _v('pet_count')), ('动物编号', _v('pet_codes')),
    ('状态', _choice('status')), ('签收日期', _v('received_at', 'date')),
    ('备注', _v('note')), ('操作员', _v('operator_name')),
    ('创建时间', _v('created_at')),
]
CLAIM_FIELDS = [
    ('单据号', _v('ledger_no')), ('所属区县', _district_name),
    ('动物编号', _v('pet_code')), ('主人姓名', _v('owner_name')),
    ('联系电话', _v('owner_phone')), ('身份证号', _v('owner_id_card')),
    ('住址', _v('owner_address')), ('领回原因', _v('reason')),
    ('领回时间', _v('return_time', 'date')), ('操作员', _v('operator_name')),
]
RELEASE_FIELDS = [
    ('单据号', _v('ledger_no')), ('所属区县', _district_name),
    ('动物编号', _v('pet_code')), ('放归小区', _v('community_name')),
    ('接收人', _v('receiver_name')), ('接收人电话', _v('receiver_phone')),
    ('放归日期', _v('released_at', 'date')), ('状态', _choice('status')),
    ('操作员', _v('operator_name')),
]
ADOPTION_FIELDS = [
    ('单据号', _v('ledger_no')), ('所属区县', _district_name),
    ('动物编号', _v('pet_code')), ('领养人', _v('adopter_name')),
    ('联系电话', _v('adopter_phone')), ('身份证号', _v('adopter_id_card')),
    ('住址', _v('adopter_address')), ('领养医院', _v('hospital_name')),
    ('领养日期', _v('adopted_at', 'date')), ('状态', _choice('status')),
    ('操作员', _v('operator_name')),
]
EUTH_FIELDS = [
    ('单据号', _v('ledger_no')), ('所属区县', _district_name),
    ('动物编号', _v('pet_code')), ('医院', _v('hospital_name')),
    ('安乐死原因', _v('reason')), ('动物状况', _v('condition')),
    ('安乐死日期', _v('euthanized_at', 'date')),
    ('遗体领取', _v('body_received', 'bool')),
    ('领取日期', _v('body_received_at', 'date')),
    ('领取人', _v('body_received_by_name')), ('操作员', _v('operator_name')),
]
TREATMENT_FIELDS = [
    ('单据号', _v('ledger_no')), ('所属区县', _district_name),
    ('动物编号', _v('pet_code')), ('医院', _v('hospital_name')),
    ('绝育手术', _v('items_sterilization', 'bool')),
    ('手术日期', _v('sterilization_surgery_date', 'date')),
    ('主刀医生', _v('sterilization_surgeon')),
    ('麻醉方式', _v('sterilization_anesthesia')),
    ('诊断', _v('sterilization_diagnosis')),
    ('疫苗', _v('items_vaccine', 'bool')), ('疫苗类型', _v('vaccine_type')),
    ('疫苗批号', _v('vaccine_batch_no')), ('疫苗日期', _v('vaccine_date', 'date')),
    ('驱虫', _v('items_deworming', 'bool')), ('驱虫类型', _v('deworming_type')),
    ('驱虫批号', _v('deworming_batch_no')), ('驱虫日期', _v('deworming_date', 'date')),
    ('芯片号', _v('chip_no')), ('植入日期', _v('chip_date', 'date')),
    ('状态', _choice('status')), ('操作员', _v('operator_name')),
]

def _infectious_text(obj):
    """传染病检查的 JSON 快照 → 一行可读文本（如「猫瘟 FPV:阴性　猫鼻支 FHV:未检」）。

    ⚠ 标签表从 `services_treatment.INFECTIOUS_TESTS` 取，**不在这里再写一份** ——
      打印页与前端各写一份的话，加了新病种就会出现「界面有、打印没有」，
      而两边都不会报错。
    """
    raw = (getattr(obj, 'infectious_tests', '') or '').strip()
    if not raw:
        return '—'
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return '—'
    if not isinstance(data, dict):
        return '—'
    species = getattr(getattr(obj, 'pet', None), 'species', '') or ''
    label_of = dict(st.INFECTIOUS_TESTS.get(species, []))
    result_of = dict(st.TEST_RESULTS)
    parts = [f'{label_of.get(k, k)}:{result_of.get(v, v)}' for k, v in data.items()]
    return '　'.join(parts) or '—'


def _items_text(obj):
    """处置项目 + 处方 → 多行文本（每项一行，处方缩进跟在下面）。

    返回的字符串里带 `\\n`，由模板的 `|linebreaksbr` 转成换行 ——
    直接塞进 `{{ value }}` 的话换行会被 HTML 折叠成空格，几项处方挤成一行。
    """
    lines = []
    for item in obj.items.all():
        name = item.item_name or item.get_item_key_display()
        head = name
        if item.planned_date:
            head += f'（{item.planned_date:%Y-%m-%d}）'
        if item.chip_no:
            head += f' 芯片 {item.chip_no}'
        head += '【已执行】' if item.is_done else '【未执行】'
        lines.append(head)

        rx = list(item.rx_rows.all())
        if rx:
            lines.append('　处方：' + '、'.join(
                f'{r.material_name} ×{r.quantity}{r.unit or ""}' for r in rx))
        if item.rx_other:
            lines.append('　手写：' + item.rx_other)
        if item.notes:
            lines.append('　注意：' + item.notes)
        if item.vet_name or item.vet_license:
            lines.append(f'　操作人：{item.vet_name} {item.vet_license}'.rstrip())
    return '\n'.join(lines) or '—'


def _treatment_fields(obj):
    """诊疗单打印字段 —— **两代字段都打**。

    旧代（Task 6 口径）是 `TREATMENT_FIELDS` 里那批明细列；
    新代（第四十六轮 v5.1 三步流程）是体检 / 医嘱 / 签名 / 办结 + 子表。

    ⚠ 为什么两代都要：历史单子是旧口径建的（新列全空），新单子走三步流程
      （旧明细列不写）。只打一代的话，另一半单子打出来是**一片空白**，
      而打印页不会报任何错 —— 归档时才发现纸面上什么都没印。
    """
    rows = [(label, getter(obj)) for label, getter in TREATMENT_FIELDS]
    rows.extend([
        ('体温(℃)', lambda o: '—' if o.temperature is None else f'{o.temperature}'),
        ('体温判定', _choice('temperature_flag')),
        ('常见病检查', _v('common_findings')),
        ('常见病其他', _v('common_other')),
        ('传染病检查', _infectious_text),
        ('皮肤黏膜', _v('skin_mucosa')),
        ('皮肤黏膜其他', _v('skin_other')),
        ('其它检查', _v('check_note')),
        ('医嘱', _v('advice')),
        ('兽医', _v('vet_name')), ('执业证号', _v('vet_license')),
        ('电子章', _v('hospital_seal')),
        ('办结去向', _choice('disposal')),
        ('检查提交', _v('checked_at')), ('诊断完成', _v('diagnosed_at')),
        ('诊疗完成', _v('done_at')), ('办结时间', _v('disposed_at')),
        ('处置项目', _items_text),
    ])
    return rows


MT_TYPE_TITLE = {
    'purchase': '物料采购入库单', 'dispatch': '物料下发单',
    'receive': '物料接收单', 'adjustment': '物料异动单', 'consume': '物料消耗记录',
}


def _mt_title(obj):
    return MT_TYPE_TITLE.get(obj.type, '物料单据')


def _mt_fields(obj):
    base = [
        ('单据号', _v('ledger_no')), ('所属区县', _district_name),
        ('物料名称', _v('material_name')), ('类别/规格', _v('unit')),
        ('数量', _v('quantity')), ('批次号', _v('batch_no')),
        ('供应商', _v('supplier')), ('往来单位', _v('from_to')),
        ('操作员', _v('operator_name')), ('日期', _v('date', 'date')),
        ('备注', _v('note')),
    ]
    if obj.type == 'receive':
        base.insert(1, ('关联下发单号', _v('ref_no')))
    return base


# ------------------------------------------------------------------
# 单据注册表：doc key → (模型, 标题, 字段清单, 关联链函数)
#
# ⚠ 这里**没有** `fetcher`（取数函数）。原先每个条目都挂了一个
#   `lambda pk: Model.objects.get(pk=pk)`，改成 `get_scoped_object()`
#   之后**一次都没被读过** —— 8 个 lambda 全是死代码，而且它们用的是
#   **不带区县隔离**的裸 `objects.get()`：哪天有人顺手改成 `spec['fetcher'](pk)`
#   就会静默绕过隔离。所以直接删掉，只留 `model` 交给真源取单。
# ------------------------------------------------------------------
# 宠物名下单据：反向关系名 → (标题, doc key)
PET_DOC_RELATIONS = [
    ('treatments', '诊疗单', 'treatment'),
    ('owner_returns', '领回单', 'owner_return'),
    ('releases', '放归单', 'release'),
    ('adoptions', '领养单', 'adoption'),
    ('euthanasia_records', '安乐死单', 'euthanasia'),
]


def _pet_chain(pet, exclude_pk=None):
    """某只宠物的全生命周期单据链（捕捉单 + 各类后续单据）。

    ⚠ 这里埋过两个坑，症状都是「**静默少列单据**」而不是报错：

    1. **入参是「宠物」不是「单据」。** 上一版写成 `getattr(obj, 'pet', None)`，
       从捕捉单侧调用时传进来的是 `Pet`，`pet.pet` 不存在 → 永远拿到 None
       → **关联链恒为空**。打印出来的单子上一条关联单据都没有，也不报错，
       只有断言「页面里有『关联单据』」的测试能抓到。
    2. **`Transfer` 没有 `pet` 外键** —— 它靠 `pet_codes` 逗号串关联，
       所以 `Pet.transfers` 这个反向关系**根本不存在**。上一版用
       `getattr(pet, rel)` 不带默认值直接取，一旦真跑起来就是 AttributeError；
       只是因为坑 1 提前 return，一直没暴露。

       这里改为按 `pet_codes` 精确匹配：先用 `__contains` 粗筛（能用上索引），
       再逐条核对逗号切分后的完整编号 —— 避免 `TP001` 误配到 `TP0011`。
    """
    chain = []
    if pet.capture_id and pet.capture:
        chain.append(('捕捉单', pet.capture.ledger_no, 'capture', pet.capture_id))

    for rel, title, key in PET_DOC_RELATIONS:
        manager = getattr(pet, rel, None)   # 缺关系名要跳过，不能 AttributeError
        if manager is None:
            continue
        for row in manager.order_by('id'):
            if row.pk == exclude_pk:
                continue
            if getattr(row, 'ledger_no', ''):
                chain.append((title, row.ledger_no, key, row.pk))

    from business.models import Transfer
    for t in Transfer.objects.filter(pet_codes__contains=pet.code).order_by('id'):
        codes = [c.strip() for c in (t.pet_codes or '').split(',') if c.strip()]
        if pet.code in codes and getattr(t, 'ledger_no', ''):
            chain.append(('转运单', t.ledger_no, 'transfer', t.pk))
    return chain


def _get_pet_related(obj):
    """**单据**侧的关联链：同宠的全生命周期单据（含捕捉单，不含自己）。"""
    pet = getattr(obj, 'pet', None)
    if pet is None:
        return []
    return _pet_chain(pet, exclude_pk=obj.pk)


def _capture_related(obj):
    """捕捉单的关联链：它名下动物的所有后续单据（不含捕捉单自身）。"""
    from business.models import Pet
    chain = []
    codes = [c.strip() for c in (obj.pet_codes or '').split(',') if c.strip()]
    seen = set()
    for pet in Pet.objects.filter(code__in=codes).select_related('capture'):
        for title, no, key, pk in _pet_chain(pet):
            if key == 'capture':
                continue          # 捕捉单自己不列进自己的关联链
            if (key, pk) not in seen:
                seen.add((key, pk))
                chain.append((title, no, key, pk))
    return chain


def _transfer_related(obj):
    chain = []
    if obj.capture_id:
        chain.append(('捕捉单', obj.capture.ledger_no, 'capture', obj.capture_id))
    return chain


def _mt_related(obj):
    """物料单据链：接收 ↔ 下发；下发 → 同批签收；采购/异动 → 无。"""
    from business.models import MaterialTransaction as MT
    chain = []
    if obj.type == 'receive' and obj.ref_no:
        target = MT.objects.filter(type='dispatch', ledger_no=obj.ref_no).first()
        if target:
            chain.append(('下发单', target.ledger_no, 'material', target.pk))
    if obj.type == 'dispatch':
        for r in MT.objects.filter(type='receive', ref_no=obj.ledger_no).order_by('id'):
            chain.append(('接收单', r.ledger_no, 'material', r.pk))
    return chain


DOC_SPECS = {
    'capture': {'model': Capture, 'title': '捕捉单', 'fields': CAPTURE_FIELDS,
                'related': _capture_related},
    'transfer': {'model': Transfer, 'title': '转运单', 'fields': TRANSFER_FIELDS,
                 'related': _transfer_related},
    'owner_return': {'model': OwnerReturn, 'title': '领回单', 'fields': CLAIM_FIELDS,
                     'related': _get_pet_related},
    'release': {'model': Release, 'title': '放归单', 'fields': RELEASE_FIELDS,
                'related': _get_pet_related},
    'adoption': {'model': Adoption, 'title': '领养单', 'fields': ADOPTION_FIELDS,
                 'related': _get_pet_related},
    'euthanasia': {'model': Euthanasia, 'title': '安乐死单', 'fields': EUTH_FIELDS,
                   'related': _get_pet_related},
    # ⚠ `fields_fn` 是**按单据对象动态**算字段清单的钩子（诊疗单要读子表，
    #   物料单要按 `type` 决定是否加「关联下发单号」）。静态 `fields` 留给
    #   字段固定的那几类；两者只能有一个非空，`fields_fn` 优先。
    'treatment': {'model': Treatment, 'title': '诊疗单', 'fields': None,
                  'fields_fn': _treatment_fields,
                  'related': _get_pet_related},
    'material': {'model': MaterialTransaction, 'title': None, 'fields': None,
                 'fields_fn': _mt_fields,
                 'related': _mt_related},
}


def _qr_data_uri(url):
    img = qrcode.make(url, box_size=4, border=1)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


@login_required
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
def print_doc(request, doc, pk):
    spec = DOC_SPECS.get(doc)
    if spec is None:
        return render(request, 'print/doc_missing.html', {'doc': doc}, status=404)

    # 区县隔离内取单，走**真源** `get_scoped_object`。
    #
    # ⚠ 这里原先自己写了一遍隔离判断（`gov_city` / `district.is_city` /
    #   `district_id`），那是**第二份实现** —— 项目历史上正因为隔离口径
    #   写了两遍、对「账号没挂区县」解释相反，出现过「列表全空、写接口却
    #   全放行」的静默缺陷（详见 `core/scope.py` 开头）。一律改问真源。
    # ⚠ 「存在但不属于我」与「不存在」必须**完全不可区分** ——
    #   同一个模板、同一个状态码，否则就成了存在性预言机。
    obj = get_scoped_object(spec['model'], pk, request.user)
    if obj is None:
        return render(request, 'print/doc_missing.html', {'doc': doc}, status=404)

    title = spec['title'] or _mt_title(obj)
    fields_fn = spec.get('fields_fn')
    fields = fields_fn(obj) if fields_fn else spec['fields']
    rows = [(label, getter(obj)) for label, getter in fields]
    related = spec['related'](obj)
    print_url = request.build_absolute_uri()
    org = getattr(getattr(request.user, 'institution', None), 'name', '') or '襄阳市流浪动物 TNR 管理平台'
    ctx = {
        'title': title,
        'doc_no': getattr(obj, 'ledger_no', '') or '—',
        'rows': rows,
        'related': related,
        'qr': _qr_data_uri(print_url),
        'print_url': print_url,
        'org': org,
        'printed_by': request.user.get_full_name() or request.user.username,
        'printed_at': timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M'),
    }
    return render(request, 'print/doc.html', ctx)
