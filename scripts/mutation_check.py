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

   只验本轮新增的几条（全表 190+ 条并逐轮递增，一轮约 1 分钟，跑完要几小时
   —— 准确条数看启动时的 `[n/total]` 心跳）：

       MUTATION_LABELS=M18,M19 .venv/bin/python scripts/mutation_check.py

   只验「本轮改动波及的那批」（一行一条完整标签）：

       MUTATION_LABELS_FILE=/tmp/mut.txt .venv/bin/python scripts/mutation_check.py

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
import fcntl
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _preflight_asset_gate():
    """开跑前确认「静态资源缓存键」闸门还立在原地（第五十二轮重写）。

    以前这里从 `templates/base.html` 抠手写 `?v=` 版本号，供 M11/M12/M47 拼锚点。
    那套写法有两层病灶：
      - 版本号写死在锚点里 → 每次升 `?v=` 都要同步改本文件，第二十四/二十五轮改过，
        升到 `20261019a` 时漏改，两条变异**命中 0 次 → SKIP**，从此无人验证；
      - 更根本的是：**手写日期串本身就是缺陷** —— 改了 `static/` 而忘了升版本，
        浏览器继续跑旧文件，而源码判据全绿（它查的是磁盘）。
    现已改为 `{% asset_url %}` 按内容哈希出键，模板里**不该再有任何 `?v=`**。
    所以闸门从「版本号还在不在」换成「`base.html` 有没有走 `asset_url`」：
    一旦有人退回手写版本或直接 `{% static %}`，这里立刻报错，
    而不是让下面三条变异静默 SKIP。
    """
    src = (ROOT / 'templates' / 'base.html').read_text(encoding='utf-8')
    if "{% asset_url 'js/tnr-common.js' %}" not in src:
        raise SystemExit(
            'templates/base.html 里的 tnr-common.js 不再走 {% asset_url %} '
            '—— 内容哈希缓存闸门已失效，M11/M12/M47/M65–M67 无法验证')
    if not (ROOT / 'core' / 'templatetags' / 'assets.py').exists():
        raise SystemExit('core/templatetags/assets.py 不存在 —— 内容哈希闸门已失效')


_preflight_asset_gate()

# 测试目标：默认跑本轮新增的用例文件（跑全量会很慢）。
# 可在命令行覆盖：`.venv/bin/python scripts/mutation_check.py 业务.用例 另一.用例`
#
# ⚠ 收窄/放宽**权限**的变异（M7 这类）会打到存量用例上 ——
# 第二十二轮就漏了 `business.tests.test_blacklist_views`（4 条按旧口径写的用例），
# 变异脚本当时「全绿」，是**全量套件**才把它们翻出来。
# 所以凡涉及角色集合的变异，目标列表里必须带上受影响的存量模块。
# ⚠ 每条变异的「期望失败用例」所在模块**必须登记在下面**，否则那条变异只会
# 得到「变异体下测试全部通过 —— 空转!」的**假指控**（用例根本没被跑到，
# 与「断言空转」是两回事）。补新用例开了新模块，这里要一并加上。
TEST_TARGETS = sys.argv[1:] or [
    'business.tests.test_write_scope',
    'business.tests.test_own_institution_scope',
    'business.tests.test_param_contract',
    'business.tests.test_silent_read_contract',
    'business.tests.test_transfer_views',
    'business.tests.test_material_views',
    'business.tests.test_endpoint_role_scope',
    'business.tests.test_blacklist_views',
    'business.tests.test_euthanasia_views',
    'business.tests.test_portal_views',
    'business.tests.test_treatment_flow',
    'business.tests.test_discharge_views',
    # 打印取单的「本机构 ∪ 区县」口径（M195–M197）。单号/打印页用例只在**全量套件**
    # 里跑到过，本表以前没收它 → 这三条判据的判别力长期无人验证。
    'business.tests.test_doc_numbers',
    # 放归两段的守卫用例（M81/M82）住在这两个模块里，本表以前没登记它们 ——
    # 第六十一轮全表复扫时它们各吃到一条**假指控**「变异体下测试全部通过 —— 空转!」：
    # 用例根本没被跑到，与「断言空转」是两回事（见上面第 103 行的老警告）。
    'business.tests.test_adoption_views',
    'business.tests.test_release_views',
    # 一宠一档 A4 档案打印页（M200–M203）的用例住在这里。
    'business.tests.test_pet_archive',
    # 第六十六轮：回访驳回闭环（M230–M234）与捕捉单状态单一真源（M236–M243）
    # 的判据用例分别住在这两个模块里。本表以前没登记它们 —— 不登记就是
    # 「变异体下测试全部通过 —— 空转!」的**假指控**（用例根本没跑到）。
    'business.tests.test_checkin_views',
    'business.tests.test_capture_views',
    'core.tests_audit',
    'core.tests_frontend_consistency',
    'core.tests_data_integrity',
    'supervision.tests',
    'supervision.tests_doc_maintenance',
]

# 全表 190+ 条 × 一轮 1 分钟 ≈ 3 小时，本轮改判据不可能每次都等它。
# `MUTATION_LABELS=M18,M19` 只跑标签含这些片段的变异（预检仍然扫全表，
# 所以失效锚点照样会在测试启动前点名）；收尾仍需不带过滤地全表复扫。
#
# `MUTATION_LABELS_FILE=/tmp/x.txt`（一行一条**完整标签**）用于「本轮改动波及
# 哪几条就跑哪几条」这种批量定点复扫 —— 逗号分隔那种表达不了带逗号的标签
# （例：`M60 入库时间改用 slice(0,10) 取 UTC 日期`），也不会被 shell 的
# 引号/分词问题绊倒。
MUTATION_LABELS = [s for s in os.environ.get('MUTATION_LABELS', '').split(',') if s]
_LABELS_FILE = os.environ.get('MUTATION_LABELS_FILE', '')
if _LABELS_FILE:
    MUTATION_LABELS += [
        ln.strip() for ln in pathlib.Path(_LABELS_FILE).read_text().splitlines()
        if ln.strip() and not ln.startswith('#')]

# (标签, 目标文件相对路径, old, new, 期望失败的测试名)
# new 为 None 表示「删除该段」；配合 move_after 可表达「挪位」
MUTATIONS = [
    (
        # 第六十一轮重挂：`adoption_info_edit` 的机构判据已不带
        # `user.role == 'hospital'` 前缀（角色由装饰器分流），且单行串在
        # `adoption_application_outbound` 里同名出现 → 锚点必须带注释行。
        'M1 删掉机构判据（复现原洞）', 'business/views_adoption.py',
        "    # 医院只能动**当前挂在本院名下**的宠物 —— 与同类写接口口径一致。\n"
        "    # 放在任何落库动作之前：否则会先建/改 listing 再报错，留下半截写入。\n"
        "    if pet.hospital_id != user.institution_id:\n"
        "        return json_fail('无权编辑此动物的领养信息')\n",
        None,
        # ⚠ 不能指 `cross_institution` —— 那条用例的受害宠物压根不在本院的
        # 取单范围里，`get_hospital_scoped_pet()` 先 404，判据删了它照样绿
        #（第六十一轮实测：M1 长期 WEAK，红的全是源码契约用例）。
        'test_edit_info_refused_for_pet_moved_to_other_hospital',
    ),
    (
        'M2 判据反转（!= 改 ==）', 'business/views_adoption.py',
        "    if pet.hospital_id != user.institution_id:\n"
        "        return json_fail('无权编辑此动物的领养信息')\n",
        "    if pet.hospital_id == user.institution_id:\n"
        "        return json_fail('无权编辑此动物的领养信息')\n",
        'own_pet_ok',
    ),
    (
        # 判据换成区县维度：同区县另一家医院的宠物照样放行（原洞的形状）。
        'M3 比错字段（institution_id → district_id）', 'business/views_adoption.py',
        "    if pet.hospital_id != user.institution_id:\n"
        "        return json_fail('无权编辑此动物的领养信息')\n",
        "    if pet.hospital_id != user.district_id:\n"
        "        return json_fail('无权编辑此动物的领养信息')\n",
        'own_pet_ok',
    ),
    (
        'M4 判据挪到落库之后', 'business/views_adoption.py',
        "    # 医院只能动**当前挂在本院名下**的宠物 —— 与同类写接口口径一致。\n"
        "    # 放在任何落库动作之前：否则会先建/改 listing 再报错，留下半截写入。\n"
        "    if pet.hospital_id != user.institution_id:\n"
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
        "\n    if pet.hospital_id != user.institution_id:\n"
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
        'M11 门户引入退回手写 ?v= 日期串（改了 JS 浏览器仍跑旧版）',
        'templates/portal/hospital/portal.html',
        "{% asset_url 'js/tnr-api.js' %}",
        "{% static 'js/tnr-api.js' %}?v=20261019b",
        'test_no_manual_version_stamp_survives_in_templates',
    ),
    (
        # 原先这两条把 `?v=` 版本号写死在锚点里，靠 `_asset_version()` 从 base.html 派生；
        # 版本号一到 20261019a 时锚点命中 0 次、静默 SKIP 过（见上面的历史说明）。
        # 第五十二轮把缓存键换成内容哈希后，锚点不再含任何需要人维护的值，
        # 判据也从「这些版本号彼此相等」换成「人工版本号必须绝迹」。
        'M12 某个门户的 tnr-api.js 不带缓存键（直接 {% static %}）',
        'templates/portal/adopter/portal.html',
        "{% asset_url 'js/tnr-api.js' %}",
        "{% static 'js/tnr-api.js' %}",
        'test_every_static_include_goes_through_asset_url',
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
        'static/js/portals/shelter.js',
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
        [('static/js/portals/shelter.js', "      '黑名单管理': 'blacklist',\n", '')],
    ),
]

# ---------- 第二十三轮：存在性预言机（按 id 取单）----------
MUTATIONS += [
    (
        # 原缺陷的**完整复现**：裸取 + 状态检查排在机构检查之前。
        # 于是「他院的单」会被状态分支接走，把**真实状态**写进错误文案。
        'M16 复现原缺陷：裸取 + 状态检查早于机构检查', 'business/views_transfer.py',
        # 第四十八轮把 `transfer_receive` / `transfer_reject` 写成了同款，
        # 取单那三行在文件里命中 2 次 —— 用**上一行注释**把它钉到签收那一处。
        "    # 也不能按区县收敛：转运允许跨区县送医，那会打断正常路径。\n"
        "    transfer = get_own_institution_object(Transfer, pk, user, 'to_hospital_id')\n"
        "    if transfer is None:\n"
        "        return json_fail('转运记录不存在或无权访问', status=404)\n"
        "\n"
        "    if transfer.status not in Transfer.OPEN_STATUSES:\n"
        "        return json_fail(f'当前状态({transfer.status})不可签收')\n",
        "    # 也不能按区县收敛：转运允许跨区县送医，那会打断正常路径。\n"
        "    try:\n"
        "        transfer = Transfer.objects.get(id=pk)\n"
        "    except Transfer.DoesNotExist:\n"
        "        return json_fail('转运记录不存在', status=404)\n"
        "\n"
        "    if transfer.status not in Transfer.OPEN_STATUSES:\n"
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
        # 同 M16：取单三行在两处同款，用上一行注释钉住签收那处。
        "    # 也不能按区县收敛：转运允许跨区县送医，那会打断正常路径。\n"
        "    transfer = get_own_institution_object(Transfer, pk, user, 'to_hospital_id')\n"
        "    if transfer is None:\n"
        "        return json_fail('转运记录不存在或无权访问', status=404)\n"
        "\n"
        "    if transfer.status not in Transfer.OPEN_STATUSES:\n"
        "        return json_fail(f'当前状态({transfer.status})不可签收')\n",
        "    # 也不能按区县收敛：转运允许跨区县送医，那会打断正常路径。\n"
        "    transfer = get_district_filtered_queryset(Transfer, user).filter(pk=pk).first()\n"
        "    if transfer is None:\n"
        "        return json_fail('转运记录不存在或无权访问', status=404)\n"
        "\n"
        "    if transfer.status not in Transfer.OPEN_STATUSES:\n"
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

    data = [serialize_instance(t) for t in qs]
""",
        """    material_id = request.GET.get('material_id')
    if material_id:
        qs = qs.filter(material_id=material_id)

    data = [serialize_instance(t) for t in qs]
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
            q &= Q(**{f'{date_field}__{"lt" if exclusive else "lte"}': upper})
""",
        """        if end_date:
            from datetime import timedelta
            q &= Q(**{f'{date_field}__lt': end_date + timedelta(days=1)})
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
        'business/services_stock.py',
        """    if (txn_type in OUTBOUND_TXN_TYPES
            and (inst is None or inst.type == 'shelter')):
        if inst is not None:
            current = get_institution_stock(material, inst)
            if current < quantity:
                raise ValueError(
                    f'{inst.name}库存不足（当前 {current}，需 {quantity}）')
        elif material.shelter_stock < quantity:
            raise ValueError(f'捕捉点库存不足（当前 {material.shelter_stock}，需 {quantity}）')

    # ---- 批次计划（B5）----
""",
        """    # ---- 批次计划（B5）----
""",
        'test_insufficient_stock_leaves_no_orphan_transaction',
    ),
    (
        # 只把「数量必须大于 0」这条**业务拒绝**挪进事务、挪到建物料之后 ——
        # 解析仍在前（不然芯片路径会 NameError，那是另一种错，抓的东西就不纯了）。
        # `return json_fail()` 从 `with transaction.atomic():` 里**正常退出是不回滚的**，
        # 于是物料已建、请求报错、台账里凭空多一条没有流水的物料。
        'M35 采购数量校验挪到建物料之后（孤儿物料）',
        'business/views_material.py',
        """    quantity, err = parse_int_param(data.get('quantity'), '采购数量', minimum=0)
    if err:
        return json_fail(err)
    if not quantity:
        return json_fail('采购数量必须大于0')
""",
        """    quantity, err = parse_int_param(data.get('quantity'), '采购数量', minimum=0)
    if err:
        return json_fail(err)
""",
        'test_purchase_zero_quantity_creates_no_orphan_material',
    ),
    (
        # 界面路径的静默失败：`getLedger` 退回 `_get()`。
        # `_get()` **完全忽略 HTTP 状态**，非 2xx 返回 `[]` ——
        # 服务端的 400「机构必须是数字」于是被渲染成一张**空表**。
        'M36 getLedger 退回 _get（失败被静默渲染成空表）',
        'static/js/tnr-api.js',
        # ⚠ 第二十五轮把状态感知逻辑提成了 `getData()`，`getLedger` 改成委托它 ——
        # 原来的「内联 fetch + throw」目标串已经不存在了（预检报「命中 0 次」）。
        # 所以这里改成拦「委托」这一句。
        """    // 台账接口的失败必须**抛出去**，由调用方决定怎么显示。
    return this.getData(url);
""",
        """    // 台账接口的失败必须**抛出去**，由调用方决定怎么显示。
    return this._get(url);
""",
        'test_get_ledger_does_not_use_silent_get',
    ),
    (
        # 服务端抛了，界面没接住 —— 表格区既没有数据也没有错误提示，
        # 表现为「卡住」或「空白」，同样属于静默失败。
        'M37 renderLedgerTable 去掉 try/catch（错误没人接）',
        'static/js/portals/gov.js',
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
        'static/js/portals/shelter.js',
        'id="ca_petCount" min="1" max="100"',
        'id="ca_petCount" min="1" max="200"',
        'test_frontend_input_max_matches_backend_cap',
    ),
    (
        # 严格读退回软版本 —— catch 又变成「承诺了却兑现不了」。
        # 第六十一轮重挂：一宠一档随 `showPetArchive` 迁到了 `tnr-common.js`，
        # shelter.js 只剩一行委派（旧锚点命中 0 次，只会 SKIP）。
        'M39 一宠一档退回静默读（死 catch 复现）',
        'static/js/tnr-common.js',
        'data = await TNR_API.getPetLifecycleStrict(petId);',
        'data = await TNR_API.getPetLifecycle(petId);',
        'test_archive_impl_is_shared',
    ),
    (
        # 严格读内核不抛错 —— 整轮修复的地基没了（`XxxStrict` 全变成软版本）。
        'M40 getData 去掉抛错（严格读内核失效）',
        'static/js/tnr-api.js',
        """    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      this._handleUnauthorized(data.message);
      throw new Error(data.message || '请先登录');
    }
    if (!res.ok || !data.success) {
      throw new Error(data.message || `加载失败（HTTP ${res.status}）`);
    }
    return data.data;
  },
  async getLedger(filters) {""",
        """    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      this._handleUnauthorized(data.message);
      throw new Error(data.message || '请先登录');
    }
    return data.data;
  },
  async getLedger(filters) {""",
        'test_get_data_throws_with_server_message',
    ),
    (
        # `XxxStrict` 偷偷走 `_get` —— 名字是严格读、行为是静默读，最难查的一种。
        'M41 getTransfersStrict 偷偷走 _get（名实不符）',
        'static/js/tnr-api.js',
        "  async getTransfersStrict() { return this.getData('/api/business/transfers/'); },",
        "  async getTransfersStrict() { return this._get('/api/business/transfers/'); },",
        'test_every_strict_method_delegates_to_getdata',
    ),
    (
        # 还原「`${` 抹了、闭 `}` 没抹」—— 大括号多一个闭括号，
        # `match_brace` 提前收尾、方法体被静默截断（位置全对，只有结构坏了）。
        'M42 词法器闭 `}` 原样输出（结构静默截断）',
        'scripts/jslex.py',
        """                    emit(' ', 'template')
                    stack.pop()          # 回到模板字符串""",
        """                    emit('}', 'template')
                    stack.pop()          # 回到模板字符串""",
        'test_audit_passes_on_every_frontend_file',
    ),
    (
        # `<` 加回正则前导集 —— 它本身不单独出事，但会把「一处失步」
        # 放大成「整段代码被吞」（第二十五轮实测：13 处 try 吞到只剩 3 处）。
        'M43 `<` 加回正则前导集（失步放大器）',
        'scripts/jslex.py',
        "REGEX_PRECEDERS = set('(,=:[!&|?{};')",
        "REGEX_PRECEDERS = set('(,=:[!&|?{};<>')",
        'test_regex_preceder_set_is_exactly_the_standard_heuristic',
    ),
    (
        # 「正则体里出现引号就判否」—— 看着更安全，实际会误杀 `/"/g`，
        # 于是整段 `.replace(/"/g, '""')` 失步（shelter 第 3462 行实测）。
        'M44 正则判否加回「体里有引号」（误杀合法正则）',
        'scripts/jslex.py',
        """        if d == '\\n':
            return None
        if d == '[':""",
        """        if d == '\\n':
            return None
        if d in ('"', "'", '`') and not in_class:
            return None
        if d == '[':""",
        'test_quoted_regex_is_not_mistaken_for_a_string',
    ),
]

# ---- 第五十二轮 重构 B：外壳 block 的 `{{ block.super }}` 与门户自有 JS 的版本号 ----
#
# M45/M46 复现的是本轮**实测到**的缺陷：`portal.html` 覆写 `{% block extra_js %}`
# 却没写 `{{ block.super }}`，把外壳那段移动端侧栏开关脚本整段顶掉 ——
# 页面照样 200、电脑端照样能用，只有手机上侧栏再也展不开。
# 判据 = `PortalShellTest.test_real_portals_keep_the_mobile_sidebar_toggle`。
MUTATIONS += [
    (
        'M45 shelter 的 extra_js 丢掉 {{ block.super }}（移动端侧栏打不开）',
        'templates/portal/shelter/portal.html',
        '{% block extra_js %}\n{{ block.super }}',
        '{% block extra_js %}',
        'keep_the_mobile_sidebar_toggle',
    ),
    (
        'M46 gov 的 extra_js 丢掉 {{ block.super }}（换一个端复现）',
        'templates/portal/gov/portal.html',
        '{% block extra_js %}\n{{ block.super }}',
        '{% block extra_js %}',
        'keep_the_mobile_sidebar_toggle',
    ),
    (
        # 与 M12 的区别：M12 打的是公共的 `tnr-api.js`，这条打的是**门户自有 JS** ——
        # 那两个引入点各写各的，漏升/漏键常常只发生在其中一处。
        # 与 M65 的区别：M65 是「退回手写版本号」，这条是「整段不带缓存键」。
        'M47 门户自有 JS 整段不带缓存键',
        'templates/portal/shelter/portal.html',
        "{% asset_url 'js/portals/shelter.js' %}",
        "{% static 'js/portals/shelter.js' %}",
        'test_every_static_include_goes_through_asset_url',
    ),
    # ---- M48–M54：物料流水类型「枚举单一来源」判据（第五十二轮 重构 B）----
    # 抽表之前三端各有本地映射表，判据只能逐表查「这张全不全」；
    # 抽成 `TNR_UI.MATERIAL_TXN_TYPES` 之后判据变成「只有一份，且它全」，
    # 于是**每一条都要重新证明它抓得住** —— 尤其要防「表被搬走后判据空转」。
    (
        'M48 真源少一条 receive（界面漏英文码）',
        'static/js/tnr-common.js',
        "    receive:    { long: '签收入库', short: '签收', badge: 'cinnabar' },\n",
        '',
        'test_single_source_covers_all_material_types',
    ),
    (
        'M49 purchase 少 short 字段（紧凑列渲染成 undefined）',
        'static/js/tnr-common.js',
        "purchase:   { long: '采购入库', short: '采购', badge: 'success' }",
        "purchase:   { long: '采购入库', badge: 'success' }",
        'test_single_source_entries_are_fully_populated',
    ),
    (
        # 键名拼错**不会报错**，只会静默不生效 —— 医院端又显示成「下发出库」。
        # 这类「改完看不出、下轮才被发现」的洞必须有用例钉着。
        'M50 视角表键名写错成 dispath（静默不生效）',
        'static/js/tnr-common.js',
        "hospital: { dispatch: { long: '捕捉点下发', short: '捕捉点下发' } }",
        "hospital: { dispath: { long: '捕捉点下发', short: '捕捉点下发' } }",
        'test_perspective_override_only_renames_known_types',
    ),
    (
        'M51 有人在监管端又抄一张本地类型表',
        'static/js/portals/gov.js',
        '      const shelterByDistrict = {};',
        "      const shelterByDistrict = {};\n"
        "      const typeMap = { purchase: '采购入库', dispatch: '下发出库',\n"
        "        receive: '签收入库', consume: '消耗', adjustment: '调整',\n"
        "        outbound: '出库' };",
        'test_no_portal_redeclares_material_type_map',
    ),
    (
        'M52 捕捉点筛选退回硬编码下拉',
        'static/js/portals/shelter.js',
        "options: TNR_UI.materialTxnOptions('long', 'shelter')",
        "options: [{ value: 'purchase', label: '采购入库' },"
        " { value: 'dispatch', label: '下发出库' },"
        " { value: 'receive', label: '签收入库' }, { value: 'consume', label: '消耗' },"
        " { value: 'adjustment', label: '调整' }, { value: 'outbound', label: '出库' }]",
        'test_no_portal_redeclares_material_type_map',
    ),
    (
        'M53 监管台账丢掉同源出口（退回裸列，显示枚举码）',
        'static/js/portals/gov.js',
        "{ title: '类型', render: r => TNR_UI.materialTxnBadge(r.type, 'long', 'gov') },",
        "{ title: '类型', key: 'type' },",
        'test_material_filter_uses_the_single_source',
    ),
    (
        # `form` 打成 `'shot'` 时真源返回 null、界面退回枚举码：
        # 靠「调用点参数必须合法」这条判据拦，而不是靠肉眼。
        'M54 调用点 form 参数拼错成 shot',
        'static/js/portals/shelter.js',
        "TNR_UI.materialTxn(rec.type, 'short', 'shelter')",
        "TNR_UI.materialTxn(rec.type, 'shot', 'shelter')",
        'test_material_call_sites_pass_a_known_form_and_view',
    ),

    # ---- M55–M56：出院弹窗「医生已建议」映射与后端去向 key（第五十二轮）----
    (
        # 后端 `Treatment.DISPOSAL_CHOICES` 加一个 key、前端那张手写表没跟上，
        # 表现不是报错而是静默：弹窗弹一句「诊疗完成时没记去向建议」。
        'M55 去向映射漏一条 euthanized（建议静默丢掉）',
        'static/js/portals/hospital.js',
        "      pending_adopt: 'adopt', dead: 'dead', euthanized: 'dead',\n",
        "      pending_adopt: 'adopt', dead: 'dead',\n",
        'test_advised_kind_covers_every_backend_disposal_key',
    ),
    (
        # 值写错成弹窗 OPTS 里不存在的 kind：`suggested` 永远匹配不上任何按钮，
        # 高亮与标签一起失效，而且两处代码各自都「看起来没问题」。
        'M56 去向映射的值拼成不存在的单据类型 died',
        'static/js/portals/hospital.js',
        "      owner_returned: 'owner', released: 'release'\n",
        "      owner_returned: 'owner', released: 'died'\n",
        'test_advised_kind_targets_a_real_discharge_option',
    ),

    # ---- M57–M58：首页待办「待诊疗 / 待出院」分流（第五十二轮电脑端巡检）----
    (
        # 退回到「按 `Pet.status` 一刀切」：放归两段的动物（诊疗单已办结、
        # 动物还在院）会被重新挂成「待诊疗」，而诊疗看板里根本没有它。
        # 这是本轮在浏览器里实测到的缺陷，不是假想。
        'M57 首页待办退回按 Pet.status 一刀切（待诊疗/待出院不分）',
        'static/js/portals/hospital.js',
        "      pendTreatPets.forEach(p => {\n        todoItems.push({icon: '🔬', title: '待诊疗：'",
        "      inHouse.forEach(p => {\n        todoItems.push({icon: '🔬', title: '待诊疗：'",
        'test_pending_treatment_is_split_by_latest_record',
    ),
    (
        # 卡片与列表分家：数字用一刀切的 `inHouse`，列表用分流后的数组 ——
        # 「待诊疗任务 5」配三条待办，用户按数字行动就会扑空。
        'M58 待诊疗卡片与待办列表不同源（数字与列表分家）',
        'static/js/portals/hospital.js',
        "      const pendTreat = pendTreatPets.length;",
        "      const pendTreat = inHouse.length;",
        'test_stat_card_shares_the_todo_source',
    ),
]

# 把被删掉的判据插回**落库之后** —— 表达「判据晚于写库」这类变异
MOVE_AFTER.update({
    'M30 库存判据挪到建流水之后（孤儿流水）': (
        'business/services_stock.py',
        "    # ---- 库存落账 ----\n",
        "\n    if (txn_type in OUTBOUND_TXN_TYPES\n"
        "            and (inst is None or inst.type == 'shelter')):\n"
        "        if inst is not None:\n"
        "            current = get_institution_stock(material, inst)\n"
        "            if current < quantity:\n"
        "                raise ValueError(\n"
        "                    f'{inst.name}库存不足（当前 {current}，需 {quantity}）')\n"
        "        elif material.shelter_stock < quantity:\n"
        "            raise ValueError(f'捕捉点库存不足（当前 {material.shelter_stock}，需 {quantity}）')\n",
    ),
    'M35 采购数量校验挪到建物料之后（孤儿物料）': (
        'business/views_material.py',
        # ⚠ 锚点**必须落在事务里面**（`Material.objects.create()` 之后）。
        # 早期版本打在「建物料」之前的 `district_id = material.district_id` 上，
        # 后来采购把建物料整体挪进了 `transaction.atomic()`，那个位置**还在写库之前**，
        # 于是这条变异变成纯位移、行为完全等价 → 判据全绿 = 空转（第五十二轮实测）。
        # 现在这一行在事务内、物料已建好：`return json_fail()` 从 `with` 里正常退出
        # **不回滚**，孤儿物料真的会落库，判据才有东西可抓。
        "            institution = _receiving_institution(user, material)\n",
        "\n            if not quantity:\n"
        "                return json_fail('采购数量必须大于0')\n",
    ),
})

# ---- M59–M64：表格列的日期格式化 + 处置日期必填（第五十二轮电脑端巡检）----
MUTATIONS += [
    (
        # 把列退回「直绑 DateTimeField」：界面上就是
        # `2026-10-16T16:33:51.504093+00:00`。不报错、不 500，只是没人会念给它听。
        # 第六十一轮重挂：领养「登记时间」列已随出院看板改版换成了 `_date` 派生列，
        # 这里改钉物料入库时间列（同一类判据：直绑 created_at 的列必须配 render）。
        'M59 入库时间列退回直绑 created_at',
        'static/js/portals/hospital.js',
        "{ title: '入库时间', key: 'created_at', render: r => this.formatDateTime(r.created_at) },",
        "{ title: '入库时间', key: 'created_at' },",
        'test_no_column_binds_a_datetime_field_raw',
    ),
    (
        # 格式化写了，但用的是 `.slice(0, 10)` —— 拿的是 **UTC 日期**，
        # 北京时间 00:00–08:00 的记录显示成前一天。肉眼看界面完全正常。
        'M60 入库时间改用 slice(0,10) 取 UTC 日期',
        'static/js/portals/hospital.js',
        "{ title: '入库时间', key: 'created_at', render: r => this.formatDateTime(r.created_at) },",
        "{ title: '入库时间', key: 'created_at', render: r => r.created_at.slice(0, 10) },",
        'test_formatter_used_by_those_columns_is_the_shared_one',
    ),
    (
        'M61 处置日期缺失再次静默落成 NULL',
        'business/views_euthanasia.py',
        "    if not date_str:\n        return json_fail(f'{kind_label}日期不能为空')",
        "    if not date_str:\n        euthanized_at = None",
        'test_create_missing_date_rejected',
    ),
    (
        # 与 M61 分开的理由是判据不同：这条防的是**解析失败**被吞掉，
        # 用户填 `2026-3-1` 这类错格式时单据照样创建、日期却是空的。
        'M62 处置日期解析失败再次被吞成 NULL',
        'business/views_euthanasia.py',
        "    except (ValueError, TypeError):\n        return json_fail(f'{kind_label}日期「{date_str}」格式不正确，应为 YYYY-MM-DD')",
        "    except (ValueError, TypeError):\n        euthanized_at = None",
        'test_create_unparseable_date_rejected',
    ),
    (
        # 第六十一轮重挂：出院看板的死亡列表改用派生列 `_date`，
        # 台账那一列仍由 M64 钉住。
        'M63 出院看板死亡列表的处置日期不再兜底格式化',
        'static/js/portals/hospital.js',
        "          {title: '处置日期', key: '_date', sortable: false,\n            render: r => this.formatDate(r._date)},",
        "          {title: '处置日期', key: '_date', sortable: false},",
        'test_euthanasia_date_column_shows_dash_when_null',
    ),
    (
        # 台账与列表是两处独立挂载，只补一边是常态 —— 所以两边各来一刀。
        'M64 安乐死台账处置日期空值不再显示破折号',
        'static/js/portals/hospital.js',
        "          { title: '原因', key: 'reason' },\n          { title: '处置日期', key: 'euthanized_at', render: r => this.formatDate(r.euthanized_at) },",
        "          { title: '原因', key: 'reason' },\n          { title: '处置日期', key: 'euthanized_at' },",
        'test_euthanasia_date_column_shows_dash_when_null',
    ),
]

# ---- M65–M67：静态资源缓存键交给内容哈希（第五十二轮电脑端巡检）----
MUTATIONS += [
    (
        # 退回手写版本号：改了 `static/` 下的文件而忘了升这一处，
        # 浏览器就继续跑旧文件 —— 而**源码判据全绿**，因为它查的是磁盘。
        'M65 门户 JS 引入退回手写 ?v= 日期串',
        'templates/portal/shelter/portal.html',
        "<script src=\"{% asset_url 'js/portals/shelter.js' %}\"></script>",
        "<script src=\"{% static 'js/portals/shelter.js' %}?v=20261019b\"></script>",
        'test_no_manual_version_stamp_survives_in_templates',
    ),
    (
        # 失效判据取整到**秒**：同一秒内连改两次（编辑器保存后立刻再存、
        # 脚本批量写文件）会命中缓存里的旧哈希，「改了没生效」换个形式回来。
        'M66 缓存判据退化到秒级 mtime（同秒改写命中旧哈希）',
        'core/templatetags/assets.py',
        "    marker = (st.st_mtime_ns, st.st_size)",
        "    marker = (int(st.st_mtime), st.st_size)",
        'test_stamp_tracks_file_content_not_a_hand_typed_date',
    ),
    (
        # 忘 `{% load assets %}`：Django 把未知标签渲染成**空串**，
        # `<script src="">` 指向当前页自己，门户逻辑整块消失而不报错。
        'M67 模板忘了 load assets',
        'templates/portal/shelter/portal.html',
        "{% load static assets %}",
        "{% load static %}",
        'test_every_static_include_goes_through_asset_url',
    ),
]

# ---- M68–M72：宠物状态显示真源（第五十二轮电脑端巡检）----
# 同一个 `in_treatment` 曾在监管端 / 捕捉点端 / 医院端分别叫
# 「待诊疗」「诊疗中」「待诊疗/诊疗中」，配色也各挑各的。
# 收成 `TNR_UI.PET_STATUS` 之后，判据从「每张表各自全不全」变成
# 「只有一份，且它跟后端 choices 逐项相等」—— 以下几刀逐条证明新判据有牙。
MUTATIONS += [
    (
        # 文案漂移回老样子：后端 label 是「待诊疗」，前端多写「/诊疗中」。
        # 这一处**没有**任何功能坏掉，纯粹靠元数据判据抓。
        'M68 真源文案与后端 label 漂移（待诊疗/诊疗中）',
        'static/js/tnr-common.js',
        "    in_treatment:   { text: '待诊疗',        badge: 'info' },",
        "    in_treatment:   { text: '待诊疗/诊疗中',  badge: 'info' },",
        'test_pet_status_text_equals_backend_label',
    ),
    (
        # 后端加了新状态而真源没跟：界面直接漏出 `owner_returned` 英文码。
        'M69 真源少一条 owner_returned（漏出英文枚举码）',
        'static/js/tnr-common.js',
        "    owner_returned: { text: '主人领回',      badge: 'default' },\n",
        None,
        'test_pet_status_covers_every_backend_choice',
    ),
    (
        # 配色名拼一个错字：CSS 里没有 `.badge-imfo`，徽章渲染成**没有底色**，
        # 文案还在，所以肉眼看列表时最难发现。
        'M70 配色名拼错（imfo，徽章没有底色）',
        'static/js/tnr-common.js',
        "    in_treatment:   { text: '待诊疗',        badge: 'info' },",
        "    in_treatment:   { text: '待诊疗',        badge: 'imfo' },",
        'test_pet_status_badge_exists_in_css',
    ),
    (
        # 取值函数绕过真源：宠物状态又开始各写各的。
        # 第五十二轮末 `Shelter.statusBadge` 的本地兜底表已删除，
        # `TNR_API` 的两个取值函数也搬进了 `TNR_UI` —— 变异跟到真源旁边。
        'M71 宠物状态取值函数绕开真源（退回本地表）',
        'static/js/tnr-common.js',
        "  petStatusText(status) {\n"
        "    const m = TNR_UI.PET_STATUS[status];\n",
        "  petStatusText(status) {\n"
        "    const m = ({ in_treatment: { text: '诊疗中' } })[status];\n",
        'test_shelter_pet_status_badge_covers_pet',
    ),
    (
        # 别处再长一份表（回归到巡检时的三端三种叫法）。
        'M72 捕捉点端重新写一份宠物状态表',
        'static/js/portals/shelter.js',
        "const Shelter = {\n",
        "const Shelter = {\n"
        "  petStatus: {\n"
        "    in_treatment: ['诊疗中', 'info'],\n"
        "  },\n",
        'test_no_other_frontend_file_maps_pet_status',
    ),
]

# ---- M73–M82：放归两段流程的「未办结」口径与显示真源（第五十二轮电脑端巡检）----
# 这一组对应的缺陷是静默的：放归单进入 `transferred` 之后不报错、不红色，
# 只是**从待放归页签和看板待办里一起消失**，办理人再也点不到「确认放归」。
MUTATIONS += [
    (
        # 前端列表退回只筛 pending —— 中间态单子从界面消失（原缺陷本身）。
        'M73 待放归页签只筛 pending（已回小区待签字的单子看不见）',
        'static/js/portals/shelter.js',
        "      const pendingReleases = releases.filter(r => openReleaseStatus.includes(r.status));",
        "      const pendingReleases = releases.filter(r => r.status === 'pending');",
        'test_release_lists_use_the_open_status',
    ),
    (
        # 看板的「待放归」统计同样漏一段（动物送回小区登记转运后从待办消失）。
        'M74 看板待放归数只数 pending',
        'static/js/portals/shelter.js',
        "    const openRelease = releases.filter(r => TNR_UI.OPEN_RELEASE_STATUS.includes(r.status));",
        "    const openRelease = releases.filter(r => r.status === 'pending');",
        'test_release_lists_use_the_open_status',
    ),
    (
        # 监管端退回二值兜底：released 之外一律「待放归」，两段流程分不出来。
        'M75 监管端放归状态退回「released/其余」二值兜底',
        'static/js/portals/gov.js',
        "{ key: 'status', title: '状态', width: '120px', render: r => TNR_UI.releaseStatusBadge(r.status) }",
        "{ key: 'status', title: '状态', width: '120px', render: r => r.status === 'released' ? "
        "`<span class=\"badge badge-success\">已放归</span>` : `<span class=\"badge badge-warning\">待放归</span>` }",
        'test_no_binary_release_status_fallback',
    ),
    (
        # 真源漏中间态（后端加一段、前端没跟上 —— 漏出英文枚举码）。
        'M76 放归真源少 transferred 一条',
        'static/js/tnr-common.js',
        "    transferred: { text: '已回小区待放归', badge: 'info' },\n", None,
        'test_release_status_covers_every_backend_choice',
    ),
    (
        # 文案与后端 choices 的 label 漂移（单据与界面两种叫法）。
        'M77 放归真源文案与后端 label 漂移',
        'static/js/tnr-common.js',
        "    transferred: { text: '已回小区待放归', badge: 'info' },",
        "    transferred: { text: '已转运', badge: 'info' },",
        'test_release_status_text_equals_backend_label',
    ),
    (
        # 配色名拼错 —— 徽章渲染成没有底色。
        'M78 放归真源配色名拼错',
        'static/js/tnr-common.js',
        "    pending:     { text: '待转运回小区',   badge: 'warning' },",
        "    pending:     { text: '待转运回小区',   badge: 'wording' },",
        'test_release_status_badge_exists_in_css',
    ),
    (
        # 「未办结」数组少一段，与后端 Release.OPEN_STATUSES 口径分叉。
        'M79 前端未办结数组漏 transferred',
        'static/js/tnr-common.js',
        "  OPEN_RELEASE_STATUS: ['pending', 'transferred'],",
        "  OPEN_RELEASE_STATUS: ['pending'],",
        'test_open_release_status_matches_backend',
    ),
    (
        # 医院端重新自带一份（还带回那条后端根本不存在的 cancelled）。
        # 第六十一轮改成**注入型**变异：出院看板改版后 hospital.js 里已经没有
        # 那份 `map:`（旧锚点命中 0 次）。判据是「别处不许出现 `transferred:`
        # 这种本地放归表」，所以注入一份本地表才能证明扫描本身有效。
        'M80 医院端又自带一份放归状态表（含幽灵 cancelled）',
        'static/js/portals/hospital.js',
        "  openDischargeChoice(petId, advised, tid) {",
        "  _LOCAL_RELEASE_STATUS: {\n"
        "    transferred: ['已回小区待放归', 'badge-info'],\n"
        "    released: ['已放归', 'badge-success'],\n"
        "    cancelled: ['已撤销', 'badge-default']\n"
        "  },\n"
        "  openDischargeChoice(petId, advised, tid) {",
        'test_no_other_frontend_file_maps_release_status',
    ),
    (
        # 后端互斥判据退回只认 pending：动物一登记转运，同一只既能再开放归单
        # 又能被领养 —— 双重承诺。
        'M81 后端「放归占用」判据退回只认 pending',
        'business/services.py',
        "    return Release.objects.filter(\n        pet=pet, status__in=Release.OPEN_STATUSES).exists()",
        "    return Release.objects.filter(pet=pet, status='pending').exists()",
        'business.tests.test_adoption_views',
    ),
    (
        # 发起放归处的重复单判据写回内联 pending 查询（与上面同源的另一处）。
        'M82 发起放归的重复单判据只认 pending',
        'business/views_release.py',
        "    if pet_has_pending_release(pet):",
        "    if Release.objects.filter(pet=pet, status='pending').exists():",
        'business.tests.test_release_views',
    ),
    (
        # 一宠一档时间线（捕捉点端与领养人端各一处）也中过同样的二值兜底：
        # 放归两段在时间线上只显示「待放归」，看不出动物在车上还是在小区。
        # 第六十一轮重挂：时间线已随 `showPetArchive` 迁到 `tnr-common.js`。
        'M83 时间线放归状态退回二值兜底',
        'static/js/tnr-common.js',
        "            + '<br>状态：' + this.escape(TNR_UI.releaseStatusText(ev.status));",
        "            + '<br>状态：' + this.escape(ev.status === 'released' ? '已放归' : '待放归');",
        'test_no_binary_release_status_fallback',
    ),
    (
        # 台账筛选侧早有 created_at 兜底，行里的 `date` 却没有 —— 复现
        # 「筛得出来、日期一栏空白、按日期排序全沉底」。
        'M84 监管台账未办结行的日期留空（与筛选兜底口径分叉）',
        'supervision/views.py',
        "                'date': _row_date(r, 'released_at', 'created_at'),",
        "                'date': r.released_at.isoformat() if r.released_at else '',",
        'test_ledger_unfinished_rows_have_dates',
    ),
    # ---- M85–M92：回访打卡审核状态（CheckIn.status）的显示真源 ----
    # 同一类缺陷在第五十二轮巡检（续）又抓到一个枚举：打卡的 `rejected`
    # 在监管端时间线被二值兜底压成「待审核」，机构端还自创「未通过」叫法。
    (
        'M85 监管端打卡时间线退回二值兜底（已驳回显示成待审核）',
        'static/js/portals/gov.js',
        "                <div class=\"timeline-title\">${TNR_UI.escape(TNR_UI.checkinStatusText(c.status))}</div>",
        "                <div class=\"timeline-title\">${c.status === 'approved' ? '已审核' : '待审核'}</div>",
        'test_no_binary_checkin_status_fallback',
    ),
    (
        'M86 打卡状态真源文案与后端 label 漂移',
        'static/js/tnr-common.js',
        "  CHECKIN_STATUS: {\n"
        "    pending:  { text: '待审核', badge: 'warning' },\n",
        "  CHECKIN_STATUS: {\n"
        "    pending:  { text: '待处理', badge: 'warning' },\n",
        'test_checkin_status_text_equals_backend_label',
    ),
    (
        'M87 打卡状态真源缺 rejected 一档',
        'static/js/tnr-common.js',
        "    rejected: { text: '已驳回', badge: 'danger' },",
        "    // rejected 这一档忘了补",
        'test_checkin_status_covers_every_backend_choice',
    ),
    (
        'M88 打卡状态配色名拼错（徽章渲染成无底色）',
        'static/js/tnr-common.js',
        "  CHECKIN_STATUS: {\n"
        "    pending:  { text: '待审核', badge: 'warning' },\n"
        "    approved: { text: '已通过', badge: 'success' },\n",
        "  CHECKIN_STATUS: {\n"
        "    pending:  { text: '待审核', badge: 'warning' },\n"
        "    approved: { text: '已通过', badge: 'succcess' },\n",
        'test_checkin_status_badge_exists_in_css',
    ),
    (
        'M89 领养人端自带一份打卡状态表',
        'static/js/portals/adopter.js',
        "  return TNR_UI.checkinStatusBadge(status);",
        "  const map = {\n"
        "    'pending': '<span class=\"badge badge-warning\">待审核</span>',\n"
        "    'approved': '<span class=\"badge badge-success\">已通过</span>',\n"
        "    'rejected': '<span class=\"badge badge-danger\">已驳回</span>'\n"
        "  };\n"
        "  return map[status] || '<span class=\"badge badge-default\">未知</span>';",
        'CheckInStatusSingleSourceTest.test_renderers_use_the_source',
    ),
    (
        'M90 监管端回访打卡列只报条数（全驳回也绿）',
        'static/js/portals/gov.js',
        "          const detail = Object.keys(TNR_UI.CHECKIN_STATUS)\n"
        "            .filter(k => cnt[k])\n"
        "            .map(k => TNR_UI.CHECKIN_STATUS[k].text + cnt[k]);\n"
        "          const allApproved = mine.length > 0 && detail.length === 1 && cnt.approved === mine.length;\n"
        "          const cls = allApproved ? 'badge-success' : 'badge-warning';\n"
        "          const tip = detail.length ? detail.join(' / ') : '尚无回访打卡';",
        "          const cls = mine.length > 0 ? 'badge-success' : 'badge-warning';\n"
        "          const tip = '';",
        'test_gov_checkin_count_shows_review_result',
    ),
    (
        'M91 监管端领养详情抽屉的打卡只报次数',
        'static/js/portals/gov.js',
        "            const parts = Object.keys(TNR_UI.CHECKIN_STATUS)\n"
        "              .filter(k => by[k])\n"
        "              .map(k => TNR_UI.CHECKIN_STATUS[k].text + by[k]);",
        "            const parts = [];",
        'test_gov_checkin_count_shows_review_result',
    ),
    (
        'M92 机构端回访筛选写死一套文案（含自创的「未通过」）',
        'static/js/portals/shelter.js',
        "        options: Object.keys(TNR_UI.CHECKIN_STATUS).map(k => ({ value: k, label: TNR_UI.CHECKIN_STATUS[k].text })) }",
        "        options: [ { value: 'pending', label: '待审核' },\n"
        "          { value: 'approved', label: '已通过' }, { value: 'rejected', label: '未通过' } ] }",
        'test_shelter_checkin_words_match_backend_label',
    ),

    # ---- M93–M102：领养单 / 领养申请单状态的显示真源 ----
    # 同一格发现的是一整套四种叫法（后端 待领出/已完成/已取消、兜底表「已撤销」、
    # 医院看板「已领出」、领养人卡片写死绿色「已领养」），另加两处放归显示盲区。
    (
        'M93 监管台账领养列退回写死徽章',
        'static/js/portals/shelter.js',
        "        { title: '医院', key: 'hospitalName' },\n"
        "        { title: '状态', render: r => TNR_UI.adoptionStatusBadge(r.status) },",
        "        { title: '医院', key: 'hospitalName' },\n"
        "        { title: '状态', render: r => this.statusBadge(r.status) },",
        'test_shelter_adoption_surfaces_use_the_source',
    ),
    (
        'M94 台账放归列退回写死「已放归」',
        'static/js/portals/shelter.js',
        "        { title: '状态', render: r => TNR_UI.releaseStatusBadge(r.status) },",
        "        { title: '状态', render: r => statusBadge('已放归', 'success') },",
        'test_pet_archive_release_detail_shows_stage',
    ),
    (
        'M95 一宠一档放归明细删掉状态与转运时间',
        'static/js/portals/shelter.js',
        "        { label: '状态', value: TNR_UI.releaseStatusBadge(rec.status) },\n"
        "        { label: '转运回小区', value: TNR_UI.formatDateTime(rec.transferredAt || rec.transferred_at) },",
        '',
        'test_pet_archive_release_detail_shows_stage',
    ),
    (
        # 同 M80：出院看板改版后那份本地领养表已经不在 hospital.js 里，
        # 用**注入**来表达「旧叫法回到前端」。
        'M96 医院端把 completed 叫成「已领出」（旧叫法）',
        'static/js/portals/hospital.js',
        "  openDischargeForm(kind, petId) {",
        "  _LEGACY_ADOPTION_LABEL: { completed: '已领出', cancelled: '已撤销' },\n"
        "  openDischargeForm(kind, petId) {",
        'test_no_legacy_adoption_wording',
    ),
    (
        'M97 领养人卡片写死绿色「已领养」（不看单据状态）',
        'static/js/portals/adopter.js',
        "              <div class=\"pet-card-name\">${TNR_UI.escape(petTitle)} ${TNR_UI.adoptionStatusBadge(adoption.status)}</div>",
        "              <div class=\"pet-card-name\">${TNR_UI.escape(petTitle)} "
        "<span class=\"badge badge-success\">已领养</span></div>",
        'test_no_hardcoded_adoption_badge',
    ),
    (
        'M98 领养单真源文案与后端 label 漂移',
        'static/js/tnr-common.js',
        "    completed:     { text: '已完成', badge: 'success' },",
        "    completed:     { text: '已办结', badge: 'success' },",
        'test_adoption_status_source_matches_backend',
    ),
    (
        'M99 领养单真源配色名拼错（徽章渲染成无底色）',
        'static/js/tnr-common.js',
        "    pending_claim: { text: '待领出', badge: 'cinnabar' },",
        "    pending_claim: { text: '待领出', badge: 'cinnabarr' },",
        'test_adoption_status_source_matches_backend',
    ),
    (
        'M100 申请单真源缺 cancelled 一档',
        'static/js/tnr-common.js',
        "    cancelled: { text: '已取消', badge: 'default' },\n",
        '',
        'test_apply_status_source_matches_backend',
    ),
    (
        'M101 领养人端退回自带两张申请状态表',
        'static/js/portals/adopter.js',
        "function getApplyStatusText(status) {\n"
        "  // 本文件曾自带两张申请状态表（文本一份、徽章一份），现在只剩调用点，\n"
        "  // 文案/配色由 `TNR_UI.APPLY_STATUS` 给出（对齐 `AdoptionApplication.STATUS_CHOICES`）。\n"
        "  return TNR_UI.applyStatusText(status);\n"
        "}\n"
        "\n"
        "function getApplyStatusBadge(status) {\n"
        "  return TNR_UI.applyStatusBadge(status);\n"
        "}",
        "const APPLY_TEXT = { pending: '待审核', approved: '已通过',\n"
        "  rejected: '已拒绝', cancelled: '已撤销' };\n"
        "const APPLY_BADGE = { pending: 'warning', approved: 'success',\n"
        "  rejected: 'danger', cancelled: 'default' };\n"
        "function getApplyStatusText(status) { return APPLY_TEXT[status] || status; }\n"
        "function getApplyStatusBadge(status) {\n"
        "  return `<span class=\"badge badge-${APPLY_BADGE[status] || 'default'}\">`\n"
        "    + TNR_UI.escape(getApplyText(status)) + '</span>';\n"
        "}",
        'AdoptionStatusSingleSourceTest.test_renderers_use_the_source',
    ),
    # 原 M102「机构端领养筛选下拉自创文案」锚的是手写字面量选项。
    # 第五十二轮末筛选条改为 `TNR_UI.adoptionStatusOptions()` 生成，
    # 该变异已由 M141（退回手写字面量）等价接管，故删除。

    # ---- M103–M114：捕捉 / 转运 / 诊疗三张状态表与「待签收」口径 ----
    # 监管端三张转运表**都漏 partial**（状态列印出英文码）、文案写「待接收/已接收」，
    # 诊疗列拿阶段词「诊疗中」当状态名；后端签收/驳回门禁写了两遍字面量。
    (
        'M103 监管端全业务转运列退回自带表（漏 partial + 待接收）',
        'static/js/portals/gov.js',
        "        { key: 'status', title: '状态', render: r => TNR_UI.transferStatusBadge(r.status) }",
        "        { key: 'status', title: '状态', render: r => {\n"
        "          const m = { pending: { t: '待接收', b: 'badge-warning' }, received: { t: '已接收', b: 'badge-success' }, rejected: { t: '已驳回', b: 'badge-danger' }, void: { t: '已撤回', b: 'badge-default' } };\n"
        "          const s = m[r.status] || { t: r.status, b: 'badge-default' };\n"
        "          return `<span class=\"badge ${s.b}\">${s.t}</span>`;\n"
        "        } }",
        'test_no_legacy_transfer_wording_in_status_maps',
    ),
    (
        'M104 监管端台账转运列退回自带表（状态列印出 partial）',
        'static/js/portals/gov.js',
        "        { key: 'status', title: '状态', width: '90px', render: r => TNR_UI.transferStatusBadge(r.status) },",
        "        { key: 'status', title: '状态', width: '90px', render: r => {\n"
        "          const m = { pending: { t: '待接收', b: 'badge-warning' }, received: { t: '已接收', b: 'badge-success' }, rejected: { t: '已驳回', b: 'badge-danger' }, void: { t: '已撤回', b: 'badge-default' } };\n"
        "          const v = m[r.status] || { t: r.status || '—', b: 'badge-default' };\n"
        "          return `<span class=\"badge ${v.b}\">${v.t}</span>`;\n"
        "        } },",
        'test_no_legacy_transfer_wording_in_status_maps',
    ),
    (
        'M105 监管端诊疗列拿阶段文案「诊疗中」当状态名',
        'static/js/portals/gov.js',
        "        { key: 'status', title: '状态', render: r => TNR_UI.treatmentStatusBadge(r.status) }",
        "        { key: 'status', title: '状态', render: r => {\n"
        "          const m = { in_progress: { t: '诊疗中', b: 'badge-info' }, completed: { t: '已完成', b: 'badge-success' } };\n"
        "          const s = m[r.status] || { t: r.status, b: 'badge-default' };\n"
        "          return `<span class=\"badge ${s.b}\">${s.t}</span>`;\n"
        "        } }",
        'test_treatment_status_not_labelled_by_stage',
    ),
    (
        'M106 监管端捕捉详情退回本地状态表',
        'static/js/portals/gov.js',
        "        { label: '状态', value: TNR_UI.captureStatusText(r.status) },",
        "        { label: '状态', value: ({ pending: '待转运', partial: '部分转运', completed: '已完成', void: '已作废' })[r.status] || r.status || '—' },",
        'test_renderers_use_the_sources',
    ),
    (
        'M107 领养人端时间线漏 partial 并兜底成「待签收」',
        'static/js/portals/adopter.js',
        "${TNR_UI.escape(TNR_UI.transferStatusText(ev.status))}",
        "${TNR_UI.escape({ received: '已签收', rejected: '已驳回', void: '已撤回' }[ev.status] || '待签收')}",
        'test_no_status_fallback_invents_a_state',
    ),
    (
        'M108 转运状态真源缺 partial 一档',
        'static/js/tnr-common.js',
        "    partial:  { text: '部分签收', badge: 'info' },",
        "    // partial 这一档忘了补",
        'test_sources_cover_backend_choices',
    ),
    (
        'M109 捕捉状态真源文案与后端 label 漂移',
        'static/js/tnr-common.js',
        "    pending:   { text: '待转运',   badge: 'warning' },",
        "    pending:   { text: '待送医',   badge: 'warning' },",
        'test_source_texts_equal_backend_labels',
    ),
    (
        'M110 诊疗状态配色名拼错（徽章渲染成无底色）',
        'static/js/tnr-common.js',
        "    in_progress: { text: '进行中', badge: 'info' },",
        "    in_progress: { text: '进行中', badge: 'infoo' },",
        'test_source_badges_exist_in_css',
    ),
    (
        'M111 「待签收」口径退回只认 pending（部分签收的单从待办消失）',
        'static/js/tnr-common.js',
        "  OPEN_TRANSFER_STATUS: ['pending', 'partial'],",
        "  OPEN_TRANSFER_STATUS: ['pending'],",
        'test_open_transfer_status_matches_backend',
    ),
    (
        'M112 医院端标签页筛选回退字面量 pending+partial',
        'static/js/portals/hospital.js',
        "    const transfers = allTransfers.filter(t => tab === 'pending'\n"
        "      ? TNR_UI.isOpenTransferStatus(t.status)\n"
        "      : t.status === tab);",
        "    const transfers = allTransfers.filter(t => tab === 'pending'\n"
        "      ? (t.status === 'pending' || t.status === 'partial')\n"
        "      : t.status === tab);",
        'test_no_frontend_reimplements_open_transfer',
    ),
    (
        'M113 捕捉点可选动物回退字面量判定',
        'static/js/portals/shelter.js',
        "      if (TNR_UI.isOpenTransferStatus(t.status)) (t.petCodes || []).forEach(c => busy.add(c));",
        "      if (t.status === 'pending' || t.status === 'partial') (t.petCodes || []).forEach(c => busy.add(c));",
        'test_no_frontend_reimplements_open_transfer',
    ),
    (
        'M114 后端驳回门禁回退字面量（与前端真源分叉）',
        'business/views_transfer.py',
        "    if transfer.status not in Transfer.OPEN_STATUSES:\n"
        "        return json_fail(f'当前状态({transfer.status})不可驳回')",
        "    if transfer.status not in ('pending', 'partial'):\n"
        "        return json_fail(f'当前状态({transfer.status})不可驳回')",
        'test_backend_open_statuses_is_the_only_predicate',
    ),
    (
        'M115 看板五态把 checking 叫回「待检」（同一时刻两种文案）',
        'static/js/tnr-common.js',
        "    checking:          { text: '待接诊',  badge: 'warning' },",
        "    checking:          { text: '待检',  badge: 'warning' },",
        'test_shared_stage_code_has_one_name',
    ),
    (
        'M116 页签顺序被打乱（诊疗中排到待诊断前面）',
        'static/js/tnr-common.js',
        "  BOARD_STAGE_ORDER: ['checking', 'pending_diagnose', 'treating',",
        "  BOARD_STAGE_ORDER: ['checking', 'treating', 'pending_diagnose',",
        'test_board_stage_order_matches_backend_stages',
    ),
    (
        'M117 五态表漏 pending_diagnose（页签漏出英文码）',
        'static/js/tnr-common.js',
        "    pending_diagnose:  { text: '待诊断',  badge: 'info' },\n",
        '',
        'test_board_stage_keys_and_badges_are_complete',
    ),
    (
        'M118 三态 done 文案与后端 label 脱钩（诊疗完毕→已办结）',
        'static/js/tnr-common.js',
        "    done:     { text: '诊疗完毕', badge: 'success' },",
        "    done:     { text: '已办结', badge: 'success' },",
        'test_treatment_stage_matches_backend_labels',
    ),
    (
        'M119 看板配色名拼错（待出院徽章渲染成无底色）',
        'static/js/tnr-common.js',
        "    pending_discharge: { text: '待出院',  badge: 'warning' },",
        "    pending_discharge: { text: '待出院',  badge: 'warningg' },",
        'test_board_stage_keys_and_badges_are_complete',
    ),
    (
        'M120 医院端页签又自写一份阶段文案表',
        'static/js/portals/hospital.js',
        "      k: k, t: TNR_UI.boardStageText(k), tip: TIPS[k] || ''",
        "      k: k, t: { checking: '待接诊', treating: '诊疗中' }[k] || '', tip: TIPS[k] || ''",
        'test_no_portal_restates_stage_labels',
    ),
    (
        'M121 详情抽屉「当前阶段」回退本地三态表',
        'static/js/portals/hospital.js',
        "    return TNR_UI.treatmentStageText(stage);",
        "    const map = { checking: '待接诊', treating: '诊疗中', done: '诊疗完毕' };\n"
        "    return map[stage] || this.escape(stage || '—');",
        'test_no_portal_restates_stage_labels',
    ),
    (
        'M122 看板页签改回硬编码五态列表（不再读顺序真源）',
        'static/js/portals/hospital.js',
        "    const FILTERS = TNR_UI.BOARD_STAGE_ORDER.map(k => ({",
        "    const FILTERS = ['checking', 'pending_diagnose', 'treating', "
        "'pending_discharge', 'discharged'].map(k => ({",
        'test_hospital_board_reads_the_stage_source',
    ),
    (
        'M123 看板空桶退回五键字面量（与后端桶名两处各写一遍）',
        'business/views_treatment.py',
        "    buckets = _board_buckets()",
        "    buckets = {'checking': [], 'pending_diagnose': [], 'treating': [],\n"
        "               'pending_discharge': [], 'discharged': []}",
        'test_backend_buckets_and_filter_read_the_tables',
    ),
    (
        'M124 ?stage= 筛选退回手写时间戳判据（与 stage 属性两份真源）',
        'business/views_treatment.py',
        "        stage_kwargs = Treatment.STAGE_FILTERS.get(stage)\n"
        "        if stage_kwargs is None:\n"
        "            return json_fail(f'未知的诊疗阶段：{stage}')\n"
        "        qs = qs.filter(**dict(stage_kwargs))",
        "        if stage == 'checking':\n"
        "            qs = qs.filter(checked_at__isnull=True)\n"
        "        elif stage == 'treating':\n"
        "            qs = qs.filter(checked_at__isnull=False, done_at__isnull=True)\n"
        "        elif stage == 'done':\n"
        "            qs = qs.filter(done_at__isnull=False)\n"
        "        else:\n"
        "            return json_fail(f'未知的诊疗阶段：{stage}')",
        'test_backend_buckets_and_filter_read_the_tables',
    ),
    (
        'M125 STAGE_FILTERS 里 treating 少算 done_at（筛出的单点开是另一阶段）',
        'business/models.py',
        "        STAGE_TREATING: (('checked_at__isnull', False), ('done_at__isnull', True)),",
        "        STAGE_TREATING: (('checked_at__isnull', False),\n"
        "                         ('diagnosed_at__isnull', False)),",
        'test_stage_filter_and_property_agree',
    ),
    (
        'M126 后端 BOARD_STAGES 漏掉待出院（看板整列动物消失）',
        'business/models.py',
        "    BOARD_STAGES = ('checking', 'pending_diagnose', 'treating',\n"
        "                    'pending_discharge', 'discharged')",
        "    BOARD_STAGES = ('checking', 'pending_diagnose', 'treating',\n"
        "                    'discharged')",
        'test_board_stage_order_matches_backend_stages',
    ),
    (
        'M127 实体状态徽章「启用」文案改写（与后端 STATUS_LABELS 脱节）',
        'static/js/tnr-common.js',
        "    active:   { text: '启用', badge: 'success' },",
        "    active:   { text: '启用中', badge: 'success' },",
        'test_entity_status_matches_backend_domain',
    ),
    (
        'M128 幽灵态 disabled 重回 ENTITY_STATUS（后端从不产出）',
        'static/js/tnr-common.js',
        "    inactive: { text: '停用', badge: 'danger' },",
        "    inactive: { text: '停用', badge: 'danger' },\n"
        "    disabled: { text: '停用', badge: 'danger' },",
        'test_entity_status_matches_backend_domain',
    ),
    (
        'M129 角色名退回「市级管理员」（后端 label 是市级政府管理员）',
        'static/js/tnr-common.js',
        "    gov_city:       { text: '市级政府管理员', badge: 'ink' },",
        "    gov_city:       { text: '市级管理员', badge: 'ink' },",
        'test_role_keys_and_labels_match_backend_choices',
    ),
    (
        'M130 机构类型文案改写（与 Institution.TYPE_CHOICES 脱节）',
        'static/js/tnr-common.js',
        "    shelter:   { text: '捕捉点', badge: 'cinnabar' },",
        "    shelter:   { text: '收容点', badge: 'cinnabar' },",
        'test_inst_type_matches_backend_choices',
    ),
    (
        'M131 芯片「已使用」退回旧称「已植入」',
        'static/js/tnr-common.js',
        "    used:       { text: '已使用', badge: 'cinnabar' },",
        "    used:       { text: '已植入', badge: 'cinnabar' },",
        'test_chip_status_matches_backend_choices',
    ),
    (
        'M132 平台端状态筛选下拉退回字面量选项',
        'static/js/portals/platform.js',
        "      { key: 'role', label: '角色', type: 'select', options: TNR_UI.roleOptions(['adopter']) },\n"
        "      { key: 'status', label: '状态', type: 'select', options: TNR_UI.entityStatusOptions() },",
        "      { key: 'role', label: '角色', type: 'select', options: TNR_UI.roleOptions(['adopter']) },\n"
        "      { key: 'status', label: '状态', type: 'select', options: [{ value: 'active', label: '启用' }, { value: 'inactive', label: '停用' }] },",
        'test_platform_portal_reads_the_enum_sources',
    ),
    (
        'M133 账号列表角色列不再走徽章真源',
        'static/js/portals/platform.js',
        "      role: r => TNR_UI.roleBadge(r.role),",
        "      role: r => '<span class=\"badge badge-info\">' + (r.role_display || r.role) + '</span>',",
        'test_platform_portal_reads_the_enum_sources',
    ),
    (
        'M134 捕捉点端账号状态写死绿色「启用」',
        'static/js/portals/shelter.js',
        "              { label: '账号状态', value: TNR_UI.entityStatusBadge(user?.status) }",
        "              { label: '账号状态', value: '<span class=\"badge badge-success\">启用</span>' }",
        'test_shelter_account_status_reads_the_source',
    ),
    (
        'M135 机构创建写回裸字符串 status=\'active\'',
        'supervision/views.py',
        "        phone=phone,\n"
        "        status=STATUS_ACTIVE,\n"
        "    )",
        "        phone=phone,\n"
        "        status='active',\n"
        "    )",
        'test_backend_status_writes_use_the_constants',
    ),
    (
        'M136 STATUS_ACTIVE 取值改成后端永不产出的 enabled',
        'core/models.py',
        "STATUS_ACTIVE = 'active'",
        "STATUS_ACTIVE = 'enabled'",
        'test_backend_status_writes_use_the_constants',
    ),
    (
        'M137 ROLE_ORDER 漏掉领养人（筛选/建号下拉少一个角色）',
        'static/js/tnr-common.js',
        "  ROLE_ORDER: ['platform_admin', 'gov_city', 'gov_district',\n"
        "               'shelter', 'hospital', 'adopter'],",
        "  ROLE_ORDER: ['platform_admin', 'gov_city', 'gov_district',\n"
        "               'shelter', 'hospital'],",
        'test_role_keys_and_labels_match_backend_choices',
    ),
    (
        'M138 流转时间线转运状态退回本地表（漏 partial，印英文码）',
        'static/js/tnr-common.js',
        "            + '<br>状态：' + this.escape(TNR_UI.transferStatusText(ev.status))",
        "            + '<br>状态：' + this.escape(({ pending: '待签收', received: '已签收',"
        " rejected: '已驳回' })[ev.status] || ev.status)",
        'test_shelter_lifecycle_timeline_uses_the_sources',
    ),
    (
        'M139 流转时间线诊疗状态退回「已完成 / 进行中」二分',
        'static/js/tnr-common.js',
        "            + '<br>状态：' + this.escape(TNR_UI.treatmentStatusText(ev.status))",
        "            + '<br>状态：' + this.escape(ev.status === 'completed' ? '已完成' : '进行中')",
        'test_shelter_lifecycle_timeline_uses_the_sources',
    ),
    (
        'M140 捕捉单状态筛选退回手写字面量（漏作废档）',
        'static/js/portals/shelter.js',
        "      { key: 'status', label: '状态', type: 'select', options: TNR_UI.captureStatusOptions() },",
        "      { key: 'status', label: '状态', type: 'select', options: [\n"
        "        { value: 'pending', label: '待捕捉' },\n"
        "        { value: 'completed', label: '已完成' },\n"
        "      ] },",
        'test_shelter_status_filters_use_generators',
    ),
    (
        'M141 领养单状态筛选退回手写字面量',
        'static/js/portals/shelter.js',
        "      { key: 'status', label: '状态', type: 'select', options: TNR_UI.adoptionStatusOptions() }",
        "      { key: 'status', label: '状态', type: 'select', options: [\n"
        "        { value: 'pending_claim', label: '待领出' },\n"
        "        { value: 'completed', label: '已完成' },\n"
        "      ] },",
        'test_shelter_adoption_filter_uses_source',
    ),
    (
        'M142 statusOptions 把英文枚举码当文案（下拉漏出 status code）',
        'static/js/tnr-common.js',
        "      value, label: TNR_UI[table][value].text,",
        "      value, label: value,",
        'test_status_options_reads_text_in_key_order',
    ),
    (
        'M143 领养人端手拼宠物状态徽章（把类名前缀拆回来）',
        'static/js/portals/adopter.js',
        '  html += `<div style="margin-top:8px;">${TNR_UI.petStatusBadge(pet.status)}</div>`;',
        '  html += `<div style="margin-top:8px;">${TNR_UI.statusBadge('
        'TNR_UI.petStatusText(pet.status), '
        "TNR_UI.PET_STATUS[pet.status].badge.replace('badge-',''))}</div>`;",
        'test_portals_use_the_badge_helper',
    ),
    (
        'M144 医院物料页退回静默读（加载失败伪装成没有数据）',
        'static/js/portals/hospital.js',
        '        TNR_API.getMaterialTransactionsStrict(),\n'
        '        TNR_API.getMaterialsStrict()',
        '        TNR_API.getMaterialTransactions(),\n'
        '        TNR_API.getMaterialsStrict()',
        'test_material_page_uses_strict_reads_only',
    ),
    (
        'M146 物料页签点击丢掉物料缓存（实时库存显示暂无物料）',
        'static/js/portals/hospital.js',
        '        this.renderMaterialTab(txns, materials);',
        '        this.renderMaterialTab(txns);',
        'test_material_tab_switch_passes_the_cached_materials',
    ),
    (
        'M147 平台机构页退回静默读（加载失败伪装成暂无数据）',
        'static/js/portals/platform.js',
        '  async renderInstitutionsContent() {\n'
        '    const contentEl = document.getElementById(\'instContent\');\n'
        '    let districts;\n'
        '    try {\n'
        '      districts = (await TNR_API.getDistrictsStrict()) || [];',
        '  async renderInstitutionsContent() {\n'
        '    const contentEl = document.getElementById(\'instContent\');\n'
        '    let districts;\n'
        '    try {\n'
        '      districts = (await TNR_API.getDistricts()) || [];',
        'test_platform_lists_use_strict_reads_only',
    ),
    (
        'M148 平台编辑机构在记录缺失时退化成新增（重复建机构）',
        'static/js/portals/platform.js',
        "      if (!inst) { TNR_UI.toast('机构不存在或已被删除，请刷新列表', 'warning'); return; }\n",
        '',
        'test_edit_modals_abort_instead_of_falling_back_to_create',
    ),
    (
        'M149 平台建号前端回退默认口令 111111',
        'static/js/portals/platform.js',
        '          username, password, name, role,',
        "          username, password: password || '111111', name, role,",
        'test_account_creation_has_no_default_password',
    ),
    (
        # 前端不再预填默认口令，但接口若还允许空口令，
        # 直接 POST 就能建出一个「口令为空」的管理账号。
        'M150 建号接口放弃口令必填与强度校验',
        'supervision/views.py',
        '    probe = User(username=username, first_name=name)\n'
        "    err = password_policy_error(password, probe, empty='请设置初始密码')\n"
        '    if err:\n'
        '        return json_fail(err)\n',
        '',
        'test_create_without_password_rejected',
    ),
    (
        # 读失败后筛选框仍绑着渲染函数，缓存不置空就会把错误态换成过期名单。
        'M151 账号读失败后保留旧缓存（筛选覆盖错误态）',
        'static/js/portals/platform.js',
        '      this._users = null;\n',
        '',
        'test_filter_after_load_error_does_not_overwrite_error_state',
    ),
    (
        # 标签栏 4–6 个标签时内容 497–707px，容器只有 336–342px；
        # 去掉 overflow-x 后右侧标签直接被裁掉、永远点不到。
        'M152 移动端标签栏退回不可横向滚动（右侧标签点不到）',
        'static/css/tnr-traditional.css',
        '  body .page-view .tabs {\n'
        '    overflow-x: auto;\n',
        '  body .page-view .tabs {\n',
        'test_tabs_scroll_horizontally_on_mobile',
    ),
    (
        # 日志描述不换行时只剩 ~70px 宽，单条日志实测 217–255px 高。
        'M153 移动端日志描述不独占一行（单条日志撑到 200px+）',
        'templates/portal/platform/portal.html',
        '    .log-detail{flex:1 1 100%;min-width:0;word-break:break-word}',
        '    .log-detail{flex:1;min-width:0;word-break:break-word}',
        'test_mobile_log_rows_stack_detail',
    ),
    (
        # 日期不成对时手机端筛选条多占一整行 ~78px，首屏列表被挤没。
        'M154 起止日期不再成对渲染（筛选条多占一整行）',
        'static/js/tnr-common.js',
        "      if (f && f.type === 'date' && all[i + 1] && all[i + 1].type === 'date') {",
        '      if (false) {',
        'test_date_filters_are_rendered_as_a_pair',
    ),
    (
        'M155 移动端分页按钮退回 32px（触控点不准）',
        'static/css/tnr-traditional.css',
        '  .pagination-btn { min-width: 44px; height: 44px; }',
        '  .pagination-btn { min-width: 32px; height: 32px; }',
        'test_pagination_targets_are_touch_sized_on_mobile',
    ),
    (
        'M156 领养人端「清空」退回 25px 小热区',
        'templates/portal/adopter/portal.html',
        '    min-width: 44px; min-height: 44px;\n'
        '    display: inline-flex; align-items: center; justify-content: center;',
        '    min-width: 0; min-height: 0;\n'
        '    display: inline-flex; align-items: center; justify-content: center;',
        'test_adopter_date_clear_is_touch_sized',
    ),
    (
        # 内联三列会盖掉移动端的两列 stat-grid，390px 下每列只有 111px。
        'M157 政府端统计卡退回内联三列（移动端挤成 111px）',
        'static/js/portals/gov.js',
        '            <div class="stat-grid stat-grid-3" style="margin-bottom:var(--sp-5);">',
        '            <div class="stat-grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:var(--sp-5);">',
        'test_gov_stat_grid_does_not_pin_three_columns_inline',
    ),
    (
        # 日志读失败只剩一行红字，没有重试入口，只能切页签碰运气。
        'M158 操作日志失败态退回不可重试（只有一行红字）',
        'static/js/portals/platform.js',
        "        this.showLoadError(el, '操作日志加载失败', e, () => this.renderSettingsContent());\n",
        "        el.innerHTML = '<div class=\"table-empty\">加载失败</div>';\n",
        'test_settings_failures_are_visible_and_retryable',
    ),
    (
        'M159 公告失败态退回不可重试（只有一行红字）',
        'static/js/portals/platform.js',
        "        this.showLoadError(el, '公告数据加载失败', e, () => this.renderSettingsContent());\n",
        "        el.innerHTML = '<div class=\"table-empty\">加载失败</div>';\n",
        'test_settings_failures_are_visible_and_retryable',
    ),
    (
        # 动物档案读失败曾被静默成「未知」；去掉警告与重试入口后，
        # 用户只会看到一串没有姓名/性别的编号。
        'M160 交接页档案读失败退回静默（只剩「未知」编号）',
        'static/js/portals/hospital.js',
        '        + \'<button type="button" class="btn btn-secondary btn-sm" data-reload-pets>重新加载</button></div>\');',
        '        + \'</div>\');',
        'test_hospital_transfer_pages_surface_pet_read_failure',
    ),
    (
        # 第五十六轮桌面复核：把 min-width:0 写回基础规则，1366px 下日期输入只剩 121px。
        'M161 成对日期在桌面端被压窄（基础规则加 min-width:0）',
        'static/css/tnr-traditional.css',
        '.filter-date-pair .filter-item .form-input { width: 100%; }',
        '.filter-date-pair .filter-item .form-input { width: 100%; min-width: 0; }',
        'test_date_pair_inputs_keep_desktop_width',
    ),
    (
        # 移动端三列卡每张只有 111px；去掉断点回退后，手机端会退回三列。
        'M162 三列统计区在移动端不回退两列',
        'static/css/tnr-traditional.css',
        '  body .stat-grid.stat-grid-3 { grid-template-columns: repeat(2, minmax(0, 1fr)); }\n',
        '',
        'test_gov_stat_grid_does_not_pin_three_columns_inline',
    ),
    (
        # 领养人本来只该看到流转事实；退回完整 PII 后端点判据必须报出来。
        # 第六十三轮：一宠一档的事件拼装从 `views_portal.pet_lifecycle` 抽到
        # `services_lifecycle.owner_contact_fields`（打印页与接口共用），锚点跟着挪。
        'M163 生命周期把前任主人完整联系方式下发给领养人',
        'business/services_lifecycle.py',
        "    if user.role == 'adopter':\n"
        "        return {'owner_phone': '', 'owner_id_card': '',\n"
        "                'owner_address': '', 'signature': ''}\n",
        "    if user.role == 'adopter' and False:\n"
        "        return {'owner_phone': '', 'owner_id_card': '',\n"
        "                'owner_address': '', 'signature': ''}\n",
        'test_adopter_gets_no_previous_owner_contact',
    ),
    (
        'M164 电话脱敏退回原文（监管端拿到完整号码）',
        'core/privacy.py',
        "        return s[:3] + '*' * (len(s) - 7) + s[-4:]",
        '        return s',
        'test_regulator_gets_masked_owner_contact',
    ),
    (
        'M165 监管批量台账退回明文身份证号',
        'supervision/views.py',
        "                    'adopter_id_card': mask_id_card(a.adopter_id_card),",
        "                    'adopter_id_card': a.adopter_id_card,",
        'test_ledger_masks_adopter_id_card',
    ),
    (
        # 第五十八轮移动端：退回单列后 5 个筛选条件要占满整屏。
        'M166 移动端筛选条退回单列堆叠',
        'static/css/tnr-traditional.css',
        '  body .filter-bar {\n'
        '    display: grid;\n'
        '    grid-template-columns: repeat(2, minmax(0, 1fr));',
        '  body .filter-bar {\n'
        '    display: grid;\n'
        '    grid-template-columns: minmax(0, 1fr);',
        'test_mobile_filter_bar_uses_two_columns',
    ),
    (
        # 日期对不跨列时，「开始/结束」各占一格、各占一行，反而比单列更高。
        'M167 日期对不再跨列（开始/结束被拆到两行）',
        'static/css/tnr-traditional.css',
        '  .filter-date-pair, .filter-actions { grid-column: 1 / -1; }\n',
        '',
        'test_mobile_filter_bar_uses_two_columns',
    ),
    (
        # 取消右侧吸附后，7–9 列的宽表必须滑到最右才能点操作。
        'M168 移动端操作列不再固定在右侧',
        'static/css/tnr-traditional.css',
        '    position: sticky;\n    right: 0;\n    z-index: 2;',
        '    position: sticky;\n    right: auto;\n    z-index: 2;',
        'test_mobile_table_actions_stay_visible',
    ),
    (
        # 低频列不隐藏：9 列账号表在 390px 下仍要横滑 664px。
        'M169 移动端低频列不再隐藏',
        'static/css/tnr-traditional.css',
        '  body .dt-table col.dt-col-mobile-hide { display: none; }',
        '  body .dt-table col.dt-col-mobile-hide { display: table-column; }',
        'test_mobile_wide_tables_collapse_low_priority_columns',
    ),
    (
        # 主列不再补回：低频列隐藏后，姓名/角色/状态在手机上彻底消失。
        'M170 账号表主列不再补回手机端详情',
        'static/js/portals/platform.js',
        "{ key: 'username', title: '用户名', mobileRender: mobileCard },",
        "{ key: 'username', title: '用户名' },",
        'test_mobile_wide_tables_collapse_low_priority_columns',
    ),
    (
        # 捕捉记录主列不再补回：小区/数量/转运状态在手机上消失。
        'M171 捕捉记录主列不再补回手机端详情',
        'static/js/portals/shelter.js',
        "{ title: '捕捉单号', key: 'ledgerNo', mobileRender: mobileCard, render:",
        "{ title: '捕捉单号', key: 'ledgerNo', render:",
        'test_mobile_wide_tables_collapse_low_priority_columns',
    ),
    (
        # 撤回转运单把终态动物一起回退（现网真有一条：放归单已 released，
        # 档案却被打回 in_transit）。去掉 exclude 必须让新用例变红。
        'M172 撤回转运单不再排除终态动物',
        'business/views_transfer.py',
        "    Pet.objects.filter(code__in=pet_codes, district_id=transfer.district_id).exclude(\n"
        "        status__in=Pet.TERMINAL_STATUSES).update(\n",
        "    Pet.objects.filter(code__in=pet_codes, district_id=transfer.district_id).update(\n",
        'test_withdraw_keeps_released_pet_released',
    ),
    (
        'M173 驳回转运单不再排除终态动物',
        'business/views_transfer.py',
        "        Pet.objects.filter(code__in=remaining).exclude(\n"
        "            status__in=Pet.TERMINAL_STATUSES).update(\n",
        "        Pet.objects.filter(code__in=remaining).update(\n",
        'test_reject_keeps_terminal_pet_terminal',
    ),
    (
        # 巡检项写了方法却没挂进 `checks` 也一样是零 —— 这条证明「挂上」本身有判据。
        'M174 放归办结巡检没进检查清单',
        'core/management/commands/check_data_integrity.py',
        "            ('放归已办结但宠物状态未出库', self.check_released_pet_status),\n",
        '',
        'test_released_document_on_non_terminal_pet_is_reported',
    ),
    (
        'M175 重置密码不再拦截本人',
        'supervision/views.py',
        '    if user.id == request.user.id:\n'
        "        return json_fail('不能在此重置本人密码，请前往个人中心修改')\n",
        '',
        'test_self_reset_rejected',
    ),
    (
        'M176 重置密码不再拦截超级管理员',
        'supervision/views.py',
        '    if user.is_superuser:\n'
        "        return json_fail('超级管理员账号的密码不可由平台端重置')\n",
        '',
        'test_superuser_reset_rejected',
    ),
    (
        'M177 重置密码跳过强度校验',
        'supervision/views.py',
        "    err = password_policy_error(password, user, empty='请设置新密码')\n"
        '    if err:\n'
        '        return json_fail(err)\n',
        '',
        'test_short_and_weak_password_rejected',
    ),
    (
        'M178 重置密码把明文写进审计',
        'supervision/views.py',
        "    request.audit_detail = '平台端重置登录密码'\n",
        "    request.audit_detail = f'平台端重置登录密码：{password}'\n",
        'test_audit_records_reset_without_password',
    ),
    (
        # 非 JSON 的 404/502 被吞成 `{}`，`assertOk` 的 `success === false`
        # 判据放行 —— 界面谎报「密码已重置」，实际数据库里什么都没写。
        'M179 写接口把非 JSON 错误页当成功',
        'static/js/tnr-api.js',
        "    if (!data || typeof data !== 'object') {\n"
        "      return {success: false, message: `服务器返回异常（HTTP ${res.status}），请稍后重试`};\n"
        '    }\n',
        '    if (!data) data = {};\n',
        'test_non_json_write_response_is_reported_as_failure',
    ),
]

# ---------- 第六十一轮：出院候选按「建议去向」分流 + 平台端单据状态维护 ----------
MUTATIONS += [
    (
        # 四个出院去向只认「诊疗完成」时给的建议。去掉比对 = 建议领养的动物
        # 也能被开成放归/运回单，同一只动物同时出现在两张终态单据上。
        'M180 出院候选不再比对办结建议去向',
        'business/services_discharge.py',
        "    if advised not in DISCHARGE_ADVICE[kind]:\n"
        "        return (f'{pet.code} 的办结建议去向是「{disposal_label(advised)}」，'\n"
        "                f'不能按「{label}」办理出院；确需更换去向，请先修改建议去向')\n",
        None,
        'test_adoption_advice_blocks_other_three',
    ),
    (
        # 动物已是终态时改建议不会动任何单据，只会让「单据说领养、建议说放归」
        # 同时成立 —— 纠错得由平台端维护单据，不能靠改建议绕过。
        'M181 已出院动物仍可改建议去向',
        'business/services_discharge.py',
        "    if pet.status in Pet.TERMINAL_STATUSES:\n"
        "        return (f'动物已出院（{pet.get_status_display()}），建议去向不能再修改；'\n"
        "                '确需纠错请由平台端维护对应出院单据')\n",
        None,
        'test_advice_locked_when_pet_already_discharged',
    ),
    (
        'M182 在办运回单不再占用建议去向',
        'business/services_discharge.py',
        "    if pet_has_pending_return(pet):\n"
        "        return '该动物已在未办结的运回单上，请先撤回运回单再修改建议去向'\n",
        None,
        'test_advice_locked_while_return_order_pending',
    ),
    (
        # 「没点诊疗完成就出库」的动物会留下一张 treating 旧单：宠物早已终态，
        # 看板却还能打开它补点完成、顺手安排去向。
        'M183 终态动物仍可补记「诊疗完成」',
        'business/services_treatment.py',
        "    pet = treatment.pet\n"
        "    if pet is not None and pet.status in Pet.TERMINAL_STATUSES:\n"
        "        raise ValueError(f'动物已出院（{pet.get_status_display()}），'\n"
        "                         '本单不能再补记「诊疗完成」；如需纠错请由平台端维护出院单据')\n",
        None,
        'test_mark_done_refused_for_discharged_pet',
    ),
    (
        # 未诊疗完成 = 动物还没进待出院；此时放开改建议，建议就成了
        # 「诊疗中途改口」，出院候选会被一张还没做完的单子分流。
        'M184 未诊疗完成也能改建议去向',
        'business/services_treatment.py',
        "    if treatment.done_at is None:\n"
        "        raise ValueError('请先点击「诊疗完成」，动物进入待出院后才能修改建议去向')\n",
        None,
        'test_advice_needs_done_first',
    ),
    (
        # 建议**不可清空**（`DISPOSAL_CHOICES` 没有空 key）：清空后动物
        # 从四个去向的候选里同时消失，出院页只会显示「暂无动物」。
        'M185 建议去向可以被清空',
        'business/services_treatment.py',
        "    advised = (disposal_advised or '').strip()\n"
        "    if advised not in dict(Treatment.DISPOSAL_CHOICES):\n",
        "    advised = (disposal_advised or '').strip()\n"
        "    if advised and advised not in dict(Treatment.DISPOSAL_CHOICES):\n",
        'test_advice_cannot_be_cleared',
    ),
    (
        # 本轮实测打出的真实缺陷：`ReturnOrder` 是「多宠一单」，没有 `pet_id`
        # 属性，默认「全部单据」的检索会整页 500，四类单据一起看不见。
        'M186 维护列表按 doc.pet_id 取动物（运回单 500）',
        'supervision/doc_maintenance.py',
        "    return getattr(doc, 'pet_id', None)\n",
        "    return doc.pet_id\n",
        'test_return_order_row_survives_without_pet_fk',
    ),
    (
        'M187 回退不再查白名单',
        'supervision/doc_maintenance.py',
        "    if target not in allowed_targets(doc, doc_type):\n"
        "        raise MaintenanceError(\n",
        "    if False:\n"
        "        raise MaintenanceError(\n",
        'test_target_must_be_in_whitelist',
    ),
    (
        # 白名单只列**回退方向**：往前推是业务角色自己的流程，平台端只纠错。
        'M188 白名单放进一个「前进」方向',
        'supervision/doc_maintenance.py',
        "    'release': {\n"
        "        'released': ('transferred', 'pending'),\n",
        "    'release': {\n"
        "        'pending': ('released',),\n"
        "        'released': ('transferred', 'pending'),\n",
        'test_forward_transitions_are_not_the_platform_job',
    ),
    (
        # 这类操作直接改监管台账里的终态记录，无理由不可执行；
        # 原因还要进审计 detail，不设上限等于允许往台账塞任意大的文本。
        'M189 维护原因不再必填',
        'supervision/doc_maintenance.py',
        "    if not reason:\n"
        "        return json_fail('请填写维护原因')\n",
        None,
        'test_reason_is_required',
    ),
    (
        'M190 维护原因不再限长',
        'supervision/doc_maintenance.py',
        "    if len(reason) > REASON_MAX:\n"
        "        return json_fail(f'维护原因不能超过 {REASON_MAX} 字')\n",
        None,
        'test_reason_length_is_capped',
    ),
    (
        # 出院页横幅是「建议不符 → 去补录」的唯一入口；不渲染就等于
        # 操作员只看到四个空页签，看不出是「没动物」还是「去向不对」。
        'M191 出院页不再渲染建议去向横幅',
        'static/js/portals/hospital.js',
        "    html += '</div>' + this.dspAdviceNotice() + '<div id=\"dspContent\"></div>';\n",
        "    html += '</div>' + '<div id=\"dspContent\"></div>';\n",
        'test_discharge_shell_renders_the_notice_and_binds_it',
    ),
    (
        # 先切页再开流程：`navigateTo` 会重渲染诊疗页，顺序颠倒则
        # 「现在就去修改」点开的是上一次的看板，建议照旧改不了。
        'M192 横幅跳转先开流程再切页',
        'static/js/portals/hospital.js',
        "    this.navigateTo('treatment');\n"
        "    this.openTreatmentFlow(petId, tid, 3);\n",
        "    this.openTreatmentFlow(petId, tid, 3);\n"
        "    this.navigateTo('treatment');\n",
        'test_notice_jumps_into_step_three_of_the_flow',
    ),
    (
        # 看板数据在途时用户已经进了某只动物的流程（横幅「去补录」就是这样），
        # 少掉第二次守卫会把刚渲染的流程整块覆盖回看板。
        'M193 看板渲染丢掉「流程已在途」守卫',
        'static/js/portals/hospital.js',
        "      if (this.state.treatmentFlow) { this.renderFlowView(); return; }\n"
        "      this.renderBoard();\n",
        "      this.renderBoard();\n",
        'test_flow_render_keeps_the_opened_flow',
    ),
    (
        'M194 出院弹窗不再接「现在就去修改」',
        'static/js/portals/hospital.js',
        "        const edit = bodyEl.querySelector('[data-dspedit]');\n"
        "        if (edit) edit.onclick = () => {\n"
        "          closeFn();\n"
        "          this.openTreatmentFlow(petId, tid, 3);\n"
        "        };\n",
        None,
        'test_discharge_popup_offers_the_edit_entry',
    ),
]

# ---------- 第六十一轮：打印取单的「本机构 ∪ 区县」口径 ----------
# 这四条用例（`CrossDistrictPrintTest`）一直只在**全量套件**里跑，变异表以前
# 既没收这个模块、也没给它们配过变异 —— 也就是说「跨区县送医的医院能不能打出
# 自己的单」这条判据**从来没被验证过有没有判别力**。本轮补上：先给
# TEST_TARGETS 加上 `business.tests.test_doc_numbers`，再挂三条变异。
MUTATIONS += [
    (
        # 复现原洞：只按区县收敛 → 动物在甲区、手术在乙区，乙区医院点打印 404。
        'M195 打印取单退回「只按区县」（复现原洞）', 'business/services.py',
        "    if user.institution_id:\n"
        "        own_q = Q(institution_id=user.institution_id)\n"
        "        for field in extra_own_fields:\n"
        "            own_q |= Q(**{field: user.institution_id})\n"
        "        own = model.objects.filter(own_q, pk=pk).first()\n"
        "        if own is not None:\n"
        "            return own\n",
        None,
        'test_hospital_prints_own_doc_across_district',
    ),
    (
        # 机构分支在，但**没接** `extra_own_fields`：转运单的 `institution` 是
        # 发出捕捉点，接收医院挂在 `to_hospital` 上 → 它签收后打不出自己那张单。
        'M196 打印取单忽略 extra_own_fields', 'business/services.py',
        "        for field in extra_own_fields:\n"
        "            own_q |= Q(**{field: user.institution_id})\n",
        None,
        'test_receiving_hospital_prints_transfer',
    ),
    (
        # 反向对照：放宽成「按 pk 直取」时，隔离判据必须变红。
        # 只挂「打不开」那两条的话，`get_printable_object` 被改成无条件放行
        # 也能全绿 —— 那正是第四十八轮修复最容易走过头的方向。
        'M197 打印取单取消越权收敛（存在性预言机 + 越权打印）', 'business/services.py',
        "    return get_scoped_object(model, pk, user)\n",
        "    return model.objects.filter(pk=pk).first()\n",
        'test_other_hospital_cannot_print',
    ),
]

# ---------- 第六十三轮：台账 CSV 导出 + 一宠一档 A4 档案 ----------
# 两处现场缺陷：
#   ① 捕捉点端「一宠一档 → 导出」的 CSV 只有表头（本端自拼导出器，取值只认
#      `column.key`，而该页 11 列全用 `render`）；政府端干脆只弹「模拟成功」。
#   ② 一宠一档档案没有可归档/报送的整份文书。
# M198/M199 验的是「公共实现与入口」这两条静态判据真的有判别力；
# M200–M203 验的是打印页的取数范围、PII 分档与事件行完整度。
MUTATIONS += [
    (
        # 复现原缺陷的起点：导出不再调公共实现（自拼 CSV 必然丢掉 render 列）。
        'M198 台账导出不再走公共实现', 'static/js/portals/shelter.js',
        "      const n = TNR_UI.exportCsv(columns, data, title);\n",
        "      const n = data.length;\n",
        'test_ledger_pages_call_the_shared_exporter',
    ),
    (
        # 「档案抽屉没有导出按钮」= 后端写得再好，用户在界面上也点不到。
        'M199 一宠一档抽屉摘掉导出 PDF 入口', 'static/js/tnr-common.js',
        "      + this.printDocBtn('pet-archive', petId, '导出 PDF（本动物全部档案）')\n",
        "",
        'test_archive_drawer_has_pdf_export_button',
    ),
    (
        # 打印页不分角色、直接按主键取 → 「猜到主键就能读全档」。
        # 这条同时会打红 adopter / hospital 的范围用例，expect 取最早暴露的
        # 「越权与不存在不可区分」。
        'M200 一宠一档取数退回「按 pk 直取」', 'business/services_lifecycle.py',
        "    if user.role == 'adopter':\n"
        "        return Pet.objects.filter(id=pet_id).filter(\n"
        "            Q(adoptions__adopter=user) | Q(adoption_applications__applicant=user)\n"
        "        ).distinct().first()\n"
        "    if user.role == 'hospital':\n"
        "        return Pet.objects.filter(id=pet_id).filter(\n"
        "            hospital_pet_scope(user)).distinct().first()\n"
        "    return get_scoped_object(Pet, pet_id, user)\n",
        "    return Pet.objects.filter(id=pet_id).first()\n",
        'test_out_of_scope_and_missing_are_indistinguishable',
    ),
    (
        # 医院支退回按区县收敛：同区县另一家医院能打印别人的动物档案（含主人 PII）。
        'M201 一宠一档医院支退回按区县收敛', 'business/services_lifecycle.py',
        "    if user.role == 'hospital':\n"
        "        return Pet.objects.filter(id=pet_id).filter(\n"
        "            hospital_pet_scope(user)).distinct().first()\n",
        "    if user.role == 'hospital':\n"
        "        return get_scoped_object(Pet, pet_id, user)\n",
        'test_hospital_scope_is_institution_not_district',
    ),
    (
        # 监管端不脱敏：整页明文的电话/身份证随「导出 PDF」进纸质报表。
        'M202 档案打印页监管端不再脱敏主人 PII', 'business/services_lifecycle.py',
        "    if user.role in ('gov_city', 'gov_district'):\n"
        "        return {\n"
        "            'owner_phone': mask_phone(record.owner_phone),\n"
        "            'owner_id_card': mask_id_card(record.owner_id_card),\n"
        "            'owner_address': record.owner_address,\n"
        "            'signature': record.signature or '',\n"
        "        }\n",
        None,
        'test_regulator_gets_masked_owner_pii',
    ),
    (
        # 事件字段漏一行：纸上少一个单号，且不报错（只能靠对账发现）。
        'M203 档案打印的事件行漏掉单据号', 'business/services_lifecycle.py',
        "            ('转运单号', ev.get('ledger_no')),\n",
        "",
        'test_scoped_shelter_gets_the_whole_archive',
    ),
]

# ---------- 第六十四轮：体温读数派生 / 库存处方收窄 / 领养审核移到捕捉点 ----------
# 本批三处判据都不靠界面，必须由后端钉死：
#   ① 体温判定若采信客户端传值，「40.2℃ + 正常」会照常落库；
#   ② 库存处方若不对项目收口，绝育麻药会被记进库存并扣耗；
#   ③ 审核若留在医院，同一张申请两边都能改状态。
MUTATIONS += [
    (
        # 客户端传什么 flag 就存什么 —— 读数与判定再次可以互相矛盾。
        'M204 体温判定退回采信客户端传值', 'business/services_treatment.py',
        "        treatment.temperature_flag = (\n"
        "            fever_judge(value) if value is not None else '')\n",
        "        treatment.temperature_flag = body.get('temperature_flag') or ''\n",
        'test_temperature_flag_is_derived_server_side',
    ),
    (
        # 绝育 / 芯片 / 其他也能从药品库存开方 → 麻药被记进库存并扣耗。
        'M205 非驱虫免疫项目也允许从库存开处方', 'business/services_treatment.py',
        "        if rx_ids and not _item_meta(key).get('rx_stock'):\n"
        "            raise ValueError(\n"
        "                f'{_item_meta(key)[\"name\"]}：该项目不从库存出处方'\n"
        "                '（用药请写在手写记录里）')\n",
        "",
        'test_stock_rx_rejected_for_non_prescription_items',
    ),
    (
        # 审核退回医院：同一张申请两边都能改状态。
        'M206 领养申请审核放宽回医院', 'business/views_adoption.py',
        "@role_required('shelter')\n"
        "@login_required\n"
        "def adoption_application_review(request, pk):",
        "@role_required('shelter', 'hospital')\n"
        "@login_required\n"
        "def adoption_application_review(request, pk):",
        'test_hospital_cannot_review',
    ),
    (
        # 未审 / 已拒的申请也进医院视野 → 医院能绕过捕捉点看到申请人材料。
        'M207 医院申请列表不再只给已通过', 'business/views_adoption.py',
        "            qs = qs.filter(hospital_id=user.institution_id, status='approved')\n",
        "            qs = qs.filter(hospital_id=user.institution_id)\n",
        'test_hospital_list_only_shows_approved',
    ),
    (
        # 审核取单取消归属收敛：别的捕捉点能审，且与「不存在」可区分。
        'M208 审核取单取消捕捉点归属收敛', 'business/views_adoption.py',
        "    qs = (AdoptionApplication.objects.filter(pet__shelter_id=user.institution_id)\n"
        "          if user.institution_id else AdoptionApplication.objects.none())\n",
        "    qs = AdoptionApplication.objects.all()\n",
        'test_review_other_shelter_404',
    ),
    (
        # 拒绝原因退回原生 `window.prompt`：阻塞式对话框在自动化 / 内嵌 webview
        # 里点了「确认拒绝」既不报错也不发请求，申请还挂在待审列表上。
        'M209 领养拒绝原因退回原生 prompt', 'static/js/portals/shelter.js',
        "        const { close } = TNR_UI.modal({\n"
        "          title: '拒绝领养申请',\n",
        "        const note = window.prompt('请输入拒绝原因：') || '';\n"
        "        const close = () => {};\n"
        "        TNR_UI.modal({\n"
        "          title: '拒绝领养申请',\n",
        'test_portal_js_has_no_native_dialog_calls',
    ),
    (
        # 第三十轮：医院端「办理领回」重新去调 `/blacklist/check/`。
        # 这条验的是**新判据的判别力** —— 「门户调用点 ↔ 接口放行集合」不是
        # 靠注释里那句「前端零调用点」保证的：把调用加回去，界面立刻 403、
        # 领回整个流程走不下去（实测），而接口侧的用例一条都不会红。
        'M210 医院端恢复黑名单预检（调用点越过放行集合）',
        'static/js/portals/hospital.js',
        "            const res = await TNR_API.createOwnerReturn({\n",
        "            const bl = await TNR_API.checkBlacklist(idCard, phone);\n"
        "            if (bl && bl.name) { TNR_UI.toast('该主人在黑名单中', 'danger'); return; }\n"
        "            const res = await TNR_API.createOwnerReturn({\n",
        'test_portal_calls_match_own_role',
    ),
    (
        # 第五十九轮：跨区县运回签收后的放归链路。
        # 根判据 = `shelter_scope()` 的「挂本站名下」这一支。动物签收后
        # `pet.shelter` 落到接收站、`pet.district` 仍是捕捉地（放归回原小区），
        # 砍掉这一支就等于退回「只按区县」—— 签收方既看不到也办不了。
        'M211 捕捉点可见范围退回只按区县（砍掉挂本站名下支）',
        'business/services.py',
        "    q = Q(**{housed_field: inst_id})\n"
        "    district_id = getattr(getattr(user, 'institution', None), 'district_id', None)\n"
        "    if district_id:\n"
        "        q |= Q(district_id=district_id)\n",
        "    district_id = getattr(getattr(user, 'institution', None), 'district_id', None)\n"
        "    return Q(district_id=district_id) if district_id else Q(pk__in=[])\n",
        'test_signing_shelter_lists_the_pet_it_received',
    ),
    (
        # 反向：把「放开」写成「一律放开」（机构收口失效）= 跨机构越权。
        # 没有这条，`shelter_scope` 被改成不认机构也能让正面用例全绿。
        'M212 捕捉点范围一律放开（机构收口失效）', 'business/services.py',
        "    q = Q(**{housed_field: inst_id})\n",
        "    q = Q(pk__isnull=False)\n",
        'test_third_district_shelter_still_cannot_reach_it',
    ),
    (
        # 账号没挂机构时必须**收紧**（空集），不能当「跳过校验」。
        # 这条复现的是 `core/scope.py` 顶部记录的老坑：字段为空 → 判据静默失效。
        'M213 捕捉点账号未挂机构时跳过范围校验', 'business/services.py',
        "    inst_id = getattr(user, 'institution_id', None)\n"
        "    if not inst_id:\n"
        "        return Q(pk__in=[])\n",
        "    inst_id = getattr(user, 'institution_id', None)\n"
        "    if not inst_id:\n"
        "        return Q(pk__isnull=False)\n",
        'test_third_district_shelter_still_cannot_reach_it',
    ),
    (
        # 调用点也要钉：口径正确但某个页面退回区县过滤，就会出现
        # 「放归备选框有这只、一宠一档里没有」的两页打脸。
        'M214 一宠档捕捉点分支退回只按区县', 'business/views_portal.py',
        "        # 而放归备选框里正躺着这只（列表有、台账没有 = 两个页面互相打脸）。\n"
        "        qs = Pet.objects.filter(is_deleted=False).filter(\n"
        "            shelter_scope(user, 'shelter'))\n",
        "        # 而放归备选框里正躺着这只（列表有、台账没有 = 两个页面互相打脸）。\n"
        "        qs = Pet.objects.filter(is_deleted=False).filter(\n"
        "            district_id=getattr(user.institution, 'district_id', None))\n",
        'test_signing_shelter_pet_archive_lists_the_pet',
    ),
    (
        # 写入口径必须与列表口径一致，否则「点得开却报错」。
        'M215 发起放归取单退回按区县（看得见点不开）', 'business/views_release.py',
        "        pet = get_shelter_scoped_pet(pet_id, user)\n",
        "        from business.services import get_active_pet\n"
        "        pet = get_active_pet(pet_id, user)\n",
        'test_signing_shelter_can_create_transfer_and_confirm_release',
    ),
    (
        # 最后一步（小区确认）单独钉一条：`Release.district` 是捕捉地、
        # `institution` 才是签收站，只按区县会把放归永远卡在「已转运」。
        'M216 确认放归取单退回按区县', 'business/views_release.py',
        "    release = Release.objects.filter(shelter_scope(user, 'institution'), pk=pk).first()\n",
        "    from business.services import get_scoped_object\n"
        "    release = get_scoped_object(Release, pk, user)\n",
        'test_signing_shelter_can_create_transfer_and_confirm_release',
    ),
    (
        # 放归小区带出：前端只查本站捕捉单，跨机构捕捉单的动物就填不出小区，
        # 界面拦在「请填写放归小区」（服务端明明知道）。
        'M217 动物列表不再下发捕捉单小区名', 'business/views_portal.py',
        "        item['capture_community'] = (p.capture.community_name if p.capture_id else '')\n",
        "",
        'test_pet_list_carries_capture_community',
    ),
]

# ---------- 第五十九轮：界面录入巡检查出的五处候选/文案口径 ----------
# 这一批全是「界面摆了必错的选项」或「界面谎报待办」这一族：服务端判据都对，
# 红不了任何接口用例，只有源码契约能钉住 —— 所以每条都要有变异证明守卫不是摆设。
MUTATIONS += [
    (
        # 领回弹窗动物下拉退回读 `Pet.status`：诊疗完成未办结的动物会被说成
        # 「待诊疗」（实测 TNR261026014/016），操作员转去诊疗页白跑一趟。
        'M218 领回弹窗动物下拉退回原始状态', 'static/js/portals/hospital.js',
        "          + '<select class=\"form-select\" id=\"dspOrPet\"><option value=\"\">请选择</option>'\n"
        "          + eligible.map(p => '<option value=\"' + p.id + '\">' + this.escape(p.code)\n"
        "            + (p.name ? ' · ' + this.escape(p.name) : '') + '（'\n"
        "            + this.dspPetStageText(p) + '）</option>').join('')\n",
        "          + '<select class=\"form-select\" id=\"dspOrPet\"><option value=\"\">请选择</option>'\n"
        "          + eligible.map(p => '<option value=\"' + p.id + '\">' + this.escape(p.code)\n"
        "            + (p.name ? ' · ' + this.escape(p.name) : '') + '（'\n"
        "            + TNR_UI.petStatusText(p.status) + '）</option>').join('')\n",
        'test_pet_dropdowns_use_the_derived_stage',
    ),
    (
        # 死亡登记弹窗同一条判据单独钉：两处下拉是各写各的，
        # 只修一处时另一处照样说错（本文件的同类前例：M30/M31 分钉两处）。
        'M219 死亡登记下拉退回原始状态', 'static/js/portals/hospital.js',
        "          + '<select class=\"form-select\" id=\"dspEuthPet\"><option value=\"\">请选择</option>'\n"
        "          + eligible.map(p => '<option value=\"' + p.id + '\">' + this.escape(p.code)\n"
        "            + (p.name ? ' · ' + this.escape(p.name) : '') + '（'\n"
        "            + this.dspPetStageText(p) + '）</option>').join('')\n",
        "          + '<select class=\"form-select\" id=\"dspEuthPet\"><option value=\"\">请选择</option>'\n"
        "          + eligible.map(p => '<option value=\"' + p.id + '\">' + this.escape(p.code)\n"
        "            + (p.name ? ' · ' + this.escape(p.name) : '') + '（'\n"
        "            + TNR_UI.petStatusText(p.status) + '）</option>').join('')\n",
        'test_pet_dropdowns_use_the_derived_stage',
    ),
    (
        # 派生判据本身：不看看板桶 = 恒等于读原始状态，两个下拉同时说错，
        # 而上面的 M218/M219 抓不到（它们只看调用点写没写对）。
        'M220 派生阶段不再看看板 pending_discharge 桶', 'static/js/portals/hospital.js',
        "    if (this.dspAdviceOf(pet.id) !== undefined) {\n"
        "      return TNR_UI.boardStageText('pending_discharge');\n"
        "    }\n",
        "    if (false) {\n"
        "      return TNR_UI.boardStageText('pending_discharge');\n"
        "    }\n",
        'test_stage_helper_follows_the_board_bucket',
    ),
    (
        # 首页「待领养资料待维护」不按缺口过滤：资料已齐、已上架的动物
        # 仍被写「请完善照片和简介」（实测 TNR261026003 花卷）。
        'M221 首页领养待办不按资料缺口过滤', 'static/js/portals/hospital.js',
        "        .filter(it => it.gaps.length);\n"
        "      const pendAdopt = adoptTodo.length;\n",
        "      const pendAdopt = adoptTodo.length;\n",
        'test_dashboard_filters_and_counts_by_gaps',
    ),
    (
        # 待办文案写死：缺口判据算出来了却不读，等于回到谎报。
        'M222 首页领养待办文案写死照片和简介', 'static/js/portals/hospital.js',
        "          desc: TNR_UI.joinInfo([p.name, '请完善' + it.gaps.join('和')]),\n",
        "          desc: TNR_UI.joinInfo([p.name, '请完善照片和简介']),\n",
        'test_dashboard_filters_and_counts_by_gaps',
    ),
    (
        # 大厅清单换成静默读：接口拒绝时返回 `[]`，首页会把**所有**待领养动物
        # 判成「缺资料」—— 与「真的都缺」在界面上完全一样，方向正好相反。
        'M223 首页改用静默读大厅清单', 'static/js/portals/hospital.js',
        "        TNR_API.getHallListingsStrict()\n      ]);",
        "        TNR_API.getHallListings()\n      ]);",
        'test_dashboard_reads_hall_listings_strictly',
    ),
    (
        # 照片只数张数、不看显隐：把系统照片逐张关掉「大厅显示」的动物
        # 会被判成资料齐全，待办提前消失（真正缺的是「大厅里一张都不剩」）。
        'M224 资料缺口判据不看照片显隐', 'static/js/portals/hospital.js',
        "    ? (listing.photos || []).some(p => p.visible)\n",
        "    ? (listing.photos || []).length > 0\n",
        'test_gap_check_is_one_shared_source',
    ),
    (
        # 弹窗另抄一份系统照片清单：加一个照片环节（如术后再加一栏）时，
        # 首页的「有没有照片」判据不认识新字段，会把有照片的说成没照片。
        'M225 维护资料弹窗另抄一份系统照片清单', 'static/js/portals/hospital.js',
        "    const systemPhotos = PET_SYSTEM_PHOTOS\n      .map(([field, label]) => {\n",
        "    const SYSTEM_PHOTOS = [\n"
        "      ['photo_capture', '捕捉照片'],\n"
        "      ['photo_group', '整体合影'],\n"
        "      ['photo_before', '术前照片'],\n"
        "      ['photo_after', '术后照片'],\n"
        "      ['photo_treatment', '诊疗后照片']\n"
        "    ];\n"
        "    const systemPhotos = SYSTEM_PHOTOS\n      .map(([field, label]) => {\n",
        'test_system_photo_fields_have_one_frontend_source',
    ),
    (
        # 领养登记候选放回 `adopted`（原缺陷）：5 只已领出的动物进下拉，
        # 选中必被 `adoption_register` 的状态判据挡回，操作员只能逐只试。
        'M226 领养登记候选放回已领出动物', 'static/js/portals/shelter.js',
        "    const adoptablePets = pets.filter(p => p.status === 'pending_adopt'\n"
        "      && !claimedPetIds.has(String(p.id)));\n",
        "    const adoptablePets = pets.filter(p => p.status === 'pending_adopt'\n"
        "      || p.status === 'adopted');\n",
        'test_register_dropdown_excludes_adopted_and_claimed',
    ),
    (
        # 违规收回候选退回整份 `getAdoptions()`（原缺陷）：
        # 「已取消」的记录被列进来，选中后 `adoption_reclaim` 报「请勿重复操作」。
        'M227 违规收回候选退回整份领养单', 'static/js/portals/shelter.js',
        "id=\"fu_adoption\">${reclaimable.map(a =>",
        "id=\"fu_adoption\">${adoptions.map(a =>",
        'test_reclaim_dropdown_only_lists_established_adoptions',
    ),
    (
        # 下发换物料后不回填已选接收方（原缺陷）：`<select>` 静默落到第一项，
        # 提交时货发去了另一家，而界面看起来「有值」，不会引起怀疑。
        'M228 下发重建接收方后不回填已选值', 'static/js/portals/shelter.js',
        "        if (keep && list.some(t => String(t.id) === String(keep))) {\n"
        "          targetSel.value = keep;\n"
        "        }\n",
        "",
        'test_selected_target_is_restored_after_reload',
    ),
    (
        # 「门户调用点 × 角色」巡检自己也得有锚：这条判据扫的是源码里有没有
        # 出现某个路径，**不剥注释**时，说明注释里提一句接口名就会被当成真实
        # 调用点，造出「领养人端点即 403」的假指控（实测把全量测试打红）。
        # 变异打在巡检的剥离器上 —— 它坏了，整片判据都在看假数据。
        'M229 调用点巡检不再抹 JS 注释', 'business/tests/test_endpoint_role_scope.py',
        "    src = blank_comments(read_js(rel_path))\n",
        "    src = read_js(rel_path)\n",
        'test_comment_paths_are_not_counted_as_calls',
    ),
]

# ---------- 第六十六轮：回访「驳回 → 更正 → 重交」闭环 + 捕捉单状态单一真源 ----------
# 这一轮的缺陷全部是**不报错的口径错**：驳回存不下原因、被驳回的月份再也交不进去、
# 撤回运回单把「待出院」的动物说成「待领养」、库里「已完成」而按单据算「待转运」的单
# 在捕捉点端和监管端各显示一套。变异表里必须留一条钉子，否则下一次重构又会把
# 判据写回两份。
MUTATIONS += [
    (
        # 驳回不再要求原因：`review_note` 字段照收，但没人校验，
        # 领养人端就只剩一条「已驳回」——本轮修的就是这件事。
        'M230 驳回不再强制填写原因', 'business/views_checkin.py',
        "    if new_status == 'rejected' and not review_note:\n"
        "        return json_fail('驳回需要填写原因，领养人据此更正后才能重新提交')\n",
        "    if new_status == 'rejected' and False:\n"
        "        return json_fail('驳回需要填写原因，领养人据此更正后才能重新提交')\n",
        'test_reject_without_reason_refused',
    ),
    (
        # 重复打卡判据退回「不看状态」：被驳回的那一条继续占位，
        # 「驳回 → 更正 → 重交」这条路就断了（现网 2026-10-27 实测复现）。
        'M231 被驳回的月份仍占住打卡名额', 'business/views_checkin.py',
        "        status__in=('pending', 'approved')).exists()\n",
        "        status__in=('pending', 'approved', 'rejected')).exists()\n",
        'test_rejected_month_can_be_checked_in_again',
    ),
    (
        # 驳回不再推站内消息：领养人不一定每天翻打卡页，消息页才是他们会看的
        # 地方；少了这一条，「更正后重交」根本传不到人。
        'M232 驳回不再推站内消息', 'business/views_checkin.py',
        "    if new_status == 'rejected' and checkin.adopter_id:\n",
        "    if new_status == 'rejected' and checkin.adopter_id is None:\n",
        'test_reject_reason_is_trimmed_stored_and_pushed',
    ),
    (
        # 领养人端「本月待打卡」退回把所有打卡都算已办结：被驳回的动物
        # 从待打卡列表消失，而它其实还欠一次 —— 与 M231 是同一口径的两端。
        'M233 领养人端把已驳回的打卡算成已办结', 'static/js/portals/adopter.js',
        "      return !petCheckIns.some(c => c.month === currentMonth\n"
        "        && c.status !== 'rejected');\n",
        "      return !petCheckIns.some(c => c.month === currentMonth);\n",
        'test_adopter_pending_month_treats_rejected_as_unmet',
    ),
    (
        # 打卡历史不再渲染驳回原因：原因存进了库、接口也下发了，
        # 界面上不显示就等于让人猜。
        'M234 打卡历史不渲染驳回原因', 'static/js/portals/adopter.js',
        "        ${checkin.status === 'rejected' && reviewNote\n"
        "          ? `<div class=\"checkin-history-review\">驳回原因：${TNR_UI.escape(reviewNote)}（更正后请重新提交本月打卡）</div>`\n"
        "          : ''}\n",
        None,
        'test_adopter_shows_the_reject_reason',
    ),
    (
        # 驳回弹层取消非空校验：空原因交给服务端拒，操作员看到的是「驳回失败」，
        # 而领养人收到的仍是没有原因的驳回。
        'M235 驳回弹层不再校验原因非空', 'static/js/portals/shelter.js',
        "          if (!note) { TNR_UI.toast('请填写驳回原因', 'warning'); return; }\n",
        None,
        'test_shelter_reject_dialog_collects_a_reason',
    ),
    (
        # 关掉「取消 = 不发请求」这条路：`checkinRejectDialog` 取消时 resolve(null)，
        # 判据写坏后 null 会被当成原因提交出去（服务端拒 / 领养人收到空原因）。
        'M236 取消驳回弹层仍发出请求', 'static/js/portals/shelter.js',
        "            if (note === null) return;\n",
        "            if (note === undefined) return;\n",
        'test_shelter_reject_dialog_collects_a_reason',
    ),
    (
        # 运回单撤回/驳回文案退回写死「待领养」：诊疗中建议放归的动物被打回后
        # 其实是「待出院」，操作员按提示去领养登记页白跑一趟。
        'M237 撤回运回单文案退回写死待领养', 'business/views_return.py',
        "        _, display = pet_stage(pet)\n",
        "        display = '待领养'\n",
        'test_withdraw_message_reports_actual_stage',
    ),
    (
        # 派生规则不再认「部分转运」这一档：转了 3 只（共 4 只）的单显示成已完成，
        # 剩下的那只从待办里消失。
        'M238 派生规则吃掉部分转运', 'business/services.py',
        "    if transferred < total:\n        return 'partial'\n",
        "    if transferred < total:\n        return 'completed'\n",
        'test_partial_shows_partial_not_counts',
    ),
    (
        # 作废单不再派生成 void：已作废的捕捉单显示成「待转运」，
        # 监管端还会把它当成未办完的活。
        'M239 作废单不再派生成 void', 'business/services.py',
        "    if is_deleted:\n        return 'void'\n",
        "    if is_deleted:\n        return 'pending'\n",
        'test_voided_capture_status_is_void',
    ),
    (
        'M240 状态中文退回直接印枚举码', 'business/services.py',
        "    return dict(Capture.STATUS_CHOICES).get(status, status)\n",
        "    return status\n",
        'test_list_status_is_derived_when_column_is_stale',
    ),
    (
        # 列表下发退回读缓存列：派生值仍在 `transfer_state` 里，
        # 而顶层 `status`（前端徽章与筛选都读它）回到旧缓存 —— 一页两种说法。
        'M241 捕捉列表退回读缓存状态', 'business/views_capture.py',
        "        item['status'] = state.get('status') or cap.status\n",
        "        item['status'] = cap.status\n",
        'test_list_status_is_derived_when_column_is_stale',
    ),
    (
        # 筛选也退回按缓存判：界面显示「待转运」而 `?status=completed` 能把它筛出来。
        'M242 捕捉筛选退回按缓存状态', 'business/views_capture.py',
        "        if status and item['status'] != status:\n            continue\n",
        "        if status and cap.status != status:\n            continue\n",
        'test_status_filter_uses_displayed_value',
    ),
    (
        # 监管业务表退回读列：现网 CAP-T0001 正是这一条（库里已完成、按单据算待转运），
        # 监管与执行两端对不上账。
        'M243 监管业务表退回读缓存状态', 'supervision/views.py',
        "            state = states.get(c.id) or {}\n            if state:\n                item['status'] = state['status']\n",
        "            state = states.get(c.id) or {}\n            if False:\n                item['status'] = state['status']\n",
        'test_supervision_and_ledger_agree_with_capture_list',
    ),
    (
        # 监管台账同一条判据单独钉：两处各写各的，只修一处时另一处照样旧值
        # （本文件同类前例：M218/M219 分钉两处下拉）。
        'M244 监管台账退回读缓存状态', 'supervision/views.py',
        "                'status': (cap_states.get(c.id) or {}).get('status') or c.status,\n",
        "                'status': c.status,\n",
        'test_supervision_and_ledger_agree_with_capture_list',
    ),
    (
        # 捕捉点端徽标退回自己判色：`st` 取空后徽章落回缓存 `rec.status`，
        # 而进度数字仍按派生值算 —— 「已完成 + 0/1 已转运」这种自相矛盾的组合
        # 就是这么来的。
        'M245 捕捉点徽标不再翻译派生状态', 'static/js/portals/shelter.js',
        "  transferStatusBadge(rec) {\n    const st = rec.transferState || rec.transfer_state || {};\n",
        "  transferStatusBadge(rec) {\n    const st = {};\n",
        'test_capture_badge_translates_the_derived_state',
    ),
]

# 允许 5 元组（单文件变异）与 6 元组（+ 附加改动，用于「复现原缺陷」这类跨文件变异）
MUTATIONS = [m if len(m) == 6 else m + ([],) for m in MUTATIONS]

ENV = dict(os.environ, DEBUG='on', SECRET_KEY='t',
           # ⚠ `127.0.0.1` 不能省。`business.tests.test_doc_numbers` 里的跨区县
           # 打印用例固定用 `HTTP_HOST='127.0.0.1'`（打印页要拿它拼二维码绝对链接），
           # 而这里的 ENV 会**覆盖** settings 的 DEBUG 兜底列表 —— 少了它，
           # Django 直接回 400 DisallowedHost，用例全红，而红的**不是被打磨的那条判据**。
           # 夹具的环境变量必须**不比真实测试环境更窄**，否则「期望失败的用例名
           # 没出现在失败列表」会伪装成 WEAK，「别的用例也红了」会伪装成 PASS。
           ALLOWED_HOSTS='localhost,testserver,127.0.0.1')
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
    ENV.pop(_k, None)


def run_tests():
    cmd = [str(ROOT / '.venv/bin/python'), 'manage.py', 'test', *TEST_TARGETS, '-v', '0']
    r = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


# `FAIL: <用例名> (<模块.类.用例>)`，后面可能再跟一个 `(subTest 参数)`
_FAIL_LINE_RE = re.compile(r'^(?:FAIL|ERROR):\s+(\S+)\s+\([^)]*\)(?:\s+\((.*)\))?\s*$')


def _short(line):
    """把 unittest 的失败行压成「用例名 + subTest 标签」。

    原始形态：`FAIL: test_xxx (mod.Cls.test_xxx) (method='getTransfersStrict')`

    ⚠ **必须带上 subTest 标签**：用 `subTest` 循环的用例（本仓库大量使用）
    会在**同一个用例名**下产生**多行**失败 —— 只取 `split(' ')[1]`
    会打印出四行一模一样的名字，**看不出是哪一次迭代红的**（M42 实测）。
    ⚠ 后缀是 `(method='x')`（**圆括号 + repr**），不是 `[method=x]` ——
    这一点是实测出来的（`TextTestResult.getDescription` 对 `_SubTest` 走
    `str()`，其形态为 `test (mod.Cls.test) (kwarg=repr)`）。
    """
    m = _FAIL_LINE_RE.match(line)
    if not m:
        parts = line.split(' ', 2)
        return parts[1] if len(parts) > 1 else '?'
    name, sub = m.group(1), m.group(2)
    return f'{name} {sub}' if sub else name


# 信号处理器要用的「原始内容」快照（在 main 里填）
_SNAPSHOT = {}
# 单实例锁的 fd（在 main 里填）—— 关掉它就等于放锁
_LOCK_KEEPALIVE = None


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
    # ---- 同一时刻只允许一个扫描进程 ----
    # 两条变异扫描**并发跑会毁代码**：每个进程都在启动时对目标文件拍一份
    # 「原始内容」快照，跑完（或收到信号）再把快照写回。第二个进程拍到的
    # 往往是第一个进程**正在变异**的那一版，于是它的「还原」会把变异体
    # 永久写进工作区 —— 而它自己的预检只会抱怨「锚点命中 0 次」，
    # 看起来像旧锚点失效，不像数据损坏。（第六十一轮实测踩到：三条扫描同时在跑，
    # 预检报 M1/M4 命中 0 次，而 `business/views_adoption.py` 的机构判据
    # 一度只剩半截。）锁必须在**拍快照之前**拿。
    _lock = open('/tmp/tnr-mutation-check.lock', 'w')
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            '另一个 mutation_check 正在跑（锁：/tmp/tnr-mutation-check.lock）。\n'
            '并发扫描会互相覆盖快照、把变异体永久留在工作区 —— 请先结束它：\n'
            '  pgrep -fl mutation_check   # 看 pid\n'
            '  pkill -TERM -f mutation_check   # 收到信号会先还原再退出')
    global _LOCK_KEEPALIVE
    _LOCK_KEEPALIVE = _lock          # 关掉 fd 会释放锁，必须持有到进程结束

    files = sorted({m[1] for m in MUTATIONS} | {e[0] for m in MUTATIONS for e in m[5]})
    originals = {f: (ROOT / f).read_text() for f in files}
    _SNAPSHOT.update(originals)
    for _name in ('SIGTERM', 'SIGINT', 'SIGHUP'):
        _sig = getattr(signal, _name, None)
        if _sig is not None:
            signal.signal(_sig, _on_signal)

    # ---- 预检：先把「打不准的锚点」一次性列出来 ----
    # 主循环里也有命中数检查，但那是**每条变异各查各的**：一条失效锚点要等到
    # 它排到的时候才现形，而前面每条都得先跑完一遍测试（本轮 52 条 ≈ 十几分钟）。
    # 更糟的是「⚠️ SKIP」和「✅ PASS」在收尾日志里长得几乎一样，
    # 而第五十二轮实测发现 12 条锚点因**代码搬家/改写法**集体失配 ——
    # 那 12 条上一轮根本没被跑到（只跑了新增判据相关的模块）。
    # 所以这里在任何测试启动之前先扫一遍全表，异常直接点名，避免「结论过期还读着像有效」。
    stale = []
    for label, rel, old, _new, _expect, extras in MUTATIONS:
        n = originals[rel].count(old)
        if n != 1:
            stale.append(f'{label}  [{rel}] 命中 {n} 次')
        for erel, eold, _enew in extras:
            en = originals[erel].count(eold)
            if en != 1:
                stale.append(f'{label} 的附加改动 [{erel}] 命中 {en} 次')
    for label in (m[0] for m in MUTATIONS if m[0] in MOVE_AFTER):
        arel, anchor, _payload = MOVE_AFTER[label]
        if arel not in originals:
            continue
        n = originals[arel].count(anchor)
        if n != 1:
            stale.append(f'{label} 的 MOVE_AFTER 锚点 [{arel}] 命中 {n} 次')
    if stale:
        print('!' * 76)
        print(f'预检发现 {len(stale)} 处锚点与当前代码不符（这些变异会 SKIP，'
              f'即对应判据的判别力**本轮未被验证**）：')
        for s in stale:
            print('  -', s)
        print('!' * 76)

    results = []
    filtered = 0
    # 进度行：**每条变异跑完立刻打一行**（带序号、累计耗时）。
    # 收尾才打表 + 重定向到文件时 stdout 是块缓冲的，几小时里日志一片空白，
    # 既看不出跑到哪、也看不出是不是卡住了 —— 全表 ≈3 小时的复扫必须有心跳。
    total = len([t for t in MUTATIONS
                 if not MUTATION_LABELS or any(s in t[0] for s in MUTATION_LABELS)])
    started = time.time()
    done = 0

    def record(label, verdict, detail):
        nonlocal done
        results.append((label, verdict, detail))
        done += 1
        mins = (time.time() - started) / 60
        print(f'[{done}/{total}] {verdict} {label} '
              f'({mins:.1f} 分钟，均 {mins / max(done, 1) * 60:.0f}s/条)',
              flush=True)

    try:
        for label, rel, old, new, expect, extras in MUTATIONS:
            if MUTATION_LABELS and not any(s in label for s in MUTATION_LABELS):
                filtered += 1
                continue
            original = originals[rel]
            hits = original.count(old)
            if hits != 1:
                record(label, 'SKIP', f'目标串命中 {hits} 次（应恰好 1 次）')
                continue

            mutated = original.replace(old, new if new is not None else '', 1)
            if label in MOVE_AFTER:
                arel, anchor, payload = MOVE_AFTER[label]
                if arel != rel:
                    record(label, 'SKIP', 'MOVE_AFTER 与目标文件不一致')
                    continue
                if mutated.count(anchor) != 1:
                    record(label, 'SKIP', f'锚点不唯一：{anchor!r}')
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
                record(label, 'SKIP', bad)
                continue

            for f, text in payloads.items():
                (ROOT / f).write_text(text)
            code, out = run_tests()
            for f, text in originals.items():      # 全量还原，天然幂等
                (ROOT / f).write_text(text)

            if code == 0:
                record(label, 'FAIL', '变异体下测试**全部通过** —— 空转！')
            else:
                failed = [ln for ln in out.splitlines()
                          if ln.startswith('FAIL:') or ln.startswith('ERROR:')]
                hit = any(expect in ln for ln in failed)
                detail = '; '.join(_short(ln) for ln in failed[:4]) or '?'
                record(label, 'PASS' if hit else 'WEAK', f'捕获={detail}')
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
    if filtered:
        print(f'（本轮按 MUTATION_LABELS 过滤：跑 {len(results)} 条、跳过 {filtered} 条）')
    print('全部变异均被捕获' if ok else '存在未被捕获的变异 —— 测试判别力不足')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

