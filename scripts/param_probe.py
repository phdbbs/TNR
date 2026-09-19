"""参数探针：把「查询参数」当作攻击面扫一遍（第二十四轮）。

## 这一轮为什么换这个维度

前几轮扫的是「**谁**」（角色矩阵）、「**哪条记录**」（id 越权 / 存在性预言机）。
这一轮扫**参数**：同一个接口、同一个角色、完全合法的会话，
只用查询参数去操纵结果集。两个正交的检查：

### A. 类型契约 —— 非法参数值是「被拒绝」还是「未捕获异常」

参数值进入强类型上下文时（`filter(<整型外键>=参数)`、`int()`、`float()`），
传非数字会抛 `ValueError`。**没有 try/except 就是 500**。

为什么这算缺陷（而不只是「难看」）：
- 500 是**未处理的异常路径**，`DEBUG=True` 下会吐出完整栈、设置、局部变量；
- 它把「用户输入错误」错误地归类成「服务端故障」，前端无法区分、无法提示；
- 它让**本来该生效的功能静默失效** —— 例如
  `Q(district__name__icontains=d) | Q(district_id=d)` 本意是支持中文区县名，
  但 `district_id=d` 那一半**无论如何都会被求值**，于是「按中文区县名筛选」
  从来没成功过（实测 `?district=襄城区` 直接 500）。

判据：`?P=<投毒值>` → 期望 4xx 或 200（忽略非法值），**500 即缺陷**。

### B. 可见范围 —— 参数会不会**放宽**可见集合

看起来像「开关 / 范围」的参数（`include_deleted`、`*_id`、`district`、
`institution_id`…），传进去会不会让**本来看不到**的记录出现？

判据**自校准**：把响应的 id 集合与**无参数基线**对比 ——
只要出现基线里没有的 id，就是放宽。不需要猜参数语义、不需要猜错误文案。

### C. 数量上限 —— 「合法但荒谬」的数值会不会把服务端吃光

A 类只覆盖「格式非法」，B 类只覆盖「范围放宽」。还有第三类：
**值完全合法，但量级荒谬**。`?count=999999999` 不会让接口报错 ——
它会真的去循环生成 10 亿个编号（实测 20 万条约 23 ms，线性外推 ≈ 110 秒
CPU + 数十 GB 内存），进程被拖死，而响应最终仍是 200。
**这类问题 A/B 都扫不出来**，必须单独量：给数量类参数一个「大到能看出
没有上限、又小到不会拖死探针」的值（`LARGE_COUNT`），数响应里的条目数。

判据：数量类参数传 `LARGE_COUNT` 后，响应 `data` 列表长度不得超过
`MAX_ROWS`。超了 = 该参数没有上限。

**A 类必须对数量类参数剔除超大数毒值**（见 `poison_for`）：未修复的
`?count=999999999999999999999999` 会把服务端（以及探针自己）一起 OOM 掉，
那样探针连结论都报不出来 —— 一个「会把检测工具杀掉」的检测工具没有用。
量级问题归 C 类管。

## 用法

    .venv/bin/python scripts/param_probe.py [路由子串]

退出码：0 = 无发现；1 = 有 🔴。
"""
import inspect
import logging
import os
import pathlib
import re
import sys

# 脚本在 scripts/ 下，项目根不在 sys.path 上 —— 不补这一行 django.setup() 会
# 报 `ModuleNotFoundError: No module named 'tnr_system'`。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tnr_system.settings')
os.environ.setdefault('ALLOWED_HOSTS', 'testserver,localhost,127.0.0.1')

import django  # noqa: E402

django.setup()

from django.db.models import Q  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import get_resolver  # noqa: E402

from accounts.models import User  # noqa: E402

ROLES = {'gov_city', 'gov_district', 'shelter', 'hospital', 'adopter'}

# 投毒值：非数字 / 中文 / 超长数字 / 负数 / 小数 / 空串
# 空串单独有意义：多数视图写 `if param:`，空串应当被「当作没传」而不是报错。
#
# 后四个是**日期**专用毒值。加进来是因为探针原先的毒值表里没有任何
# 日期格式的串，于是漏掉了 `?end_date=9999-12-31` —— 那个值格式完全合法，
# 却在 `end_date + timedelta(days=1)` 处抛 `OverflowError`（`date.max` 加一天）。
# 这是与 SQLite 那个 `OverflowError` **同名不同源**的第四种异常类型。
POISON = ['abc', '襄城区', '9' * 24, '-1', '1.5', '',
          '9999-12-31', '0000-01-01', '2026-02-30', '2026-13-01']

# C 类：数量上限。`LARGE_COUNT` 要大到足以暴露「没有上限」，
# 又小到探针自己不会被拖死（实测 10 万条编号约 12 ms）。
LARGE_COUNT = 100_000
MAX_ROWS = 2_000
COUNT_PARAM_RE = re.compile(r'count|quantity|qty|limit|num|size|page|offset')

# 数量类参数**不能**在 A 类里投超大数。
# 实测：未修复的 `?count=999999999999999999999999` 会让服务端真的去
# `range(10 ** 24)` —— 进程被 OOM 杀掉，**探针自己也活不下来**，
# 于是什么结论都报不出来（本轮做修复前校准时就是这样被 SIGTERM 的）。
# 量级问题交给 C 类，用一个「跑得完、但足以证明没有上限」的值。
EXTREME_NUMBERS = {'9' * 24}


def poison_for(name):
    """该参数的投毒值表。数量类参数去掉会拖死探针的超大数。"""
    if COUNT_PARAM_RE.search(name):
        return [v for v in POISON if v not in EXTREME_NUMBERS]
    return POISON

# 从视图源码里提取查询参数名
PARAM_RE = re.compile(
    r"request\.GET\.get\(\s*'([^']+)'|request\.GET\[\s*'([^']+)'\]")


# ---------------------------------------------------------------- 路由 / 角色
def roles_of(callback):
    """从 `role_required` 的闭包里取允许角色集合。"""
    f = callback
    for _ in range(6):
        for cell in getattr(f, '__closure__', None) or []:
            try:
                v = cell.cell_contents
            except Exception:
                continue
            if isinstance(v, (tuple, list)) and v and all(
                    isinstance(x, str) and x in ROLES for x in v):
                return set(v)
        f = getattr(f, '__wrapped__', None)
        if f is None:
            break
    return None


def params_of(callback):
    """视图源码里读过的查询参数名（去重、保序）。"""
    try:
        src = inspect.getsource(callback)
    except Exception:
        return []
    out = []
    for m in PARAM_RE.finditer(src):
        name = m.group(1) or m.group(2)
        if name and name not in out:
            out.append(name)
    return out


def list_routes():
    """全部「无路径参数」的 `/api/` 路由（带 id 的归 scope_probe 管）。"""
    pairs = []

    def walk(resolver, prefix=''):
        for p in resolver.url_patterns:
            if hasattr(p, 'url_patterns'):
                walk(p, prefix + str(p.pattern))
            else:
                pairs.append(('/' + (prefix + str(p.pattern)).lstrip('/'), p.callback))

    walk(get_resolver())
    out = []
    for pat, cb in pairs:
        if '/api/' not in pat or '<' in pat:
            continue
        rs = roles_of(cb)
        if rs is None:
            continue
        out.append((pat, getattr(cb, '__name__', ''), rs, cb))
    return sorted(out)


# ---------------------------------------------------------------- 上下文
def build_context(actor):
    """给「放宽」检查准备跨范围的 id。

    只取**与本账号不在同一区县**的对象 —— 传进去若可见集合变大，就是放宽。
    """
    from core.models import District, Institution
    from business.models import Material, Pet

    d = actor.district_id
    other_d = District.objects.exclude(id=d).filter(is_city=False).first()
    ctx = {'actor_district': d, 'other_district': None,
           'other_institution': None, 'other_material': None, 'other_pet': None}
    if other_d is None:
        return ctx
    ctx['other_district'] = other_d.id
    inst = Institution.objects.filter(district=other_d).first()
    if inst:
        ctx['other_institution'] = inst.id
    mat = Material.objects.filter(district=other_d).first()
    if mat:
        ctx['other_material'] = mat.id
    pet = Pet.objects.filter(district=other_d, is_deleted=False).first()
    if pet:
        ctx['other_pet'] = pet.id
    return ctx


def scope_candidates(name, ctx):
    """「如果服务端信了这个参数，会不会放宽范围」的取值。"""
    vals = []
    if re.search(r'deleted|include|(^|_)all$|scope', name):
        vals += ['1', 'true']
    if name in ('district', 'district_id', 'districtId'):
        vals += [str(ctx['other_district'])] if ctx['other_district'] else []
    if 'institution' in name or name in ('shelter_id', 'hospital_id'):
        vals += [str(ctx['other_institution'])] if ctx['other_institution'] else []
    if name.endswith('_id'):
        vals += [str(v) for v in (ctx['other_material'], ctx['other_pet']) if v]
    return vals


# ---------------------------------------------------------------- 响应解析
def ids_of(resp):
    """响应里 `data` 是列表时，取其 `id` 集合；否则 None（该接口不做放宽对比）。"""
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    data = body.get('data')
    if not isinstance(data, list):
        return None
    out = set()
    for row in data:
        if isinstance(row, dict) and isinstance(row.get('id'), int):
            out.add(row['id'])
    return out


def size_of(resp):
    """响应 `data` 是列表时返回条目数，否则 None。用于 C 类「数量上限」检查。"""
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    data = body.get('data')
    if isinstance(data, list):
        return len(data)
    return None


def pick_actor(role):
    qs = User.objects.filter(role=role, is_active=True)
    narrow = qs.filter(Q(district__isnull=True) | Q(district__is_city=False))
    if narrow.exists():
        return narrow.first(), None
    if qs.exists():
        return None, '该角色只有「账号区县=全市」的账号（用它看不出收窄，跳过）'
    return None, '演示库无该角色的可用账号'


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    logging.disable(logging.CRITICAL)   # 500 的 ERROR 日志会淹没结论

    routes = list_routes()
    print('=' * 100)
    print('参数探针 —— A 类型契约（非法值应 4xx / 忽略，不该 500）')
    print('                  B 可见范围（传范围类参数不该出现基线里没有的 id）')
    print(f'                  C 数量上限（数量类参数传 {LARGE_COUNT} 不该吐回 > {MAX_ROWS} 条）')
    print('=' * 100)

    crashes, widened, tolerated, skipped, unbounded = [], [], [], [], []
    sessions = {}

    for pattern, name, roles, cb in routes:
        if only and only not in pattern:
            continue
        for role in sorted(roles):
            if role not in sessions:
                actor, why = pick_actor(role)
                if actor is None:
                    sessions[role] = (None, why)
                else:
                    c = Client(raise_request_exception=False)
                    c.force_login(actor)
                    sessions[role] = (c, actor)
            client, actor = sessions[role]
            if client is None:
                skipped.append((pattern, role, actor))
                continue

            params = params_of(cb)
            if not params:
                skipped.append((pattern, role, '视图不读任何查询参数'))
                continue

            # --- C. 数量上限：**不能挂在「基线必须 200」的门槛后面** ---
            # `captures/codes-preview/` 无参数时就是 400（「数量必须大于0」），
            # 若把 C 也放在基线门槛之后，这条检查正好跳过它本该守的那个接口
            # —— 判据自己变成空转。（实测踩到过，所以 C 提到门槛之前。）
            for p in params:
                if COUNT_PARAM_RE.search(p):
                    r = client.get(pattern, {p: LARGE_COUNT})
                    n = size_of(r)
                    if n is not None and n > MAX_ROWS:
                        unbounded.append((pattern, role, p, r.status_code, n))

            # 基线只用来判断「这个接口对这个角色能不能正常访问」。
            # 400 **不算**不可访问 —— 很多列表接口缺必填参数就是 400，
            # 那种情况下 A（非法值不 500）与 B（不放宽）仍然要检查。
            # 只有 401/403/404/405 才让 A/B 的结论失去意义。
            base = client.get(pattern)
            if base.status_code in (401, 403, 404, 405):
                skipped.append((pattern, role,
                                f'无参数基线 {base.status_code}（不可访问或方法不符）'))
                continue
            base_ids = ids_of(base)
            ctx = build_context(actor)

            for p in params:
                # --- A. 类型契约 ---
                bad = []
                for v in poison_for(p):
                    r = client.get(pattern, {p: v})
                    if r.status_code >= 500:
                        bad.append((v, r.status_code))
                if bad:
                    crashes.append((pattern, role, p, bad))
                else:
                    tolerated.append((pattern, role, p))

                # --- B. 可见范围 ---
                for v in scope_candidates(p, ctx):
                    r = client.get(pattern, {p: v})
                    if r.status_code != 200:
                        continue
                    got = ids_of(r)
                    if base_ids is None or got is None:
                        continue
                    extra = got - base_ids
                    if extra:
                        widened.append((pattern, role, p, v, sorted(extra)[:5],
                                        len(extra)))

    def dump(title, rows, fmt):
        print()
        print('-' * 100)
        print(f'{title}（{len(rows)}）')
        print('-' * 100)
        for row in rows:
            print('  ' + fmt(row))

    dump('🔴 A 未捕获异常 —— 非法参数把接口打成 500',
         crashes,
         lambda r: f'{r[0]}  [{r[1]}]  参数 {r[2]!r} → '
                   + ', '.join(f'{v!r}={s}' for v, s in r[3]))

    dump('🔴 B 可见范围被参数放宽 —— 出现了基线里没有的 id',
         widened,
         lambda r: f'{r[0]}  [{r[1]}]  {r[2]}={r[3]!r} → '
                   f'多出 {r[5]} 条 {r[4]}')

    dump(f'🔴 C 数量参数没有上限 —— 传 {LARGE_COUNT} 吐回了超量条目',
         unbounded,
         lambda r: f'{r[0]}  [{r[1]}]  {r[2]}={LARGE_COUNT} → '
                   f'HTTP {r[3]}，返回 {r[4]} 条')

    dump('✅ 非法值被正常拒绝 / 忽略（逐「接口×角色×参数」）',
         tolerated,
         lambda r: f'{r[0]}  [{r[1]}]  {r[2]}')

    dump('⚪ 跳过', skipped, lambda r: f'{r[0]}  [{r[1]}]  {r[2]}')

    print()
    print('=' * 100)
    print(f'合计：未捕获异常 {len(crashes)}｜可见范围放宽 {len(widened)}｜'
          f'数量无上限 {len(unbounded)}｜正常拒绝/忽略 {len(tolerated)}｜'
          f'跳过 {len(skipped)}')
    print('=' * 100)
    return 1 if (crashes or widened or unbounded) else 0


if __name__ == '__main__':
    sys.exit(main())
