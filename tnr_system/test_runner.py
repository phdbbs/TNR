"""测试专用 TestRunner：把 ``MEDIA_ROOT`` 隔离到临时目录。

## 为什么需要它

跑测试时会**真的写文件**（上传接口保存图片、`make_image_file` 造夹具）。
如果 ``MEDIA_ROOT`` 仍指向项目下的 ``media/``，每跑一次测试就往真实媒体目录里
丢几个文件，而且从不清理。

实测（改造前）：跑一遍 ``manage.py test core``，``media/photos/`` 里就多出
10 个 ``test-pet-0_*.png`` / ``test-group_*.png``；本地该目录当时已堆积
**两万多个**文件，而数据库只引用其中几十个。

这件事的危害不只是"脏"：

1. **迁移会把垃圾一起搬走。** 切对象存储时 ``media_migrate --include-orphans``
   会把这两万多个测试残留全部上传，白付存储与流量费；不带 ``--include-orphans``
   则要在对象存储里留一堆永远没人访问的 key。
2. **它掩盖真实问题。** 本地 ``/media/`` 总能访问到图片（哪怕数据库引用早已
   失效），于是「图片存不存在」这件事在开发环境里永远看不出异常。
3. **它让测试互相污染。** 前一个用例写进真实目录的文件，可能被后一个用例的
   ``exists()`` 判据误认为"已存在"。

## 为什么放在 runner 层而不是每个测试里

``business/tests/base.py`` 里的 API 助手会在**任何**调用它的测试里保存图片。
逐个测试去加 ``override_settings(MEDIA_ROOT=...)`` 是治标的：新增测试忘了加
就重新开始污染，而且没有任何东西会提醒你。

放在 runner 层是**一处修复、全部生效**，包括以后新写的测试。

⚠ 个别测试仍可用 ``@override_settings(MEDIA_ROOT=...)`` 指定自己的目录 ——
TestCase 级的覆盖优先级更高，两者不冲突。
"""

from __future__ import annotations

import shutil
import tempfile

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class MediaIsolatedTestRunner(DiscoverRunner):
    """在整场测试会话期间把 ``MEDIA_ROOT`` 指向一个临时目录。"""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)

        self._media_root = tempfile.mkdtemp(prefix='tnr-test-media-')
        # ⚠ 用 override_settings 而不是直接改 settings：
        #   它会发 `setting_changed` 信号，`FileSystemStorage` 收到后清掉自己
        #   缓存的 `location` —— 否则已经实例化的 storage 仍指向旧的 media/。
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()

    def teardown_test_environment(self, **kwargs):
        try:
            self._media_override.disable()
        finally:
            shutil.rmtree(getattr(self, '_media_root', ''), ignore_errors=True)
        super().teardown_test_environment(**kwargs)
