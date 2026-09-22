"""
Django settings for tnr_system project.
TNR (Trap-Neuter-Return) 流浪动物管理系统
"""
import secrets
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
import os

# 加载 .env 文件
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default=False):
    """环境变量取布尔值。未设置时用 default。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('true', '1', 'yes', 'on')


def _env_list(name, default=()):
    """环境变量取逗号分隔列表。未设置或全为空白时用 default。"""
    raw = os.environ.get(name, '')
    return [v.strip() for v in raw.split(',') if v.strip()] or list(default)


def _local_ips():
    """收集本机**所有**非回环 IPv4 地址（用于局域网联调）。

    局域网内其他设备访问的是本机**真实 IP**（如 192.168.x.x），而把
    ``0.0.0.0`` 写进 ALLOWED_HOSTS 是无效的——0.0.0.0 是**监听**地址，
    永远不会作为 HTTP Host 头出现，于是 Django 一律返回 400。

    必须枚举**全部网卡**而不能只取默认路由出口：多网卡机器（Wi-Fi +
    有线 + VPN/虚拟网卡）上，同事设备可能走的是另一个网段，只放通出口
    IP 会漏掉真实访问来源。

    DEBUG 模式下自动附加这些 IP，换 Wi-Fi / IP 变了无需改 .env。
    生产环境（DEBUG=False）不启用，仍要求显式配置，避免 Host 头放开。
    """
    import re
    import socket
    import subprocess

    ips = set()

    def _add(addr):
        addr = (addr or '').strip()
        # 只要 IPv4，排除回环/链路本地/未指定地址
        if not addr or addr.startswith(('127.', '169.254.', '0.')):
            return
        if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', addr):
            ips.add(addr)

    # 1) 主机名解析
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except OSError:
        pass

    # 2) 默认路由出口（SOCK_DGRAM + connect 不真实发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        _add(s.getsockname()[0])
        s.close()
    except OSError:
        pass

    # 3) 枚举全部网卡：优先 macOS 的 ifconfig，回退 Linux 的 ip
    for cmd in (['ifconfig'], ['ip', '-4', 'addr']):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        # ifconfig: "inet 192.168.0.37 netmask ..."；ip: "inet 192.168.0.37/24 ..."
        for m in re.finditer(r'\binet\s+(\d{1,3}(?:\.\d{1,3}){3})', out):
            _add(m.group(1))
        if ips:
            break

    return sorted(ips)


# ============================================
# 基础安全配置
# ============================================
# DEBUG 默认关闭。生产环境忘记配置时必须退化成「安全但不好用」，
# 而不是把完整堆栈、settings、SQL 全部暴露给访问者。
DEBUG = _env_bool('DEBUG', False)

# SECRET_KEY 没有兜底默认值。早期版本这里内置了一把公开的固定密钥，
# 一旦 .env 缺失（手动 gunicorn、supervisor 只起了 gunicorn 等），
# 所有实例就会共用同一把人人可查的密钥——会话 Cookie 可被伪造。
SECRET_KEY = os.environ.get('SECRET_KEY', '').strip()
if not SECRET_KEY:
    if DEBUG:
        # 开发环境图省事：随机生成临时密钥（重启进程即失效，会要求重新登录）
        SECRET_KEY = 'django-insecure-dev-%s' % secrets.token_urlsafe(32)
    else:
        raise ImproperlyConfigured(
            '未配置 SECRET_KEY。生产环境必须显式设置，否则会话 Cookie 可被伪造。\n'
            '生成方式：python -c "import secrets; print(secrets.token_urlsafe(50))"'
        )

# ALLOWED_HOSTS 同样不兜底成 '*'：Host 头伪造可被用于
# 密码重置链接投毒、缓存污染等。
ALLOWED_HOSTS = _env_list('ALLOWED_HOSTS')
if not ALLOWED_HOSTS:
    if DEBUG:
        ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]', '0.0.0.0']
    else:
        raise ImproperlyConfigured(
            '未配置 ALLOWED_HOSTS。生产环境必须显式列出可访问的域名/IP，'
            '例如 ALLOWED_HOSTS=example.com,127.0.0.1'
        )

# 开发模式自动放通本机局域网 IP：ALLIED_HOSTS 里写 0.0.0.0 并不能让
# 192.168.x.x 通过校验（Host 头是真实 IP），局域网联调会一律 400。
# 仅 DEBUG 生效；生产环境仍按 .env 显式配置，不放宽。
if DEBUG:
    ALLOWED_HOSTS = list(dict.fromkeys(ALLOWED_HOSTS + _local_ips()))


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'django_q',
    'accounts',
    'core',
    'business',
    'supervision',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'accounts.middleware.DistrictScopeMiddleware',
    'core.middleware.AuditLogMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'tnr_system.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'tnr_system.wsgi.application'


# ============================================
# 反向代理 / HTTPS 安全
# ============================================
# nginx 反代时必须显式告知 Django 原始协议，否则 request.is_secure() 恒为 False，
# 下游所有 HTTPS 判断（安全 Cookie、SSL 跳转）都会失真。
# gunicorn 只监听 127.0.0.1:8000，外部无法直连，因此可以信任该头。
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# 是否已启用 HTTPS。deploy.sh 生成 .env 时默认 off，
# 执行完 certbot 申请证书后改为 HTTPS=on 并重启应用即可启用下面这批加固项。
HTTPS_ENABLED = _env_bool('HTTPS', False)

if not DEBUG and HTTPS_ENABLED:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = _env_bool('SECURE_SSL_REDIRECT', True)
    SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', 31536000))

# 这些项与是否 HTTPS 无关，任何环境都应保持：
# Session Cookie 禁止 JS 读取；SameSite=Lax 是当前 CSRF 防护的兜底
# ——业务接口普遍 @csrf_exempt，跨站 POST 不携带 Cookie 全靠它。
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_HTTPONLY = False  # 前端需要读取 csrftoken 放进 X-CSRFToken 头
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True

# 站点通过域名（或 https）访问时，Django 会校验 Origin/Referer；
# 未列入的域名提交表单会 403。多个用逗号分隔。
CSRF_TRUSTED_ORIGINS = _env_list('CSRF_TRUSTED_ORIGINS')

# CSRF 失败时 `/api/` 必须回**可读 JSON**，不能是 Django 的 HTML 403 页。
# ⚠ 只能在这里配 —— `CsrfViewMiddleware` 是**直接返回** `HttpResponseForbidden`
#   （不抛异常），所以 `handler400` / `handler403` 都接不住它。
# 详见 `tnr_system/urls.py::api_aware_csrf_failure` 的说明（含「何时会真的触发」）。
CSRF_FAILURE_VIEW = 'tnr_system.urls.api_aware_csrf_failure'

# 生产环境把请求日志落到 stderr，由 supervisor 收进 /var/log/tnr/*.log
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    'root': {'handlers': ['console'], 'level': LOG_LEVEL},
    'loggers': {
        # 4xx/5xx 单独提级，便于从日志里捞出异常请求
        'django.request': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}


# Database
# 通过环境变量 DB_ENGINE 选择 (sqlite3 / mysql)
_db_engine = os.environ.get('DB_ENGINE', 'sqlite3')
if _db_engine == 'mysql':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': os.environ.get('DB_NAME', 'tnr_system'),
            'USER': os.environ.get('DB_USER', 'root'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
            'PORT': os.environ.get('DB_PORT', '3306'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (用户上传)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# ---------------------------------------------------------------------------
# 请求体闸门：默认值**低于本产品自己声明的上限**（第三十一轮）
# ---------------------------------------------------------------------------
# 线上有三道互不相干的闸门会拒绝同一个请求，而**三道的默认值都卡在产品
# 自己声明的批量上限附近**（`business.services.MAX_CAPTURE_BATCH = 100`）：
#
#   | 闸门                                  | 默认 | 触发后的响应        |
#   |---------------------------------------|------|---------------------|
#   | nginx  `client_max_body_size`         | 1M   | 413 **text/html**   |
#   | Django `DATA_UPLOAD_MAX_NUMBER_FILES` | 100  | 400 **text/html**   |
#   | Django `DATA_UPLOAD_MAX_NUMBER_FIELDS`| 1000 | 400 **text/html**   |
#
# 三者返回的都是 **HTML**，而前端 `TNR_API._postForm` 内部是 `await res.json()`
# —— 拿到 HTML 就抛错 → **静默中断**，用户看到的是「点提交没反应」。
# 与 §2.26 的 `RequestDataTooBig` 是**同一族缺陷**，只是换了一道闸门；
# `tnr_system/urls.py` 的 `handler400` 负责把 Django 侧那两道变成可读 JSON。
#
# 捕捉单在**上限**（100 只）时的实际形态（逐字段数出来的）：
#   文件：100 张单只照片 + 1 张整体合影 = **101 个** → 撞上默认的 100（off-by-one）
#   字段：13 个固定字段 + `pet_codes`×100 + 每只 4 个属性×100 = **513 个**
#
# ⚠ 这里只调 Django 侧。nginx 的 `client_max_body_size` 是**基础设施姿态**，
#   不在本文件控制范围内，见 `DEPLOY.md` §5.7（20M 约容纳 ~50 张压缩图；
#   要跑满 100 只需调到 64M）。两侧不一致时，前端会先撞 nginx 拿到 HTML 413。
DATA_UPLOAD_MAX_NUMBER_FILES = 110    # ≥ MAX_CAPTURE_BATCH(100) + 整体合影 + 余量
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000  # ≥ 上限所需的 513，留一倍余量

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# 自定义用户模型
AUTH_USER_MODEL = 'accounts.User'

# 登录重定向
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'

# Django REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

# django-q2 队列配置
Q_CLUSTER = {
    'name': 'tnr',
    'workers': 2,
    'recycle': 500,
    'timeout': 60,
    'retry': 120,
    'queue_limit': 500,
    'bulk': 1,
    'orm': 'default',
}
