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

# 6.2 演示物料有效期订正（**只在存量库上跑一次**）
python manage.py refresh_demo_material_expiry          # 预演，只打印将改的行
python manage.py refresh_demo_material_expiry --apply  # 确认无误后落库
# 背景：早期 seed_data 把物料有效期写成绝对日期，现已改成相对今天生成；
# 但 get_or_create 的 defaults **只在创建时生效**，所以已导入的库修不回来、
# 重跑 seed_data 也没用，只能靠这条命令订正。
# 判据 = 名称 ∈ 演示物料清单 + 区县 = 襄城区 + 有效期存在且已过期（三条缺一不可）：
#   - 不用批号（同一套演示数据在不同环境批号不同，会静默漏掉）
#   - 必须限定区县（现场 4 个区县各有同名物料，只有襄城区那套是演示数据）
#   - 必须限定「已过期」（`None` 是「没登记有效期」，不等于过期，不能代填）
# 非演示物料只报告不改；默认预演，可重复执行（幂等）。

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

> ⚠ **HTTPS 是「弹 GPS 权限框」的硬前提，不是优化项。**
> `navigator.geolocation.getCurrentPosition()` 只在**安全上下文**（HTTPS / `localhost` /
> `127.0.0.1`）下可用。当前生产是 `http://公网IP` → 浏览器**直接拒绝且不弹任何权限框**，
> **网页代码绕不过去**。所以磊哥要的「询问是否授予浏览器/微信 GPS 权限」，
> **必须先有域名 + 证书**；在此之前只能降级 IP 定位（城市级），这不是前端缺陷。

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

# IP 定位兜底（应返回城市/区级地址，且带 source 字段）
curl -s -b /tmp/c.txt http://127.0.0.1:8000/api/business/geocode/ip/
```

返回体里的 **`source` 决定这个地址能不能当「客户所在位置」用**：

| `source` | 含义 | 前端该怎么做 |
|---|---|---|
| `client_ip` | 按**客户端公网 IP** 定位，地址反映用户大概在哪 | 可以自动填表 |
| `server_ip` | 拿不到客户端公网 IP（内网访问、IPv6、CGNAT 等），退化成**服务器所在城市** | **绝不可自动填表**，只能提示「请手动定位」 |

> ⚠ 从**本机 `127.0.0.1`** 调这个接口，永远得到 `server_ip` —— 这是**正确行为**，不是缺陷。
> 要验证 `client_ip` 分支，必须从**带公网出口的机器**经反代访问，或直接单测
> `business/tests/test_ip_location.py`（已覆盖 CGNAT / XFF 伪造 / IPv6 等）。
>
> ⚠ **`X-Forwarded-For` 一律不采信**：nginx 用 `$proxy_add_x_forwarded_for` 追加，
> 客户端可以自己伪造一个前缀。只认 nginx 覆盖写入的 `X-Real-IP`，退回 `REMOTE_ADDR`。

## 五、部署后自检清单

| 检查项 | 命令 / 方式 | 期望 |
|---|---|---|
| 配置无告警 | `python manage.py check --deploy` | 除 HTTPS 类告警（W004/W008/W012/W016）外无警告 |
| 超级管理员可登录 | 访问 `/admin/` 用 `ensure_superuser` 输出的口令 | 能进入后台 |
| 演示账号可用 | 访问 `/login/` 用 `cy_shelter` / `123456` | 能进入捕捉点门户 |
| **照片上传可用**（含手机原图） | 用 **≥3MB 的真实手机照片**提交捕捉登记（或多张合照），再**经反代取回** | 上传成功；`media/` 出现文件；`/media/...` 返回 200 且**与原图逐字节一致**；**不得**出现 `RequestDataTooBig` 的 400 |
| **请求体上限一致** | 见下方 §5.5 | nginx `client_max_body_size` **20M**；`DATA_UPLOAD_MAX_MEMORY_SIZE` 未被人为调小 |
| **超大图被拦且可读** | 见下方 §5.5 的 curl 片段：直接发一个 >10MB 的 multipart 请求 | HTTP **400 且响应体是 JSON**，文案含「超过 10MB 上限」；**不得**是 Django 的 HTML 报错页 |
| **数据隔离生效** | 用他区账号访问本区宠物生命周期，**再访问一个不存在的 id** | **两者响应完全一致**（都是 404 且文案相同）—— 否则可用枚举 id 扫库 |
| 数据一致性 | `python manage.py check_data_integrity` | 输出「未发现一致性问题」，退出码 0 |
| 演示物料未过期 | `python manage.py refresh_demo_material_expiry` | 输出「已订正 0 行」或全部 `[跳过]`；**不应**出现「将改写」 |
| 过期物料被拦 | 把某物料有效期改成昨天，再用它建诊疗（脚本见 §5.1 同款思路） | HTTP **400** 且文案含「已过期」；**未过期时不得出现该文案** |
| 定时任务在跑 | **真投一个任务看是否被消费**（脚本见 §5.1），不能只看 `supervisorctl status` | `Success` 出现该任务且 `started`/`stopped` 有值；**清账后** `Success`/`Failure`/`OrmQ` 归零 |
| 定时注册存在 | `manage.py shell -c "from django_q.models import Schedule; print(Schedule.objects.count())"` | **≥ 1**；若为 0 见 §5.2（「5 天自动转待领养」只剩接口触发 + 启动补偿） |
| 无未捕获异常 | **只看最后一次重启之后**的 Traceback（见 §5.3），**不要**对整份日志 `grep -c` | 重启后 **0** 处业务异常；`DisallowedHost` 是**正确行为**，不算 |
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

### 5.3 ⚠ `grep -c Traceback` 对**整份日志**计数是错的判据

它会把两类**无关**的东西算成「未捕获异常」：

1. **本次重启之前**的历史错误（早就修了，日志还在）；
2. **外部扫描器**触发的 `DisallowedHost` —— 那是 `ALLOWED_HOSTS` **正确工作**的证据，
   不是缺陷。

实测（2026-09-21）：整份日志里唯一那处 Traceback 来自 `198.235.24.95` 的
`GET / HTTP/1.0`，UA 是
`Hello from Palo Alto Networks, ... Scanning-activity` —— `Host: _` 不是合法域名，
Django 回 400。**判据若写 `grep -c Traceback == 0` 就会永远红**，
因为公网 Web 服务必然被扫（本次 nginx 侧 59 条 400 里绝大多数是扫描器）。

正确做法是**按行号划窗口**：

```bash
LOG=/var/log/tnr/gunicorn-error.log
LASTBOOT=$(grep -n 'Booting worker' "$LOG" | tail -1 | cut -d: -f1)
echo "最后一次重启在第 $LASTBOOT 行；之后的新行："
sed -n "$((LASTBOOT + 1)),\$p" "$LOG"
```

**判据**：重启之后的新行里**没有** Traceback（或只有 `DisallowedHost`）。
`django.request: Bad Request / Not Found` 这类 WARNING 是**自己探针触发的 4xx**，正常。

> 同理适用于任何「日志里某关键词计数为 0」的检查 ——
> **先确定时间窗口（自上次重启以来）与排除项（已知的正常噪音），再数**。
> 不划窗口的计数会把历史问题和外部噪音混进来，判据必然失效。

### 5.4 公网暴露面（部署后顺手看一眼，不必等出事）

```bash
ss -lntp | grep -vE '127\.0\.0\.1|::1'     # 谁在 0.0.0.0 上监听
```

⚠ **本机监听 `0.0.0.0` ≠ 公网可达** —— 真正的门禁是**云安全组**。
两侧都要看：机器上绑了 `0.0.0.0` 但安全组挡住 → 当前安全，属**纵深防御**隐患；
安全组放开 → 立刻暴露。**从公网实测一次**才算数（在你自己机器上）：

```bash
for p in 22 80 3306 24216; do
  nc -z -G 5 -w 5 <公网IP> $p && echo "$p 开放" || echo "$p 不可达"
done
```

本项目实测（2026-09-21，公网 `124.223.41.44`）：**只有 80 开放**，
22 / 3306 / 24216 / 21115 均被安全组挡住 ✔。但机器上
`docker-proxy` 把 **MariaDB 绑在 `0.0.0.0:3306`**、`1panel-core` 绑在 `0.0.0.0:24216`
—— 安全组一旦被放开就会直接暴露。**加固方向**（属安全姿态变更，需单独决策）：
把这两个绑定收到 `127.0.0.1` 或 Tailscale 地址，而不是 `0.0.0.0`。

> ⚠ 顺带：`ufw` 是 **inactive**，`iptables INPUT` 只有 2 条规则 ——
> 当前**完全依赖云安全组**这一层。别以为机器上还有一道本地防火墙。

### 5.5 上传体积：nginx 与 Django 是**两道**闸，且限制方式不同

现场：2026-09-22 手机端「新增捕捉任务」提交无效。nginx 访问日志里
`POST /api/business/captures/create/` **连续 8 次 400**，gunicorn 错误日志：

```
django.core.exceptions.RequestDataTooBig: Request body exceeded settings.DATA_UPLOAD_MAX_MEMORY_SIZE.
  File "/opt/tnr/business/views_capture.py", line 410, in capture_create
    data = parse_json_body(request)
  File "/opt/tnr/business/services.py", line 46, in parse_json_body
    data = json.loads(request.body)
```

**两道闸的语义完全不同，必须分别确认**：

```bash
# ① nginx：整个请求体的硬上限，超了直接 413，请求进不到 Django
grep -rn 'client_max_body_size' /etc/nginx/ /etc/nginx/sites-enabled/ 2>/dev/null
# 期望：20M（本项目值）。缺失则为 nginx 默认 1M —— 手机原图必然被拒。

# ② Django：只有**读 request.body 时**才校验，且**不含文件上传部分**
cd /opt/tnr && venv/bin/python manage.py shell -c \
  "from django.conf import settings; print('DATA_UPLOAD_MAX_MEMORY_SIZE =', settings.DATA_UPLOAD_MAX_MEMORY_SIZE)"
# 期望：2500000（Django 默认 2.5MB）。**不要调大它来"修"上传问题** —— 见下。
```

**关键机制**（决定了正确的修法）：

- `settings.DATA_UPLOAD_MAX_MEMORY_SIZE` 由 `request.body` 触发校验，按
  `CONTENT_LENGTH` 判定，**整个请求体都算在内**。
- 但 `multipart/form-data` 的**文件部分不计入** `request.POST` / `request.FILES`：
  `MultiPartParser` 只对**非文件字段**累加字节数
  （`django/http/multipartparser.py` 的 `num_bytes_read`）。
- 所以**大图上传本来不该撞这个限制** —— 撞上纯粹是因为 `parse_json_body()`
  在 multipart 请求上也去读了 `request.body`（把整个体读进内存）。
- `RequestDataTooBig` 是 `SuspiciousOperation` 的子类，**不是** `ValueError`，
  原来那个 `except (JSONDecodeError, ValueError, TypeError)` **接不住它**。

**因此修法是两处，而不是调大阈值**：

1. `business/services.py::parse_json_body()` —— **multipart 直接返回 `{}`**，不碰 `request.body`；
   并补 `except RequestDataTooBig` 兜底（其它 content-type 的超大请求体也不该炸成裸 400）。
2. `static/js/tnr-common.js::TNR_UI.compressImage()` —— 前端先把照片压到长边 1600 / JPEG 0.82，
   从源头避免撞 nginx 的 20M（100 只猫 × 手机原图 3~5MB = 必然超）。

> ⚠ **不要靠调大 `DATA_UPLOAD_MAX_MEMORY_SIZE` 来修**：它会把整个请求体读进内存，
> 调大等于把内存占用交给客户端决定（移动端多图场景可以直接打爆 worker）。
> 正确做法是**根本不读它**（multipart 走流式解析）。
>
> ⚠ **`RequestDataTooBig` 的 400 是 Django 的 HTML 报错页**，响应体不是 JSON。
> 前端 `_postForm` 内部 `await res.json()` 会**抛错**，若不 `try/catch`，
> 用户看到的只是「正在提交…」然后**什么都没发生** —— 这就是「点了提交没反应」的表象。
> 凡是有可能返回非 JSON 的提交，调用方**必须** `try/catch` 并给出可读提示。

**部署后实测**（不需要浏览器，直接打接口）：

```bash
cd /opt/tnr

# 1) 造一个 >10MB 的文件（内容无所谓，体积才是变量）
/opt/tnr/venv/bin/python -c "
import os; os.makedirs('/tmp/tnr-big', exist_ok=True)
open('/tmp/tnr-big/big.jpg','wb').write(b'\xff\xd8\xff\xe0' + os.urandom(11*1024*1024))
print('已生成', os.path.getsize('/tmp/tnr-big/big.jpg') // 1024, 'KB')"

# 2) 登录拿会话与 CSRF
curl -s -c /tmp/c.txt -o /dev/null http://127.0.0.1:8000/login/
CSRF=$(grep csrftoken /tmp/c.txt | awk '{print $7}')
curl -s -b /tmp/c.txt -c /tmp/c.txt -o /dev/null \
  -d "username=cy_shelter&password=123456&csrfmiddlewaretoken=$CSRF" \
  -H 'Referer: http://127.0.0.1:8000/login/' http://127.0.0.1:8000/login/

# 3) 发超大 multipart，看状态码与 Content-Type
#    ⚠ 用 `-w` 取状态码，**不要** `head -1 /tmp/hdr.txt` ——
#    11MB 请求会带 `Expect: 100-continue`，头部第一行是信息响应 `HTTP/1.1 100 Continue`，
#    真正的状态码在它后面，`head -1` 会读到错的那行。
curl -s -b /tmp/c.txt -X POST \
  -H "X-CSRFToken: $CSRF" -H 'Referer: http://127.0.0.1:8000/' \
  -F 'pet_count=1' -F 'group_photo=@/tmp/tnr-big/big.jpg;type=image/jpeg' \
  -o /tmp/body.txt \
  -w 'HTTP %{http_code}  content-type=%{content_type}  上传体积=%{size_upload} 字节\n' \
  http://127.0.0.1:8000/api/business/captures/create/

cat /tmp/body.txt                             # 可读 JSON；**不得**出现 <title>…at /api/…</title>
```

本项目实测（2026-09-22，本地 `127.0.0.1:8000`，修复后）：

```
HTTP 400  content-type=application/json  上传体积=11534649 字节
{"success": false, "data": null, "message": "物业名称不能为空"}
```

**判据**：

- **`content-type` 必须是 `application/json`** —— 这一条最关键。
  它证明请求**进到了应用层**（修复生效）。
- 状态码 **400 是正常的**：这里故意只传了最小字段集，应用据此报「物业名称不能为空」。
  要紧的是这个 400 是**应用给出的 JSON**，而不是 Django 的 HTML 报错页。
- 响应体里**不得**出现 `RequestDataTooBig`，也不得出现 `<title>…at /api/…</title>`
  （后者是 Django 的 HTML 报错页特征）。
- `上传体积` 应当**接近 11MB**（约 `11534649` 字节）。若它只有几百 KB，
  说明 curl 没把文件真发出去，这个检查是**假绿**。

**改前对照**（同一段 curl、同一张图，在修复前的提交 `fcdeb86` 上跑）：

```
Content-Type: text/html; charset=utf-8
<!DOCTYPE html>
  <title>RequestDataTooBig
          at /api/business/captures/create/</title>
```

这正是**生产现场**的响应 —— 用户看不到这段 HTML，只看到「正在提交…」然后什么都没发生。
所以「`content-type` 是不是 `application/json`」就是**这个修复最直接的一条判据**。

### 5.6 ⚠ 代理头：只认 nginx **覆盖写入**的那些

nginx 会把客户端的请求头**原样透传**，所以「请求头里有某个值」**不等于**
「这个值是可信的」。区分标准只有一条：**nginx 是覆盖它，还是追加它。**

```nginx
proxy_set_header X-Real-IP       $remote_addr;                 # 覆盖 → 可信
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;  # 追加 → 不可信
proxy_set_header X-Forwarded-Proto $scheme;                    # 覆盖 → 可信
```

`$proxy_add_x_forwarded_for` 的语义是「**客户端原值** + 我们看到的对端」——
**客户端伪造的值排在最先**。所以 `X-Forwarded-For.split(',')[0]` 拿到的是
攻击者写的值。

**实测（2026-09-22）**：带 `X-Forwarded-For: 203.0.113.7` 发一个写请求，
审计台账 `AuditLog.ip` 记下的就是 `203.0.113.7`（真实出口是 `39.181.5.30`）。
已改为**只认 `X-Real-IP`**（见 `core/audit.py::client_ip`），
`X-Forwarded-For` 一律不看。

⚠ **不要改成「取 XFF 最后一段」**：「最后一段」只在「代理一定会追加」时才成立，
而 nginx 默认透传客户端发来的未知头 —— 一旦哪天这行 `proxy_set_header` 被删掉，
最后一段就又变成伪造值。**判据不该依赖另一处配置才安全。**
代价是换代理后审计 IP 会退化成 `127.0.0.1`，这是**可见的降级**，比静默记假值好。

> **加任何「按来源 IP」的逻辑之前**，先确认它读的是哪个头：
> 本项目里 `request.META['HTTP_X_REAL_IP']` 才可信，
> `HTTP_X_FORWARDED_FOR` 是客户端可控的。
> 新增代理头时，也优先用**覆盖式**写法（`$变量`），不要用追加式。

## 六、常见报错对照

| 报错 | 原因 | 处理 |
|---|---|---|
| `ImproperlyConfigured: 未配置 SECRET_KEY` | `.env` 缺 `SECRET_KEY`（注意不是 `DJANGO_SECRET_KEY`） | 填入随机长字符串后重启 |
| `ImproperlyConfigured: 未配置 ALLOWED_HOSTS` | `.env` 缺 `ALLOWED_HOSTS` | 填入域名/IP 后重启 |
| `未配置地图服务Key，请在 .env 中设置 TNR_AMAP_KEY` | 新服务器 `.env` 缺 Key（.env 不入 git） | 编辑 `.env` 填入 Key 后**重启服务** |
| `地图服务返回错误：INVALID_USER_KEY` | Key 拼写错误或不是"Web服务"类型 | 高德控制台核对 Key 及其绑定服务平台 |
| `地图服务返回错误：USERKEY_PLAT_NOMATCH (10009)` | Key 绑定的是"Web端(JS API)"而非"Web服务" | 同一应用下添加"Web服务"类型 Key |
| `地图服务请求失败：...timeout/Connection refused` | 服务器出网被墙/无外网 | 开放对 `restapi.amap.com:443` 的出网访问 |
| 前端定位报 `Only secure origins are allowed` | HTTP 非安全源，浏览器禁止 GPS 定位 | **预期行为**，浏览器**不会弹权限框**（代码绕不过去）。前端已自动降级 IP 定位；如需精确定位请配 HTTPS（见第二节） |
| 定位总是返回**北京**且 `source` 为 `server_ip` | 拿不到客户端公网 IP，高德按**服务器出口**定位 | 从公网经反代访问再验；本机 `127.0.0.1` 测永远是 `server_ip`。前端对 `server_ip` **不自动填表**，只提示手动定位 |
| 上传大图报 400，页面是 Django 的 HTML 报错页 | `RequestDataTooBig`（`request.body` 超 `DATA_UPLOAD_MAX_MEMORY_SIZE`） | 见 §5.5。**不要调大阈值**；确认 `parse_json_body()` 对 multipart 不读 `request.body` |
| 上传大图报 **413** | nginx `client_max_body_size` 不够 | 调到 20M 并 `nginx -s reload`；前端 `compressImage()` 已在源头压缩 |
| 点「提交」没反应、无任何提示 | 响应体非 JSON（413 / 502 / 400 报错页），前端 `await res.json()` 抛错 | 前端必须 `try/catch`；查 nginx access.log 的状态码与响应体大小 |
| `no such column: business_capture.latitude` | 数据库迁移未执行 | `python manage.py migrate` |
| 上传照片报 500 / Permission denied | `media/` 属主不是 `www-data` | 见第三节「目录属主」 |
| 提交表单报 403 CSRF | 通过域名访问但未配 `CSRF_TRUSTED_ORIGINS` | 按第二节填入 `https://域名` |
