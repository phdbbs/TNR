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
from business.models import Chip
from business.services import (
    get_district_filtered_queryset, json_fail, json_ok, parse_int_param,
)
from business.services_chip import available_chip_queryset
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
