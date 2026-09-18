"""
Task 13: 政府监管后端
- 数据大屏统计 / 机构管理 / 区县管理 / 用户管理
- 业务监管 / 物资监管 / 台账中心 / 操作日志 / 系统配置
"""
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Count, Q, Prefetch
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from accounts.models import User
from business.models import (
    Pet, Capture, Transfer, Treatment, Material, MaterialTransaction,
    Release, Adoption, CheckIn, Blacklist, Euthanasia,
)
from business.services import (
    json_ok, json_fail, parse_json_body, serialize_instance,
    get_district_scope, pet_brief, pet_archive_records, with_camel_keys,
)
from core.audit import ACTION_LABELS
from core.models import AuditLog, District, Institution
from .models import SystemConfig


# ============================================
# 辅助：区县范围过滤
# ============================================
def _scope_filter(qs, request, field='district'):
    """根据用户区县范围过滤 QuerySet。

    - gov_city (scope=None): 返回全部
    - gov_district (scope=district_id): 仅返回所属区县
    """
    scope = get_district_scope(request)
    if scope is None:
        return qs
    return qs.filter(**{f'{field}_id': scope})


# ============================================
# 1. 数据大屏统计
# ============================================
@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def dashboard_stats(request):
    """数据大屏聚合统计（按区县范围过滤）"""
    # 业务总量
    # 已逻辑删除（作废）的捕捉单与宠物档案不计入统计
    capture_qs = _scope_filter(Capture.objects.filter(is_deleted=False), request)
    treatment_qs = _scope_filter(Treatment.objects.all(), request)
    release_qs = _scope_filter(Release.objects.all(), request)
    adoption_qs = _scope_filter(Adoption.objects.all(), request)
    euthanasia_qs = _scope_filter(Euthanasia.objects.all(), request)
    pet_qs = _scope_filter(Pet.objects.filter(is_deleted=False), request)

    # 机构数量（按类型）
    inst_qs = _scope_filter(Institution.objects.all(), request)
    institution_counts = {
        'shelter': inst_qs.filter(type='shelter').count(),
        'hospital': inst_qs.filter(type='hospital').count(),
        'community': inst_qs.filter(type='community').count(),
        'total': inst_qs.count(),
    }

    # 物料消耗统计
    txn_qs = _scope_filter(MaterialTransaction.objects.all(), request)
    material_stats = {
        'total_purchased': txn_qs.filter(type='purchase').aggregate(t=Sum('quantity'))['t'] or 0,
        'total_dispatched': txn_qs.filter(type='dispatch').aggregate(t=Sum('quantity'))['t'] or 0,
        'total_received': txn_qs.filter(type='receive').aggregate(t=Sum('quantity'))['t'] or 0,
        'total_consumed': txn_qs.filter(type='consume').aggregate(t=Sum('quantity'))['t'] or 0,
        'total_adjustment': txn_qs.filter(type='adjustment').aggregate(t=Sum('quantity'))['t'] or 0,
    }

    # 宠物状态分布
    status_dist = {}
    for code, _label in Pet.STATUS_CHOICES:
        status_dist[code] = pet_qs.filter(status=code).count()

    data = {
        'capture_total': capture_qs.count(),
        'treatment_total': treatment_qs.count(),
        'release_total': release_qs.count(),
        'adoption_total': adoption_qs.count(),
        'euthanasia_total': euthanasia_qs.count(),
        'pet_total': pet_qs.count(),
        'institution_counts': institution_counts,
        'material_stats': material_stats,
        'pet_status_distribution': status_dist,
    }
    return json_ok(data)


# ============================================
# 2. 机构列表
# ============================================
@csrf_exempt
@login_required
def institution_list(request):
    """机构列表（支持 ?type 过滤，按区县范围过滤）

    基础数据，所有登录用户均可查询（用于下拉选择等）。
    """
    qs = _scope_filter(Institution.objects.all(), request)
    inst_type = request.GET.get('type')
    if inst_type:
        qs = qs.filter(type=inst_type)

    data = []
    for inst in qs:
        item = serialize_instance(inst)
        item['district_name'] = inst.district.name if inst.district else ''
        item['type_display'] = inst.get_type_display()
        data.append(item)
    return json_ok(data)


# ============================================
# 3. 创建机构
# ============================================
def _next_institution_code(inst_type):
    """按 `I###` / `C###` 约定生成下一个机构编号。

    机构编号（`Institution.code`）是**稳定业务键**：去重、引用、种子脚本的
    幂等匹配都靠它，机构名只是展示字段。但 `institution_create` 此前根本不写
    `code` —— 界面上新建的机构编号列永远是空的，而且与 seed 建的那批机构
    不在同一套编号体系里，后续「按 code 匹配」的脚本会把它们当成新机构重复插入。

    前缀与 seed_data 保持一致：小区用 `C`，捕捉点/医院用 `I`。
    """
    prefix = 'C' if inst_type == 'community' else 'I'
    max_no = 0
    for code in Institution.objects.filter(
            code__startswith=prefix).values_list('code', flat=True):
        suffix = (code or '')[len(prefix):]
        if suffix.isdigit():
            max_no = max(max_no, int(suffix))
    return f'{prefix}{max_no + 1:03d}'


@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def institution_create(request):
    """创建机构"""
    data = parse_json_body(request)

    name = (data.get('name') or '').strip()
    if not name:
        return json_fail('机构名称不能为空')
    if len(name) > 100:
        return json_fail('机构名称不能超过100字')

    inst_type = data.get('type')
    if inst_type not in ('shelter', 'hospital', 'community'):
        return json_fail('机构类型无效')

    district_id = data.get('district_id')
    if not district_id:
        scope = get_district_scope(request)
        if scope:
            district_id = scope
    if not district_id:
        return json_fail('缺少区县信息')

    try:
        district = District.objects.get(id=district_id)
    except District.DoesNotExist:
        return json_fail('区县不存在')

    # 医院必须挂具体区县，不能挂市级；捕捉点可以挂市级或区县
    if inst_type == 'hospital' and district.is_city:
        return json_fail('医院必须挂具体区县，不能挂市级')

    phone = (data.get('phone') or '').strip()
    if phone and not phone.replace('-', '').replace('+', '').isdigit():
        return json_fail('联系电话格式不正确')

    inst = Institution.objects.create(
        code=_next_institution_code(inst_type),
        name=name,
        type=inst_type,
        district=district,
        address=(data.get('address') or '').strip(),
        contact=(data.get('contact') or '').strip(),
        phone=phone,
        status='active',
    )
    return json_ok(serialize_instance(inst), message='机构创建成功')


# ============================================
# 4. 编辑机构
# ============================================
@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def institution_edit(request, pk):
    """编辑机构

    与 institution_list 保持一致的区县范围校验：
    列表已按区县过滤，编辑若不做校验，区级管理员猜主键即可改他区机构。
    """
    inst = _scope_filter(Institution.objects.all(), request).filter(id=pk).first()
    if inst is None:
        return json_fail('机构不存在或无权访问', status=404)

    data = parse_json_body(request)
    update_fields = []

    if data.get('name'):
        inst.name = data['name']
        update_fields.append('name')
    if data.get('type') in ('shelter', 'hospital', 'community'):
        inst.type = data['type']
        update_fields.append('type')
    if data.get('district_id'):
        scope = get_district_scope(request)
        if scope is not None and str(data['district_id']) != str(scope):
            return json_fail('无权将机构调整到其他区县')
        try:
            inst.district = District.objects.get(id=data['district_id'])
            update_fields.append('district')
        except District.DoesNotExist:
            return json_fail('区县不存在')
    if 'address' in data:
        inst.address = data['address']
        update_fields.append('address')
    if 'contact' in data:
        inst.contact = data['contact']
        update_fields.append('contact')
    if 'phone' in data:
        inst.phone = data['phone']
        update_fields.append('phone')

    if update_fields:
        inst.save(update_fields=update_fields)

    return json_ok(serialize_instance(inst), message='机构更新成功')


# ============================================
# 5. 机构状态切换
# ============================================
@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def institution_toggle_status(request, pk):
    """切换机构启用/停用状态（按区县范围校验）"""
    inst = _scope_filter(Institution.objects.all(), request).filter(id=pk).first()
    if inst is None:
        return json_fail('机构不存在或无权访问', status=404)

    inst.status = 'inactive' if inst.status == 'active' else 'active'
    inst.save(update_fields=['status'])
    # 审计用上下文：响应体只回 id/status，中间件读不出归属区县
    request.audit_district = inst.district
    return json_ok(
        {'id': inst.id, 'status': inst.status},
        message=f'机构已{"停用" if inst.status == "inactive" else "启用"}'
    )


# ============================================
# 6. 区县列表
# ============================================
@csrf_exempt
@login_required
def district_list(request):
    """区县列表（基础数据，所有登录用户可查询）"""
    qs = District.objects.all()
    data = [serialize_instance(d) for d in qs]
    return json_ok(data)


# ============================================
# 7. 创建区县
# ============================================
@csrf_exempt
@role_required('gov_city')
@login_required
def district_create(request):
    """创建区县（仅市级管理员）"""
    data = parse_json_body(request)

    name = (data.get('name') or '').strip()
    if not name:
        return json_fail('区县名称不能为空')

    code = (data.get('code') or '').strip()
    if not code:
        return json_fail('区县代码不能为空')

    if District.objects.filter(code=code).exists():
        return json_fail('区县代码已存在')

    is_city = bool(data.get('is_city', False))

    district = District.objects.create(
        name=name, code=code, is_city=is_city, status='active'
    )
    # 审计用上下文：`District` 自己没有 district 外键，响应体里读不出归属区县
    request.audit_district = district
    return json_ok(serialize_instance(district), message='区县创建成功')


# ============================================
# 7.1 编辑区县
# ============================================
@csrf_exempt
@role_required('gov_city')
@login_required
def district_edit(request, pk):
    """编辑区县（仅市级管理员）"""
    try:
        district = District.objects.get(id=pk)
    except District.DoesNotExist:
        return json_fail('区县不存在', status=404)

    data = parse_json_body(request)
    update_fields = []

    name = (data.get('name') or '').strip()
    if name:
        district.name = name
        update_fields.append('name')
    code = (data.get('code') or '').strip()
    if code:
        if District.objects.filter(code=code).exclude(id=pk).exists():
            return json_fail('区县代码已存在')
        district.code = code
        update_fields.append('code')
    if 'is_city' in data:
        district.is_city = bool(data.get('is_city'))
        update_fields.append('is_city')
    if 'status' in data:
        district.status = data.get('status')
        update_fields.append('status')

    if update_fields:
        district.save(update_fields=update_fields)

    request.audit_district = district
    return json_ok(serialize_instance(district), message='区县更新成功')


# ============================================
# 7.2 切换区县状态
# ============================================
@csrf_exempt
@role_required('gov_city')
@login_required
def district_toggle_status(request, pk):
    """切换区县启用/停用状态（仅市级管理员）"""
    try:
        district = District.objects.get(id=pk)
    except District.DoesNotExist:
        return json_fail('区县不存在', status=404)

    district.status = 'inactive' if district.status == 'active' else 'active'
    district.save(update_fields=['status'])
    request.audit_district = district
    return json_ok(
        {'id': district.id, 'status': district.status},
        message=f'区县已{"停用" if district.status == "inactive" else "启用"}'
    )


# ============================================
# 7.3 删除区县
# ============================================
@csrf_exempt
@role_required('gov_city')
@login_required
def district_delete(request, pk):
    """删除区县（仅市级管理员，且未被业务数据引用）"""
    try:
        district = District.objects.get(id=pk)
    except District.DoesNotExist:
        return json_fail('区县不存在', status=404)

    from accounts.models import User
    from business.models import (
        Pet, Capture, OwnerReturn, Transfer, Treatment, Material,
        MaterialTransaction, Release, Adoption, Blacklist, Euthanasia,
    )

    references = {
        '用户账号': User.objects.filter(district_id=pk).count(),
        '机构': Institution.objects.filter(district_id=pk).count(),
        '宠物档案': Pet.objects.filter(district_id=pk).count(),
        '捕捉记录': Capture.objects.filter(district_id=pk).count(),
        '主人领回': OwnerReturn.objects.filter(district_id=pk).count(),
        '转运记录': Transfer.objects.filter(district_id=pk).count(),
        '诊疗记录': Treatment.objects.filter(district_id=pk).count(),
        '物料': Material.objects.filter(district_id=pk).count(),
        '物料流水': MaterialTransaction.objects.filter(district_id=pk).count(),
        '放养记录': Release.objects.filter(district_id=pk).count(),
        '领养记录': Adoption.objects.filter(district_id=pk).count(),
        '黑名单': Blacklist.objects.filter(district_id=pk).count(),
        '安乐死记录': Euthanasia.objects.filter(district_id=pk).count(),
    }
    used = {k: v for k, v in references.items() if v > 0}
    if used:
        detail = '、'.join(f'{k}{v}条' for k, v in used.items())
        return json_fail(f'该区县已被业务数据引用（{detail}），不可删除，请改为停用')

    # 审计：区县本身被删掉了，**不能**把日志归属到它（外键会在写入时报错、
    # 整条日志丢失）。改为把名称写进摘要，日志保持「无归属区县」= 仅市级可见。
    request.audit_object_repr = f'{district.name}（{district.code}）'
    request.audit_summary = f'区县管理·删除 {district.name}（{district.code}）'

    district.delete()
    return json_ok({'id': pk}, message='区县删除成功')


# ============================================
# 8. 用户列表
# ============================================
@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def user_list(request):
    """用户列表（按区县范围过滤）"""
    qs = User.objects.all()
    scope = get_district_scope(request)
    if scope is not None:
        qs = qs.filter(district_id=scope)

    role = request.GET.get('role')
    if role:
        qs = qs.filter(role=role)

    data = []
    for u in qs:
        data.append({
            'id': u.id,
            'username': u.username,
            'name': u.get_full_name() or u.username,
            'role': u.role,
            'role_display': u.get_role_display(),
            'district_id': u.district_id,
            'district_name': u.district.name if u.district else '',
            'institution_id': u.institution_id,
            'institution_name': u.institution.name if u.institution else '',
            'phone': u.phone,
            'status': u.status,
            'is_active': u.is_active,
            'is_superuser': u.is_superuser,
            'date_joined': u.date_joined.isoformat() if u.date_joined else '',
        })
    return json_ok(data)


# ============================================
# 9. 创建用户
# ============================================
@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def user_create(request):
    """创建用户

    强制逻辑约束：
    - gov_city：所属区域必须是市级（is_city=True）
    - gov_district / hospital：必须选具体区县（is_city=False），不能选市级
    - shelter：可挂市级或区县（归属市级的捕捉点，各区县管理账号无权查看其数据）
    - adopter：不在政府端创建（由捕捉点在领养登记时自动创建）
    - hospital：必须关联医院机构（institution.type='hospital'）
    - shelter：必须关联捕捉点机构（institution.type='shelter'）
    """
    data = parse_json_body(request)

    username = (data.get('username') or '').strip()
    if not username:
        return json_fail('用户名不能为空')
    if User.objects.filter(username=username).exists():
        return json_fail('用户名已存在')

    password = (data.get('password') or '').strip() or '123456'
    if len(password) < 6:
        return json_fail('密码长度不能少于6位')

    role = data.get('role')
    # 政府端不允许创建领养人
    if role == 'adopter':
        return json_fail('领养人不在政府端创建，请在捕捉点领养登记时自动创建')
    if role not in ('gov_city', 'gov_district', 'shelter', 'hospital'):
        return json_fail('角色无效')

    name = (data.get('name') or '').strip()
    if not name:
        return json_fail('姓名不能为空')

    district_id = data.get('district_id')
    institution_id = data.get('institution_id')

    # 区县逻辑校验
    if not district_id:
        return json_fail('请选择所属区县')

    try:
        district = District.objects.get(id=district_id)
    except District.DoesNotExist:
        return json_fail('所选区县不存在')

    if district.status != 'active':
        return json_fail('所选区县已停用')

    # 市级角色（gov_city）必须选市级区县
    if role == 'gov_city':
        if not district.is_city:
            return json_fail('市管理员的所属区域必须为市级')
    # 区县级角色（gov_district / hospital）必须选具体区县，不能选市级
    elif role in ('gov_district', 'hospital'):
        if district.is_city:
            return json_fail('区级管理员/医院操作员必须选择具体区县，不能选市级')
    # shelter 可挂市级或区县，不做限制

    # 机构关联校验
    institution = None
    if role == 'shelter':
        if not institution_id:
            return json_fail('捕捉点操作员必须关联一个捕捉点机构')
        try:
            institution = Institution.objects.get(id=institution_id, type='shelter')
        except Institution.DoesNotExist:
            return json_fail('所选机构不是捕捉点类型')
    elif role == 'hospital':
        if not institution_id:
            return json_fail('医院操作员必须关联一个医院机构')
        try:
            institution = Institution.objects.get(id=institution_id, type='hospital')
        except Institution.DoesNotExist:
            return json_fail('所选机构不是医院类型')

    user = User.objects.create_user(
        username=username,
        password=password,
    )
    user.role = role
    user.phone = (data.get('phone') or '').strip()
    user.status = 'active'
    user.first_name = name
    user.district = district
    if institution:
        user.institution = institution
    user.save()

    request.audit_district = district
    return json_ok({
        'id': user.id,
        'username': user.username,
        'name': user.get_full_name() or user.username,
        'role': user.role,
    }, message='用户创建成功')


# ============================================
# 10. 用户状态切换
# ============================================
@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def user_toggle_status(request, pk):
    """切换用户启用/停用状态（带自锁保护 + 区县范围校验）"""
    # 与 user_list 保持一致的区县范围：区级管理员只能操作本区账号，
    # 否则可凭主键停用其他区县乃至市级管理员的账号。
    qs = User.objects.all()
    scope = get_district_scope(request)
    if scope is not None:
        qs = qs.filter(district_id=scope)
    user = qs.filter(id=pk).first()
    if user is None:
        return json_fail('用户不存在或无权访问', status=404)

    # 停用方向的保护：防止管理员把自己或最后一个市级管理员锁在系统外
    if user.is_active:
        if request.user.id == user.id:
            return json_fail('不能停用当前登录账号自己')
        if user.is_superuser:
            return json_fail('超级管理员账号不可停用')
        if user.role == 'gov_city':
            has_other = User.objects.filter(
                role='gov_city', is_active=True,
            ).exclude(id=user.id).exists()
            if not has_other:
                return json_fail('系统至少需保留一个启用中的市级管理员账号')

    user.is_active = not user.is_active
    user.status = 'active' if user.is_active else 'inactive'
    user.save(update_fields=['is_active', 'status'])
    request.audit_district = user.district
    request.audit_object_repr = user.username
    return json_ok(
        {'id': user.id, 'is_active': user.is_active, 'status': user.status},
        message=f'用户已{"启用" if user.is_active else "停用"}'
    )


# ============================================
# 11. 业务监管
# ============================================
def _treatment_items(t):
    """聚合诊疗项目为可读列表"""
    items = []
    if t.items_sterilization:
        items.append('绝育')
    if t.items_vaccine:
        items.append('疫苗')
    if t.items_deworming:
        items.append('驱虫')
    if t.items_chip:
        items.append('芯片')
    return items


def _pet_brief(pet):
    """宠物简要信息（含全部阶段照片）。

    实际实现已下沉到 ``business.services.pet_brief``，供捕捉端「一宠一档」、
    政府端「一宠一档」共用，避免两端字段口径漂移。这里保留原函数名，
    因为本模块多处调用它。
    """
    return pet_brief(pet)


@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def business_supervision(request):
    """业务监管：返回所有业务记录，支持 ?business_type 过滤

    每类记录都附带 district_name 字段，详情数据通过 detail 字段返回。
    """
    business_type = request.GET.get('business_type')

    result = {}

    if business_type is None or business_type == 'capture':
        # prefetch 逐只动物：详情里要展示「单只信息 + 各自照片」，
        # 不做预取会变成每张捕捉单一次查询（N+1）。
        qs = _scope_filter(Capture.objects.all(), request).prefetch_related(
            Prefetch('pet_set', queryset=Pet.objects.filter(is_deleted=False)
                     .select_related('district', 'shelter', 'hospital')))
        captures = []
        for c in qs:
            item = serialize_instance(c)
            item['district_name'] = c.district.name if c.district else ''
            # pet_codes 字段是逗号分隔字符串，转成列表
            if item.get('pet_codes') and isinstance(item['pet_codes'], str):
                item['pet_codes'] = [p.strip() for p in item['pet_codes'].split(',') if p.strip()]
            else:
                item['pet_codes'] = []
            item['group_photo'] = c.group_photo.url if c.group_photo else ''
            # 逐只动物明细（编号/类别/品种/性别/芯片/状态 + 各阶段照片），
            # 与捕捉端「一宠一档」、政府端「台账中心」共用 pet_brief，字段口径一致。
            item['pets'] = [_pet_brief(p) for p in c.pet_set.all()]
            captures.append(item)
        result['captures'] = captures

    if business_type is None or business_type == 'transfer':
        qs = _scope_filter(Transfer.objects.all(), request)
        transfers_raw = list(qs)
        # 转运单按编号关联宠物，先把全部编号一次性捞出来，避免逐单查库
        all_pet_codes = set()
        for t in transfers_raw:
            all_pet_codes.update(
                p.strip() for p in (t.pet_codes or '').split(',') if p.strip())
        pet_by_code = {}
        if all_pet_codes:
            for p in Pet.objects.filter(
                code__in=all_pet_codes, is_deleted=False
            ).select_related('district', 'shelter', 'hospital'):
                pet_by_code[p.code] = p

        transfers = []
        for t in transfers_raw:
            item = serialize_instance(t)
            item['district_name'] = t.district.name if t.district else ''
            if item.get('pet_codes') and isinstance(item['pet_codes'], str):
                item['pet_codes'] = [p.strip() for p in item['pet_codes'].split(',') if p.strip()]
            else:
                item['pet_codes'] = []
            item['pets'] = [_pet_brief(pet_by_code[c]) for c in item['pet_codes']
                            if c in pet_by_code]
            transfers.append(item)
        result['transfers'] = transfers

    if business_type is None or business_type == 'treatment':
        qs = _scope_filter(Treatment.objects.all(), request)
        treatments = []
        for t in qs:
            item = serialize_instance(t)
            item['district_name'] = t.district.name if t.district else ''
            item['ledger_no'] = f'TRE-{t.id:06d}'
            # 聚合诊疗项目为可读字段
            item['treatment_items'] = _treatment_items(t)
            item['items'] = {
                'sterilization': t.items_sterilization,
                'vaccine': t.items_vaccine,
                'deworming': t.items_deworming,
                'chip': t.items_chip,
            }
            item['treatment_detail'] = {
                'sterilization_surgery_date': t.sterilization_surgery_date.isoformat() if t.sterilization_surgery_date else '',
                'sterilization_surgeon': t.sterilization_surgeon,
                'sterilization_diagnosis': t.sterilization_diagnosis,
                'sterilization_anesthesia': t.sterilization_anesthesia,
                'sterilization_procedure': t.sterilization_procedure,
                'sterilization_recovery': t.sterilization_recovery,
                'vaccine_type': t.vaccine_type,
                'vaccine_batch_no': t.vaccine_batch_no,
                'vaccine_date': t.vaccine_date.isoformat() if t.vaccine_date else '',
                'vaccine_quantity': t.vaccine_quantity,
                'deworming_type': t.deworming_type,
                'deworming_batch_no': t.deworming_batch_no,
                'deworming_date': t.deworming_date.isoformat() if t.deworming_date else '',
                'deworming_quantity': t.deworming_quantity,
                'chip_no': t.chip_no,
                'chip_date': t.chip_date.isoformat() if t.chip_date else '',
            }
            # 关联宠物照片
            if t.pet:
                item['pet'] = _pet_brief(t.pet)
            treatments.append(item)
        result['treatments'] = treatments

    if business_type is None or business_type == 'release':
        qs = _scope_filter(Release.objects.all(), request)
        releases = []
        for r in qs:
            item = serialize_instance(r)
            item['district_name'] = r.district.name if r.district else ''
            item['pet_count'] = 1  # 放养按只记录
            # 关联宠物照片
            if r.pet:
                item['pet'] = _pet_brief(r.pet)
            releases.append(item)
        result['releases'] = releases

    if business_type is None or business_type == 'adoption':
        qs = _scope_filter(Adoption.objects.all(), request)
        adoptions = []
        for a in qs:
            item = serialize_instance(a)
            item['district_name'] = a.district.name if a.district else ''
            if a.pet:
                item['pet'] = _pet_brief(a.pet)
            adoptions.append(item)
        result['adoptions'] = adoptions

    if business_type is None or business_type == 'checkin':
        # CheckIn 无 district 字段，通过 pet 关联过滤
        qs = CheckIn.objects.all()
        scope = get_district_scope(request)
        if scope is not None:
            qs = qs.filter(pet__district_id=scope)
        result['checkins'] = [serialize_instance(c) for c in qs]

    if business_type is None or business_type == 'euthanasia':
        qs = _scope_filter(Euthanasia.objects.all(), request)
        euthanasia = []
        for e in qs:
            item = serialize_instance(e)
            item['district_name'] = e.district.name if e.district else ''
            if e.pet:
                item['pet'] = _pet_brief(e.pet)
            euthanasia.append(item)
        result['euthanasia'] = euthanasia

    return json_ok(result)


# ============================================
# 12. 物资监管
# ============================================
@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def material_supervision(request):
    """物资全局统计：流水、库存、预警"""
    # 物资列表（含库存）
    material_qs = _scope_filter(Material.objects.all(), request)
    materials = []
    alerts = []
    for m in material_qs:
        item = serialize_instance(m)
        item['category_display'] = m.get_category_display()
        item['district_name'] = m.district.name if m.district else ''
        # 预警：库存低于安全库存
        if m.shelter_stock < m.safety_stock:
            alerts.append({
                'id': m.id,
                'name': m.name,
                'category': m.category,
                'shelter_stock': m.shelter_stock,
                'safety_stock': m.safety_stock,
                'shortage': m.safety_stock - m.shelter_stock,
            })
        materials.append(item)

    # 流水列表
    txn_qs = _scope_filter(MaterialTransaction.objects.all(), request)
    transactions = [serialize_instance(t) for t in txn_qs]

    # 汇总
    stats = {
        'total_purchased': txn_qs.filter(type='purchase').aggregate(t=Sum('quantity'))['t'] or 0,
        'total_dispatched': txn_qs.filter(type='dispatch').aggregate(t=Sum('quantity'))['t'] or 0,
        'total_received': txn_qs.filter(type='receive').aggregate(t=Sum('quantity'))['t'] or 0,
        'total_consumed': txn_qs.filter(type='consume').aggregate(t=Sum('quantity'))['t'] or 0,
        'total_adjustment': txn_qs.filter(type='adjustment').aggregate(t=Sum('quantity'))['t'] or 0,
        'material_count': material_qs.count(),
        'transaction_count': txn_qs.count(),
        'alert_count': len(alerts),
    }

    data = {
        'materials': materials,
        'transactions': transactions,
        'alerts': alerts,
        'stats': stats,
    }
    return json_ok(data)


# ============================================
# 13. 台账中心
# ============================================
@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def ledger_center(request):
    """统一台账：合并所有业务台账记录，支持筛选

    每笔记录附带详情数据(detail)，包括动物编号、照片、具体字段等。
    """
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    institution_id = request.GET.get('institution_id')
    business_type = request.GET.get('business_type')

    records = []

    def _date_filter(qs, date_field='created_at'):
        if start_date:
            try:
                sd = datetime.strptime(start_date[:10], '%Y-%m-%d').date()
                qs = qs.filter(**{f'{date_field}__gte': sd})
            except (ValueError, TypeError):
                pass
        if end_date:
            try:
                ed = datetime.strptime(end_date[:10], '%Y-%m-%d').date()
                # 结束日期含当天：用次日零点开区间，避免当天非零点记录被排除
                qs = qs.filter(**{f'{date_field}__lt': ed + timedelta(days=1)})
            except (ValueError, TypeError):
                pass
        return qs

    # 一宠一档（按宠物档案聚合全生命周期数据）
    # 聚合逻辑下沉到 business.services.pet_archive_records，与捕捉端
    # 「全量台账 → 一宠一档」共用同一份实现，两端字段/口径天然一致。
    if business_type == 'pet':
        qs = _scope_filter(Pet.objects.filter(is_deleted=False), request)
        qs = _date_filter(qs)
        qs = qs.select_related('district', 'shelter', 'hospital', 'capture').prefetch_related(
            'treatments', 'releases', 'adoptions', 'euthanasia_records', 'owner_returns')
        records.extend(pet_archive_records(qs))

    # 捕捉台账
    if business_type is None or business_type == 'capture':
        qs = _scope_filter(Capture.objects.filter(is_deleted=False), request)
        qs = _date_filter(qs)
        if institution_id:
            qs = qs.filter(shelter_id=institution_id)
        for c in qs:
            pet_codes = [p.strip() for p in (c.pet_codes or '').split(',') if p.strip()]
            records.append({
                'business_type': 'capture',
                'ledger_no': c.ledger_no,
                'id': c.id,
                'date': c.created_at.isoformat() if c.created_at else '',
                'shelter_name': c.shelter_name,
                'community_name': c.community_name,
                'pet_count': c.pet_count,
                'status': c.status,
                'operator_name': c.operator_name,
                'district_name': c.district.name if c.district else '',
                'detail': {
                    'address': c.address,
                    'geo_address': c.geo_address,
                    'latitude': c.latitude,
                    'longitude': c.longitude,
                    'property_name': c.property_name,
                    'contact_person': c.contact_person,
                    'contact_phone': c.contact_phone,
                    'signature': c.signature,
                    'pet_codes': pet_codes,
                    'group_photo': c.group_photo.url if c.group_photo else '',
                    'pets': [_pet_brief(p) for p in Pet.objects.filter(capture=c, is_deleted=False)],
                }
            })

    # 转运台账
    if business_type is None or business_type == 'transfer':
        qs = _scope_filter(Transfer.objects.all(), request)
        qs = _date_filter(qs)
        if institution_id:
            qs = qs.filter(Q(from_shelter_id=institution_id) | Q(to_hospital_id=institution_id))
        for t in qs:
            pet_codes = [p.strip() for p in (t.pet_codes or '').split(',') if p.strip()]
            records.append({
                'business_type': 'transfer',
                'ledger_no': t.ledger_no,
                'id': t.id,
                'date': t.created_at.isoformat() if t.created_at else '',
                'from_shelter_name': t.from_shelter_name,
                'to_hospital_name': t.to_hospital_name,
                'pet_count': t.pet_count,
                'status': t.status,
                'operator_name': t.operator_name,
                'district_name': t.district.name if t.district else '',
                'detail': {
                    'pet_codes': pet_codes,
                    'received_at': t.received_at.isoformat() if t.received_at else '',
                    'reject_reason': t.reject_reason,
                    'note': t.note,
                    'pets': [_pet_brief(p) for p in Pet.objects.filter(code__in=pet_codes, is_deleted=False)],
                }
            })

    # 诊疗台账
    if business_type is None or business_type == 'treatment':
        qs = _scope_filter(Treatment.objects.all(), request)
        qs = _date_filter(qs)
        if institution_id:
            qs = qs.filter(hospital_id=institution_id)
        for t in qs:
            records.append({
                'business_type': 'treatment',
                'ledger_no': f'TRE-{t.id:06d}',
                'id': t.id,
                'date': t.created_at.isoformat() if t.created_at else '',
                'pet_code': t.pet_code,
                'hospital_name': t.hospital_name,
                'treatment_items': _treatment_items(t),
                'status': t.status,
                'operator_name': t.operator_name,
                'district_name': t.district.name if t.district else '',
                'detail': {
                    'items': {
                        'sterilization': t.items_sterilization,
                        'vaccine': t.items_vaccine,
                        'deworming': t.items_deworming,
                        'chip': t.items_chip,
                    },
                    'sterilization_surgery_date': t.sterilization_surgery_date.isoformat() if t.sterilization_surgery_date else '',
                    'sterilization_surgeon': t.sterilization_surgeon,
                    'sterilization_diagnosis': t.sterilization_diagnosis,
                    'sterilization_anesthesia': t.sterilization_anesthesia,
                    'sterilization_procedure': t.sterilization_procedure,
                    'sterilization_recovery': t.sterilization_recovery,
                    'vaccine_type': t.vaccine_type,
                    'vaccine_batch_no': t.vaccine_batch_no,
                    'vaccine_date': t.vaccine_date.isoformat() if t.vaccine_date else '',
                    'vaccine_quantity': t.vaccine_quantity,
                    'deworming_type': t.deworming_type,
                    'deworming_batch_no': t.deworming_batch_no,
                    'deworming_date': t.deworming_date.isoformat() if t.deworming_date else '',
                    'deworming_quantity': t.deworming_quantity,
                    'chip_no': t.chip_no,
                    'chip_date': t.chip_date.isoformat() if t.chip_date else '',
                    'pet': _pet_brief(t.pet),
                }
            })

    # 物资流水台账
    if business_type is None or business_type == 'material':
        qs = _scope_filter(MaterialTransaction.objects.all(), request)
        qs = _date_filter(qs, 'date')
        if institution_id:
            qs = qs.filter(hospital_id=institution_id)
        for t in qs:
            material_category = ''
            material_category_display = ''
            if t.material:
                material_category = t.material.category
                material_category_display = t.material.get_category_display()
            records.append({
                'business_type': 'material',
                'ledger_no': t.ledger_no,
                'id': t.id,
                'date': t.date.isoformat() if t.date else '',
                'type': t.type,
                'type_display': t.get_type_display(),
                'material_name': t.material_name,
                'material_category': material_category,
                'material_category_display': material_category_display,
                'quantity': t.quantity,
                'unit': t.unit,
                'batch_no': t.batch_no,
                'from_to': t.from_to,
                'operator_name': t.operator_name,
                'district_name': t.district.name if t.district else '',
                'detail': {
                    'supplier': t.supplier,
                    'note': t.note,
                    'hospital_name': t.hospital.name if t.hospital else '',
                }
            })

    # 放养台账
    if business_type is None or business_type == 'release':
        qs = _scope_filter(Release.objects.all(), request)
        qs = _date_filter(qs, 'released_at')
        if institution_id:
            qs = qs.filter(community_id=institution_id)
        for r in qs:
            records.append({
                'business_type': 'release',
                'ledger_no': r.ledger_no,
                'id': r.id,
                'date': r.released_at.isoformat() if r.released_at else '',
                'pet_code': r.pet_code,
                'pet_count': 1,
                'community_name': r.community_name,
                'receiver_name': r.receiver_name,
                'status': r.status,
                'operator_name': r.operator_name,
                'district_name': r.district.name if r.district else '',
                'detail': {
                    'receiver_phone': r.receiver_phone,
                    'released_at': r.released_at.isoformat() if r.released_at else '',
                    'pet': _pet_brief(r.pet),
                }
            })

    # 领养台账
    if business_type is None or business_type == 'adoption':
        qs = _scope_filter(Adoption.objects.all(), request)
        qs = _date_filter(qs, 'adopted_at')
        if institution_id:
            qs = qs.filter(hospital_id=institution_id)
        for a in qs:
            records.append({
                'business_type': 'adoption',
                'ledger_no': a.ledger_no,
                'id': a.id,
                'date': a.adopted_at.isoformat() if a.adopted_at else '',
                'pet_code': a.pet_code,
                'adopter_name': a.adopter_name,
                'adopter_phone': a.adopter_phone,
                'hospital_name': a.hospital_name,
                'status': a.status,
                'operator_name': a.operator_name,
                'district_name': a.district.name if a.district else '',
                'detail': {
                    'adopter_id_card': a.adopter_id_card,
                    'adopter_address': a.adopter_address,
                    'qualification': a.qualification,
                    'pet': _pet_brief(a.pet),
                }
            })

    # 安乐死台账
    if business_type is None or business_type == 'euthanasia':
        qs = _scope_filter(Euthanasia.objects.all(), request)
        qs = _date_filter(qs, 'euthanized_at')
        if institution_id:
            qs = qs.filter(hospital_id=institution_id)
        for e in qs:
            records.append({
                'business_type': 'euthanasia',
                'ledger_no': e.ledger_no,
                'id': e.id,
                'date': e.euthanized_at.isoformat() if e.euthanized_at else '',
                'pet_code': e.pet_code,
                'hospital_name': e.hospital_name,
                'reason': e.reason,
                'operator_name': e.operator_name,
                'district_name': e.district.name if e.district else '',
                'detail': {
                    'condition': e.condition,
                    'body_received': e.body_received,
                    'body_received_at': e.body_received_at.isoformat() if e.body_received_at else '',
                    'body_received_by_name': e.body_received_by_name,
                    'pet': _pet_brief(e.pet),
                }
            })

    # 按日期倒序
    records.sort(key=lambda x: x.get('date') or '', reverse=True)

    return json_ok({
        'records': records,
        'total': len(records),
    })


# ============================================
# 14. 操作日志
# ============================================
@csrf_exempt
@role_required('gov_city', 'gov_district')
@login_required
def operation_logs(request):
    """业务操作审计日志。

    数据源是 `core.AuditLog`（由 `core.middleware.AuditLogMiddleware` 写入），
    **不是** Django admin 的 `LogEntry`：业务接口从不经过 admin，`LogEntry`
    永远是 0 行；而且它没有 district 字段，只能按操作人区县过滤，会让挂在
    「全市（市级）」下的捕捉点操作员产生的日志对区县政府不可见。

    区县隔离按**业务记录的归属区县**过滤，与捕捉单/转运单同一口径。
    """
    qs = AuditLog.objects.all()

    scope = get_district_scope(request)
    if scope is not None:
        # 未解析出归属区县的日志（district 为空）不对区县政府展示，
        # 否则等于绕开区县隔离。
        qs = qs.filter(district_id=scope)

    module = (request.GET.get('module') or '').strip()
    if module:
        qs = qs.filter(module=module)

    action_flag = (request.GET.get('action_flag') or request.GET.get('actionFlag') or '').strip()
    if action_flag.isdigit():
        qs = qs.filter(action_flag=int(action_flag))

    success = (request.GET.get('success') or '').strip()
    if success in ('0', '1'):
        qs = qs.filter(success=(success == '1'))

    keyword = (request.GET.get('q') or request.GET.get('keyword') or '').strip()
    if keyword:
        qs = qs.filter(
            Q(summary__icontains=keyword)
            | Q(object_repr__icontains=keyword)
            | Q(user_name__icontains=keyword)
            | Q(user__username__icontains=keyword)
            | Q(module__icontains=keyword)
            | Q(detail__icontains=keyword)
        )

    limit = request.GET.get('limit', '200')
    try:
        limit_int = int(limit)
    except (ValueError, TypeError):
        limit_int = 200
    limit_int = max(1, min(limit_int, 1000))

    role_labels = dict(User.ROLE_CHOICES)
    data = []
    for log in qs.select_related('user', 'district')[:limit_int]:
        item = {
            'id': log.id,
            'action_time': log.action_time.isoformat() if log.action_time else '',
            'user_id': log.user_id,
            'user_name': log.user_name,
            # 账号名与显示名是两回事：`user_name` 存的是写入时的显示名快照
            # （如「襄城捕捉点操作员」），按账号名 `cy_shelter` 搜是搜不到的。
            'username': log.user.username if log.user else '',
            'role': log.role,
            'role_label': role_labels.get(log.role, log.role),
            'district_id': log.district_id,
            'district_name': log.district_name,
            'module': log.module,
            'action_flag': log.action_flag,
            'action_label': ACTION_LABELS.get(log.action_flag, ''),
            'object_type': log.object_type,
            'object_id': log.object_id,
            'object_repr': log.object_repr,
            'summary': log.summary,
            'detail': log.detail,
            'method': log.method,
            'path': log.path,
            'success': log.success,
            'ip': log.ip or '',
            # 兼容旧前端渲染字段（renderLogList 读的是 object_repr + change_message）
            'change_message': log.summary,
        }
        data.append(with_camel_keys(item))
    return json_ok(data)


# ============================================
# 15. 系统配置
# ============================================
@csrf_exempt
@role_required('gov_city')
@login_required
def system_config(request):
    """系统配置（仅市级管理员）

    GET: 返回当前配置
    POST: 更新配置（键值对）
    """
    if request.method == 'GET':
        configs = SystemConfig.objects.all()
        data = {c.key: c.value for c in configs}
        # 默认配置
        defaults = {
            'pet_code_prefix': 'TNR',
            'ledger_no_format': 'PREFIX-YYMMDD-SSS',
            'capture_prefix': 'CAP',
            'transfer_prefix': 'TRF',
            'treatment_prefix': 'TRE',
            'release_prefix': 'REL',
            'adoption_prefix': 'ADP',
            'euthanasia_prefix': 'EUT',
            'purchase_prefix': 'PUR',
            'dispatch_prefix': 'DIS',
            'consume_prefix': 'CON',
        }
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        return json_ok(data)

    # POST: 更新配置
    data = parse_json_body(request)
    updated = []
    for key, value in data.items():
        if key in ('id',):
            continue
        obj, created = SystemConfig.objects.update_or_create(
            key=key,
            defaults={'value': str(value)},
        )
        updated.append(key)

    return json_ok({'updated': updated}, message=f'已更新 {len(updated)} 项配置')
