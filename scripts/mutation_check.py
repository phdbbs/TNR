"""变异验证通用夹具：把「刚补上的判据」逐个破坏，确认测试真的会红。

## 为什么需要它

「测试全绿」只证明**当前代码**能过，不证明测试**有判别力**。
如果断言写得空转（无论代码对错都通过），重构时判据被删掉也不会有任何信号 ——
这是「静默失败」在测试层的形态。

## 用法

1. 改下面的 `MUTATIONS`：每条 = (标签, 目标文件, old, new, 期望失败的测试名)。
2. 先确认 `old` 在文件里**唯一命中**（脚本会检查，命中数 ≠ 1 就 SKIP 并告警）——
   否则 `replace(..., 1)` 可能打在同名的别处代码上，那是**第四种空转**。
3. 跑：

       .venv/bin/python scripts/mutation_check.py

   退出码 0 = 全部变异被捕获；非 0 = 存在空转，测试判别力不足。

4. 脚本**改完即还原**，并在退出前断言还原成功；不会把变异体留在工作区。

## 五种已知空转（本夹具能挡住第 ④ 种）

① 断言匹配到任意一处同名文本；② 匹配到注释/docstring；
③ 正则漏 `re.M`；④ **变异没落在目标代码上**（`replace(old, new, 1)` 打偏）；
⑤ 夹具造不出触发条件（这要靠「判别力自检」在用例里显式报出来，本脚本挡不住）。

## 特殊变异：把判据「挪位」

需要把一段代码从 A 处挪到 B 处时，`new` 里写锚点语法：见 `MOVE_AFTER` 的处理。
"""
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 测试目标：默认跑本轮新增的用例文件（跑全量会很慢）
TEST_TARGETS = ['business.tests.test_write_scope']

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

ENV = dict(os.environ, DEBUG='on', SECRET_KEY='t',
           ALLOWED_HOSTS='localhost,testserver')
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
    ENV.pop(_k, None)


def run_tests():
    cmd = [str(ROOT / '.venv/bin/python'), 'manage.py', 'test', *TEST_TARGETS, '-v', '0']
    r = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


def main():
    files = sorted({m[1] for m in MUTATIONS})
    originals = {f: (ROOT / f).read_text() for f in files}
    results = []
    try:
        for label, rel, old, new, expect in MUTATIONS:
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

            (ROOT / rel).write_text(mutated)
            code, out = run_tests()
            (ROOT / rel).write_text(original)

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
