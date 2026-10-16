"""芯片接口 —— 「一行 = 一个号码」（第四十六轮新建）。

为什么单开一个文件：芯片原来挂在 `views_material` 里，但第四十六轮之后
它有了自己的模型字段（持有机构 / 所属物料 / 入库批次 / 在途下发单）、
自己的状态机（`available` / `in_transit` / `used`）和自己的服务层
（`services_chip`）。继续挤在物料文件里，只会让「物料的判据」与
「芯片的判据」互相看不见 —— 本项目已经栽过好几次这种形态。

⚠ 这里**只有只读接口**。写路径（入库 / 下发 / 签收）留在
`views_material.purchase_create` / `dispatch_create` / `material_receive`，
因为它们本来就是「一笔物料业务」的两半，拆开反而会让
「库存扣了、芯片没动」这类半截状态重新出现。
"""

from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import Chip, MaterialTransaction
from business.services import (
    body_str, get_district_filtered_queryset, json_fail, json_ok,
    parse_int_param, parse_json_body,
)
from business.services_chip import (
    _duplicate_message, available_chip_segments, available_chip_queryset,
    chip_batch_precheck, find_duplicate_chip_numbers, parse_chip_numbers,
)
from business.views_material import get_operable_material

# 单次返回的芯片上限。存在的意义不是「性能」而是**防静默截断**：
# 截断时会把总数一起返回，前端能看出「只显示了前 N 颗」，
# 而不是以为「本院就这么多芯片」。
MAX_CHIP_ROWS = 2000


def _chip_row(chip):
    """芯片的统一出参形态（驼峰由 `json_ok` 递归补）。"""
    return {
        'number': chip.number,
        'status': chip.status,
        'status_display': chip.get_status_display(),
        'institution_id': chip.institution_id,
        'institution_name': chip.institution.name if chip.institution else '',
        'material_id': chip.material_id,
        'material_name': chip.material.name if chip.material else '',
        'batch_no': chip.batch_no,
        'pet_id': chip.pet_id,
        'pet_code': chip.pet.code if chip.pet else '',
        'used_at': chip.used_at.isoformat() if chip.used_at else '',
        'created_at': chip.created_at.isoformat() if chip.created_at else '',
    }


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def chip_list(request):
    """芯片列表 —— 「一行 = 一个号码」。

    查询参数：

    - `status`：`available` / `in_transit` / `used`。
      非法值一律 400（**不静默忽略** —— 忽略的话前端传错参数会看到
      「全部芯片」，看起来像「筛选没生效」而不是「参数写错了」）。
    - `material_id`：只看某条芯片物料。
    - `keyword`：号码模糊匹配。

    返回 `{'total': N, 'items': [...]}`：`total` 是**过滤后的总数**，
    `items` 最多 `MAX_CHIP_ROWS` 条。给 `total` 是为了让截断**可见** ——
    只返回数组的话，被截断时前端会以为「本院就这么多芯片」。
    """
    user = request.user

    # 区县隔离走声明式路径（`Chip.DISTRICT_LOOKUP = 'institution__district'`）。
    # 少了它，区县角色一访问就 `FieldError` 500 —— 而市级账号走
    # `return all()` 绕过该分支，**只测市级账号永远发现不了**。
    qs = (get_district_filtered_queryset(Chip, user)
          .select_related('institution', 'material', 'pet'))

    # 机构账号默认只看**本机构**的芯片。
    # ⚠ 这是**界面聚焦**，不是安全边界 —— 安全边界是上面那句区县过滤。
    #   少了它，捕捉点操作员会看到同区县别家医院的芯片，
    #   误以为是自己手上的货（本项目的「归属必须从业务对象推导」同理）。
    if user.institution_id and user.role in ('shelter', 'hospital'):
        qs = qs.filter(institution_id=user.institution_id)

    status = (request.GET.get('status') or '').strip()
    if status:
        if status not in dict(Chip.STATUS_CHOICES):
            return json_fail('芯片状态无效')
        qs = qs.filter(status=status)

    material_id, err = parse_int_param(request.GET.get('material_id'), '物料')
    if err:
        return json_fail(err)
    if material_id:
        qs = qs.filter(material_id=material_id)

    keyword = (request.GET.get('keyword') or '').strip()
    if keyword:
        qs = qs.filter(number__icontains=keyword)

    total = qs.count()
    return json_ok({
        'total': total,
        'items': [_chip_row(c) for c in qs[:MAX_CHIP_ROWS]],
    })


@csrf_exempt
@role_required('hospital', 'shelter')
@login_required
def chip_available(request):
    """本机构**可用**的芯片（供诊疗时挑选，v5.1 原型口径）。

    查询参数 `material_id`（可选）：指定物料时只列该物料的，
    不给时列出本院全部可用芯片 —— 与原型「芯片弹层」一致
    （弹层里按号码列出，已绑定的置灰）。

    只返回 `available`：`used` 的已经植出去了、`in_transit` 的还在路上，
    都不该出现在「挑一颗来用」的列表里。
    """
    user = request.user
    institution = user.institution
    if institution is None:
        return json_fail('当前账号未挂机构')

    material_id, err = parse_int_param(request.GET.get('material_id'), '物料')
    if err:
        return json_fail(err)

    material = None
    if material_id:
        # 与下发/消耗同一份「可操作物料」判据（区县内 ∪ 在本机构有库存），
        # 否则会出现「下拉里选得到、挑芯片时 404」的孪生漂移。
        material = get_operable_material(material_id, user)
        if material is None:
            return json_fail('物料不存在或无权访问', status=404)

    qs = available_chip_queryset(institution, material).select_related('material')
    return json_ok([_chip_row(c) for c in qs])


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def chip_preview(request):
    """「首号 + 数量」的只读预检（C1）—— 提交前就知道这批号码能不能用。

    POST 参数：`first_number`、`count`、`purpose`（`purchase` / `dispatch`
    / `void`）、`material_id`（dispatch / void 时必填）。

    ⚠ 预检结果**总是 200**：`ok: false` 是一种正常的预检结果，不是请求
    失败 —— 前端在 oninput 里防抖调用，输入拼写中间态层出不穷，用 400
    语义的话每次都会打一条 Bad Request 日志，把真正的错误淹没。

    虽然是 POST，但**不写任何库表**（与文件头「只有只读接口」的约定
    一致）：POST 只是为了把首号/数量/用途一起带上。
    """
    data = parse_json_body(request)
    user = request.user

    purpose = str(body_str(data, 'purpose') or 'purchase').strip()
    if purpose not in ('purchase', 'dispatch', 'void'):
        return json_fail('预检类型无效')

    first_number = body_str(data, 'first_number')
    count, err = parse_int_param(data.get('count'), '数量', minimum=0)
    if err:
        return json_ok({'ok': False, 'reason': err})

    material = None
    if purpose in ('dispatch', 'void'):
        material_id, m_err = parse_int_param(data.get('material_id'), '物料')
        if m_err:
            return json_ok({'ok': False, 'reason': m_err})
        if not material_id:
            return json_ok({'ok': False, 'reason': '请先选择物料'})
        material = get_operable_material(material_id, user)
        if material is None:
            return json_fail('物料不存在或无权访问', status=404)

    try:
        if purpose == 'purchase':
            numbers = parse_chip_numbers(first_number, count)
            existed = find_duplicate_chip_numbers(numbers)
            if existed:
                return json_ok({
                    'ok': False,
                    'reason': _duplicate_message(existed),
                    'duplicates': existed[:10],
                })
        else:
            institution = user.institution
            if institution is None:
                return json_ok({'ok': False, 'reason': '当前账号未挂机构'})
            # dispatch 与 void 的预检判据相同（存在 / 归属本机构 / 归属该
            # 物料 / 状态可用），与真正落库的 `chip_batch_dispatch` /
            # `chip_batch_void` 走同一个 `chip_batch_precheck` ——
            # 「预检说能用、提交却被拦」就是孪生漂移（本项目反复栽过的形态）。
            numbers = chip_batch_precheck(material, institution,
                                          first_number, count)
    except ValueError as e:
        return json_ok({'ok': False, 'reason': str(e)})

    return json_ok({
        'ok': True,
        'first_number': numbers[0],
        'last_number': numbers[-1],
        'count': len(numbers),
    })


@csrf_exempt
@role_required('shelter', 'hospital')
@login_required
def chip_segments(request):
    """本机构**可用**芯片的连续号段摘要（C2）—— 下发/作废表单里
    「我手上有哪些号」的答案，点一段自动填首号，不用凭记忆翻 Excel。

    查询参数 `material_id`（可选）：只看该物料的。
    """
    user = request.user
    institution = user.institution
    if institution is None:
        return json_fail('当前账号未挂机构')

    material_id, err = parse_int_param(request.GET.get('material_id'), '物料')
    if err:
        return json_fail(err)

    material = None
    if material_id:
        material = get_operable_material(material_id, user)
        if material is None:
            return json_fail('物料不存在或无权访问', status=404)

    segments = available_chip_segments(institution, material)
    return json_ok({
        'total': sum(s['count'] for s in segments),
        'segments': segments,
    })


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def chip_trace(request):
    """单颗芯片的追溯时间线（C3）：采购 → 下发 → 签收 → 植入 / 作废。

    查询参数 `number`：芯片号。

    环节串联方式（`Chip` 行是唯一真源）：

    - 采购：`chip.batch_no` 记的是**采购单号**（`chip_batch_create` 落的），
      反查 `type='purchase'` 的流水拿到日期 / 供应商 / 操作人；
    - 下发：`chip.dispatch_txn` —— 签收后**保留**（见 `chip_batch_receive`
      的注释），指向最后一次下发的流水；
    - 签收：接收单 `ref_no` 指向下发单号（`material_receive` 的关联口径）；
    - 植入：`chip.pet` + `chip.used_at`；
    - 作废：`status='void'`（作废流水不含号码级关联，时间不可考，
      时间线里放在最后并注明）。
    """
    user = request.user
    number = (request.GET.get('number') or '').strip()
    if not number:
        return json_fail('请提供芯片号')

    chip = (get_district_filtered_queryset(Chip, user)
            .select_related('institution', 'material', 'pet', 'dispatch_txn')
            .filter(number=number).first())
    if chip is None:
        return json_fail('芯片不存在或无权访问', status=404)

    events = []

    # 1) 采购入库
    purchase = None
    if chip.batch_no:
        purchase = MaterialTransaction.objects.filter(
            ledger_no=chip.batch_no, type='purchase').first()
    purchase_time = (purchase.date.isoformat() if purchase and purchase.date
                     else chip.created_at.isoformat())
    purchase_detail = f'采购单 {chip.batch_no or "（未记录）"}'
    if purchase and purchase.supplier:
        purchase_detail += f'，供应商 {purchase.supplier}'
    if purchase and purchase.operator_name:
        purchase_detail += f'，操作人 {purchase.operator_name}'
    events.append({'time': purchase_time, 'type': 'purchase',
                   'title': '采购入库', 'detail': purchase_detail})

    # 2) 下发出库 + 3) 签收入库（有下发单才谈得上签收）
    dispatch = chip.dispatch_txn
    if dispatch is not None:
        events.append({
            'time': dispatch.date.isoformat() if dispatch.date else '',
            'type': 'dispatch', 'title': '下发出库',
            'detail': f'下发单 {dispatch.ledger_no}'
                      + (f'，接收方 {dispatch.from_to}' if dispatch.from_to else ''),
        })
        receive = MaterialTransaction.objects.filter(
            ref_no=dispatch.ledger_no, type='receive').first()
        if receive is not None:
            receiver_name = (receive.institution.name if receive.institution_id
                             else (receive.hospital.name if receive.hospital_id else ''))
            events.append({
                'time': receive.date.isoformat() if receive.date else '',
                'type': 'receive', 'title': '签收入库',
                'detail': f'接收单 {receive.ledger_no}'
                          + (f'，{receiver_name} 签收' if receiver_name else ''),
            })

    # 4) 植入使用
    if chip.status == 'used' and chip.pet is not None:
        events.append({
            'time': chip.used_at.isoformat() if chip.used_at else '',
            'type': 'use', 'title': '植入使用',
            'detail': f'动物 {chip.pet.code}（{chip.pet.get_status_display()}）',
        })

    # 5) 作废（无号码级流水关联，时间不可考 —— 排序时放最后）
    if chip.status == 'void':
        events.append({'time': '', 'type': 'void', 'title': '已作废',
                       'detail': '作废流水不含号码级关联，作废时间未记录'})

    events.sort(key=lambda e: e['time'] or '9999')
    return json_ok({'chip': _chip_row(chip), 'events': events})
