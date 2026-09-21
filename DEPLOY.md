# TNR 系统部署文档

## 〇、⚠ 已有生产服务器（`100.99.98.71`）——**不要在这台上跑 `deploy.sh`**

该服务器 **2026-09-21** 已部署，架构与 `deploy.sh` 的假设**不一致**：

| 项 | 实况 |
|---|---|
| 项目目录 | `/opt/tnr`（属主 `ubuntu:ubuntu`） |
| 应用服务器 | gunicorn 6 worker + master，**全部以 `ubuntu` 运行**（**不是 `www-data`**） |
| 进程管理 | supervisor：`tnr-gunicorn` + `tnr-qworker`（`user=ubuntu`） |
| 反向代理 | nginx 1.24.0；`/media/` → `alias /opt/tnr/media/`，`/static/` 同理 |
| 数据库 | **1Panel Docker 化的 MariaDB**（容器 `1Panel-mariadb-LQ69`），3306 由 docker-proxy 暴露 |
| venv | `/opt/tnr/venv`（python3.12） |
| 入口 | `http://100.99.98.71/`（**HTTP**，HTTPS 未配） |

**为什么不能跑 `deploy.sh`**：它会 `apt install mysql-server`（与现成的 Docker MariaDB 冲突）、
**重写 `.env`**（丢掉现有 `SECRET_KEY`/`DB_PASSWORD`）、`chown root:www-data`
（而进程实际以 `ubuntu` 跑）。三者都会破坏现状。

**该服务器的更新方式**（手工分阶段，已验证可复用）：

```bash
# 0. 备份（服务器上执行；容器里没有 mysql 客户端，要用 mariadb-dump）
mkdir -p /root/tnr-backup-$(date +%Y%m%d-%H%M%S)
#   mariadb-dump tnr_system > .../db-tnr_system.sql
#   tar czf .../opt-tnr-code.tar.gz -C / opt/tnr      # ⚠ -C / 之后路径不再带 /opt
#   cp /opt/tnr/.env /etc/nginx/... /etc/supervisor/... 一并备份

# 1. 发布包（本地）——只含已提交内容，天然干净
git archive --format=tar.gz --prefix=tnr/ HEAD -o /tmp/tnr-release.tar.gz

# 2. 服务器上解包覆盖 → 3. 校准 .env（保留 SECRET_KEY/DB_PASSWORD，只补 TNR_AMAP_KEY）
# 4. migrate + seed_data + ensure_superuser → 5. mkdir media + collectstatic
# 6. supervisorctl restart tnr-gunicorn tnr-qworker
```

> 属主判据按**进程实际身份**定：先 `ps -o user= -C gunicorn` 确认，再决定 `media/` 归谁。
> 这台机器上「`www-data` 不可写 `media/`」是**正常**的。

## 一、新服务器部署步骤（逐项执行）

> **推荐**：全新服务器用 `sudo bash deploy.sh` 一键部署（含 Nginx + Gunicorn + MySQL + Supervisor）。
> 下面是手工部署步骤，适用于已有环境或自定义架构。
> ⚠ **已有环境的机器先看上面第〇节** —— `deploy.sh` 会重写 `.env` 与目录属主。

```bash
# 1. 拉取代码
git pull origin main        # 或 git clone https://cnb.cool/tnrxy/TNR.git

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量（关键！.env 不随 git 分发，必须手动创建/更新）
cp .env.example .env
# 编辑 .env，至少填写：
#   TNR_AMAP_KEY=高德"Web服务"类型Key（逆地理编码必需）
#   SECRET_KEY=随机长字符串    <-- 注意变量名没有 DJANGO_ 前缀
#   DEBUG=False
#   ALLOWED_HOSTS=服务器IP或域名
# 生成随机密钥：python -c "import secrets; print(secrets.token_urlsafe(50))"
vim .env

# 4. 数据库迁移（经纬度/地址字段在 0006 迁移中，未执行会报"no such column"）
python manage.py migrate

# 5. 收集静态文件 + 确保超级管理员可用
python manage.py collectstatic --noinput
python manage.py ensure_superuser          # 已有启用的超管则跳过，不会覆盖其口令
# 需指定口令：ADMIN_PASSWORD=你的口令 python manage.py ensure_superuser --reset-password

# 6. 启动前自检（生产配置缺失会在这里就报出来，而不是等到线上出问题）
python manage.py check --deploy

# 6.1 数据一致性巡检（只读，不改库；有违规时退出码为 1）
python manage.py check_data_integrity
# 检查「界面上看不出来、但会让某个角色看到错误数据」的隐性问题：
# 账号区县 ≠ 所属机构区县、业务记录区县 ≠ 归属对象区县、捕捉单作废但宠物未作废。
# 有输出时逐条人工确认后再处理，命令本身不会自动修复。

# 7. 按你的部署方式重启（gunicorn/supervisor/systemd 或 runserver）
```

### 关于 `DEBUG=False` 下的「启动即失败」

生产模式下（`DEBUG=False`）以下两项**缺失会导致应用直接启动失败**，这是有意设计：

| 缺失项 | 报错 |
|---|---|
| `SECRET_KEY` | `ImproperlyConfigured: 未配置 SECRET_KEY…` |
| `ALLOWED_HOSTS` | `ImproperlyConfigured: 未配置 ALLOWED_HOSTS…` |

原因：早期版本给 `SECRET_KEY` 内置了一个公开的兜底值、`DEBUG` 默认 `True`、
`ALLOWED_HOSTS` 默认 `*`。三者叠加意味着任何绕过部署脚本的启动方式
（手动 gunicorn、`.env` 丢失）都会**静默降级**成「堆栈全公开 + 所有人共用同一把
公开密钥 + 任意 Host 都接受」，而服务状态看起来完全正常。现在改为缺配置就拒绝启动。

## 二、启用 HTTPS（强烈建议）

nginx 只监听 80，且 Django 在反代后必须显式被告知原始协议，否则
`request.is_secure()` 恒为 `False`，所有 HTTPS 相关判断都会失真。

```bash
apt install certbot python3-certbot-nginx
certbot --nginx -d 你的域名
```

拿到证书后编辑 `.env`：

```ini
HTTPS=on
CSRF_TRUSTED_ORIGINS=https://你的域名
```

然后重启应用。`HTTPS=on` 会一并启用 Secure Cookie、HSTS 与强制跳转 HTTPS。

## 三、目录属主（手工部署必查）

应用以 `www-data` 运行，但项目目录通常是 root 建的。**不改属主会导致照片、
合照、签名等上传全部失败，而服务状态看起来完全正常。**

```bash
chown -R root:www-data /opt/tnr
chmod -R u=rwX,g=rX,o= /opt/tnr
chown -R www-data:www-data /opt/tnr/media /opt/tnr/staticfiles
chmod 640 /opt/tnr/.env
```

## 四、验证定位相关接口

```bash
# 登录（用现有账号，如 cy_shelter/123456）
curl -s -c /tmp/c.txt -o /dev/null http://127.0.0.1:8000/login/
CSRF=$(grep csrftoken /tmp/c.txt | awk '{print $7}')
curl -s -b /tmp/c.txt -c /tmp/c.txt -d "username=cy_shelter&password=123456&csrfmiddlewaretoken=$CSRF" \
  -H 'Referer: http://127.0.0.1:8000/login/' -o /dev/null -w '登录: %{http_code}\n' http://127.0.0.1:8000/login/

# 逆地理编码（应返回 success:true 与地址）
curl -s -b /tmp/c.txt 'http://127.0.0.1:8000/api/business/geocode/reverse/?lat=30.25&lng=119.90'

# IP 定位兜底（应返回城市/区级地址）
curl -s -b /tmp/c.txt http://127.0.0.1:8000/api/business/geocode/ip/
```

## 五、部署后自检清单

| 检查项 | 命令 / 方式 | 期望 |
|---|---|---|
| 配置无告警 | `python manage.py check --deploy` | 除 HTTPS 类告警（W004/W008/W012/W016）外无警告 |
| 超级管理员可登录 | 访问 `/admin/` 用 `ensure_superuser` 输出的口令 | 能进入后台 |
| 演示账号可用 | 访问 `/login/` 用 `cy_shelter` / `123456` | 能进入捕捉点门户 |
| **照片上传可用** | 捕捉登记里传一张照片，再**经反代取回** | 上传成功；`media/` 出现文件；`/media/...` 返回 200 且**与原图逐字节一致** |
| **数据隔离生效** | 用他区账号访问本区宠物生命周期，**再访问一个不存在的 id** | **两者响应完全一致**（都是 404 且文案相同）—— 否则可用枚举 id 扫库 |
| 数据一致性 | `python manage.py check_data_integrity` | 输出「未发现一致性问题」，退出码 0 |
| 定时任务在跑 | **真投一个任务看是否被消费**（脚本见 §5.1），不能只看 `supervisorctl status` | `Success` 出现该任务且 `started`/`stopped` 有值；**清账后** `Success`/`Failure`/`OrmQ` 归零 |
| 定时注册存在 | `manage.py shell -c "from django_q.models import Schedule; print(Schedule.objects.count())"` | **≥ 1**；若为 0 见 §5.2（「5 天自动转待领养」只剩接口触发 + 启动补偿） |
| 无未捕获异常 | `grep -c Traceback <gunicorn stderr 日志>` | **0** |
| 非法参数不炸 | 非数字进整型外键 / 非日期进日期字段 / 超大数 | 4xx，**不得 500** |
| 地图 Key 生效 | `GET /api/business/geocode/reverse/?lng=112.144&lat=32.045`（需登录） | 返回真实地址（不是「未配置地图服务Key」） |
| 静态文件已更新 | 页面上确认 `?v=` 为本次版本号 | 与 `templates/base.html` 一致 |

> ⚠ **上传那一项必须走真实 HTTP multipart**，不能只用 shell 试写文件 ——
> shell 试写只证明「磁盘可写」，证明不了「Django 存储 + 反代 `alias` + 目录属主」串起来是对的。
>
> ⚠ **排查线上配置时先怀疑探针**：`getattr(settings, 'X')` 取不到**不等于**没配
> （代码可能是 `os.environ.get('X')`，由 `load_dotenv()` 灌入）；
> `curl --noproxy *` 的 `*` 会被 shell glob 展开成当前目录文件名，**必须写 `--noproxy '*'`**。

### 5.1 `tnr-qworker` 必须「真投真跑」才算过

`supervisorctl status` 显示 `RUNNING` **只证明进程活着**：证明不了 broker 通、
证明不了 worker 真能消费队列、也证明不了它能 bootstrap Django 跑应用代码。
实测证据：2026-09-21 首次部署时，生产上 `django_q_task` / `django_q_success` /
`django_q_failure` / `django_q_ormq` **四张表全是 0 行** —— 那个 `RUNNING` 的 worker
**从未处理过任何任务**。

```bash
cd /opt/tnr

# 1) 纯 stdlib 任务：只验「broker + worker 消费」通路，零副作用
venv/bin/python manage.py shell -c "
from django_q.models import Success, OrmQ
from django_q.tasks import async_task
import time
before = Success.objects.filter(func='time.sleep').count()
async_task('time.sleep', 0)
for _ in range(40):
    if Success.objects.filter(func='time.sleep').count() > before:
        s = Success.objects.filter(func='time.sleep').order_by('-stopped').first()
        print('OK', s.func, s.started, s.stopped); break
    time.sleep(1)
else:
    print('FAIL 40s 未被消费; 残留 OrmQ =', OrmQ.objects.count())
"

# 2) 真实业务任务：证明 worker 能 import 应用代码并跑通
venv/bin/python manage.py shell -c "
from django_q.models import Success
from django_q.tasks import async_task
import time
before = Success.objects.filter(func__contains='auto_promote_to_adoptable').count()
async_task('business.tasks.auto_promote_to_adoptable')
for _ in range(60):
    q = Success.objects.filter(func__contains='auto_promote_to_adoptable')
    if q.count() > before:
        print('OK result =', repr(q.order_by('-stopped').first().result)); break
    time.sleep(1)
else:
    print('FAIL 60s 无记录')
"
```

> ⚠ **投递真实任务前先数超期记录**：若
> `Treatment.objects.filter(status='completed', created_at__lte=now-5d)`
> 里还有 `pet.status == 'in_treatment'` 的行，投递会**真的改生产数据**
> （这正是该任务的职责，不是 bug）。只想验通路不想动数据时，用第 1 步的 `time.sleep`。

> ⚠ **探针必须清账**：验完删掉 `Success`/`Failure` 中 `func` 含 `time.sleep` /
> `call_command` / `auto_promote_to_adoptable` 的记录，并确认 `OrmQ` 归零。
> 否则 `Success` 表里会长期躺着 `time.sleep` 这类**不是业务产生的**痕迹，
> 日后排查真任务时会被误当成「有任务跑过」。

### 5.2 定时任务：已由数据迁移注册（附历史缺口说明）

`business/tasks.py::auto_promote_to_adoptable`（诊疗完成 5 天后自动转待领养并上架领养大厅）
的**周期调度**由数据迁移
`business/migrations/0016_register_auto_promote_schedule.py` 注册：
**每天 03:00（`Asia/Shanghai`）**、`schedule_type='D'`、`repeats=-1`、`cluster=None`。
它随 `migrate` 自动创建，任何环境（开发 / 测试 / 生产 / 换机器重建）都自带，
不再依赖运维手工补。

**核验**（部署后必做，见 §5.1 的 `Schedule.objects.count()`）：

```bash
cd /opt/tnr
venv/bin/python manage.py shell -c "
from django_q.models import Schedule
for s in Schedule.objects.all():
    print(s.pk, s.name, s.func, s.schedule_type, s.repeats, s.cluster, s.next_run)
"
# 期望恰好一条：... business.tasks.auto_promote_to_adoptable D -1 None 2026-09-22 03:00:00+08:00
```

> **历史缺口（2026-09-21 已修，别再重复排查）**：迁移之前 `django_q_schedule`
> 是 0 行 —— 而且**清库前的旧库同样是 0 行**（已从备份转储 `db-tnr_system.sql` 核对），
> 所以**不是**某次部署或清库造成的，是项目自带缺口。
> 当时该任务只有三条**非周期**触发路径：启动补偿（`business/apps.py`，
> 每进程启动后首次请求一次）、接口投递（`views_treatment.py::_schedule_auto_promote()`，
> 诊疗完成流程里一次）、手工命令（`manage.py promote_adoptable`）。
> 源码注释原先写的是「部署时配置定时任务即可」—— 把周期注册当成运维的手工动作，
> **从未被做过**。后果：服务器长期不重启、期间又没人走「诊疗完成」流程时，
> 超期宠物不会被自动转待领养。现已固化进迁移，该注释也已同步更正。

**幂等**：`get_or_create` 以 `name` 为键，已存在（运维手工建过 / 迁移重跑）
**不覆盖** `next_run` / `repeats` / `schedule_type`，不打断运维的既有调整。

> ⚠ **验证「调度循环真的会认领」不必等到凌晨**：临时建一条 `Schedule`
> （`next_run` 故意设为过去、`func='time.sleep'`、`args='0'`、
> `schedule_type='O'`、`repeats=1`），django-q2 的调度器每 ~30 秒轮询一次，
> 实测 25 秒内产生 `Success` 记录，用完删干净。
> 想验**真实那条**，可临时把它的 `next_run` 改到过去、等它跑完再改回 03:00 ——
> ⚠ 但此时它会**真的执行转待领养**，务必先确认没有会被改动的记录。

## 六、常见报错对照

| 报错 | 原因 | 处理 |
|---|---|---|
| `ImproperlyConfigured: 未配置 SECRET_KEY` | `.env` 缺 `SECRET_KEY`（注意不是 `DJANGO_SECRET_KEY`） | 填入随机长字符串后重启 |
| `ImproperlyConfigured: 未配置 ALLOWED_HOSTS` | `.env` 缺 `ALLOWED_HOSTS` | 填入域名/IP 后重启 |
| `未配置地图服务Key，请在 .env 中设置 TNR_AMAP_KEY` | 新服务器 `.env` 缺 Key（.env 不入 git） | 编辑 `.env` 填入 Key 后**重启服务** |
| `地图服务返回错误：INVALID_USER_KEY` | Key 拼写错误或不是"Web服务"类型 | 高德控制台核对 Key 及其绑定服务平台 |
| `地图服务返回错误：USERKEY_PLAT_NOMATCH (10009)` | Key 绑定的是"Web端(JS API)"而非"Web服务" | 同一应用下添加"Web服务"类型 Key |
| `地图服务请求失败：...timeout/Connection refused` | 服务器出网被墙/无外网 | 开放对 `restapi.amap.com:443` 的出网访问 |
| 前端定位报 `Only secure origins are allowed` | HTTP 非安全源，浏览器禁止 GPS 定位 | 正常现象，前端已自动降级 IP 定位（城市/区级）；如需精确定位请配 HTTPS |
| `no such column: business_capture.latitude` | 数据库迁移未执行 | `python manage.py migrate` |
| 上传照片报 500 / Permission denied | `media/` 属主不是 `www-data` | 见第三节「目录属主」 |
| 提交表单报 403 CSRF | 通过域名访问但未配 `CSRF_TRUSTED_ORIGINS` | 按第二节填入 `https://域名` |
