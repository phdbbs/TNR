# 图片存储：对象存储就绪改造

> 一句话：**图片现在一律经 Django Storage API 读写**。以后要迁到对象存储，
> 改几个环境变量 + 跑一条命令即可，**业务代码、模板、前端、数据库都不用动**。

---

## 1. 改造前后

| | 改造前 | 改造后 |
|---|---|---|
| 存储后端 | 隐式默认（`FileSystemStorage`） | **显式** `STORAGES`，由 `TNR_MEDIA_STORAGE` 驱动 |
| 切换后端 | 要改代码 | **改环境变量**（`local` / `cos` / `oss` / `s3` / `minio`） |
| 存量搬迁 | 无手段 | `manage.py media_migrate` |
| 测试写文件 | **污染项目真实 `media/`** | 隔离到临时目录 |
| `/media/` 路由 | 只要 `DEBUG` 就挂 | 仅**本地存储**模式才挂 |

**本来就已经规范的部分**（这次没改，是既有优势）：

- 上传：全部 `request.FILES[...]` 直接赋值给 `ImageField` → 走 `storage.save()`
- 读取：全部 `.url` → 走 `storage.url()`
- 校验：`services.validate_image_upload()` 用 Pillow 读**流**，不碰磁盘路径
- 前端：全部直接使用后端返回的 URL，**没有任何地方拼 `/media/` 前缀**

所以这次改造的重点不是"重写上传逻辑"，而是**把隐式的存储假设显式化 + 补上迁移手段 + 补上判据**。

---

## 2. ⚠ 唯一的硬约束：object key 必须与数据库里的 `name` 逐字一致

`ImageField` 在数据库里存的是**相对路径字符串**（如 `photos/abc.jpg`）。
对象存储的 object key 若与它逐字相同，迁移就退化成**纯文件搬运** ——
数据库一行都不用改，回滚也只是把文件搬回去。

由此推出两条**不要碰**的规则：

1. **不要修改 `upload_to`。**
   给它加日期分目录（`photos/2026/10/09/`）会让**新文件**的 key 形态与历史文件不同，
   "一次批量命令完成迁移"就不再成立 —— 脚本得同时懂两套命名规则。
   宁可 `photos/` 下文件多，也不要命名漂移。

2. **`TNR_MEDIA_LOCATION` 一旦设定就不要改。**
   它会让 key 从 `photos/a.jpg` 变成 `prefix/photos/a.jpg`，改了等于所有历史图片失联。

---

## 3. 迁移到对象存储（三步）

### 第 1 步：装依赖

```bash
pip install "django-storages[s3]"
```

这是**可选依赖**：不装也能跑，只是只能用本地存储。配了对象存储却没装时，
启动会给出可读报错（而不是等用户上传第一张图才 500）。

### 第 2 步：改 `.env`

```bash
TNR_MEDIA_STORAGE=cos                              # cos / oss / s3 / minio
TNR_MEDIA_BUCKET=tnr-media-1250000000
TNR_MEDIA_REGION=ap-guangzhou
TNR_MEDIA_ENDPOINT=https://cos.ap-guangzhou.myqcloud.com   # COS 必须显式指定
TNR_MEDIA_ACCESS_KEY_ID=...
TNR_MEDIA_SECRET_ACCESS_KEY=...
# TNR_MEDIA_CUSTOM_DOMAIN=cdn.example.com          # 有 CDN 就填，URL 直接走它
# TNR_MEDIA_QUERYSTRING_AUTH=false                 # 私有桶改 true
```

**先确认配置正确**：

```bash
python manage.py media_migrate --status
```

### 第 3 步：搬存量文件

```bash
# ① 先看清单，什么都不动
python manage.py media_migrate

# ② 真搬（幂等，可中断重跑）
python manage.py media_migrate --apply

# ③ 核对两边一致
python manage.py media_migrate --verify

# ④ 确认无误后清理本地残留（危险，双闸门）
python manage.py media_migrate --prune --yes
```

搬完之后：

```bash
python manage.py check && sudo systemctl restart tnr   # 让新配置生效
```

> **迁移期间服务可以继续跑。** 新上传的图直接进对象存储，跑完后再执行一次
> `--apply` 就能把期间漏掉的补齐（幂等）。

---

## 4. 命令参考

| 参数 | 作用 |
|---|---|
| （无参数） | **dry-run**，只列清单 |
| `--apply` | 真正搬运 |
| `--verify` | 只核对源与目标是否一致，不一致则以非 0 退出 |
| `--reverse` | 反向：从对象存储搬回本地（**回滚**） |
| `--prune` | 搬运成功后删除源文件（**必须**同时给 `--yes`） |
| `--yes` | 确认执行危险操作 |
| `--include-orphans` | 把源里存在、但数据库未引用的文件也纳入 |
| `--only photos/` | 只处理指定前缀 |
| `--limit 100` | 最多处理 100 个（试跑用） |
| `--status` | 只打印当前存储配置快照 |
| `--source-dir` / `--target-dir` | 指定本地目录，**不接对象存储也能完整演练** |

**安全设计**：

- 默认 dry-run；`--prune` 需要 `--yes` 双闸门
- 有任何一个文件搬运失败，**整体跳过清理**（绝不删没搬走的）
- 清理时**逐个重新核对目标里确实有同名文件**才删源 —— 删除不可逆，
  只凭"上一轮成功了"的推断就删，推断错了就把唯一副本删掉了
- 源文件默认永不删除

---

## 5. 回滚

```bash
# 改回 .env：TNR_MEDIA_STORAGE=local
python manage.py media_migrate --reverse --apply
python manage.py check && sudo systemctl restart tnr
```

因为 key 没变，回滚就是把文件搬回来，数据库同样一行都不用改。

---

## 6. 孤儿文件：已处理（2026-10-13）

**本地现状**（2026-10-13 实测，清理前）：

```
数据库引用的图片：     54 个     （全部是 photos/phone_photo_*.jpg，共 6.65 MB）
media/ 目录里的文件： 20523 个
孤儿文件：           20469 个    （合计 113.5 MB）
```

孤儿按来源分三类，前两类是噪声，第三类才是空间大头：

| 来源 | 数量 | 占用 | 说明 |
|---|---|---|---|
| `photos/TNR*.png` | 13981 | 1.0 MB | 历史登记 / seed 图，单个约 75 B |
| `photos/pet*_*.png` | ~350 | 90.2 MB | seed 宠物图，**单个 264 KB，同一张反复复制** |
| `photos/test-*` | 5444 | 0.4 MB | 改造前测试没隔离 `MEDIA_ROOT` 时留下的残留 |
| 其他 | ~690 | 21.9 MB | 同上两类之外的早期残留 |

> 空间的 98.7% 集中在 435 个 >5 KB 的文件里，全是 `pet_*` / `pet2_*` 的 seed 图。
> 「文件多」和「占空间」在这里是两件事，别被 20469 这个数字误导。

### 清理命令

```bash
# ① 干跑：只出报告，零改动（默认）
python manage.py media_clean_orphans

# ② 真清：先移送到 media/ 同级的 media_orphans_<时间戳>/（可恢复）
python manage.py media_clean_orphans --apply

# ③ 备份确认无价值后彻底删除（危险，双闸门）
python manage.py media_clean_orphans --purge --backup-dir <备份目录> --yes
```

### 为什么是「移到备份」而不是「删」

直接 `os.remove` 的一次误判就是永久丢失，而判断依据（「数据库里没有引用」）
本身依赖几个不牢靠的假设：历史数据可能是手工导入的、逻辑删除记录的 `name`
可能被误清过、**本地库不是生产库**。所以默认走移动，并写 `MANIFEST.txt`
记录「原 key → 备份位置」；真要把文件找回来，搬回 `MEDIA_ROOT` 下同相对路径即可，
**数据库一行都不用改**。

### 三条硬规则

1. **备份目录必须在 `MEDIA_ROOT` 之外**（默认就在它同级）。
   落在 `media/` 内会同时踩三个坑：被下次扫描当成新的孤儿、
   被 `media_migrate` 一起搬上对象存储、让 `/media/` 路由下「看起来干净」变成假象。
   代码里是**硬校验**，落在里面直接报错。
2. **逻辑删除记录的引用照样算有效。** 照片仍是审计凭证，仍可能被恢复；
   Django 默认 manager 不过滤软删，所以天然满足（`SoftDeleteReferenceKeptTest` 守着）。
3. **引用集合动态扫描全部 `FileField`/`ImageField`**，没有硬编码字段清单 ——
   硬编码清单会在新增图片字段时静默漏扫，表现为「新功能的图被当孤儿删掉」
   且没有任何报错（`NewFileFieldTest` 守着）。

### 被中断了怎么办

中断（Ctrl-C / OOM / `SIGTERM`）后**重跑同一条命令即可**，它是幂等的。
但有一个坑要知道：默认备份目录名带时间戳，所以**续跑会新建一个目录**，
文件于是分到两处，而第一个目录的 MANIFEST 还没写（它在末尾统一落盘）。

补救办法（2026-10-14 真实踩到过：20469 个文件被分成 17669 + 2800 两份）：

```bash
# ① 合并（--ignore-existing 保证不覆盖已有文件，先核对无重叠）
rsync -a --ignore-existing media_orphans_<新的>/ media_orphans_<旧的>/

# ② 删除已合并完的目录（先核对里面每个文件都在旧目录里）

# ③ 按备份目录里的现有文件重写清单 —— 条数必须等于文件数
python manage.py media_clean_orphans --rebuild-manifest \
    --backup-dir media_orphans_<旧的>
```

`--rebuild-manifest` 不移动任何文件，只重建清单。
合并后若不重建，**多出来的那些文件就永久失去溯源记录，而且没有任何报错**。

### 对迁移的影响

清完之后本地只剩 54 个被引用的文件，`media_migrate` 不带 `--include-orphans`
搬走的就是干净的一份，**不会再白付两万多个文件的存储与流量费**。

> ⚠ 本地 `db.sqlite3` 是**开发库**，引用数（54）不代表生产。
> 生产（MySQL）的孤儿情况要在生产环境跑 `media_clean_orphans` 才知道，
> 同样先干跑、看清楚、再 `--apply`。**本次清理只动了本地开发库的 media/ 目录。**

---

## 7. 涉及的文件

| 文件 | 改动 |
|---|---|
| `core/storage.py` | **新增** —— 存储抽象、后端工厂、环境变量契约 |
| `core/management/commands/media_migrate.py` | **新增** —— 迁移命令 |
| `core/management/commands/media_clean_orphans.py` | **新增** —— 孤儿文件清理命令（2026-10-13） |
| `core/tests_media_storage.py` | **新增** —— 26 条判据 |
| `core/tests_media_clean_orphans.py` | **新增** —— 18 条判据（2026-10-13） |
| `tnr_system/test_runner.py` | **新增** —— 测试隔离 `MEDIA_ROOT` |
| `tnr_system/settings.py` | 显式 `STORAGES` + `TEST_RUNNER` |
| `tnr_system/urls.py` | `/media/` 挂载改为仅本地存储模式 |
| `.env.example` | 新增「媒体文件存储」配置段 |

**没有改动**：任何 `models.py`（`upload_to` 一个字没动）、任何上传接口、
任何模板、任何前端 JS。这正是这套设计的目的 —— 存储后端与业务代码解耦。

---

## 8. 判据（`core/tests_media_storage.py`，26 条）

按项目约定，判据断言**行为**而非标识符，且**成对**：

- **切换后端**：保存图片后断言文件落在新目录（正向）**且旧目录里没有**（反向对照）
  —— 只测正向的话，「两边都写」也能通过
- **`.url` 前缀**跟随 storage（这是"前端零改动"的依据）
- **静态扫描**：业务代码不得出现 `MEDIA_ROOT`；先用 `tokenize` 剥注释，
  并用**变异样本**证明扫描器不是恒真（把违规代码喂进去必须能抓到）
- **迁移端到端**：搬完后断言目标文件数、**数据库 `name` 一个字没变**、
  重跑幂等、`--verify` 在不完整时**必须报错退出**
- **prune 安全**：目标里没有同名文件时**必须保留源文件**（反向对照）
- **测试隔离**：断言测试图片**没有**写进项目真实 `media/`

---

## 9. 一个踩到的 Django 坑（值得记）

**`override_settings(STORAGES=...)` 在 Django 5.0.6 下不生效**（缓存已建立时）。

- `django.test.signals.storages_changed` 用 `del storages.backends` 清缓存；
- 但 Django 的 `cached_property` **只实现了 `__set_name__` 与 `__get__`，没有 `__delete__`**
  —— `del` 抛 `AttributeError`，被紧跟的 `except AttributeError: pass` 吞掉；
- 于是重建出来的还是旧配置的 storage 实例。

对照：`override_settings(MEDIA_ROOT=...)` **是**有效的 ——
`FileSystemStorage.__init__` 里 `setting_changed.connect(self._clear_cached_properties)`
自己监听了 `MEDIA_ROOT`。两者行为不同，极易误判成"时灵时不灵"。

**生产代码不受影响**（切换后端靠改环境变量 + 重启进程，那时缓存还没建立）。
测试里需要显式替换 `storages._storages['default']` 并重置 `default_storage._wrapped`，
见 `core/tests_media_storage.py::use_media_storage`。
