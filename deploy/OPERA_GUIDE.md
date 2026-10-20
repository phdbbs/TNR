# TNR 流浪猫管理系统 · 运维手册（运行 / 值班 / 交付）

命令可直接复制；每条结论后标注出处（`文件:行号`）；未能从代码确认的内容集中在第 12 节。
与其他文档的分工：`DEPLOY.md` 管部署步骤与已知部署陷阱（§5.1–§5.15），`MEDIA_STORAGE.md` 管媒体存储后端与迁移/回滚，`DEMO_ACCOUNTS.md` 管演示账号清单，`TEST_REPORT.md` 是逐轮整改历史（本手册不复述），`deploy/README.txt` 是部署包自带的简版说明；本手册只写日常运行、巡检、排障与发布，冲突时以引用的代码为准。

> 任务交接时提到的 `deploy/monitoring-prometheus-grafana.md` 在本仓库 <strong>不存在</strong>（`deploy/` 只有 `README.txt`、`nginx.conf`、`supervisor.conf`），因此第 6 节不依赖它，直接给业务指标与 SQL。

## 1. 系统形态速览

| 项 | 值 | 出处 |
|---|---|---|
| 框架 | Django 5.0.6 / Python 3.12 | `requirements.txt:1`、`deploy/README.txt:6` |
| 应用目录 / venv | `/opt/tnr`、`/opt/tnr/venv` | `deploy.sh:13`、`deploy.sh:87` |
| WSGI | `tnr_system.wsgi:application` | `deploy.sh:217` |
| 监听 | `127.0.0.1:8000`，只本机 | `gunicorn.conf.py:4` |
| 进程 | supervisor 两个 program：`tnr-gunicorn`、`tnr-qworker` | `deploy.sh:216`、`deploy.sh:226` |
| 反代 | nginx 80 → `http://127.0.0.1:8000` | `deploy/nginx.conf:25`、`deploy.sh:262` |
| 静态 / 媒体 | `STATIC_ROOT=/opt/tnr/staticfiles`、`MEDIA_ROOT=/opt/tnr/media` | `tnr_system/settings.py:316`、`tnr_system/settings.py:320` |
| 生产库 | MySQL/MariaDB，库 `tnr_system`，账号 `tnr`，`utf8mb4` / `utf8mb4_unicode_ci` | `deploy.sh:16`–`deploy.sh:17`、`deploy.sh:95` |
| 开发库 | sqlite `db.sqlite3` | `tnr_system/settings.py:292`、`.env:12` |
| 时区 | `Asia/Shanghai` + `USE_TZ=True` → <strong>库里 datetime 存 UTC</strong>，所以本手册所有 SQL 一律用 `UTC_TIMESTAMP()` / `UTC_DATE()`，不要用 `NOW()` / `CURDATE()`（差 8 小时） | `tnr_system/settings.py:308`、`tnr_system/settings.py:310` |

## 2. 日常启停与健康检查

### 2.1 gunicorn 实值与日志落点

`gunicorn.conf.py` 全文 12 行，supervisor 以 `--config` 加载（`deploy.sh:217`）：`bind 127.0.0.1:8000`（:4）、`workers = cpu_count()*2+1`（:5）、`timeout 60`（:6）、`accesslog/errorlog = '-'`（:7、:8）、`loglevel info`（:9）、`max_requests 1000 / jitter 50`（:10、:11，worker 自动轮换防内存累积）。

日志走 stdout/stderr，真实落盘文件是 supervisor 配的四个：`/var/log/tnr/gunicorn.log`、`gunicorn-error.log`（`deploy.sh:222`、`deploy.sh:223`）、`qworker.log`、`qworker-error.log`（`deploy.sh:232`、`deploy.sh:233`）。应用异常由 `LOGGING` 打到控制台后被同一文件收走（`tnr_system/settings.py:243`–`tnr_system/settings.py:260`，`django.request` 单独提 WARNING，:258）。项目根的 `server.log` <strong>只是本地 `start_dev.sh` 的调试日志</strong>，`deploy.sh:81` 会把它从部署目录删掉。

### 2.2 开发启动与生产启动的差异

| 维度 | `start_dev.sh` | 生产 |
|---|---|---|
| 服务器 | `runserver 0.0.0.0:$PORT`（`start_dev.sh:83`） | gunicorn 只绑 127.0.0.1（`gunicorn.conf.py:4`） |
| `DEBUG` | `True`（`.env:7`） | `False`（`deploy.sh:105`） |
| 库 | sqlite（`.env:12`） | MySQL（`deploy.sh:108`） |
| `ALLOWED_HOSTS` | DEBUG 下自动追加本机所有网卡 IP（`tnr_system/settings.py:130`） | 必须 `.env` 显式列出（`deploy.sh:107`），漏配直接 400 |
| 模板缓存 | 显式不缓存 loader，改 `.html` 立即生效（`tnr_system/settings.py:195`–`tnr_system/settings.py:198`） | Django 5.0 无条件套 `cached.Loader`（`tnr_system/settings.py:167`–`tnr_system/settings.py:180`），<strong>改模板必须重启</strong> |
| `/media/` | Django 挂路由，且只在本地存储模式（`tnr_system/urls.py:43`–`tnr_system/urls.py:56`） | nginx `alias /opt/tnr/media/`（`deploy/nginx.conf:19`） |
| 初始化 | `migrate` + `seed_data`（`start_dev.sh:72`–`start_dev.sh:73`） | `migrate` + `collectstatic` + `seed_data` + `ensure_superuser` + `ensure_platform_admin`（`deploy.sh:142`–`deploy.sh:189`） |

`SECRET_KEY` 与 `ALLOWED_HOSTS` 都没有兜底默认值，生产缺 `.env` 直接启动失败（`tnr_system/settings.py:103`–`tnr_system/settings.py:112`、`tnr_system/settings.py:117`–`tnr_system/settings.py:124`）；这是有意设计，不要「为了先跑起来」放宽。

### 2.3 健康检查：当前没有 /health 端点

`tnr_system/urls.py:24`–`tnr_system/urls.py:42` 是全部路由（`admin/`、`login|logout|api/me`、五个门户、`print/<doc>/<pk>/`、`api/business/`、`api/supervision/`），<strong>没有 `/health`、`/healthz`、`/ok` 之类的探针端点</strong>。替代手段：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/          # 期望 302：未登录被 @login_required 挡住
#   依据 accounts/urls.py:9（'' → dashboard_redirect）+ accounts/views.py:64（该视图带 @login_required）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login/    # 期望 200：模板渲染可用
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: 100.99.98.71' http://127.0.0.1/   # 端到端，必须经 80
cd /opt/tnr && venv/bin/python manage.py check                            # 配置自检（不连库）
cd /opt/tnr && venv/bin/python manage.py check --deploy                   # 生产告警（HTTPS 类 W004/W008/W012/W016 可残留，DEPLOY.md:267）
cd /opt/tnr && venv/bin/python manage.py shell -c "from accounts.models import User; from django_q.models import OrmQ; print(User.objects.count(), OrmQ.objects.count())"   # DB 真连接 + 队列积压
```

前三条都不碰数据库，发现不了 DB 挂；直连 8000 不反映真实链路（`DEPLOY.md:272`）。要真正的 `/health`（返回 DB + 队列状态）属代码改动，本手册不擅自加，见第 12 节。

### 2.4 启停命令

```bash
sudo supervisorctl status
sudo supervisorctl restart tnr-gunicorn              # 改 Python 代码；生产改模板也要重启
sudo supervisorctl restart tnr-gunicorn tnr-qworker
sudo supervisorctl stop all
sudo nginx -t && sudo systemctl reload nginx
```

改过 `/etc/supervisor/conf.d/tnr.conf` 必须重跑 `reread` → `update` → `restart`（`deploy.sh:236`–`deploy.sh:238`），否则新配置不生效。`.env` 任何变量改动都要重启进程才生效（`load_dotenv()` 只在启动时读，`tnr_system/settings.py:13`）。

## 3. 数据库运维

### 3.1 连接参数与两个坑

库 `tnr_system`（`deploy.sh:16`）、账号 `tnr`@localhost 全库权限（`deploy.sh:17`、`deploy.sh:97`）、`utf8mb4/utf8mb4_unicode_ci`（`deploy.sh:95`）、`127.0.0.1:3306`（`deploy.sh:112`–`deploy.sh:113`）、引擎 `django.db.backends.mysql`（`tnr_system/settings.py:269`），口令取 `.env` 的 `DB_PASSWORD`（未指定时 `deploy.sh:22`–`deploy.sh:27` 随机生成并只打印一次）。

坑 1：sqlite 分支 <strong>不读 `DB_NAME`</strong>，只读 `DB_SQLITE_PATH`（`tnr_system/settings.py:288`–`tnr_system/settings.py:293`）。照抄生产 `.env` 的 `DB_NAME` 想在本地预演迁移，会被静默忽略、迁移照打在 `db.sqlite3` 上（成因写在 `tnr_system/settings.py:278`–`tnr_system/settings.py:287`）。预演请写 `DB_SQLITE_PATH=/tmp/preexec.sqlite3 venv/bin/python manage.py migrate`。

坑 2：`deploy.sh` 第 5 步是 `cat >` <strong>覆盖</strong>写 `.env`（`deploy.sh:102`），重跑会换 `SECRET_KEY`（全员掉线）并覆盖 `DB_PASSWORD`；只有 `ADMIN_PASSWORD` / `PLATFORM_ADMIN_PASSWORD` 靠 `sed` 从旧 `.env` 继承（`deploy.sh:49`–`deploy.sh:59`）。所以已有生产服务器不要跑 `deploy.sh`（`DEPLOY.md:3`–`DEPLOY.md:14`），走 §8.4 的手工流程。

### 3.2 迁移顺序

```bash
cd /opt/tnr && source venv/bin/activate
python manage.py showmigrations
python manage.py makemigrations --check --dry-run     # 有未生成的迁移文件时非零退出（发布闸门）
python manage.py migrate --noinput                    # deploy.sh:143
```

四条事实：`django_q` 的迁移必须先跑完，`business/0016`、`0017` 显式依赖它，否则 `LookupError: No installed app with label 'django_q'`（`business/migrations/0016_register_auto_promote_schedule.py:80`–`business/migrations/0016_register_auto_promote_schedule.py:84`）；`0020`/`0023`/`0024`/`0029`/`0030` 是数据回填迁移，首跑会全表扫、耗时明显；两条注册定时任务的迁移用 `get_or_create(name=…)`，重跑不覆盖运维的手工调整（`business/migrations/0016_register_auto_promote_schedule.py:60`–`business/migrations/0016_register_auto_promote_schedule.py:71`）；迁移跑完必须紧接 `check_data_integrity`（§5.3）。

### 3.3 回滚与不可逆迁移

退一条：`python manage.py migrate business 0034_treatment_disposal_advised`；再滚回来：`python manage.py migrate business 0035_alter_adoption_ledger_no_alter_capture_ledger_no_and_more`。反向是 `noop`、回退后数据无法复原的迁移：

| 迁移 | 做了什么 | 出处 |
|---|---|---|
| `business/0007` | 回填捕捉单状态 | `business/migrations/0007_capture_deleted_at_capture_deleted_by_and_more.py`（`_noop_reverse`） |
| `business/0010` | 订正历史捕捉单区县与机构名 | `business/migrations/0010_fix_legacy_capture_district_and_names.py:84` |
| `business/0011` | 清理遗留地点名 | `business/migrations/0011_cleanup_legacy_place_names.py:121` |
| `business/0012` | 东津新区改名 | `business/migrations/0012_rename_dongjing_district.py:56` |
| `business/0023` | 全量重发单据号 | `business/migrations/0023_renumber_doc_nos.py:90` |
| `business/0024` | 给空单据号补号 | `business/migrations/0024_backfill_blank_doc_nos.py:86` |

可逆（有真实反向）：`business/0009`、`0016`、`0017`、`0020`、`0029`、`0030`、`core/0003`。`0023`/`0024` 改的是打印件与监管台账对账用的业务键，回退不会还原旧号 —— 只能按 §3.5 用备份恢复。另：MySQL/MariaDB 的 DDL 隐式提交，迁移中途失败不会整体回滚，按 `showmigrations` 结果续跑即可。

### 3.4 备份

命名沿用仓库已有形态 `<对象>.bak-YYYYMMDD-HHMMSS[-原因]`（例：`db.sqlite3.bak-20260929-174902-before-r46-docno-backfill`、`media.bak-photos-20261016-222742`），`.gitignore:7`、`.gitignore:14` 就按该前缀忽略；生产 SQL 备份建议 `db-tnr_system-YYYYMMDD-HHMMSS-pre-release.sql`。

```bash
DB_NAME=$(sed -n 's/^DB_NAME=//p' /opt/tnr/.env); DB_USER=$(sed -n 's/^DB_USER=//p' /opt/tnr/.env)
OUT=/root/tnr-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$OUT"
mysqldump -u"$DB_USER" -p --single-transaction --routines --triggers \
  --default-character-set=utf8mb4 "$DB_NAME" > "$OUT/db-$DB_NAME.sql"
test -s "$OUT/db-$DB_NAME.sql" && grep -c '^CREATE TABLE' "$OUT/db-$DB_NAME.sql"   # 表数闸门，期望 33
tar czf "$OUT/opt-tnr-code.tar.gz" -C / opt/tnr --exclude=opt/tnr/media --exclude=opt/tnr/staticfiles
tar czf "$OUT/media.tar.gz" -C / opt/tnr/media
cp /opt/tnr/.env /etc/nginx/sites-available/tnr /etc/supervisor/conf.d/tnr.conf "$OUT/" && chmod 600 "$OUT"/*
```

两条实测踩过的坑（`deploy/README.txt:110`–`deploy/README.txt:132`）：库是 Docker 化 MariaDB 而宿主机是 Oracle `mysql-client 8.0` 时，`mysqldump` 会在导完第一张表后 `exit=2`（`COLUMN_STATISTICS` 不兼容），而输出文件已经写出来 —— 坏备份 2KB/1 表 vs 正常 220KB/33 表，必须改用容器内同版本的 `docker exec 1Panel-mariadb-LQ69 mariadb-dump -utnr -p<PASS> --single-transaction --routines --triggers tnr_system > backup.sql`（`deploy/README.txt:122`–`deploy/README.txt:124`）。`tar` 同理：`-C /opt opt/tnr` 是错的（等于 `/opt/opt/tnr`），会 `exit=2` 产出 45 字节空包；正确写法 `-C / opt/tnr` 并做大小闸门（正常 8MB 量级）。sqlite 开发库：`cp db.sqlite3 db.sqlite3.bak-$(date +%Y%m%d-%H%M%S)-before-<动作>`。

### 3.5 恢复

```bash
mysql -u"$DB_USER" -p -e "DROP DATABASE IF EXISTS $DB_NAME; CREATE DATABASE $DB_NAME CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
gunzip < "$OUT/db-$DB_NAME.sql" | mysql -u"$DB_USER" -p "$DB_NAME"
cd /opt/tnr && venv/bin/python manage.py showmigrations && venv/bin/python manage.py check_data_integrity
tar xzf "$OUT/media.tar.gz" -C / && chown -R www-data:www-data /opt/tnr/media && sudo supervisorctl restart tnr-gunicorn tnr-qworker
```

### 3.6 大表加索引：要 online-DDL 窗口

`MaterialTransaction` 增长最快（采购/下发/签收/消耗/异动/出库各一行，跨批次下发还按 FEFO 拆成多行，`business/views_material.py:489`–`business/views_material.py:490`）。最近一次给它加索引的是 `business/migrations/0035_alter_adoption_ledger_no_alter_capture_ledger_no_and_more.py`，对 8 张单据表的 `ledger_no` 统一 `db_index=True`（动机：按号查等于全表扫，`business/models.py:109`–`business/models.py:113`）。

- 低峰执行并显式要 online 语义：`ALTER TABLE business_materialtransaction ADD INDEX ledger_no_idx (ledger_no), ALGORITHM=INPLACE, LOCK=NONE;`
- 先 `SELECT VERSION();` 确认支持（MySQL 8 与 MariaDB 行为不同，容器化部署要确认容器内版本）。
- Django 自己发出的 DDL 不带 `ALGORITHM`/`LOCK` 子句。若迁移在大表上锁写，先由 DBA 手工做完 online-DDL，再让 `migrate` 补 `django_migrations` 记账 —— 这步要人工判断，列入第 12 节。
- 纯索引迁移（`0035`）可单独回退（`DROP INDEX`）；与数据回填（`0020`/`0023`/`0024`/`0029`）混在同一批时必须拆成两个变更窗口。迁移前一律先做 §3.4 备份。

## 4. 静态资源与媒体（运维侧要点；细节见 `MEDIA_STORAGE.md`）

### 4.1 collectstatic

`STATIC_ROOT = BASE_DIR/'staticfiles'`（`tnr_system/settings.py:316`）、源目录 `static/`（:315）、`STATIC_URL = '/static/'`（:314）；生产由 nginx 直接吐 `/opt/tnr/staticfiles/`，带 `expires 30d` + `Cache-Control: public, immutable`（`deploy/nginx.conf:12`–`deploy/nginx.conf:16`）。

<strong>`collectstatic` 必须以 root 跑，跑完把属主交回运行账号</strong>：`staticfiles/` 属主是 `www-data`，以 `ubuntu` 跑会因删不掉旧文件而 `PermissionError`；危险处在于它不是彻底失败 —— 页面里的 `?v=` 已是新版本号、nginx 仍返回旧 JS，浏览器按新 URL 缓存旧内容，表现为「改了代码、部署了、功能没生效」（完整说明与判据 `DEPLOY.md:941`–`DEPLOY.md:979`）。

```bash
/opt/tnr/venv/bin/python /opt/tnr/manage.py collectstatic --noinput
ps -o user= -C gunicorn                                  # 生产账号以此为准（现网曾是 ubuntu，DEPLOY.md:10）
chown -R www-data:www-data /opt/tnr/staticfiles; stat -c '%y' /opt/tnr/staticfiles/js/tnr-api.js   # 产物时间 = 本次部署时间
```

### 4.2 目录权限

姿态由 `deploy.sh:199`–`deploy.sh:211` 定：代码 `root:www-data` 只读（应用被攻破也改不了自己的代码），`media/` + `staticfiles/` 给运行账号可写，`.env` 保持 `root:www-data` + `640`（`deploy.sh:138`–`deploy.sh:139`）。属主永远按<strong>进程实际身份</strong>定：现网 gunicorn 以 `ubuntu` 跑时，「`www-data` 不可写 `media/`」才是正常的（`DEPLOY.md:43`）。历史上漏掉这一步的后果：上传全线失败而服务状态看着一切正常（`deploy.sh:200`–`deploy.sh:202`）。

### 4.3 备份范围与存储后端

进备份：`media/` 全量、`.env`、nginx/supervisor 配置、代码包。不进备份：`db.sqlite3`（`.gitignore:5`）、`staticfiles/`（可重建）、`__pycache__/`。媒体备份：`tar czf media_backup_$(date +%Y%m%d).tar.gz /opt/tnr/media/`（`deploy/README.txt:128`）。

后端切换只看 `TNR_MEDIA_STORAGE`（`core/storage.py:86`、`tnr_system/settings.py:336`、`.env.example:61`），业务代码/模板零改动；`TNR_MEDIA_STORAGE` 与 `TNR_MEDIA_LOCATION` <strong>设了就别再改</strong>，改值等于让库里的 key 指不到文件（硬约束 `MEDIA_STORAGE.md:29`）。切到对象存储后 Django 不再挂 `/media/` 路由（`tnr_system/urls.py:43`–`tnr_system/urls.py:56`），此时图片 404 是「没搬干净」的真实信号，不要靠挂回路由掩盖。

### 4.4 `media.bak-*` 与 `media_orphans_*` 是什么、能不能删

| 目录 | 来源 | 可否删 |
|---|---|---|
| `media.bak-photos-<时间戳>/` | 清空业务数据前的 `media/` 整目录快照（用途写在忽略规则上，`.gitignore:13`–`.gitignore:14`） | 确认那次数据清空不需回退后可删；删前按单号抽样几张图 |
| `media_orphans_<时间戳>/` | `manage.py media_clean_orphans --apply` 移送的「库里已不引用」文件，含 `MANIFEST.txt` + `README.txt` | 可删，走 `--purge --backup-dir <目录> --yes` 双闸门（`core/management/commands/media_clean_orphans.py:9`–`core/management/commands/media_clean_orphans.py:14`）；逻辑删除记录的引用照样算有效，照片仍是审计凭证（`MEDIA_STORAGE.md:188`） |
| 任何落在 `media/` 里面的备份 | —— | 不允许：会被下次扫描当成新孤儿、被 `media_migrate` 一起搬上对象存储、掩盖「迁移没搬干净」，代码里硬校验直接报错（`MEDIA_STORAGE.md:185`–`MEDIA_STORAGE.md:191`） |

清理被中断后重跑同一条命令即可（幂等），但默认备份目录名带时间戳、续跑会新建目录把文件分到两处，而第一个目录的 `MANIFEST.txt` 还没落盘。补救三步（`rsync -a --ignore-existing` 合并 → 核对 → `--rebuild-manifest`）见 `MEDIA_STORAGE.md:197`–`MEDIA_STORAGE.md:218`；不重建清单的话，多出来的文件会永久失去溯源记录且不报错。

## 5. 定时任务、后台任务与管理命令

### 5.1 用的是 django-q2，不是 celery；仓库内没有 crontab

依赖 `django-q2==1.6.2`（`requirements.txt:6`、`start_dev.sh:57`），app 已装（`tnr_system/settings.py:142`）。`Q_CLUSTER`：`workers 2`、`timeout 60`、`retry 120`、`queue_limit 500`、`bulk 1`、`recycle 500`、`orm default`（`tnr_system/settings.py:397`–`tnr_system/settings.py:406`）—— ORM broker，<strong>不需要 Redis</strong>。worker 由 supervisor 拉起：`manage.py qcluster`（`deploy.sh:226`–`deploy.sh:233`）。`deploy/README.txt:134` 只是「建议配置 crontab 定时备份」，仓库内没有任何 crontab / systemd timer 文件 —— 是否已落地见第 12 节。

### 5.2 周期任务恰好两条，由数据迁移注册

| 名称 | 函数 | 频率 | 出处 |
|---|---|---|---|
| 诊疗完成5天后自动转待领养 | `business.tasks.auto_promote_to_adoptable` | 每天 03:00（Asia/Shanghai），`schedule_type='D'`、`repeats=-1` | `business/migrations/0016_register_auto_promote_schedule.py:36`–:64、业务体 `business/tasks.py:14` |
| 每月回访打卡提醒 | `business.tasks.send_checkin_reminders` | 每月 1 日 09:00，`schedule_type='M'` | `business/migrations/0017_register_checkin_reminder_schedule.py:49`–:60、业务体 `business/tasks.py:68` |

```bash
cd /opt/tnr && venv/bin/python manage.py shell -c "from django_q.models import Schedule; [print(s.pk, s.name, s.func, s.schedule_type, s.repeats, s.next_run) for s in Schedule.objects.all()]"
```

判据：<strong>只有一行就是缺陷</strong>；月任务 `next_run` 的<strong>日号必须是 1</strong>（django-q2 的 MONTHLY 按日号重复，照抄「今天+1 个月」会固定在部署当天，`business/migrations/0017_register_checkin_reminder_schedule.py:17`–`business/migrations/0017_register_checkin_reminder_schedule.py:30`、`DEPLOY.md:880`–`DEPLOY.md:904`）。`auto_promote_to_adoptable` 自第四十七轮起只是存量兜底，判据必须避开已被出院流程接管的动物（`business/tasks.py:20`–`business/tasks.py:26`）。投递真实任务前先数超期记录，否则探针会真的改生产数据；验通路用 `async_task('time.sleep', 0)`，验完必须清账（`DEPLOY.md:336`–`DEPLOY.md:343`）。`supervisorctl status` 的 `RUNNING` 只证明进程活着，证明不了能消费（`DEPLOY.md:292`–`DEPLOY.md:345`）。`business/tasks.py` 是普通函数（非脚本、无常驻入口），只能由 django-q2 的 `Schedule` 或 `async_task` 触发；`auto_promote_to_adoptable` 另有两条非周期路径，排障时容易误判：每进程启动后首次请求补偿一次（`business/apps.py:30`–`business/apps.py:40`），以及「诊疗完成」流程投递一次。手工补跑用 `manage.py promote_adoptable`（§5.3），回访提醒没有对应管理命令、只能等 `Schedule` 或手工调 `async_task('business.tasks.send_checkin_reminders')`（`business/tasks.py:68`）。

### 5.3 管理命令全清单

`core/management/commands/`：

| 命令 | 用途 | 出处 |
|---|---|---|
| `check_data_integrity` | 只读巡检 16 项一致性（账号/机构/单据的区县归属、库存对账、软删孤儿），有违规退出码 1；`--limit` 控明细条数 | `core/management/commands/check_data_integrity.py:62`–`check_data_integrity.py:84` |
| `sync_operator_district` | 把「账号区县 ≠ 机构区县」改为与机构一致；默认预演，`--apply` 才写库 | `core/management/commands/sync_operator_district.py:24`–`sync_operator_district.py:28` |
| `media_clean_orphans` | 孤儿媒体清理：默认干跑 → `--apply` 移送 → `--purge --yes` 真删；`--rebuild-manifest` 重建清单 | `core/management/commands/media_clean_orphans.py:3`–`media_clean_orphans.py:14` |
| `media_migrate` | 媒体在本地 ↔ 对象存储间搬家：`--apply` / `--verify` / `--prune --yes` / `--reverse` | `core/management/commands/media_migrate.py:3`–`media_migrate.py:20` |

`business/management/commands/`：

| 命令 | 用途 | 出处 |
|---|---|---|
| `seed_data` | 演示种子数据，幂等（区县/机构按稳定编号、业务记录按单号匹配，不重置口令）；`--flush` 先清空 | `business/management/commands/seed_data.py:1`–:7、`start_dev.sh:19`–`start_dev.sh:22` |
| `promote_adoptable` | 手动触发「完成 5 天转待领养」，`--force` 跳过 5 天限制用于补偿 | `business/management/commands/promote_adoptable.py:1`–:8 |
| `reconcile_material_stock` | 按流水重算机构库存：默认 dry-run，`--apply` 才写库；绝不动 `MaterialTransaction` 一行 | `business/management/commands/reconcile_material_stock.py:11`–:31 |
| `refresh_demo_material_expiry` | 订正演示物料的绝对有效期（存量库跑一次；非演示物料只报告不改） | `business/management/commands/refresh_demo_material_expiry.py:20`–:27 |

`accounts/management/commands/`：`ensure_superuser`（`accounts/management/commands/ensure_superuser.py:1`–:15）、`ensure_platform_admin`（`accounts/management/commands/ensure_platform_admin.py:1`–:20）。

### 5.4 巡检节奏与报问题的处置

`check_data_integrity` <strong>每天跑</strong> —— 它只读，可在生产、只读副本、排查现场放心跑（这条性质也是它不带 `--fix` 的理由，`core/management/commands/sync_operator_district.py:6`–`sync_operator_district.py:10`）。处置对应关系：归属类 → `sync_operator_district`（先预演）；库存类 → `reconcile_material_stock`（先 dry-run）；演示数据类 → `refresh_demo_material_expiry`。「医院操作员未登记执业证号」是<strong>待补录提示、不计入退出码</strong>，把它算进退出码会让巡检天天红、最后没人看输出（`core/management/commands/check_data_integrity.py:81`–:84、:107–:112、:140–:148）。政府端「用户管理」只有新增与启停、没有编辑，所以归属问题在界面上没有出口，必须靠命令行修（`core/management/commands/sync_operator_district.py:4`–:8）。

## 6. 必须盯的业务指标（SQL / 命令可直接粘贴）

基础设施指标（CPU、内存、5xx）走现成监控栈；本节只列<strong>业务口径</strong> —— 这些指标异常时页面照样 200、接口照样回 JSON，只有对账才发现（为什么需要巡检：`core/management/commands/check_data_integrity.py:1`–:13）。表名沿用 Django 默认（项目未设 `db_table`）。

### 6.1 库存与流水对账（最优先）

```sql
-- 命令版：venv/bin/python manage.py check_data_integrity --limit 50（只读）
-- 口径：quantity == opening_quantity + 按机构归属的流水合计
-- 符号规则 business/services_stock.py:69、:70、:98；判据 check_data_integrity.py:318
SELECT i.name AS org, m.name AS item, s.quantity AS actual,
       s.opening_quantity + IFNULL(l.net,0) AS expected, s.quantity - (s.opening_quantity + IFNULL(l.net,0)) AS drift
FROM business_materialstock s
JOIN core_institution i ON i.id = s.institution_id JOIN business_material m ON m.id = s.material_id
LEFT JOIN (SELECT material_id, institution_id,
                  SUM(CASE WHEN type IN ('purchase','receive') THEN quantity ELSE -quantity END) AS net
           FROM business_materialtransaction WHERE institution_id IS NOT NULL
           GROUP BY material_id, institution_id) l
  ON l.material_id = s.material_id AND l.institution_id = s.institution_id
WHERE s.quantity <> s.opening_quantity + IFNULL(l.net,0);
```

处置：`reconcile_material_stock` 先 dry-run 再 `--apply`；<strong>不要改流水去迁就库存</strong>，那会抹掉审计痕迹（`business/management/commands/reconcile_material_stock.py:17`–:19）。

### 6.2 可用芯片数与库存数对账

芯片逐号一行，四态 `available`/`in_transit`/`used`/`void`（`business/models.py:784`–`business/models.py:789`）；`in_transit` 与 `void` 存在的意义就是让「芯片数」与「库存数」不在在途期/报废后错开（`business/models.py:766`–`business/models.py:775`）。

```sql
-- 存量行 institution/material 为 NULL（归属信息已丢，不回填）必须先排除：business/models.py:777
SELECT i.name AS org, m.name AS item,
       SUM(CASE WHEN c.status='available' THEN 1 ELSE 0 END) AS chips_available, MAX(IFNULL(s.quantity,0)) AS stock
FROM business_chip c
JOIN core_institution i ON i.id = c.institution_id JOIN business_material m ON m.id = c.material_id
LEFT JOIN business_materialstock s ON s.institution_id = c.institution_id AND s.material_id = c.material_id
WHERE c.institution_id IS NOT NULL AND c.material_id IS NOT NULL
GROUP BY c.institution_id, c.material_id HAVING chips_available <> stock;

-- 作废/在途芯片不得已植入动物（期望 0 行；CAS 判据 business/services.py:778）
SELECT number, status, pet_id FROM business_chip WHERE status IN ('void','in_transit') AND pet_id IS NOT NULL;
```

### 6.3 待签收单据积压

```sql
-- 转运单 pending/partial 超 3 天（状态枚举 business/models.py:169–:181）
SELECT id, ledger_no, status, pet_count, created_at, from_shelter_name, to_hospital_name
FROM business_transfer WHERE status IN ('pending','partial')
AND created_at < UTC_TIMESTAMP() - INTERVAL 3 DAY ORDER BY created_at;

-- 物料下发单有 DIS 无对应 RCV（关联字段 ref_no：business/models.py:585、business/views_material.py:546）
SELECT d.ledger_no, MIN(d.date) AS dispatched_on, i.name AS receiver, SUM(d.quantity) AS qty
FROM business_materialtransaction d JOIN core_institution i ON i.id = d.hospital_id
WHERE d.type='dispatch' AND d.date < UTC_DATE() - INTERVAL 3
  AND NOT EXISTS (SELECT 1 FROM business_materialtransaction r WHERE r.type='receive' AND r.ref_no = d.ledger_no)
GROUP BY d.ledger_no, i.name;
```

处置：联系接收方在界面里签收或驳回；<strong>不要靠改库收尾</strong>。物料签收不因过期而拦（否则过期物料永远卡在待签收、捕捉点已扣库存而医院加不上，`business/services.py:1891`–`business/services.py:1893`）。

### 6.4 绝育病历缺失率

```sql
-- 六栏判据与栏名：business/services_treatment.py:117–:124、:147；麻醉枚举 :162
SELECT COUNT(*) AS steril_total, SUM(CASE WHEN sterilization_surgery_date IS NULL OR sterilization_surgeon=''
       OR sterilization_diagnosis='' OR sterilization_procedure='' OR sterilization_recovery=''
       OR sterilization_anesthesia NOT IN ('吸入麻醉','注射麻醉','复合麻醉') THEN 1 ELSE 0 END) AS missing_some
FROM business_treatment WHERE items_sterilization = 1;
```

历史行的旧六列可能本就为空（新流程明细在子表 `business_treatmentitem`，`business/models.py:248`–`business/models.py:251`），解读边界见第 12 节第 11 条。

### 6.5 过期批次仍有余量 / 过期物料仍在被消耗

有效期判据已是<strong>批次级</strong>（主表 `expiry_date` 只是最近一次采购的快照，会两头判错，`business/services.py:1873`–`business/services.py:1880`；批次表 `business/models.py:669`–`business/models.py:698`）。

```sql
SELECT i.name AS org, m.name AS item, b.batch_no, b.expiry_date, b.quantity
FROM business_materialbatch b JOIN business_material m ON m.id=b.material_id JOIN core_institution i ON i.id=b.institution_id
WHERE b.expiry_date IS NOT NULL AND b.quantity>0 AND b.expiry_date < UTC_DATE() ORDER BY b.expiry_date;
-- 临期 30 天预警（阈值真源 business/services_stock.py:334）：AND b.expiry_date BETWEEN UTC_DATE() AND UTC_DATE() + INTERVAL 30 DAY
-- 事后核查：消耗日期落在所扣批次效期之后（应当 0 行）
SELECT t.id, t.ledger_no, t.date, t.material_name, t.batch_no, t.quantity, b.expiry_date
FROM business_materialtransaction t JOIN business_materialbatch b
  ON b.material_id=t.material_id AND b.institution_id=t.institution_id AND b.batch_no=t.batch_no
WHERE t.type='consume' AND t.date > b.expiry_date;
```

处置：走「库存异动」申报过期报废 —— 异动是过期物料唯一的正规出口，系统不拦它（`business/services.py:1885`–`business/services.py:1890`）。

### 6.6 单据号重号与发号水位

```sql
-- 重号：7 张单据表（MaterialTransaction 按设计有同号多行，不参与本判据，views_material.py:489）
SELECT tbl, ledger_no, c FROM (
  SELECT 'capture' tbl, ledger_no, COUNT(*) c FROM business_capture      WHERE ledger_no<>'' GROUP BY ledger_no HAVING c>1
  UNION ALL SELECT 'transfer',  ledger_no, COUNT(*) FROM business_transfer  WHERE ledger_no<>'' GROUP BY ledger_no HAVING c>1
  UNION ALL SELECT 'treatment', ledger_no, COUNT(*) FROM business_treatment WHERE ledger_no<>'' GROUP BY ledger_no HAVING c>1
  UNION ALL SELECT 'release',   ledger_no, COUNT(*) FROM business_release   WHERE ledger_no<>'' GROUP BY ledger_no HAVING c>1
  UNION ALL SELECT 'adoption',  ledger_no, COUNT(*) FROM business_adoption  WHERE ledger_no<>'' GROUP BY ledger_no HAVING c>1
  UNION ALL SELECT 'ownerreturn', ledger_no, COUNT(*) FROM business_ownerreturn WHERE ledger_no<>'' GROUP BY ledger_no HAVING c>1
  UNION ALL SELECT 'euthanasia', ledger_no, COUNT(*) FROM business_euthanasia  WHERE ledger_no<>'' GROUP BY ledger_no HAVING c>1
) d;

-- 发号水位（唯一键 uniq_docseq_prefix_year，business/models.py:1147）
SELECT prefix, year, last_no FROM business_documentsequence ORDER BY prefix, year;
```

### 6.7 队列积压与失败

```bash
cd /opt/tnr && venv/bin/python manage.py shell -c "from django_q.models import OrmQ, Failure; print('积压:', OrmQ.objects.count(), '| 上限 500 (settings.py:403)'); [print(f.time_stamp, f.func) for f in Failure.objects.order_by('-time_stamp')[:10]]"
```

告警阈值建议：`OrmQ > 50` 持续 10 分钟；`Failure` 当日新增 > 0；`Schedule` 行数 != 2；`check_data_integrity` 退出码 != 0；`gunicorn-error.log` 自最后一次重启后出现非 `DisallowedHost` 的 Traceback —— 计数必须按行号划窗口，对整份日志 `grep -c` 是错的判据（`DEPLOY.md:389`–`DEPLOY.md:418`）。

## 7. 故障排查 Runbook（按症状）

日志位置：`/var/log/tnr/gunicorn.log`、`gunicorn-error.log`、`qworker.log`、`qworker-error.log`（`deploy.sh:222`–`deploy.sh:233`）；反代 `/var/log/nginx/access.log`、`error.log`（用法 `DEPLOY.md:978`）。错误日志取「本次重启之后」的窗口：`LOG=/var/log/tnr/gunicorn-error.log; L=$(grep -n 'Booting worker' $LOG | tail -1 | cut -d: -f1); sed -n "$((L+1)),\$p" $LOG`（`DEPLOY.md:405`–`DEPLOY.md:410`）。

| 症状 | 先看哪个日志 | 定位命令 | 最可能原因 → 处置 |
|---|---|---|---|
| 5xx / 502 | `gunicorn-error.log`（划窗口）+ `supervisorctl status` | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login/`；`manage.py check` | worker 被 `timeout 60` 杀（`gunicorn.conf.py:6`）；`.env` 缺 `SECRET_KEY`/`ALLOWED_HOSTS` 起不来（`tnr_system/settings.py:103`、`tnr_system/settings.py:117`）；DB 断连 → 重启对应 program，DB 按 §3.5。`/api/` 的 500 被 `handler500` 转成 JSON 且<strong>不碰数据库</strong>（`tnr_system/urls.py:154`–`tnr_system/urls.py:184`），堆栈只在日志里 |
| 登录 403 | `gunicorn.log` 里这条请求的状态码 | `manage.py shell -c "from accounts.models import User; u=User.objects.get(username='X'); print(u.role,u.is_active,u.is_superuser,u.district_id,u.institution_id)"` | 角色与端不符（门户只认 `ROLE_PORTAL_MAP`，`accounts/views.py:21`–`accounts/views.py:30`）；`role_required` 不看 `is_superuser`，超管进不去 `/platform/`（`accounts/management/commands/ensure_platform_admin.py:6`–:11）；HTTPS 打开后 CSRF 的 Origin 校验首次生效而 `CSRF_TRUSTED_ORIGINS` 为空（`tnr_system/settings.py:233`、`tnr_system/urls.py:110`–`tnr_system/urls.py:135`、`DEPLOY.md:696`）→ 改 `.env` 后重启 |
| 登录后跳回首页 | 页面顶部红色提示「无权访问该页面」 | 同上，再查 `core_auditlog` 是否落 `success=False` | 非 API 请求被角色拒绝时走 `messages.error + redirect('/')`（`accounts/decorators.py:48`–`accounts/decorators.py:52`）→ 改角色；或账号「没挂区县」→ 列表全空但写接口放行（`accounts/middleware.py:12`–`accounts/middleware.py:18`）→ 补区县 |
| 口令怎么试都不对 | 部署脚本尾部打印的凭据行 | `grep -E '^(ADMIN_PASSWORD|PLATFORM_ADMIN_PASSWORD)=' /opt/tnr/.env` | `.env` 有值就每次部署强制重置（`deploy.sh:162`–`deploy.sh:166`、`DEPLOY.md:847`）→ 用 `ADMIN_PASSWORD=<新值> manage.py ensure_superuser --username admin --reset-password`；不带 `--reset-password` 的 `ensure_*` 会静默跳过（`DEPLOY.md:31`） |
| 打印页空白或 500 | `gunicorn-error.log` 找 `No module named 'qrcode'` | `venv/bin/python -c "import qrcode; print('ok')"`；`manage.py shell -c "from business.services import DOC_PREFIXES; print(sorted(DOC_PREFIXES))"` | venv 缺 `qrcode`（打印页顶部就 import，`business/views_print.py:20`；`start_dev.sh:51`–`start_dev.sh:57` 记录过探测漏包导致打印崩）；角色不在放开名单（只有 shelter/hospital/gov_city/gov_district，`business/views_print.py:18`–:20）；跨区县取不到单据时<strong>与不存在同为 404</strong>（同处说明）；改了模板没重启（`tnr_system/settings.py:167`–:190）→ `pip install -r requirements.txt` + 重启 |
| 库存对不上 | `check_data_integrity` 的三条库存项（`check_data_integrity.py:76`–:78） | §6.1 的 SQL | 某条写路径漏了同事务的增量维护（`MaterialStock` 是流水的缓存，飘了不报错，`business/models.py:601`–:620）；或该区县有两个捕捉点、`shelter_stock` 里有分不下去的存量（预期，不是缺陷，`check_data_integrity.py:361`–:366）→ `reconcile_material_stock` |
| 单据编号重复 | §6.6 的重号 SQL（先排除 FEFO 拆批同号） | `SELECT prefix,year,last_no FROM business_documentsequence;` 对比各表 `MAX(ledger_no)` | 库外直接插单绕过取号；平台端改了前缀（改前缀后新号从 00001 重排，旧前缀不受影响，`business/services.py:689`–:691）→ 取号本身是行锁 + 唯一约束 + 3 次重试（`business/services.py:703`–:715），先查库外写入；历史重号人工订正并留审计记录 |
| 上传照片不显示 | `nginx error.log` + `gunicorn-error.log` 的 `PermissionError` | `ls -ld /opt/tnr/media /opt/tnr/media/photos`；`ps -o user= -C gunicorn`；`curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/media/<库里的 key>` | `media/` 属主不是进程身份（`deploy.sh:200`–:209）；nginx alias 与实际目录不符（`deploy/nginx.conf:19`–:22）；对象存储模式下 `/media/` 不再挂载，404 是真信号（`tnr_system/urls.py:43`–:56）；被三道闸门拒：nginx `client_max_body_size 20M`（`deploy/nginx.conf:32`）+ `DATA_UPLOAD_MAX_NUMBER_FILES 110` / `FIELDS 2000`（`tnr_system/settings.py:372`–:373），跑满 100 只需把 nginx 调到 64M（`tnr_system/settings.py:371`）→ 改属主后重启，或 `media_migrate --verify` 确认搬干净 |
| 提交「点了没反应」 | `nginx access.log` 的状态码与响应大小 | `curl -s -o /dev/null -w '%{http_code} %{content_type}\n' -X POST http://127.0.0.1/api/business/captures/create/` | 响应体是 HTML 而前端 `res.json()` 抛错（413/502/400 报错页，`DEPLOY.md:1002`）→ 补 nginx `error_page 413`（`DEPLOY.md:560`–`DEPLOY.md:590`） |
| 移动端侧栏遮挡正文 | 先判「是不是旧 CSS」：比对 `templates/base.html:16` 的 `?v=` 与产物时间 | `curl -s http://127.0.0.1/static/css/tnr-traditional.css \| grep -c sidebar-overlay` | 规则本身：`≤768px` 时侧栏 `translateX(-100%)`、加 `.show` 才划出，遮罩默认 `display:none`（`static/css/tnr-traditional.css:1500`–:1517），主区 `margin-left` 归零（:1519），抽屉收窄 `92vw`（:1836）。所以「侧栏压住正文」只有两种成因：媒体查询没生效（旧 CSS 被缓存 → 升一位 `?v=` 重跑 `collectstatic`，`DEPLOY.md:974`–:979）；或某页面上级容器自带 `transform` 造成 fixed 祖先错位（页面缺陷，提给开发，运维<strong>不要</strong>手改 `staticfiles/` 产物） |
| 定时任务没跑 / 待领养不动 | `qworker-error.log` | `manage.py shell -c "from django_q.models import Schedule,OrmQ; print(Schedule.objects.count(), OrmQ.objects.count())"` | `Schedule` 应为 2，为 0 说明 `0016`/`0017` 没跑到 → `showmigrations business` 后补 `migrate`；临时补业务 `manage.py promote_adoptable`；按 `DEPLOY.md:292` 投 `time.sleep` 探针验「真投真跑」后清账 |

## 8. 变更与发布流程

### 8.1 `deploy.sh` 实际做的 8 步

| 步 | 动作 | 出处 |
|---|---|---|
| 1/8 | apt 装 `python3(-venv/-dev)`、`build-essential`、`libmysqlclient-dev`、`nginx`、`supervisor`、`mysql-server`、`pkg-config` | `deploy.sh:66`–`deploy.sh:69` |
| 2/8 | 建 `/opt/tnr`、`cp -r .`，再删 `.git`、`.venv`、`db.sqlite3`、`server.log`、`.workbuddy-ai`、`gui-test-screenshots` | `deploy.sh:72`–`deploy.sh:83` |
| 3/8 | `python3 -m venv venv` + `pip install -r requirements.txt` | `deploy.sh:87`–`deploy.sh:91` |
| 4/8 | 建库（utf8mb4）、建账号 `tnr`、授权、`FLUSH PRIVILEGES` | `deploy.sh:94`–`deploy.sh:98` |
| 5/8 | <strong>覆盖写 `.env`</strong>（`DEBUG=False`、新 `SECRET_KEY`、`ALLOWED_HOSTS`、`DB_*`、`HTTPS`、`CSRF_TRUSTED_ORIGINS`、两个口令、`TNR_AMAP_KEY`），再 `chown root:www-data` + `chmod 640` | `deploy.sh:101`–`deploy.sh:139` |
| 6/8 | `migrate --noinput` → `collectstatic --noinput` → `seed_data` → `ensure_superuser` → `ensure_platform_admin` | `deploy.sh:142`–`deploy.sh:189` |
| 6.5/8 | 目录属主与权限 + 建 `/var/log/tnr` | `deploy.sh:199`–`deploy.sh:211` |
| 7/8 | 写 supervisor 配置（gunicorn + qcluster）→ `reread`/`update`/`restart` | `deploy.sh:214`–`deploy.sh:238` |
| 8/8 | 写 nginx 站点 → 软链 `sites-enabled` → 删默认站 → `nginx -t` → `systemctl reload nginx` | `deploy.sh:241`–`deploy.sh:275` |

脚本结尾还会提示两件必须手工完成的事：填 `TNR_AMAP_KEY`（否则捕捉登记的「定位」不可用）与配 HTTPS（`HTTPS=on` + `CSRF_TRUSTED_ORIGINS`，`deploy.sh:306`–`deploy.sh:313`）。

### 8.2 发布前检查清单

```bash
cd /opt/tnr && source venv/bin/activate
python manage.py test                              # 用项目自带 runner（测试会隔离 MEDIA_ROOT，settings.py:346）
python manage.py test core.tests core.tests_gov_readonly core.tests_frontend_consistency business.tests.test_migrations business.tests.test_schedule_registration
python manage.py test core.tests.AllApiRoutesContractTest core.tests_session_expiry supervision.tests_notice   # 依次验：/api/ 一律 JSON（DEPLOY.md:761）、401 先提示再跳登录（DEPLOY.md:839）、公告收件人推导（DEPLOY.md:934）
python manage.py check --deploy && python manage.py makemigrations --check --dry-run && python manage.py check_data_integrity   # 最后一条退出码须为 0
# 然后做一次 §3.4 备份，并检查备份的「表数 = 33」闸门
```

发布包用 `git archive`，天然只含已提交内容：`git archive --format=tar.gz --prefix=tnr/ HEAD -o /tmp/tnr-release.tar.gz`（`DEPLOY.md:36`）。

### 8.3 静态资源缓存串 `?v=` 要同步的位置

当前值 `20261019a`，全仓库 <strong>7 处 / 6 个文件</strong>，改哪个都要一次改齐（漏一个就是新旧 JS 混跑）：

| 文件:行号 | 资源 |
|---|---|
| `templates/base.html:16` | `css/tnr-traditional.css` |
| `templates/base.html:76` | `js/tnr-common.js` |
| `templates/portal/platform/portal.html:47`、`templates/portal/hospital/portal.html:298`、`templates/portal/shelter/portal.html:160`、`templates/portal/gov/portal.html:67`、`templates/portal/adopter/portal.html:702` | `js/tnr-api.js` |

`templates/login.html` 与 `templates/print/*.html` 不含 `?v=`。命名规则 `YYYYMMDD` + 当日序号字母。窗口期内已有客户端按新 URL 缓存过旧内容时必须再升一位，判断方式是读访问日志而不是猜：`grep -c 'v=20261019a' /var/log/nginx/access.log`，为 0 才不用再升（`DEPLOY.md:974`–`DEPLOY.md:979`）。nginx 对 `/static/` 下了 `immutable` + `expires 30d`（`deploy/nginx.conf:14`–`deploy/nginx.conf:15`），所以<strong>不带新 `?v=` 的静态改动等于没发布</strong>。

### 8.4 发布步骤（现网手工流程，等价于 `DEPLOY.md:26`–`DEPLOY.md:40`）

```bash
# 0. §3.4 备份 → 1. 本地 git archive → 2. 上传并在 /opt/tnr 解包覆盖
grep -E '^(SECRET_KEY|DB_PASSWORD)=' /opt/tnr/.env          # 3. 确认 .env 没被覆盖，只补新增变量
cd /opt/tnr && source venv/bin/activate                      # 4. 迁移 → 5. 静态 → 6. 属主 → 7. 重启
python manage.py migrate --noinput && python manage.py collectstatic --noinput && chown -R www-data:www-data /opt/tnr/staticfiles
sudo supervisorctl restart tnr-gunicorn tnr-qworker && python manage.py check_data_integrity   # 8. 验证
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: 100.99.98.71' http://127.0.0.1/
# 9. 过一遍 DEPLOY.md 第五节自检清单（尤其「照片上传经反代取回」与「/api/ 响应形状」两项）
```

### 8.5 回滚

1. 代码：解包上一个发布包，`?v=` 同时回退到那一版模板里的值，否则浏览器拿到新 URL + 旧文件。
2. 数据库：先判断本次迁移是否可逆（§3.3 清单）。`migrate <app> <上一条>` 只对可逆迁移有意义；碰到 `0007`/`0010`/`0011`/`0012`/`0023`/`0024` 时不要指望回退，只能 `mysql < 备份.sql`（§3.5）。单据号相关迁移一旦回退，纸面归档件与库里的号会永久不一致 —— 属合规事故，必须走备份恢复。
3. 只加索引的迁移（`0035`）可以只回滚代码、迁移留在库里，多余索引不影响正确性。
4. 回滚后固定四项：`check_data_integrity`、登录一个真实账号、`Schedule.objects.count() == 2`、最后一次重启之后的 `gunicorn-error.log` 无新 Traceback。

## 9. 账号与权限

### 9.1 六类角色与各自能进的端

角色定义 `accounts/models.py:6`–`accounts/models.py:17`；入口映射 `accounts/views.py:21`–`accounts/views.py:30`。

| 角色 | 门户 | 能做什么 | 归属约束 |
|---|---|---|---|
| `platform_admin` 平台管理员 | `/platform/` | 全局主数据维护：区县、机构、账号权限、单据编号规则、公告发布、操作日志 | 不挂区县，可见范围恒为全部（`accounts/models.py:7`–:10） |
| `gov_city` 市级政府 | `/gov/` | 全市<strong>查看</strong>业务与监管统计；管理市级/区级管理员与操作员账号 | 必须挂市级 `is_city=True`（`supervision/views.py:612`） |
| `gov_district` 区级政府 | `/gov/` | 本区县<strong>查看</strong>；只能建/管 `shelter`、`hospital` 两类操作员 | 必须挂具体区县（`business/services.py:1715`–:1719） |
| `shelter` 捕捉点操作员 | `/shelter/` | 捕捉登记、转运下发、领回、放归、物料采购/下发/签收/异动/出库、领养登记 | 可挂市级或区县（`supervision/views.py:614`） |
| `hospital` 医院操作员 | `/hospital/` | 收治、诊疗三步流程（检查 / 医嘱处方 / 执行办结）、消耗物料、植入芯片、签收转运单 | 必须挂医院机构并登记执业证号（`supervision/views.py:616`–:619） |
| `adopter` 领养人 | `/adopter/` | 领养大厅申请、我的领养、回访打卡 | 由捕捉点在领养登记时自动创建，<strong>不在管理端建</strong>（`supervision/views.py:615`、`supervision/views.py:636`–:637） |

`/admin/` 只给 Django 超管（`tnr_system/urls.py:25`）；<strong>业务角色判定不看 `is_superuser`</strong>（`accounts/decorators.py:48`，`role_required` 只比对 `user.role`）—— 超管进不去 `/platform/` 是设计，不是故障。

### 9.2 政府端只读（口径与判据）

判据文件 `core/tests_gov_readonly.py`，四条闸门（`core/tests_gov_readonly.py:18`–:33）：显式可达清单 `GOV_API_ALLOWLIST`（:47，每一项都必须是查询）、按角色把清单里的路径 GET + POST 各打一遍并断言<strong>业务数据指纹一比特没变</strong>（方法无关：项目多数写接口根本不判 `request.method`，所以「禁止 POST」这种闸门本身就是漏的，:24–:27）、已关闭写路径对两个政府角色统一 403（:339–:363，清单 :103）、以及反向对照用例证明指纹抓得到一次真实的写。唯一例外是改自己的口令 `/api/me/password/`（`GOV_SELF_SERVICE_WRITES`，:97–:101）—— 封掉等于把监管账号永久锁死在初始口令上，账号自助不属于「监管代录业务数据」。

<strong>为什么这么定</strong>：本轮之前 25 个会改库的接口都还挂着政府角色、且没有任何判据（`core/tests_gov_readonly.py:3`–:8）。捕捉/诊疗/放归/无害化必须由经办方（捕捉点、医院）亲手登记并留下自己的操作员与签名快照；政府替他们录入会让单据上的经办人、责任链与审计日志断裂，监管同时变成被审计对象 —— <strong>政府代录业务数据会造成责任链断裂</strong>。所以新接口想放行政府端，必须先回答「这个 view 会 `.save()` 吗」，会的话要按「监管代录」单独论证，不能顺手加角色（`core/tests_gov_readonly.py:298`–:299、:317–:319）。运维落地含义：监管方提出「帮忙补录一条」应当拒绝并转给经办机构，不要临时开权限。

### 9.3 新建账号入口与管理边界

- 入口：平台端「账号权限」页 → `user_create`，装饰器 `@role_required('platform_admin')`（`supervision/views.py:605`–:607）。政府端的「用户管理」只有新增与启停、没有编辑（`core/management/commands/sync_operator_district.py:4`–:8），所以归属问题必须走命令行修。
- 谁能建谁：`MANAGEABLE_ROLES` 是服务端唯一真源（`business/services.py:1715`–:1719）。这条补的是「前端做了、后端没做」的洞 —— 实测区级管理员可直接 POST 建出一个全市可见的市级管理员（`business/services.py:1705`–:1713）。区级管理员只能管「代表对象落在本区县」的账号，判据取机构所在区县而不是账号自己的区县（`business/services.py:1729`–:1755）。
- 医院账号必须登记执业兽医师证号且全局唯一（`supervision/views.py:617`、`business/services.py:1626`、`business/services.py:1661`）；证号在落库时<strong>快照</strong>进诊疗单，账号后续变更不改历史单据（`accounts/models.py:23`–:29）。
- 停用用 `is_active` / `status`（`accounts/models.py:22`），不要删号；停用账号登录会给明确提示而不是误导性的「密码错误」（`accounts/views.py:49`–:54）。

### 9.4 初始口令策略与凭据声明

- 建号接口口令留空时回落默认值 `123456`，且只校验长度 ≥ 6（`supervision/views.py:630`–:632）。它<strong>没有</strong>调用 `AUTH_PASSWORD_VALIDATORS`（`tnr_system/settings.py:298`–:303），即「公共口令 / 纯数字」两条校验在建号路径上不生效 —— 这是当前实现的真实边界，交付时须向业务方说明并约定首登改密。
- 超管与平台管理员口令由 `.env` 的 `ADMIN_PASSWORD` / `PLATFORM_ADMIN_PASSWORD` 固定：有值就每次部署强制重置，留空则不改动、首次无启用账号时随机生成一次且只显示一次（`deploy.sh:48`–:59、`deploy.sh:157`–:166、`DEPLOY.md:847`–:878）。演示期固定为 `123456` 是业务方决策，<strong>上线时手工删号重建</strong>（`DEPLOY.md:21`）。
- 领养人账号由业务动作产生，不适用平台建号口令（`supervision/views.py:615`）。

<strong>凭据声明</strong>：本手册<strong>不包含任何可用的真实口令</strong>。生产口令、数据库口令、`SECRET_KEY`、高德 Key 一律由运维负责人在 `.env` 中自行设置并保存到公司的密钥管理渠道；`.env` 已列入忽略规则不入版本库（`.gitignore:8`、`deploy.sh:104`）。文中出现的 `123456` 是代码里的默认回落值与演示期约定（`supervision/views.py:630`、`DEPLOY.md:21`），不是生产口令。

## 10. 监管合规要点（留痕与可追溯）

### 10.1 单据编号规则

格式 `{类型码}{YY}{5位流水}`（如 `CAP2600001`）：全市统一按「类型码 + 年份」递增，事务内行锁取号、并发不重号，无横线（手机录入/播报友好），5 位流水对应年单量 ≤ 99999（`business/services.py:679`–:684）。类型码唯一清单，13 个：`CAP` 捕捉单、`TRF` 转运单、`RET` 领回单、`REL` 放归单、`ADP` 领养单、`EUT` 安乐死单、`TRE` 诊疗单、`PUR` 采购入库、`DIS` 下发、`RCV` 接收、`ADJ` 异动、`CON` 消耗记录、`OUT` 出库（`business/services.py:619`–:633）。新增类型必须同改三处：这张清单、产生点的 `generate_doc_no` 调用、打印规格 `DOC_SPECS`（`business/services.py:614`–:617、`business/views_print.py:370`–:395）。

前缀可被平台端「编号规则」页覆盖（`DOC_CONFIG_KEY` + `_configured_prefix`，`business/services.py:637`–:654），读取异常一律回退内置码、取号永远不因配置表问题而失败（`business/services.py:654`–:678）；改前缀后新号从 `00001` 重排、旧前缀号不受影响（`business/services.py:689`–:691）。未知类型码<strong>直接抛错、不生成怪号</strong>（`business/services.py:686`–:687、:695–:698）—— 报错就按提示补清单，不要凑一个前缀。旧式编号 `CAP-260925-0142` 只作历史兼容保留（`business/models.py:1137`–:1139）。计数器表 `DocumentSequence` 的唯一键 `uniq_docseq_prefix_year` 是并发安全的最后一道（`business/models.py:1129`–:1148）。

### 10.2 禁物理删除

统一口径：业务数据一律逻辑删除、行留在库里（`business/models.py:54`、`business/migrations/0026_chip_status_add_void.py:18`、`business/services_treatment.py:913`），落地形态是 `is_deleted` + `deleted_at` + `deleted_by`（`business/models.py:114`–:118）。芯片「作废」也只改状态为 `void`、不删行 —— 「这颗哪去了」本身是要能回答的问题（`business/models.py:772`–:775）；撤销/驳回同样保留「被撤过一次」这一事实（`business/services_treatment.py:913`）。
运维红线：<strong>不要在库里 `DELETE`</strong>；任何清理先与业务方确认并留备份。唯一允许回收磁盘的是媒体孤儿文件，而且必须走「移送 + 清单」路径（§4.4）。

### 10.3 审计留痕

`AuditLogMiddleware` 记录 `/api/` 下所有写操作，视图抛异常时补记一条失败日志再原样上抛（`tnr_system/settings.py:156`、`core/middleware.py:33`–:45）。每条留：操作人及姓名快照、角色、归属区县与区县名快照、模块、动作（新增/修改/删除）、对象类型与 ID、摘要、详情、请求方法、路径、成功标志、来源 IP（`core/models.py:85`–:106）；时间列有索引 `audit_time_idx` / `audit_dist_time_idx`（`core/models.py:112`–:114）。

<strong>区县口径取业务记录的归属区县，不是操作人的区县</strong>：现场两个捕捉点操作员挂在「全市（市级）」下，而他们的单据归属襄城/樊城，按操作人归属会让本区政府在自己的日志页里看不到本区操作（`core/audit.py:3`–:9）。审计写库全包在 try/except，永不成为业务请求的故障点（`core/audit.py:11`、`core/audit.py:126`）。姓名/证号/区县都是<strong>快照</strong>而非实时关联，账号变更不回改历史单据（`accounts/models.py:23`–:29）—— 所以绝不要「批量更新账号表来订正历史单据的显示」。

### 10.4 绝育手术病历六栏与补录时机

六栏（`business/services_treatment.py:117`–:124）：手术日期 `sterilization_surgery_date`、主刀医生 `sterilization_surgeon`、麻醉方式 `sterilization_anesthesia`、诊断 `sterilization_diagnosis`、手术过程 `sterilization_procedure`、恢复情况 `sterilization_recovery`；中文栏名映射见 :126。不新建平行字段的原因：打印页、监管统计、宠物档案时间轴、政府端/医院端列表都在读这六列，新建字段等于那几处一起印出空白且不报错（`business/services_treatment.py:113`–:116）。除手术日期外其余四栏必填（:164、:528–:536），单栏上限 500 字（:165）。

| 阶段 | 能否写病历 | 依据 |
|---|---|---|
| 计划（未诊断） | 可随医嘱一并提交；只填 1 栏会被拒 —— 半途病历会印成「做了手术、日期空着」的归档单 | `business/services_treatment.py:131`–:142、:427 |
| 执行 / 术后（已诊断、未办结） | <strong>这就是正规时机</strong>：走病历专用提交入口。`save_plan()` 在已诊断时一律拒绝，所以必须留这个口 | `business/services_treatment.py:558`–:568、落库 :548–:556 |
| 已办结（`disposed_at` 非空） | 拒绝：「本单已办结，不能再补录手术病历」 | `business/services_treatment.py:574`–:575 |

所以「病历忘了填」的处置窗口只在办结之前；办结后<strong>不要</strong>直接改库补字段，应走撤销执行 / 撤回诊断的业务流程（判据 `business/services_treatment.py:747`、:778、:924）由业务方决定。执行阶段的病历闸门看的是<strong>已落库的六列</strong>而不是前端草稿（:147–:160）。

### 10.5 麻醉方式枚举与必检项允许全空

麻醉方式三选一 `(吸入麻醉, 注射麻醉, 复合麻醉)`，服务端等值校验，不在枚举内直接报错（`business/services_treatment.py:162`、:539–:541），库里是 `CharField(max_length=50)`（`business/models.py:302`）。增删取值属业务口径变更 + 前后端同步（一致性用例 `core/tests_frontend_consistency.py:1687` 会比对后端枚举与前端映射），不要手工往库里塞新值。

传染病筛查按物种给清单（猫 FPV/FHV/FCV/狂犬/弓形虫，狗 CDV/CPV/CCV/狂犬），结果三态 `none` 未检 / `neg` 阴性 / `pos` 阳性（`business/services_treatment.py:45`–:61）。<strong>除体温外允许全空，服务端不加「必须检完才能办结」的硬门禁</strong>（业务 2026-10-19 确认，:55–:59、:341–:344）。向监管方解释的原话：TNR 现场是群体绝育、不做逐只隔离观察，加门禁的结果不是「都检了」而是「都填阴性」，<strong>假阴性会进入疫病监测统计，比空白更危险</strong>；`none` 是为此保留的诚实档位，且「未检」与「阴性」必须分开存、不能压成布尔（:59–:60）。唯一卡死的是体温：数值与判定必填且必须自洽（发烧线 39.5℃，:174、:333–:350）。运维含义：监管若以「检查项空白率高」提整改，不要改代码自动填阴性、也不要批量补数据，应作为「未检率」透明呈现。

## 11. 日常巡检

### 11.1 每天（开工 5 分钟）

```bash
sudo supervisorctl status
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: 100.99.98.71' http://127.0.0.1/
cd /opt/tnr && venv/bin/python manage.py check_data_integrity        # 退出码 0
venv/bin/python manage.py shell -c "from django_q.models import Schedule, OrmQ, Failure; print('schedule=',Schedule.objects.count(),'积压=',OrmQ.objects.count(),'失败=',Failure.objects.count())"
LOG=/var/log/tnr/gunicorn-error.log; L=$(grep -n 'Booting worker' $LOG|tail -1|cut -d: -f1); sed -n "$((L+1)),\$p" $LOG|grep -c Traceback
```

判据：`Schedule` 必须是 2；Traceback 只看最后一次重启之后、且 `DisallowedHost` 不算缺陷（公网必然被扫，`DEPLOY.md:389`–`DEPLOY.md:418`）；巡检非 0 按 §5.4 分类处置，<strong>不要为了让它变绿而直接改库</strong>。顺带跑 §6.3 待签收与 §6.5 过期批次两条 SQL，结果转工单给业务方 —— 运维不代替业务签收。

### 11.2 每周

- 容量：`df -h`；`du -sh /opt/tnr/media /opt/tnr/staticfiles`；`SELECT COUNT(*) FROM business_materialtransaction;`（为 §3.6 的 DDL 窗口做预判）。
- 备份有效性闸门：最近的 `db-*.sql` 必须同时「存在、非空、`CREATE TABLE` 数 = 33」（只看文件存在会把 2KB/1 表的坏备份当成功，`deploy/README.txt:116`–:120）；代码包正常量级 8MB（`deploy/README.txt:130`–:132）。
- 日志：`ls -lh /var/log/tnr/`；需要轮转时在 `/etc/logrotate.d/tnr` 配（仓库内暂无该文件，见第 12 节）。
- 只读预演留档：`reconcile_material_stock`、`media_clean_orphans`、`refresh_demo_material_expiry`（都不带 `--apply`）各跑一次，与上周对比。
- 抽查 3 张已打印单据：纸面单据号 == 库里 `ledger_no`、关联单据链能打开（规格 `business/views_print.py:370`）。

### 11.3 每月

- <strong>恢复演练</strong>：最新备份还原到临时库 → `showmigrations` 对齐 → `check_data_integrity` 通过 → 登录一个真实账号跑通一条业务。没被成功恢复过的备份不算备份。
- 门禁复跑：`manage.py test` 全量 + `check --deploy`，新增告警记进台账。
- 证书：`certbot certificates`（默认 90 天到期，`deploy/README.txt:100`–:103）；续期必须带 `renew_hook`，否则 nginx 不会 reload（`DEPLOY.md:626`–`DEPLOY.md:634`）。
- 权限回归：`ps -o user= -C gunicorn` 与 `ls -ld /opt/tnr/media` 属主一致；`.env` 仍是 `640`（`deploy.sh:139`）。
- 账号治理：离职账号停用（`is_active=False`，不删，§9.3）；核对执业证号待补录清单（`check_data_integrity.py:140`–:153）；跑 `manage.py test core.tests_gov_readonly` 抽样确认监管端做不了任何写。
- 趋势上报：把 §6.4 病历缺失率、§6.3 待签收积压、§6.5 过期批次数记成时间序，连续两周上升的项提到业务例会。

## 12. 需人工确认清单（未能从代码/配置确认的点）

以下各项我<strong>没有</strong>在仓库里找到可核对的事实，交付时请运维负责人逐项补齐再签字：

1. `deploy/monitoring-prometheus-grafana.md` 不存在（任务描述提到）。指标接入 Prometheus/Grafana 属待办。
2. 生产 gunicorn 实际 worker 数：`gunicorn.conf.py:5` 是 `cpu_count()*2+1`，`DEPLOY.md:10` 记实测「6 worker + master」；服务器 CPU 数与有无命令行覆盖未确认。
3. 生产数据库形态与版本：`DEPLOY.md:13`、`deploy/README.txt:112`–:124 记的是 1Panel Docker 化 MariaDB（容器 `1Panel-mariadb-LQ69`），与 `deploy.sh` 的假设不一致。容器名、端口、`SELECT VERSION()` 需现场确认 —— 它直接决定 §3.4 用 `mysqldump` 还是 `mariadb-dump`、以及 §3.6 的 `ALGORITHM=INPLACE, LOCK=NONE` 是否被支持。
4. 备份是否已定时化：`deploy/README.txt:134` 只是建议，仓库内没有 crontab / systemd timer 文件。
5. 是否存在异地副本或对象存储桶；`TNR_MEDIA_STORAGE` 的现网值（`.env.example:61` 默认 `local`）。
6. 现网 `/opt/tnr/.env` 实际内容（`SECRET_KEY`、`DB_PASSWORD`、两个口令、`TNR_AMAP_KEY`、`HTTPS`、`CSRF_TRUSTED_ORIGINS`）：本手册只描述生成与继承规则，未读取现网值。
7. `media/`、`staticfiles/` 在生产上的实际属主（`DEPLOY.md:43` 说按进程身份定，需现场核对）。
8. nginx 日志路径：`deploy/nginx.conf` 未显式写 `access_log`/`error_log`，本手册按 Debian 默认引用（`DEPLOY.md:978` 也这么用），需确认现网未改。
9. logrotate 配置是否存在（`/etc/logrotate.d/tnr`）：`/var/log/tnr/*.log` 由 supervisor 直写，仓库内无轮转配置。
10. `0035` 那次索引迁移在生产大表上是否走过 online-DDL、耗时多少：仓库只有迁移文件，没有执行记录。
11. §6.4 病历缺失率 SQL 的解读边界：新流程项目明细在子表 `business_treatmentitem`（`business/models.py:248`–:251），旧六列只由病历专用入口写（`business/services_treatment.py:548`–:556）；历史行旧列是否有值需现场抽样，否则该 SQL 会把历史单据全算成缺失。
12. 平台端「编号规则」各前缀现值（`supervision_systemconfig` 的 `*_prefix`）：覆盖机制在代码里，实际取值未确认 —— 它决定按前缀过滤的 SQL 写法是否成立。
13. 建号口令策略是否收紧到 Django 校验器（§9.4 描述的是代码现状：只校验长度 ≥ 6，`supervision/views.py:630`–:632）。
14. `/health` 端点的需求方与验收标准：当前没有，§2.3 给的是替代探针。
15. 「上线时手工删号重建 `admin`」是否已执行（`DEPLOY.md:21` 记录的是决策，不是执行状态）。

另注：任务描述里举例的路径 `business/services_tnr.py` 在本仓库不存在。后端服务层实际拆在 `business/services.py`、`business/services_stock.py`、`business/services_treatment.py`、`business/services_chip.py`，本手册的引用一律以真实文件为准。
