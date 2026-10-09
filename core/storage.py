"""媒体文件存储抽象（对象存储就绪）。

## 一句话目标

图片**一律经 Django Storage API 读写**，业务代码里不出现任何本地路径假设。
迁移到对象存储 = 改几个环境变量 + 跑一条 ``manage.py media_migrate``，
**不改业务代码、不改数据库、不改前端**。

## 为什么 object key 必须与 DB 里的 ``name`` 逐字一致

``ImageField`` 在数据库里存的是**相对路径字符串**（如 ``photos/abc.jpg``）。
对象存储的 object key 若与它逐字相同，迁移就退化成**纯文件搬运**：
DB 一行都不用改，回滚也只是把文件搬回去。

于是本项目有一条硬约束：

    **禁止修改 `upload_to`。**

改了 ``upload_to``（比如加日期分目录）会让**新文件**的 key 形态与历史文件不同，
「一次批量命令完成迁移」就不再成立 —— 迁移脚本得同时懂两套命名规则，
而新老不一致正是迁移事故最常见的源头。宁可 ``photos/`` 下文件多，
也不要命名漂移。

同理，``TNR_MEDIA_LOCATION``（桶内前缀）**一旦设定就不要改**：它会让 object key
从 ``photos/a.jpg`` 变成 ``prefix/photos/a.jpg``，改了就等于所有历史文件全部失联。

## 依赖

对象存储后端用 ``django-storages`` 的 S3 兼容实现（COS / OSS / MinIO / 七牛
都提供 S3 兼容端点）。它是**可选依赖**：

    pip install "django-storages[s3]"

不装也能跑 —— 此时只能用本地存储（``TNR_MEDIA_STORAGE=local``，默认）。
配了对象存储却没装依赖时，启动即给出可读报错，而不是等到上传时 500。

## 环境变量契约

============================  ==========  ============================================
变量                          默认        说明
============================  ==========  ============================================
``TNR_MEDIA_STORAGE``         ``local``   ``local`` 或 S3 兼容后端名（``cos``/``oss``/
                                          ``s3``/``minio`` …）。写别的值即自定义。
``TNR_MEDIA_BUCKET``          ——          桶名（对象存储必填）
``TNR_MEDIA_REGION``          空          区域，如 ``ap-guangzhou``
``TNR_MEDIA_ENDPOINT``        空          自定义端点。COS 形如
                                          ``https://cos.ap-guangzhou.myqcloud.com``
``TNR_MEDIA_ACCESS_KEY_ID``   ——          密钥 ID（必填）
``TNR_MEDIA_SECRET_ACCESS_KEY`` ——        密钥（必填）
``TNR_MEDIA_CUSTOM_DOMAIN``   空          CDN / 自定义域名。设了则 ``.url`` 直接返回它
``TNR_MEDIA_LOCATION``        空          桶内前缀。**设了就别改**
``TNR_MEDIA_QUERYSTRING_AUTH`` ``false``  私有桶签名 URL。``true`` 时 URL 带时效签名
============================  ==========  ============================================

## 本模块的导入约束

**不得 import ``django.conf.settings``，也不得 import 任何项目内模块。**

原因：``settings.py`` 需要在本模块顶层 import 它（见 ``build_storages()``）。
一旦这里反向 import settings，就是循环导入，Django 启动阶段直接崩。
本模块只依赖 ``django.core.files.storage`` 与标准库。
"""

from __future__ import annotations

import os

from django.core.files.storage import FileSystemStorage

# ---------------------------------------------------------------------------
# 常量：后端名与环境变量名
# ---------------------------------------------------------------------------

#: 本地磁盘存储（默认）。行为与 Django 内置 ``FileSystemStorage`` 完全一致。
LOCAL_BACKEND = 'local'

#: 走 S3 兼容协议的对象存储。
#: 腾讯云 COS / 阿里云 OSS / MinIO / 华为 OBS / 七牛 都提供 S3 兼容端点，
#: 因此共用同一套实现（``storages.backends.s3.S3Storage``）。
#: 写成别的名字（如 ``mycorp-oss``）也走同一条路 —— 这个集合只用于**报错提示**，
#: 不用于「是否允许」的判断（否则每接一家新厂商都要改代码）。
S3_COMPATIBLE_BACKENDS = frozenset({
    's3', 'cos', 'oss', 'minio', 'obs', 'qiniu', 's3-compatible',
})

ENV_BACKEND = 'TNR_MEDIA_STORAGE'
ENV_BUCKET = 'TNR_MEDIA_BUCKET'
ENV_REGION = 'TNR_MEDIA_REGION'
ENV_ENDPOINT = 'TNR_MEDIA_ENDPOINT'
ENV_ACCESS_KEY = 'TNR_MEDIA_ACCESS_KEY_ID'
ENV_SECRET_KEY = 'TNR_MEDIA_SECRET_ACCESS_KEY'
ENV_CUSTOM_DOMAIN = 'TNR_MEDIA_CUSTOM_DOMAIN'
ENV_LOCATION = 'TNR_MEDIA_LOCATION'
ENV_QUERYSTRING_AUTH = 'TNR_MEDIA_QUERYSTRING_AUTH'

#: 对象存储后端的 dotted path。
OBJECT_BACKEND_PATH = 'storages.backends.s3.S3Storage'

#: 静态文件后端。**必须在 STORAGES 里显式写出**：
#: Django 5.0 起 ``STORAGES`` 是**整块替换**而非逐键合并，
#: 只写 ``default`` 会让 ``staticfiles`` 丢失，``collectstatic`` 直接报错。
STATICFILES_BACKEND_PATH = 'django.contrib.staticfiles.storage.StaticFilesStorage'


# ---------------------------------------------------------------------------
# 环境变量读取
# ---------------------------------------------------------------------------

def _env(name, default=''):
    """取环境变量并 strip。未设置或全空白时返回 default。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('true', '1', 'yes', 'on')


# ---------------------------------------------------------------------------
# 后端判定
# ---------------------------------------------------------------------------

def media_backend():
    """当前媒体后端名（小写）。未配置时为 ``local``。"""
    return _env(ENV_BACKEND, LOCAL_BACKEND).lower()


def is_local_media():
    """当前是否使用本地磁盘存储。

    供 ``urls.py`` 判断要不要挂 ``/media/`` 静态路由用 ——
    切到对象存储后**必须不再挂**，否则本地残留的旧文件仍能通过 ``/media/`` 访问，
    会掩盖「迁移没搬干净」这个事实（页面看起来一切正常，实际有一半图走的是
    对象存储、一半走的是本地残留）。
    """
    return media_backend() == LOCAL_BACKEND


def is_object_media():
    return not is_local_media()


# ---------------------------------------------------------------------------
# 对象存储配置组装
# ---------------------------------------------------------------------------

def object_storage_missing_env():
    """返回缺失的必填环境变量名列表（空列表表示齐全）。"""
    missing = []
    if not _env(ENV_BUCKET):
        missing.append(ENV_BUCKET)
    if not _env(ENV_ACCESS_KEY):
        missing.append(ENV_ACCESS_KEY)
    if not _env(ENV_SECRET_KEY):
        missing.append(ENV_SECRET_KEY)
    return missing


def object_storage_dependency_error():
    """对象存储依赖是否齐全。返回错误说明字符串，``None`` 表示齐全。

    在 ``manage.py check`` 里被调用：配了对象存储却没装包时**启动即报错**，
    而不是等到用户上传第一张图才 500。
    """
    try:
        import storages  # noqa: F401
    except ImportError:
        return ('媒体后端配成了对象存储（%s=%s），但未安装 django-storages。'
                '请执行：pip install "django-storages[s3]"'
                % (ENV_BACKEND, media_backend()))
    try:
        import boto3  # noqa: F401
    except ImportError:
        return ('媒体后端配成了对象存储（%s=%s），但未安装 boto3。'
                '请执行：pip install "django-storages[s3]"'
                % (ENV_BACKEND, media_backend()))
    return None


def object_storage_options():
    """组装 ``S3Storage`` 的 OPTIONS 字典。

    ⚠ ``file_overwrite`` 一律为 ``False``：上传同名文件时 Django 会自动改名
    （``abc_AbC123.jpg``）而不是覆盖。这是**故意**的 —— 两个不同用户上传
    同名 ``IMG_0001.jpg`` 时互相覆盖，是静默的数据丢失。
    迁移命令**不走** ``save()``，因此不受这条限制（见 ``media_migrate``）。
    """
    opts = {
        'bucket_name': _env(ENV_BUCKET),
        'access_key': _env(ENV_ACCESS_KEY),
        'secret_key': _env(ENV_SECRET_KEY),
        'file_overwrite': False,
        # 私有桶（默认）：URL 带时效签名。公开读桶设 TNR_MEDIA_QUERYSTRING_AUTH=false
        # 即可返回裸 URL（可被 CDN 缓存）。
        'querystring_auth': _env_bool(ENV_QUERYSTRING_AUTH, False),
    }

    region = _env(ENV_REGION)
    if region:
        opts['region_name'] = region

    endpoint = _env(ENV_ENDPOINT)
    if endpoint:
        opts['endpoint_url'] = endpoint

    domain = _env(ENV_CUSTOM_DOMAIN)
    if domain:
        # 允许写 `cdn.example.com` 或完整 `https://cdn.example.com`
        if not domain.startswith(('http://', 'https://')):
            domain = 'https://' + domain
        opts['custom_domain'] = domain

    location = _env(ENV_LOCATION)
    if location:
        opts['location'] = location.strip('/')

    return opts


# ---------------------------------------------------------------------------
# STORAGES 组装
# ---------------------------------------------------------------------------

def build_storages():
    """返回给 ``settings.STORAGES`` 用的字典。

    ``settings.py`` 里只有一行 ``STORAGES = build_storages()`` ——
    切换后端的所有逻辑都收在本模块，配置中心保持干净。

    ⚠ 必须同时给出 ``default`` 与 ``staticfiles`` 两个键：
    Django 5.0 的 ``STORAGES`` 是整块替换，漏掉 ``staticfiles`` 会让
    ``collectstatic`` 报 ``InvalidStorageError``。
    """
    storages = {
        'staticfiles': {'BACKEND': STATICFILES_BACKEND_PATH},
    }

    if is_local_media():
        # 本地后端不传 location/base_url，由 FileSystemStorage 自行从
        # settings.MEDIA_ROOT / settings.MEDIA_URL 取值 —— 与改造前完全一致。
        storages['default'] = {'BACKEND': 'core.storage.LocalMediaStorage'}
    else:
        storages['default'] = {
            'BACKEND': OBJECT_BACKEND_PATH,
            'OPTIONS': object_storage_options(),
        }

    return storages


def media_storage_snapshot():
    """当前媒体存储的可读快照，供 ``manage.py media_migrate --status`` 与巡检使用。

    **不返回密钥**：只给出是否已配置（布尔）。
    """
    backend = media_backend()
    snap = {
        'backend': backend,
        'kind': 'local' if is_local_media() else 'object',
        'backend_path': ('core.storage.LocalMediaStorage' if is_local_media()
                         else OBJECT_BACKEND_PATH),
    }
    if is_object_media():
        snap.update({
            'bucket': _env(ENV_BUCKET),
            'region': _env(ENV_REGION) or '(默认)',
            'endpoint': _env(ENV_ENDPOINT) or '(厂商默认)',
            'location': _env(ENV_LOCATION) or '(桶根)',
            'custom_domain': _env(ENV_CUSTOM_DOMAIN) or '(未设，用端点默认域名)',
            'querystring_auth': _env_bool(ENV_QUERYSTRING_AUTH, False),
            'credentials_configured': not object_storage_missing_env(),
            'missing_env': object_storage_missing_env(),
        })
    return snap


# ---------------------------------------------------------------------------
# 本地存储后端
# ---------------------------------------------------------------------------

class LocalMediaStorage(FileSystemStorage):
    """本地磁盘存储（默认）。

    行为与 Django 内置的 ``FileSystemStorage`` **完全一致**，显式声明出来是为了：

    1. 让 ``STORAGES`` 一眼看出当前用的是本地还是对象存储；
    2. 给 ``is_local_media()`` 一个可判定的落点；
    3. 将来要加本地特有行为（如上传即压缩）时有明确的位置。

    ⚠ 不要在这里 override ``save()`` 去做「重命名 / 加日期目录」之类的事 ——
    那会破坏 object key 与 DB ``name`` 的一致性，是迁移事故的头号来源。
    """
