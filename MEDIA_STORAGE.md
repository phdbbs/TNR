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

## 6. ⚠ 迁移前必须处理的一件事：孤儿文件

**本地现状**（2026-10-09 实测）：

```
数据库引用的图片：    54 个
media/ 目录里的文件： 20522 个
```

也就是说有 **两万多个孤儿文件** —— 磁盘上有，但没有任何数据库记录引用它们。
主要来源是**跑测试时上传的图片**（`test-pet-0_*.png` / `test-group_*.png` 等），
改造前测试没有隔离 `MEDIA_ROOT`，每跑一次就往真实目录里丢十几个。

**这直接影响迁移决策**：

- 不加 `--include-orphans` → 只搬 54 个，对象存储干净，但那两万个文件留在本地
- 加 `--include-orphans` → 全部搬走，**白付两万多个文件的存储与流量费**

**建议顺序**：先做孤儿文件审计（比对数据库引用 + 按名称特征识别测试残留），
确认后再决定搬哪些。清理脚本应当**默认 dry-run**、**先备份**。

> ⚠ 本地 `db.sqlite3` 是**开发库**，引用数（54）不代表生产。
> 生产（MySQL）的真实引用数要在生产环境跑 `media_migrate` 才知道。
> 本次改造**没有动生产环境**。

---

## 7. 涉及的文件

| 文件 | 改动 |
|---|---|
| `core/storage.py` | **新增** —— 存储抽象、后端工厂、环境变量契约 |
| `core/management/commands/media_migrate.py` | **新增** —— 迁移命令 |
| `core/tests_media_storage.py` | **新增** —— 26 条判据 |
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
