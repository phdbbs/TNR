"""`business` 应用的初始化。

⚠ 本模块的 `ready()` **不允许访问数据库**。Django 在 app 初始化阶段
（`apps.populate()` 完成之前）执行查询会抛

    RuntimeWarning: Accessing the database during app initialization

而且此时表未必存在（`migrate` 正在跑），只能把异常吞掉 —— 真实错误就此
永久消失。补偿逻辑因此改挂到 `request_started`（见下）。
"""
import logging
import threading

from django.apps import AppConfig
from django.core.signals import request_started

logger = logging.getLogger(__name__)

# 补偿只在**每个进程**跑一次。用锁而不是裸布尔：`request_started` 可能被
# 多个线程并发触发（同步 worker 池），裸布尔会出现「两个线程同时看到
# False、于是都执行」的竞态 —— 补偿本身幂等，但会白跑一遍并放大日志噪音。
_compensation_lock = threading.Lock()
_compensation_done = False

# 连接时用固定 dispatch_uid，避免 `ready()` 被重复调用时挂上多个接收器
# （测试里 `apps.get_app_config()` 可能被反复触发）。
_COMPENSATION_UID = 'business.startup_compensation'


def run_startup_compensation(sender=None, **kwargs):
    """补偿「诊疗完成满 5 天但状态仍是 `in_treatment`」的宠物。

    挂在 `request_started` 上而不是 `ready()` 里，理由有三：

    1. **数据库此时必然可用** —— 能收到请求就说明 app 已就绪、表已建好，
       不会在 `migrate` 期间炸。
    2. **不再产生 `RuntimeWarning`**，日志里不再有每进程一条的噪音。
    3. **失败可见** —— 补偿失败意味着「动物该上架却没上架」，而界面上
       完全看不出来；必须留下日志，不能 `except: pass`。

    只跑一次：收到首个请求后立即断开信号，后续请求连函数都不会进。
    """
    global _compensation_done
    with _compensation_lock:
        if _compensation_done:
            return
        _compensation_done = True
    # 先断开再执行：即使执行抛错也不会每个请求重试一次（重试由重启或
    # 定时任务负责），避免把异常刷满日志。
    request_started.disconnect(dispatch_uid=_COMPENSATION_UID)

    try:
        from business.tasks import auto_promote_to_adoptable
        auto_promote_to_adoptable()
    except Exception:
        # 不能静默：这是「超期未上架」的兜底路径，吞掉就等于把问题藏起来。
        logger.exception('启动补偿「超期未转待领养」失败（不影响本次请求）')


class BusinessConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'business'

    def ready(self):
        """把补偿挂到首个请求上（**不在这里查库**）。

        原先这里直接调用 `auto_promote_to_adoptable()`，导致每个进程启动
        都写库：`manage.py` 的任意命令都会打印
        `RuntimeWarning: Accessing the database during app initialization`，
        且 `migrate` 期间表还不存在时异常被 `except Exception: pass` 吞掉。
        原来那句 `if os.environ.get('RUN_MAIN') != 'true' and ...: pass`
        是**空分支**，条件怎么写都不产生任何效果。
        """
        request_started.connect(
            run_startup_compensation,
            dispatch_uid=_COMPENSATION_UID,
        )
