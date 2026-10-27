"""
门户端视图 - 各角色门户页面渲染 + 门户专用辅助 API。
"""
import json

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.models import (
    Pet, AdoptionHallListing, Adoption, Message,
)
from business.services import (
    json_ok, json_fail, serialize_instance,
    get_district_filtered_queryset, hospital_pet_scope,
    pet_archive_records, shelter_scope,
)
from business.services_lifecycle import (
    build_pet_lifecycle, pet_for_lifecycle,
)
from business.views_adoption import serialize_hall_listing


def _portal_user_data(user):
    """门户页注入给前端的 `user_data`。

    ⚠ 四个门户原本各抄了一份这段字典 —— 第四个（平台端）再加一份必然漂移：
    前端 `isCityLevel()` / 区县下拉 / 归属判断全靠这几个键，漏一个键就是
    「某个端看起来正常、某个字段恒为空」。

    `institution_district_*`（第五十轮）：判断**机构**是市级还是区级，
    不能用操作员账号的 `district` —— 现场区县捕捉点的操作员也可能挂在
    「全市（市级）」下。市级捕捉点可全市转运，区级只能本区。
    """
    inst = user.institution if user.institution_id else None
    inst_district = getattr(inst, 'district', None) if inst else None
    return {
        'id': user.id,
        'username': user.username,
        'name': user.get_full_name() or user.username,
        'role': user.role,
        'role_display': user.get_role_display(),
        # 个人中心的「账号状态」徽章读它；以前没这个键，三端的写法是
        # 直接硬编码绿色「启用」，账号被平台停用后仍显示启用。
        'status': user.status,
        'district_id': user.district_id,
        'district_name': user.district.name if user.district else '',
        'institution_id': user.institution_id,
        'institution_name': inst.name if inst else '',
        'institution_district_id': inst_district.id if inst_district else None,
        'institution_district_name': inst_district.name if inst_district else '',
        'institution_district_is_city': bool(
            inst_district and inst_district.is_city),
        'phone': user.phone,
        # 医院端个人中心只读展示（平台端维护，本人不可改）。
        'vet_license': user.vet_license,
    }


def _render_portal(request, template):
    return render(request, template, {
        'user_data': json.dumps(_portal_user_data(request.user), ensure_ascii=False),
    })


# ============================================
# 捕捉点门户页面
# ============================================
@never_cache  # 门户页面禁用缓存，避免手机浏览器缓存旧版本页面
@role_required('shelter')
@login_required
def shelter_portal(request):
    """捕捉点端门户页面。

    ⚠ **政府角色不能进**（第四十九轮收口）。这里整套 UI 是围绕「能操作」做的：
    建单、审核、黑名单、物料出入库。原先放行 gov 只是为了让监管能"看一眼现场
    列表"，代价是政府账号拿到了同一个端的**全部写接口**（本文件所在 app 的
    `/api/business/**` 当时都对 gov 开放），"政府端只保留查看"（`accounts/
    models.py` 角色表注释）只剩一句口头承诺。监管所需的现场数据在
    `/gov/` 的台账中心与 `/api/supervision/**` 同源提供。
    """
    return _render_portal(request, 'portal/shelter/portal.html')


# ============================================
# 医院门户页面
# ============================================
@never_cache
@role_required('hospital')
@login_required
def hospital_portal(request):
    """宠物医院端门户页面。

    政府角色不能进 —— 口径与 ``shelter_portal`` 一致（第四十九轮）：
    诊疗三步流程、出院四类单据、物料出库的写入口全在这里。
    """
    return _render_portal(request, 'portal/hospital/portal.html')


# ============================================
# 政府监管门户页面
# ============================================
@never_cache
@role_required('gov_city', 'gov_district')
@login_required
def gov_portal(request):
    """政府监管端门户页面（**只读**：查看业务 + 监管台账）。

    「只读」不是页面中不渲染写按钮就完事，第四十九轮起落在两处：

    1. **接口层**：`business/views_*.py` 里所有会改库的 view 的 `@role_required`
       都不再包含 `gov_city` / `gov_district`（25 个写接口）。判据见
       `core/tests_gov_readonly.py` —— 政府会话把 GET 和 POST 各打一遍，
       断言**业务表指纹零变化**（含行内容，不只行数；按角色而非按方法封，
       因为本项目普遍不判 `request.method`）。
    2. **入口层**：`shelter_portal` / `hospital_portal` / `adopter_portal` 也不再
       放行政府角色，政府账号点不进那套围绕「能操作」做的 UI。

    ⚠ 摘角色前逐个核对过：**每一个被关掉政府入口的写接口都还有非政府角色
    能用**（捕捉/转运/打卡/黑名单/物料 → `shelter`，诊疗/安乐死/领养信息
    → `hospital`，领养申请 → `adopter`）。若某个接口只有政府能写，那它就是
    真实的「监管代录」需求，只能另开口子、不能这样一关了之。
    """
    return _render_portal(request, 'portal/gov/portal.html')


# ============================================
# 平台管理门户页面
# ============================================
@never_cache
@role_required('platform_admin')
@login_required
def platform_portal(request):
    """平台管理端门户页面。

    ⚠ **只有 `platform_admin` 能进**，政府角色（含 `gov_city`）一律 403 ——
    主数据维护（区县 / 机构 / 账号 / 编号规则 / 公告 / 操作日志）已经整体
    迁到这里。放行 gov_city 会让「政府端只能查看」这道约束形同虚设：
    它只是从另一个入口拿到了同一批写接口。
    """
    return _render_portal(request, 'portal/platform/portal.html')


# ============================================
# 门户专用辅助 API - 医院
# ============================================
@csrf_exempt
@role_required('hospital', 'shelter', 'gov_city', 'gov_district')
@login_required
def hospital_pets(request):
    """医院在院宠物列表（可按 status 过滤）。

    GET /api/business/pets/?status=in_treatment
    """
    user = request.user

    if user.role == 'hospital':
        # 医院只看分配给自己的宠物（不按区县过滤，因为转运可能跨区县）
        if user.institution_id:
            qs = Pet.objects.filter(hospital_id=user.institution_id, is_deleted=False)
        else:
            qs = Pet.objects.none()
    elif user.role == 'shelter':
        # 捕捉点可见范围 = **机构所在区县 ∪ 挂在本机构名下**（`shelter_scope`）。
        #
        # 不能按 ``user.district`` 过滤：现场把捕捉点操作员都挂在「全市（市级）」下，
        # 那样会返回**全市**宠物，备选框里混进其他区县的动物，用户点了「下发」必然
        # 被后端的跨区县校验拦下（"以下动物不属于本区县，无法转运"）——
        # 备选框里出现永远选不中的选项，比空列表更糟。
        #
        # 「挂在本机构名下」这一支对**所有**捕捉点都要开，不只是市级（第五十九轮）：
        # 市级捕捉点的捕捉单按**实际捕捉区县**归属（R45 Q2 口径），机构自身挂在
        # 「全市（市级）」下 —— 若仍按机构区县过滤，刚捕捉的外区动物会从「待转运」
        # 里凭空消失（2026-10-05 生产实测：bzd 建单 CAP2600023 落襄城区，列表恒空）。
        # 而区县的捕捉点接了**跨区县**运回单之后同理：签收时 `pet.shelter` 落到本站、
        # `pet.district` 仍是原捕捉地（放归要回原小区，不能跟着改），只按区县会让
        # 本站签收的动物在「发起放归」里凭空消失。
        # ``transfer_create`` 侧的锚点解析（按动物所在区县）本就能接住这类单。
        qs = Pet.objects.filter(is_deleted=False).filter(
            shelter_scope(user, 'shelter'))
    else:
        qs = get_district_filtered_queryset(Pet, user).filter(is_deleted=False)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    qs = qs.select_related('shelter', 'capture')
    data = []
    for p in qs:
        item = serialize_instance(p)
        item['shelter_name'] = p.shelter.name if p.shelter_id else ''
        # 捕捉单登记的「所在小区」文本：放归要送回这个小区，而**捕捉单本身按机构
        # 收敛**，跨区县运回签收进来的动物，它的捕捉单不属于本站（`/captures/`
        # 查不到）。前端只从本站捕捉单列表里找小区，就会把「默认带出捕捉单登记的
        # 小区」这句承诺落空 —— 界面拦在「请填写放归小区」，而服务端明明知道答案。
        item['capture_community'] = (p.capture.community_name if p.capture_id else '')
        data.append(item)
    return json_ok(data)


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def pet_archive(request):
    """一宠一档：动物档案台账（每只动物一行，聚合全生命周期数据）。

    GET /api/business/pets/archive/[?status=..&keyword=..]

    捕捉端「全量台账 → 一宠一档」用它渲染列表；点击编号后再调
    ``pets/<id>/lifecycle/`` 拉各阶段明细与照片。

    聚合逻辑与政府端「台账中心 → 一宠一档」共用
    ``business.services.pet_archive_records``，两端字段与口径完全一致，
    不会出现「一端有出库原因、另一端没有」的漂移。

    **医院角色收敛到本机构**（第二十轮）：同一区县可能有多家医院，
    按区县过滤会让 A 院读到 B 院名下动物的完整档案与主人联系方式。
    口径与 ``hospital_pets``（`/pets/`，注释写明「医院只看分配给自己的宠物」）
    和 ``hospital_hall_listings``（`/hall-listings/`）保持一致。
    """
    user = request.user
    if user.role == 'hospital':
        qs = Pet.objects.filter(is_deleted=False).filter(
            hospital_pet_scope(user)).distinct()
    elif user.role == 'shelter':
        # 捕捉点 = 本区县 ∪ 挂本站名下，与 `/pets/`（``hospital_pets``）**同一条口径**。
        # 只按区县的话，跨区县运回签收进来的动物会从「全量台账 → 一宠一档」里消失，
        # 而放归备选框里正躺着这只（列表有、台账没有 = 两个页面互相打脸）。
        qs = Pet.objects.filter(is_deleted=False).filter(
            shelter_scope(user, 'shelter'))
    else:
        # 政府端按区县：本就该看全区。
        qs = get_district_filtered_queryset(Pet, user).filter(is_deleted=False)

    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)

    keyword = (request.GET.get('keyword') or '').strip()
    if keyword:
        # 必须写成单个 Q()：qs.filter(a) | qs.filter(b) 会丢掉前面的
        # 区县隔离过滤，并产生重复行
        qs = qs.filter(
            Q(code__icontains=keyword)
            | Q(name__icontains=keyword)
            | Q(breed__icontains=keyword)
            | Q(chip_no__icontains=keyword)
        )

    qs = qs.select_related('district', 'shelter', 'hospital', 'capture').prefetch_related(
        'treatments', 'releases', 'adoptions', 'euthanasia_records', 'owner_returns')
    return json_ok(pet_archive_records(qs))


@csrf_exempt
@role_required('hospital', 'adopter', 'gov_city', 'gov_district')
@login_required
def hospital_hall_listings(request):
    """医院领养大厅上架信息列表（含已下架）。

    GET /api/business/hall-listings/
    """
    user = request.user
    qs = AdoptionHallListing.objects.prefetch_related('extra_photos')

    if user.role == 'hospital':
        if user.institution_id:
            qs = qs.filter(hospital_id=user.institution_id)
        else:
            qs = qs.none()
    elif user.role == 'adopter':
        # 领养人只看已上架的领养信息
        qs = qs.filter(is_active=True)
    else:
        qs = get_district_filtered_queryset(AdoptionHallListing, user)

    # 隐藏照片只对**本院维护界面**可见；其他角色走公开口径，避免绕过显示开关
    include_hidden = user.role == 'hospital'
    data = [serialize_hall_listing(listing, include_hidden=include_hidden)
            for listing in qs]
    return json_ok(data)


# ============================================
# 领养人门户页面
# ============================================
@never_cache
@role_required('adopter')
@login_required
def adopter_portal(request):
    """领养人端门户页面（登录后全功能）。"""
    user_data = _portal_user_data(request.user)
    return render(request, 'portal/adopter/portal.html', {
        'user_data': json.dumps(user_data, ensure_ascii=False),
        'public_mode': False,
    })


def adoption_hall_public(request):
    """领养大厅公开页面（无需登录）。

    未登录或非领养人角色时仅展示领养大厅；
    已登录的领养人直接进入完整门户。

    ⚠ 这里**只把 `adopter` 转进 `adopter_portal`**（第四十九轮）。政府角色以前
    也在白名单里，而 `adopter_portal` 现在不再放行 gov —— 沿用旧条件会让政府
    账号访问这个**公开页**时被 `@role_required` 踢到 `/`：想看看大厅长什么样，
    结果被自己的门户页面弹走。公开页对政府就是公开页，按未登录渲染即可。
    """
    if request.user.is_authenticated and request.user.role == 'adopter':
        return adopter_portal(request)

    user_data = None
    if request.user.is_authenticated:
        user_data = json.dumps({
            'id': request.user.id,
            'username': request.user.username,
            'name': request.user.get_full_name() or request.user.username,
            'role': request.user.role,
            'role_display': request.user.get_role_display(),
            'phone': request.user.phone,
        }, ensure_ascii=False)

    return render(request, 'portal/adopter/portal.html', {
        'user_data': user_data or 'null',
        'public_mode': True,
    })


# ============================================
# 门户专用辅助 API - 领养人
# ============================================
@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district')
@login_required
def my_adoptions(request):
    """领养人 - 我的领养记录列表。"""
    qs = Adoption.objects.filter(adopter=request.user).order_by('-id')
    data = []
    for a in qs:
        item = serialize_instance(a)
        if a.pet_id:
            item['pet'] = serialize_instance(a.pet)
        data.append(item)
    return json_ok(data)


@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district')
@login_required
def my_messages(request):
    """领养人 - 我的消息列表。"""
    qs = Message.objects.filter(user=request.user).order_by('-id')
    data = [serialize_instance(m) for m in qs]
    return json_ok(data)


@csrf_exempt
@role_required('adopter')
@login_required
def mark_message_read(request, pk):
    """领养人 - 标记消息已读。"""
    if request.method != 'POST':
        return json_fail('仅支持 POST 请求', status=405)
    try:
        msg = Message.objects.get(id=pk, user=request.user)
    except Message.DoesNotExist:
        return json_fail('消息不存在', status=404)

    msg.is_read = True
    msg.save(update_fields=['is_read'])
    # 读回执不写审计：`Message` 没有归属区县（审计里会是一条无法归属的记录），
    # 而且每次打开消息都会产生一条 —— 只会把真正有意义的业务操作挤出日志页。
    request.audit_skip = True
    return json_ok(serialize_instance(msg), message='已标记为已读')


# ============================================
# 宠物全生命周期溯源（领养人端）
# ============================================
# 阶段派生、范围收敛、PII 脱敏与事件拼装都在 `business/services_lifecycle.py`
# —— 打印页（`views_print.print_pet_archive`）必须与这个接口共用同一份实现，
# 否则「打印页能不能看」与「接口能不能看」会各走各的。
@csrf_exempt
@role_required('adopter', 'gov_city', 'gov_district', 'shelter', 'hospital')
@login_required
def pet_lifecycle(request, pet_id):
    """返回指定宠物的全生命周期溯源记录。

    按时间顺序返回：捕捉、转运、诊疗、放归、领养、安乐死记录。
    按角色收敛可见范围：领养人仅限自己领养/申请过的动物，
    **医院限本院在治或本院经手过（诊疗）的动物**，
    其余角色按所属区县过滤，避免只凭主键即可遍历全量动物档案。

    医院这一支**不能按区县收敛**（第二十轮）：同一区县可能有多家医院
    （襄城区有 2 家），按区县会让 A 院读到 B 院名下动物的完整溯源记录 ——
    含主人姓名与电话。判据与 ``pet_archive`` 共用 ``hospital_pet_scope()``。
    """
    pet = pet_for_lifecycle(request.user, pet_id)
    if pet is None:
        return json_fail('宠物不存在或无权访问', status=404)
    # 事件拼装、阶段派生、主人 PII 分档都在 services_lifecycle 里：
    # 打印页（`/print/pet-archive/<id>/`）调的是同一个函数，两条路径的
    # 「谁能看」与「能看到什么」不可能漂移。
    return json_ok(build_pet_lifecycle(pet, request.user))
