# 演示账号规范（部署/调试必读）

> **经验记录**：每次部署或调试启动本系统时，都必须确保下列演示账号可用。
> 账号由 `python manage.py seed_data` 创建，该命令是**幂等**的：
> 区县/机构按稳定的业务编号（`District.code` / `Institution.code`）匹配，
> 业务记录按业务单号匹配，重复执行不会产生重复数据。
> 因此可以、也应该在每次启动流程中重复执行。

## 演示账号清单（统一密码：`123456`）

| 账号 | 角色 | 归属 |
|------|------|------|
| `platform` | **平台管理员**（全局） | —（不挂区县/机构） |
| `admin` | 市级政府管理员 | 全市 |
| `cy_gov` | 区级政府管理员 | 襄城区 |
| `hd_gov` | 区级政府管理员 | 樊城区 |
| `cy_shelter` | 捕捉点操作员 | 襄城流浪动物捕捉点 |
| `hd_shelter` | 捕捉点操作员 | 樊城流浪动物捕捉点 |
| `city_shelter` | **市级捕捉点操作员** | 市级流浪动物捕捉点 |
| `aixin_hosp` | 医院操作员 | 爱心宠物医院 |
| `ruipeng_hosp` | 医院操作员 | 瑞鹏宠物医院 |
| `babitang_hosp` | 医院操作员 | 芭比堂动物医院 |
| `adopter1` | 领养人 | — |

账号与密码的定义位置：`business/management/commands/seed_data.py` 的 `_seed_users()`。

> **第四十二轮：全局设置迁到平台端 `/platform/`。** 机构 / 区县 / 账号权限 /
> 编号规则 / 公告发布 / 操作日志只在平台端可写；政府端只剩四个**只读**页面
> （数据总览大屏、全业务监管、物料全局监管、全局台账中心）。
> ⚠ **超级管理员 `admin` 进不去 `/platform/`** —— 它建出来是 `role='gov_city'`，
> 而 `role_required` 只比对业务角色、不看 `is_superuser`。
> 平台端账号由 `manage.py ensure_platform_admin` 负责（部署脚本已内置）。

> **第四十五轮：捕捉点分市级 / 区县级两级。** 区分靠**所属机构**，不是账号区县 ——
> `cy_shelter` / `hd_shelter` / `city_shelter` 的账号区县**都是「全市（市级）」**
> （历史沿革），真正的层级判据是 `services.is_city_shelter()`：
> 机构类型是捕捉点**且**机构所属区县是「全市（市级）」。
> 行为差异：
>
> | | 市级捕捉点（`city_shelter`） | 区县级捕捉点（`cy_shelter` / `hd_shelter`） |
> |---|---|---|
> | 可见数据 | 全市 | 本区县 |
> | 采购入库 | 可（物料归属市级） | 可（物料归属本区县） |
> | 下发范围 | **全市**（区县级捕捉点 + 任意医院） | 仅本区县 |
> | 签收 | 可 | 可 |
> | 二级下发 | 可（全市） | 可（仅本区县） |
> | 转运 | 可转运到**全市任何医院** | 可转运到本区县医院 |

## 各启动方式如何保障账号可用

| 场景 | 保障方式 |
|------|----------|
| 本地调试 | 运行 `bash start_dev.sh`，自动执行 `migrate` + `seed_data` 后启动 |
| 生产部署 | `deploy.sh` 第 6 步已内置 `python manage.py seed_data` |
| 手动启动 | 在 `runserver` / `gunicorn` 前先执行 `python manage.py migrate && python manage.py seed_data` |

## 注意事项

- **每次执行 `seed_data` 都会校准演示账号**：角色、电话、所属区县、所属机构、
  姓名、`is_active` 状态都会被修正回下表定义的值。因此演示账号即使被人为
  停用或改了归属，重跑一次即可恢复；命令输出会打印被校准的账号清单
  （例如 `已校准 2 个演示账号：hd_shelter(is_active)、aixin_hosp(is_active)`）。
- **密码不会被自动重置**。`seed_data` 只在账号**不存在**时写入 `123456`；
  静默覆盖生产环境里人为修改过的凭据是危险的。若确实需要重置密码，
  请手工执行：
  ```python
  from accounts.models import User
  u = User.objects.get(username='cy_shelter')
  u.set_password('123456')
  u.save()
  ```
- 演示账号被停用后，除 `seed_data` 校准外，也可由**平台管理员**在
  「账号权限管理」里重新启用（第四十二轮起政府端不再有此功能）。
- **演示账号提示落在两处 UI 上**（第四十三轮）：
  `templates/login.html` 的 `.login-demo-hint` 与
  `templates/portal/index.html` 的 `.portal-demo-hint`（仅未登录时渲染）。
  两处都是**公开页面**、都写着 `123456`，**改一处必须改另一处**。
  判据在 `core/tests_frontend_consistency.py::PortalLandingPageTest`：
  提示里出现的账号必须在本文件清单内、门户首页提示必须**列全**本文件的所有账号、
  两个提示都必须给出平台端账号 `platform`。
  ⚠ 正式上线删号重建时，这两块提示要一并删除。
- **平台管理员口令**（第四十二轮）与超管同口径：`deploy.sh` 写 `.env` 的
  `PLATFORM_ADMIN_PASSWORD`，第 7 步执行
  `manage.py ensure_platform_admin --username platform --reset-password`。
  优先级：命令行 `PLATFORM_ADMIN_PASSWORD` → **继承已有 `.env`** → 随机生成。
  单独改口令：
  ```bash
  cd /opt/tnr && PLATFORM_ADMIN_PASSWORD=新口令 ./venv/bin/python manage.py \
      ensure_platform_admin --username platform --reset-password
  ```
- **生产环境 `admin` 的口令固定为 `123456`**（演示期约定，第三十七轮磊哥决策）。
  `deploy.sh` 第 5 步会写 `.env`，第 7 步执行
  `manage.py ensure_superuser --username admin --reset-password`，
  口令来源优先级：命令行 `ADMIN_PASSWORD` → **继承已有 `.env`** → 随机生成。
  由于 `.env` 里已写入 `ADMIN_PASSWORD=123456`，**每次部署都会强制应用它**，
  不会再回退成「随机生成、只显示一次、不落盘」那个谁也不知道的口令。
  ⚠ **正式上线时**按约定手工删除已有账号、全部重建，并把 `.env` 里该行改成
  强口令或清空 —— 那时 `ensure_superuser` 才会走「没拿到口令 → SKIP」分支，
  不再覆盖运维改过的口令。
  需要单独改口令时**不要**跑整个 `deploy.sh`（会重建 `.env`、重装依赖、
  `collectstatic`），直接用最小命令：
  ```bash
  cd /opt/tnr && ADMIN_PASSWORD=新口令 ./venv/bin/python manage.py ensure_superuser \
      --reset-password --username admin
  ```
  ⚠ 改完复检 `.env` 属主仍是 `ubuntu:ubuntu`（用 root 追加会变成 `root:root`）。
- 需要全新演示数据时可用 `python manage.py seed_data --flush`
  （**会清空全部业务数据后重建**，仅限演示/开发环境使用）。

## 相关数据约定（易踩坑）

- **机构编号 `Institution.code`**（`I001`/`C001`…）是稳定业务键，机构名只是
  展示名。种子数据按 `code` 幂等匹配，因此在界面上给机构改名后重跑
  `seed_data` **不会**再插入一套重复机构。历史版本按 `name` 匹配，
  改名后每次部署都会重复创建，已在迁移 `core/0003` 中回填编号并清理。
- **芯片号段**为 `1000010001`–`1000010500`（10 位）。宠物档案
  `Pet.chip_no` 与诊疗记录 `Treatment.chip_no` 都引用这个号段，必须能在
  `Chip` 表里查到，否则医院登记「芯片植入」会报「芯片不存在」。
