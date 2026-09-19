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
    (
        'M5 契约层：换等价写法（institution_id → institution.pk）',
        'business/views_transfer.py',
        "    if user.institution_id != transfer.to_hospital_id:\n"
        "        return json_fail('无权签收此转运记录')",
        "    if user.institution.pk != transfer.to_hospital_id:\n"
        "        return json_fail('无权签收此转运记录')",
        'test_every_hospital_write_view_has_institution_check',
    ),
    (
        'M6 契约层：判据换成区县维度', 'business/views_transfer.py',
        "if user.institution_id != transfer.to_hospital_id:\n"
        "        return json_fail('无权签收此转运记录')",
        "if user.district_id != transfer.district_id:\n"
        "        return json_fail('无权签收此转运记录')",
        'cross_institution',
    ),
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
        "js/tnr-api.js' %}?v=20260919a",
        "js/tnr-api.js' %}?v=20260919b",
        'all_version_query_params_are_identical',
    ),
    (
        'M12 某个门户的 tnr-api.js 去掉版本号',
        'templates/portal/adopter/portal.html',
        "js/tnr-api.js' %}?v=20260919a",
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
