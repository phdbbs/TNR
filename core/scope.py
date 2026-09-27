"""区县可见范围的**唯一口径**。

为什么要有这个文件
--------------------
「谁能看到全部区县」这个判断，历史上在**三处各写了一遍**：

- `accounts.middleware.DistrictScopeMiddleware`（设置 `request.user_district_scope`）
- `business.services.get_district_scope`（读中间件的值）
- `business.services.get_district_filtered_queryset`（自己再判一遍）

而其中两处对「**账号没有挂区县**」给出了**相反**的解释：

- 中间件 → `user.district_id`（`None`）→ `get_district_scope` 返回 `None`
  → 下游 `scope is not None` 为假 → 解释为「**可见全部**」；
- `get_district_filtered_queryset` → `district_id` 为空 → `objects.none()`
  → 解释为「**什么都看不到**」。

于是同一账号会同时处于两种状态：**列表是空的，写接口却全部放行**。
这是本项目最难查的一类缺陷 —— 没有报错、没有 500，只是「看不到」和「能改」
同时成立（第四十二轮引入平台管理端角色时暴露，但坑本身早就在）。

放在 `core` 而不是 `business`
------------------------------
`accounts` 依赖 `core`（`User.district` 指向 `core.District`），
而 `business` 依赖 `accounts`。中间件在 `accounts` 里，若把口径放在
`business.services` 就会形成 `accounts → business → accounts` 的循环导入。
这里只用 `getattr` 读 user 的属性，不需要 import 任何上层 app。
"""
from django.conf import settings

# 「可见全部区县」的角色。⚠ 改这里必须同步确认：
#   1) 这些值都存在于 `accounts.User.ROLE_CHOICES`（有测试锁，见 core/tests.py）
#   2) 这些角色**不需要**挂区县 —— 挂了也只是冗余，`is_city` 分支同样成立
GLOBAL_SCOPE_ROLES = ('platform_admin', 'gov_city')


def has_global_district_scope(user):
    """该用户是否可见**全部**区县的数据。

    判定顺序与历史实现保持一致（角色 → 所属区县是否市级），只是把它收敛成
    一处，三处调用点都来问这里。

    :param user: 已登录的 User 对象（可为 None / AnonymousUser）
    :return: True = 全局可见；False = 按 `user.district_id` 收敛
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'role', None) in GLOBAL_SCOPE_ROLES:
        return True
    # 所属区县本身就是「全市（市级）」的账号（如挂在市级的捕捉点操作员）
    district = getattr(user, 'district', None)
    return bool(district and getattr(district, 'is_city', False))


# 「非全局角色 + 没挂区县」的范围值。
#
# ⚠ 不能复用 `None`：`None` 在现有调用点的语义是「可见全部」，于是这类账号会
#   既「列表全空」又「写接口全放行」。哨兵必须满足三点：
#     ① 不等于任何真实区县 id（自增主键从 1 起）→ `filter(district_id=...)` 恒为空；
#     ② **真值**（不能用 `0`）→ 否则 `if scope:` 这类判断会把它当「无范围」放行；
#     ③ 显式可判别 → 需要区分「全局 / 空 / 某区县」的地方用 `is_empty_scope()`。
# ⚠ 别把它写进 `district_id` 外键字段：`bulk_create` 时会撞外键约束。
EMPTY_DISTRICT_SCOPE = -1


def is_empty_scope(scope):
    """该范围值是否表示「什么都看不到」（区别于 `None` = 可见全部）。"""
    return scope == EMPTY_DISTRICT_SCOPE


def resolve_user_district_scope(user):
    """算出用户的区县范围 —— 三个调用点（中间件 / 两个 services 函数）都走这里。

    :return: `None` = 可见全部；`EMPTY_DISTRICT_SCOPE` = 什么都看不到；
             否则 = 具体区县 id
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return EMPTY_DISTRICT_SCOPE
    if has_global_district_scope(user):
        return None
    district_id = getattr(user, 'district_id', None)
    return district_id if district_id else EMPTY_DISTRICT_SCOPE
