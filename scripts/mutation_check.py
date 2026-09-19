"""变异验证通用夹具：把「刚补上的判据」逐个破坏，确认测试真的会红。

## 为什么需要它

「测试全绿」只证明**当前代码**能过，不证明测试**有判别力**。
如果断言写得空转（无论代码对错都通过），重构时判据被删掉也不会有任何信号 ——
这是「静默失败」在测试层的形态。

## 用法

1. 改下面的 `MUTATIONS`：每条 = (标签, 目标文件, old, new, 期望失败的测试名)，
   可选的**第 6 项**是附加改动列表 `[(文件, old, new), ...]`，用于「必须同时
   破坏两处才算复现原缺陷」这类跨文件变异（例：侧栏入口 + navMap 映射同时删掉）。
2. 先确认 `old` 在文件里**唯一命中**（脚本会检查，命中数 ≠ 1 就 SKIP 并告警）——
   否则 `replace(..., 1)` 可能打在同名的别处代码上，那是**第四种空转**。
3. 跑：

       .venv/bin/python scripts/mutation_check.py

   退出码 0 = 全部变异被捕获；非 0 = 存在空转，测试判别力不足。

4. 脚本**改完即还原**，并在退出前断言还原成功；不会把变异体留在工作区。

## 判读 `WEAK`

`WEAK` = 测试**确实红了**，但红的是**别的用例**（不是 `expect` 指名的那个）。
两种情况要分清：
  - 真被别的用例挡住了 → 说明 `expect` 写错了，改 `expect` 即可；
  - 该用例压根没被触发（如 M13/M14 曾报 WEAK）→ **说明新守卫没起作用**，
    要去查为什么。第二十二轮就是这样揪出「自己的 HTML 注释把可达性检查骗过」的。

## 五种已知空转（本夹具能挡住第 ④ 种）

① 断言匹配到任意一处同名文本；② 匹配到注释/docstring；
③ 正则漏 `re.M`；④ **变异没落在目标代码上**（`replace(old, new, 1)` 打偏）；
⑤ 夹具造不出触发条件（这要靠「判别力自检」在用例里显式报出来，本脚本挡不住）。

## 特殊变异：把判据「挪位」

需要把一段代码从 A 处挪到 B 处时，`new` 里写锚点语法：见 `MOVE_AFTER` 的处理。

## ⚠ 被中途杀掉会把变异体留在盘上

脚本靠 `finally` 还原，但**进程被 SIGTERM 杀掉时 `finally` 不会执行** ——
变异体就留在工作区里了。第二十二轮真实发生过：前台跑到一半被超时信号杀掉，
`templates/portal/shelter_base.html` 里的侧栏入口**就这么没了**，
后续变异验证报「目标串命中 0 次」才发现。

所以这里装了 SIGTERM / SIGINT / SIGHUP 处理器，收到信号先还原再退出。
另外：**跑完务必 `git status` 看一眼**，确认没有被改动的文件残留。
"""
import os
import pathlib
import signal
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 测试目标：默认跑本轮新增的用例文件（跑全量会很慢）。
# 可在命令行覆盖：`.venv/bin/python scripts/mutation_check.py 业务.用例 另一.用例`
#
# ⚠ 收窄/放宽**权限**的变异（M7 这类）会打到存量用例上 ——
# 第二十二轮就漏了 `business.tests.test_blacklist_views`（4 条按旧口径写的用例），
# 变异脚本当时「全绿」，是**全量套件**才把它们翻出来。
# 所以凡涉及角色集合的变异，目标列表里必须带上受影响的存量模块。
TEST_TARGETS = sys.argv[1:] or [
    'business.tests.test_write_scope',
    'business.tests.test_own_institution_scope',
    'business.tests.test_param_contract',
    'business.tests.test_transfer_views',
    'business.tests.test_material_views',
    'business.tests.test_endpoint_role_scope',
    'business.tests.test_blacklist_views',
    'core.tests_audit',
    'core.tests_frontend_consistency',
]

# (标签, 目标文件相对路径, old, new, 期望失败的测试名)
# new 为 None 表示「删除该段」；配合 move_after 可表达「挪位」
MUTATIONS = [
    (
        'M1 删掉机构判据（复现原洞）', 'business/views_adoption.py',
        "    if user.role == 'hospital' and pet.hospital_id != user.institution_id:\n"
        "        return json_fail('无权编辑此动物的领养信息')\n",
        None,
        'cross_institution',
    ),
    (
        'M2 判据反转（!= 改 ==）', 'business/views_adoption.py',
        "pet.hospital_id != user.institution_id",
        "pet.hospital_id == user.institution_id",
        'own_pet_ok',
    ),
    (
        'M3 比错字段（institution_id → district_id）', 'business/views_adoption.py',
        "pet.hospital_id != user.institution_id",
        "pet.hospital_id != user.district_id",
        'own_pet_ok',
    ),
    (
        'M4 判据挪到落库之后', 'business/views_adoption.py',
        "    if user.role == 'hospital' and pet.hospital_id != user.institution_id:\n"
        "        return json_fail('无权编辑此动物的领养信息')\n",
        None,
        'checks_before_any_write',
    ),
    # M5 / M6 已**删除**（第二十三轮）：它们的目标串
    #   `if user.institution_id != transfer.to_hospital_id:`
    # 是 `transfer_receive` 的**旧实现**；该接口改为调用共用取数函数
    # `get_own_institution_object()` 后，旧串在文件里命中 0 次 → 只会 SKIP。
    # 两条变异要表达的意思（契约层换写法 / 判据换成区县维度）已由
    # M16、M17 用新写法重新表达。
]

# 可选：把「被删除的判据」插到指定锚点之后 —— 用来表达「判据挪到落库之后」这类变异
MOVE_AFTER = {
    'M4 判据挪到落库之后': (
        'business/views_adoption.py',
        "        listing.save()\n",
        "\n    if user.role == 'hospital' and pet.hospital_id != user.institution_id:\n"
        "        return json_fail('无权编辑此动物的领养信息')\n",
    ),
}

# ---------- 第二十二轮：接口 × 角色矩阵（黑名单查询）----------
MUTATIONS += [
    (
        'M7 角色集合放回过宽版（复现原口子）', 'business/views_checkin.py',
        "@role_required('shelter', 'gov_city', 'gov_district')\n"
        "@login_required\n"
        "def blacklist_check(request):",
        "@role_required('shelter', 'hospital', 'adopter', 'gov_city', 'gov_district')\n"
        "@login_required\n"
        "def blacklist_check(request):",
        'adopter_cannot_probe_blacklist',
    ),
    (
        'M8 把黑名单查询也按区县收敛（会让跨区县拦截失效）',
        'business/services.py',
        "    qs = Blacklist.objects.filter(is_deleted=False)",
        "    qs = Blacklist.objects.filter(is_deleted=False, district_id=None)",
        'cross_district',
    ),
    (
        'M9 审计用例退回用 hospital 身份（会变成空转）',
        'core/tests_audit.py',
        "        self.login_as(self.shelter_user_a)\n"
        "        before = AuditLog.objects.count()\n"
        "        resp = self.post_json('/api/business/blacklist/check/', {",
        "        self.login_as(self.hospital_user_a)\n"
        "        before = AuditLog.objects.count()\n"
        "        resp = self.post_json('/api/business/blacklist/check/', {",
        'read_only_post_is_not_logged',
    ),
    (
        'M10 黑名单校验退回用静默的 _get',
        'static/js/tnr-api.js',
        "    return this.get(`/api/business/blacklist/check/?${params}`);",
        "    return this._get(`/api/business/blacklist/check/?${params}`);",
        'blacklist_check_does_not_use_silent_get',
    ),
    (
        'M11 只升 4 个门户里的 1 处版本号（漏升 5 处）',
        'templates/portal/hospital/portal.html',
        "js/tnr-api.js' %}?v=20260919b",
        "js/tnr-api.js' %}?v=20260919a",
        'all_version_query_params_are_identical',
    ),
    (
        # ⚠ M11 / M12 里写死了当前版本号 —— **每次升 `?v=` 都要同步改这两条**，
        # 否则预检会报「命中 0 次」。第二十四轮升 `20260919a → b` 时就踩到了。
        'M12 某个门户的 tnr-api.js 去掉版本号',
        'templates/portal/adopter/portal.html',
        "js/tnr-api.js' %}?v=20260919b",
        "js/tnr-api.js' %}",
        'versioned_in_every_portal',
    ),
    (
        'M13 侧栏去掉「黑名单管理」项（标签与 navMap 失配）',
        'templates/portal/shelter_base.html',
        '      <a class="nav-item {% block nav_blacklist %}{% endblock %}" href="{% url \'shelter_home\' %}">\n'
        '        <span class="nav-item-icon">⛔</span><span class="nav-item-label">黑名单管理</span>\n'
        '      </a>\n',
        '',
        'sidebar_labels_and_navmap_are_in_sync',
    ),
    (
        'M14 navMap 去掉「黑名单管理」映射（标签点了没反应）',
        'templates/portal/shelter/portal.html',
        "      '黑名单管理': 'blacklist',\n",
        '',
        'sidebar_labels_and_navmap_are_in_sync',
    ),
    (
        # 原缺陷的**完整复现**：侧栏没有入口、navMap 也没有映射。
        # 此时侧栏与 navMap「彼此一致」，同步检查抓不到 ——
        # 只有「页面可达性」检查能抓到。这条用来证明那个新守卫**不是摆设**。
        'M15 侧栏与 navMap 同时去掉（= 原缺陷：整页不可达）',
        'templates/portal/shelter_base.html',
        '      <a class="nav-item {% block nav_blacklist %}{% endblock %}" href="{% url \'shelter_home\' %}">\n'
        '        <span class="nav-item-icon">⛔</span><span class="nav-item-label">黑名单管理</span>\n'
        '      </a>\n',
        '',
        'every_page_view_is_reachable_from_navigation',
        [('templates/portal/shelter/portal.html', "      '黑名单管理': 'blacklist',\n", '')],
    ),
]

# ---------- 第二十三轮：存在性预言机（按 id 取单）----------
MUTATIONS += [
    (
        # 原缺陷的**完整复现**：裸取 + 状态检查排在机构检查之前。
        # 于是「他院的单」会被状态分支接走，把**真实状态**写进错误文案。
        'M16 复现原缺陷：裸取 + 状态检查早于机构检查', 'business/views_transfer.py',
        "    transfer = get_own_institution_object(Transfer, pk, user, 'to_hospital_id')\n"
        "    if transfer is None:\n"
        "        return json_fail('转运记录不存在或无权访问', status=404)\n"
        "\n"
        "    if transfer.status != 'pending':\n"
        "        return json_fail(f'当前状态({transfer.status})不可签收')\n",
        "    try:\n"
        "        transfer = Transfer.objects.get(id=pk)\n"
        "    except Transfer.DoesNotExist:\n"
        "        return json_fail('转运记录不存在', status=404)\n"
        "\n"
        "    if transfer.status != 'pending':\n"
        "        return json_fail(f'当前状态({transfer.status})不可签收')\n"
        "\n"
        "    if user.institution_id != transfer.to_hospital_id:\n"
        "        return json_fail('无权签收此转运记录')\n",
        'test_receive_does_not_leak_status',
    ),
    (
        # 修预言机时**最容易犯的错**：顺手把机构收敛换成区县收敛。
        # 转运本就允许跨区县送医 —— 这么改会掐断正常路径。
        'M17 机构收敛错换成区县收敛（会掐断跨区县送医）', 'business/views_transfer.py',
        "    transfer = get_own_institution_object(Transfer, pk, user, 'to_hospital_id')\n"
        "    if transfer is None:\n"
        "        return json_fail('转运记录不存在或无权访问', status=404)\n"
        "\n"
        "    if transfer.status != 'pending':\n"
        "        return json_fail(f'当前状态({transfer.status})不可签收')\n",
        "    transfer = get_district_filtered_queryset(Transfer, user).filter(pk=pk).first()\n"
        "    if transfer is None:\n"
        "        return json_fail('转运记录不存在或无权访问', status=404)\n"
        "\n"
        "    if transfer.status != 'pending':\n"
        "        return json_fail(f'当前状态({transfer.status})不可签收')\n",
        'test_cross_district_receive_succeeds',
    ),
    (
        # 共用函数本体：把机构过滤丢掉 → 退化成裸取，预言机原样回来。
        'M18 共用取数函数丢掉机构过滤', 'business/services.py',
        "    return qs.filter(pk=pk, **{field: user.institution_id}).first()",
        "    return qs.filter(pk=pk).first()",
        'test_returns_none_for_other_institution',
    ),
    (
        # 「账号没有机构」时**静默放行**（而不是返回 None）——
        # `if user.institution_id and ...` 这类写法在字段为空时判据整个失效。
        'M19 账号无机构时静默放行（判据静默失效）', 'business/services.py',
        "    if not user.institution_id:\n"
        "        return None\n"
        "    qs = model.objects.all()\n",
        "    qs = model.objects.all()\n"
        "    if not user.institution_id:\n"
        "        return (qs.filter(**extra) if extra else qs).filter(pk=pk).first()\n",
        'test_receive_account_without_institution_refused',
    ),
    (
        # `material_receive` 靠 `**extra` 锁定 `type='dispatch'`；
        # 丢掉它，**本院的采购单**也能被当成下发单签收。
        #
        # ⚠ 本条变异暴露了一处**存量用例空转**（第二十三轮实测）：
        # `test_material_views.test_receive_non_dispatch_txn_rejected` 造的采购单
        # **没有 `hospital`**，于是它其实是被**机构过滤**挡掉的、与 `type` 无关 ——
        # 删掉 `type='dispatch'` 它照样绿。夹具已补上 `hospital=self.hospital_a`，
        # 并在 `test_own_institution_scope` 里加了视图级 + 「无连带写入」的断言。
        'M20 丢掉 extra 过滤（本院采购单被当成下发单）', 'business/views_material.py',
        "        MaterialTransaction, pk, user, 'hospital_id', type='dispatch')",
        "        MaterialTransaction, pk, user, 'hospital_id')",
        'test_material_receive_rejects_non_dispatch_on_own_hospital',
    ),
    (
        # 契约扫描退回**文本匹配** —— 三个已改写的接口源码里没有字面量
        # `institution_id` 了，会被全部误报为「缺失」。这条证明 AST 升级是承重的。
        'M21 契约扫描退回文本匹配（误报三个已改接口）',
        'business/tests/test_write_scope.py',
        "            ok, _why = has_institution_check(src)\n"
        "            if not ok:\n",
        "            ok = 'institution_id' in src\n"
        "            if not ok:\n",
        'test_every_hospital_write_view_has_institution_check',
    ),
    (
        # 判据本身退回**朴素文本包含** —— 注释/字符串里提到过函数名就算「有判据」，
        # 这正是第二十二轮「自己的注释把可达性检查骗过」的同款空转。
        'M22 判据退回朴素文本包含（被注释/字符串骗过）',
        'business/tests/test_write_scope.py',
        "    try:\n"
        "        tree = ast.parse(src)\n"
        "    except SyntaxError as exc:          # 解析不了 = 判不了，按「缺失」处理（宁可误报）\n"
        "        return False, f'源码无法解析：{exc}'\n",
        "    if 'institution_id' in src or 'get_own_institution_object' in src:\n"
        "        return True, '朴素文本匹配'\n"
        "    tree = ast.parse(src)\n",
        'test_comment_cannot_fake_a_check',
    ),
]

# ---------- 第二十四轮：查询参数契约（类型 / 范围 / 量级）----------
#
# 这一轮的核心是「**非法参数值不能打成 500**」，而最容易犯的假修是
# 「随手包一层 `except ValueError`」。所以变异表里**必须**有一条
# 专门复现这个假修（M33），证明测试真的把它挡下来了 —— 否则「修好了」
# 只是因为恰好撞上了那一种异常类型。
MUTATIONS += [
    (
        # 复现原缺陷：`Q` 的**两半都会被求值**，于是中文区县名会让
        # `district_id='襄城区'` 直接 ValueError ——
        # 「按区县名筛选」这条分支从来没成功过。
        'M23 复现原缺陷：district 拼进 district_id（中文名 500）',
        'business/views_capture.py',
        """        cond = Q(district__name__icontains=district)
        district_id, _err = parse_int_param(district, '区县')
        if district_id is not None:
            cond |= Q(district_id=district_id)
        qs = qs.filter(cond)
""",
        """        qs = qs.filter(Q(district__name__icontains=district) | Q(district_id=district))
""",
        'test_filter_by_district_name_works',
    ),
    (
        'M24 台账 material_id 退回裸过滤（非数字 → 500）',
        'business/views_material.py',
        """    material_id, err = parse_int_param(request.GET.get('material_id'), '物料')
    if err:
        return json_fail(err)
    if material_id:
        qs = qs.filter(material_id=material_id)

    if user.role == 'hospital':
""",
        """    material_id = request.GET.get('material_id')
    if material_id:
        qs = qs.filter(material_id=material_id)

    if user.role == 'hospital':
""",
        'test_poison_material_id_rejected',
    ),
    (
        'M25 捕捉点台账 material_id 退回裸过滤（非数字 → 500）',
        'business/views_material.py',
        """    material_id, err = parse_int_param(request.GET.get('material_id'), '物料')
    if err:
        return json_fail(err)
    if material_id:
        qs = qs.filter(material_id=material_id)

    data = []
""",
        """    material_id = request.GET.get('material_id')
    if material_id:
        qs = qs.filter(material_id=material_id)

    data = []
""",
        'test_poison_material_id_rejected',
    ),
    (
        # **第四种异常类型**：`end_date == date.max` 时 `+1 天` 抛
        # `OverflowError`。注意 `except ValueError` **兜不住它** ——
        # 这正是「随手包一层」的假修在日期这条路径上的形态。
        'M26 台账 end_date 退回无保护的日期加法（date.max → 500）',
        'supervision/views.py',
        """        if end_date:
            # 结束日期含当天：用次日零点开区间，避免当天非零点记录被排除。
            # `end_date == date.max`（9999-12-31）时加一天会 `OverflowError`
            # —— 那是**第四种**异常类型，`except ValueError` 兜不住，
            # 由 `date_upper_exclusive()` 统一处理（退回闭区间上界，语义等价）。
            upper, exclusive = date_upper_exclusive(end_date)
            qs = qs.filter(**{f'{date_field}__{"lt" if exclusive else "lte"}': upper})
""",
        """        if end_date:
            from datetime import timedelta
            qs = qs.filter(**{f'{date_field}__lt': end_date + timedelta(days=1)})
""",
        'test_end_date_date_max_does_not_500',
    ),
    (
        'M27 台账 institution_id 退回裸过滤（非数字 → 500）',
        'supervision/views.py',
        """    institution_id, err = parse_int_param(request.GET.get('institution_id'), '机构')
    if err:
        return json_fail(err)
    business_type = request.GET.get('business_type')
""",
        """    institution_id = request.GET.get('institution_id')
    business_type = request.GET.get('business_type')
""",
        'test_poison_institution_id_rejected',
    ),
    (
        # 去掉业务上限：`?count=` 又能拿到任意大的数量，
        # `generate_pet_codes()` 会真的去循环那么多轮（资源耗尽，C 类缺陷）。
        'M28 编号预览去掉数量上限（数量参数无约束）',
        'business/views_capture.py',
        """    count, err = parse_int_param(
        request.GET.get('count'), '数量', minimum=0, maximum=MAX_CAPTURE_BATCH)
""",
        """    count, err = parse_int_param(
        request.GET.get('count'), '数量', minimum=0)
""",
        'test_count_over_cap_rejected',
    ),
    (
        'M29 真实建单去掉数量上限（与预览端漂移）',
        'business/views_capture.py',
        """    if pet_count > MAX_CAPTURE_BATCH:
        return json_fail('单批捕捉数量不能超过100只，请分批登记')
""",
        None,
        'test_cap_is_shared_with_checkin_create',
    ),
    (
        # 共用解析器只判下界 —— 等价于「只包 `except ValueError`」：
        # 超大数在 `int()` 这一步**不会报错**，于是照样进 ORM，
        # 要等 SQLite 绑定参数才抛 `OverflowError`。
        'M31 共用解析器只判下界（超大数漏进 ORM）',
        'business/services.py',
        """    if value < minimum:
        return None, f'{label}超出有效范围'
    if value > maximum:
        # 业务上限（如单批 100）要报出来，否则用户不知道能填多少；
        # SQLite 量级的上限属于内部实现细节，不外露具体数字。
        if maximum >= MAX_SQLITE_INT:
            return None, f'{label}超出有效范围'
        return None, f'{label}不能超过 {maximum}'
    return value, None
""",
        """    if value < minimum:
        return None, f'{label}超出有效范围'
    return value, None
""",
        # ⚠ 这里**必须**指 `test_over_sqlite_max_rejected`（用 `str(2**63)`，
        # 19 位、刚好通过长度检查），不能指 `test_huge_number_rejected_before_orm`
        # —— 后者用的 `'9' * 24` 是 24 位，在**长度检查**那一关就被挡掉了，
        # 与 `maximum` 分支无关，删掉 `maximum` 分支它照样绿。
        # （第一版就是指错了，报 WEAK；定点复验后改到这里。）
        'test_over_sqlite_max_rejected',
    ),
    (
        # 解析器忘了把字符串转成 `date` —— 字符串进 `__date__gte`
        # 会让 Django 在解析阶段抛 `ValidationError`。
        'M32 日期解析器返回字符串而不是 date 对象',
        'business/services.py',
        """        return datetime.strptime(m.group(0), '%Y-%m-%d').date(), None
""",
        """        return m.group(0), None
""",
        'test_returns_date_object_not_string',
    ),
    (
        # **本轮最重要的一条**：复现「随手包一层 `except ValueError`」这个假修。
        # 非法日期抛的是 `django.core.exceptions.ValidationError`，
        # **不是** `ValueError` —— 于是 500 原样回来。
        # 这条变异证明测试挡得住假修，而不只是挡住「完全没修」。
        'M33 假修：日期只包 except ValueError（ValidationError 漏网）',
        'business/views_capture.py',
        """    start_date, err = parse_date_param(request.GET.get('start_date'), '开始日期')
    if err:
        return json_fail(err)
    end_date, err = parse_date_param(request.GET.get('end_date'), '结束日期')
    if err:
        return json_fail(err)
    if start_date:
        qs = qs.filter(
            Q(return_time__date__gte=start_date)
            | Q(return_time__isnull=True, created_at__date__gte=start_date)
        )
""",
        """    start_date = request.GET.get('start_date', '').strip()
    end_date = request.GET.get('end_date', '').strip()
    try:
        if start_date:
            qs = qs.filter(
                Q(return_time__date__gte=start_date)
                | Q(return_time__isnull=True, created_at__date__gte=start_date)
            )
    except ValueError:
        pass
""",
        'test_poison_dates_rejected_with_400',
    ),
    (
        # 契约扫描忘了 `textwrap.dedent`：嵌套函数/方法的源码带缩进，
        # `ast.parse` 抛 IndentationError → 判据恒返回 False。
        # 于是「注释不能伪装」那条对照会**靠报错通过**，变成空转。
        'M34 契约扫描去掉 dedent（判据退化成恒 False）',
        'business/tests/test_param_contract.py',
        """        src = textwrap.dedent(inspect.getsource(view))
        tree = ast.parse(src)
""",
        """        src = inspect.getsource(view)
        tree = ast.parse(src)
""",
        'test_real_call_counts',
    ),
    (
        # 共用函数本体：库存判据挪到**建流水之后** —— 报错了，
        # 但那条流水已经落库，会出现在捕捉点台账里（孤儿记录）。
        'M30 库存判据挪到建流水之后（孤儿流水）',
        'business/services.py',
        """    if hospital is None and txn_type in ('dispatch', 'consume', 'adjustment'):
        if material.shelter_stock < quantity:
            raise ValueError(f'捕捉点库存不足（当前 {material.shelter_stock}，需 {quantity}）')

    txn = MaterialTransaction.objects.create(
""",
        """    txn = MaterialTransaction.objects.create(
""",
        'test_insufficient_stock_leaves_no_orphan_transaction',
    ),
    (
        # 数量校验挪到「按名称新建物料」之后 —— 报错了，但孤儿物料已经落库。
        'M35 采购数量校验挪到建物料之后（孤儿物料）',
        'business/views_material.py',
        """    quantity, err = parse_int_param(data.get('quantity'), '采购数量', minimum=0)
    if err:
        return json_fail(err)
    if not quantity:
        return json_fail('采购数量必须大于0')

    material_id = data.get('material_id')
""",
        """    material_id = data.get('material_id')
""",
        'test_purchase_zero_quantity_creates_no_orphan_material',
    ),
    (
        # 界面路径的静默失败：`getLedger` 退回 `_get()`。
        # `_get()` **完全忽略 HTTP 状态**，非 2xx 返回 `[]` ——
        # 服务端的 400「机构必须是数字」于是被渲染成一张**空表**。
        'M36 getLedger 退回 _get（失败被静默渲染成空表）',
        'static/js/tnr-api.js',
        """    const res = await fetch(url, { credentials: 'same-origin' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.success) {
      throw new Error(data.message || `台账加载失败（HTTP ${res.status}）`);
    }
    return data.data;
""",
        """    return this._get(url);
""",
        'test_get_ledger_does_not_use_silent_get',
    ),
    (
        # 服务端抛了，界面没接住 —— 表格区既没有数据也没有错误提示，
        # 表现为「卡住」或「空白」，同样属于静默失败。
        'M37 renderLedgerTable 去掉 try/catch（错误没人接）',
        'templates/portal/gov/portal.html',
        """    let records = [];
    try {
      const result = await TNR_API.getLedger(params);
      records = (result && result.records) || [];
    } catch (e) {
      TNR_UI.toast(e.message || '台账加载失败', 'danger');
      document.getElementById('ledgerTableWrapper').innerHTML =
        '<div class="table-empty"><div class="table-empty-icon">⚠️</div>' +
        '<div class="table-empty-text">台账加载失败：' + TNR_UI.escape(e.message) + '</div></div>';
      return;
    }
""",
        """    const result = await TNR_API.getLedger(params);
    let records = (result && result.records) || [];
""",
        'test_render_ledger_table_catches_and_shows_error',
    ),
    (
        # 前端 `max` 与服务端上限漂移：界面能填 200，提交必被 400 拒 ——
        # 「用户一改就撞错」（技能 5.14 的反向形态）。
        'M38 捕捉表单 max 与服务端上限漂移（界面能填、提交被拒）',
        'templates/portal/shelter/portal.html',
        'id="ca_petCount" min="1" max="100"',
        'id="ca_petCount" min="1" max="200"',
        'test_frontend_input_max_matches_backend_cap',
    ),
]

# 把被删掉的判据插回**落库之后** —— 表达「判据晚于写库」这类变异
MOVE_AFTER.update({
    'M30 库存判据挪到建流水之后（孤儿流水）': (
        'business/services.py',
        "        material.save(update_fields=['shelter_stock'])\n",
        "\n    if hospital is None and txn_type in ('dispatch', 'consume', 'adjustment'):\n"
        "        if material.shelter_stock < quantity:\n"
        "            raise ValueError(f'捕捉点库存不足（当前 {material.shelter_stock}，需 {quantity}）')\n",
    ),
    'M35 采购数量校验挪到建物料之后（孤儿物料）': (
        'business/views_material.py',
        "    district_id = material.district_id\n",
        "\n    quantity, err = parse_int_param(data.get('quantity'), '采购数量', minimum=0)\n"
        "    if err:\n"
        "        return json_fail(err)\n"
        "    if not quantity:\n"
        "        return json_fail('采购数量必须大于0')\n",
    ),
})

# 允许 5 元组（单文件变异）与 6 元组（+ 附加改动，用于「复现原缺陷」这类跨文件变异）
MUTATIONS = [m if len(m) == 6 else m + ([],) for m in MUTATIONS]

ENV = dict(os.environ, DEBUG='on', SECRET_KEY='t',
           ALLOWED_HOSTS='localhost,testserver')
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
    ENV.pop(_k, None)


def run_tests():
    cmd = [str(ROOT / '.venv/bin/python'), 'manage.py', 'test', *TEST_TARGETS, '-v', '0']
    r = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


# 信号处理器要用的「原始内容」快照（在 main 里填）
_SNAPSHOT = {}


def _restore_all():
    for rel, text in _SNAPSHOT.items():
        p = ROOT / rel
        if p.read_text() != text:
            p.write_text(text)


def _on_signal(signum, _frame):
    """被杀掉之前先把变异体还原 —— 否则会在工作区留下「已被删掉的判据」。"""
    _restore_all()
    print(f'\n⚠ 收到信号 {signum}：已还原 {len(_SNAPSHOT)} 个文件后退出', flush=True)
    sys.exit(1)


def main():
    files = sorted({m[1] for m in MUTATIONS} | {e[0] for m in MUTATIONS for e in m[5]})
    originals = {f: (ROOT / f).read_text() for f in files}
    _SNAPSHOT.update(originals)
    for _name in ('SIGTERM', 'SIGINT', 'SIGHUP'):
        _sig = getattr(signal, _name, None)
        if _sig is not None:
            signal.signal(_sig, _on_signal)

    results = []
    try:
        for label, rel, old, new, expect, extras in MUTATIONS:
            original = originals[rel]
            hits = original.count(old)
            if hits != 1:
                results.append((label, 'SKIP', f'目标串命中 {hits} 次（应恰好 1 次）'))
                continue

            mutated = original.replace(old, new if new is not None else '', 1)
            if label in MOVE_AFTER:
                arel, anchor, payload = MOVE_AFTER[label]
                if arel != rel:
                    results.append((label, 'SKIP', 'MOVE_AFTER 与目标文件不一致'))
                    continue
                if mutated.count(anchor) != 1:
                    results.append((label, 'SKIP', f'锚点不唯一：{anchor!r}'))
                    continue
                mutated = mutated.replace(anchor, anchor + payload, 1)

            # 附加改动（跨文件变异）：同样要求「恰好命中 1 次」，否则打偏
            payloads = {rel: mutated}
            bad = None
            for erel, eold, enew in extras:
                esrc = originals[erel]
                ehits = esrc.count(eold)
                if ehits != 1:
                    bad = f'附加目标串 {erel} 命中 {ehits} 次（应恰好 1 次）'
                    break
                payloads[erel] = esrc.replace(eold, enew, 1)
            if bad:
                results.append((label, 'SKIP', bad))
                continue

            for f, text in payloads.items():
                (ROOT / f).write_text(text)
            code, out = run_tests()
            for f, text in originals.items():      # 全量还原，天然幂等
                (ROOT / f).write_text(text)

            if code == 0:
                results.append((label, 'FAIL', '变异体下测试**全部通过** —— 空转！'))
            else:
                failed = [ln for ln in out.splitlines()
                          if ln.startswith('FAIL:') or ln.startswith('ERROR:')]
                hit = any(expect in ln for ln in failed)
                detail = '; '.join(ln.split(' ')[1] for ln in failed[:4]) or '?'
                results.append((label, 'PASS' if hit else 'WEAK', f'捕获={detail}'))
    finally:
        for f, text in originals.items():
            (ROOT / f).write_text(text)
            assert (ROOT / f).read_text() == text, f'{f} 还原失败！'

    print('=' * 76)
    ok = True
    for label, verdict, detail in results:
        mark = {'PASS': '✅', 'FAIL': '❌', 'SKIP': '⚠️ ', 'WEAK': '⚠️ '}[verdict]
        if verdict != 'PASS':
            ok = False
        print(f'{mark} {label}\n     {verdict}: {detail}')
    print('=' * 76)
    print('全部变异均被捕获' if ok else '存在未被捕获的变异 —— 测试判别力不足')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
