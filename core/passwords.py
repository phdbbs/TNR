"""登录口令强度判据的唯一定义。

建号、管理员重置、自助改密都过这一份：任何一处另写一份，最松的那条
就是实际生效的口径（历史上建号与自助改密就曾各写一份）。

长度单独判一次，是为了给出与前端一致的「至少 8 位」提示；
真正的内容判据仍交给 Django 的 `AUTH_PASSWORD_VALIDATORS`。
"""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

#: 与 `settings.AUTH_PASSWORD_VALIDATORS` 的 MinimumLengthValidator 默认值一致。
MIN_PASSWORD_LENGTH = 8

#: 演示账号的统一初始口令。**只允许 seed / 自动建号直接写库**，
#: 不经过 `password_policy_error()` —— 它只有 6 位，按强度口径必然被拒。
#: 手工建号、管理员重置、自助改密仍然必须 ≥8 位；不要为了让演示口令
#: 通过而放宽这里的校验器，否则等于给所有账号降低门槛。
DEMO_INITIAL_PASSWORD = '111111'


def password_policy_error(password, user=None, *, empty='请设置密码'):
    """返回可读的强度错误；通过时返回 None。

    :param user: 相似度校验的基准账号。建号时传未落库的探针 User，
        重置/改密时传目标账号 —— 不传会放过「口令=用户名」。
    """
    if not password:
        return empty
    if len(password) < MIN_PASSWORD_LENGTH:
        return f'密码长度不能少于{MIN_PASSWORD_LENGTH}位'
    try:
        validate_password(password, user)
    except ValidationError as exc:
        return '；'.join(exc.messages)
    return None
