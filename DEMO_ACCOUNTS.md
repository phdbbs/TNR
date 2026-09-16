# 演示账号规范（部署/调试必读）

> **经验记录**：每次部署或调试启动本系统时，都必须确保下列演示账号可用。
> 账号由 `python manage.py seed_data` 创建，该命令是**幂等**的：
> 区县/机构按稳定的业务编号（`District.code` / `Institution.code`）匹配，
> 业务记录按业务单号匹配，重复执行不会产生重复数据。
> 因此可以、也应该在每次启动流程中重复执行。

## 演示账号清单（统一密码：`123456`）

| 账号 | 角色 | 归属 |
|------|------|------|
| `admin` | 市级政府管理员 | 全市 |
| `cy_gov` | 区级政府管理员 | 襄城区 |
| `hd_gov` | 区级政府管理员 | 樊城区 |
| `cy_shelter` | 捕捉点操作员 | 襄城流浪动物捕捉点 |
| `hd_shelter` | 捕捉点操作员 | 樊城流浪动物捕捉点 |
| `aixin_hosp` | 医院操作员 | 爱心宠物医院 |
| `ruipeng_hosp` | 医院操作员 | 瑞鹏宠物医院 |
| `babitang_hosp` | 医院操作员 | 芭比堂动物医院 |
| `adopter1` | 领养人 | — |

账号与密码的定义位置：`business/management/commands/seed_data.py` 的 `_seed_users()`。

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
- 演示账号被停用后，除 `seed_data` 校准外，也可由市级管理员在
  「机构与用户管理」里重新启用。
- **生产部署（`deploy.sh`）会覆盖 `admin` 的口令**：脚本执行
  `manage.py ensure_superuser` 把 `admin` 提升为 Django 超级管理员并设置一个
  强口令（`ADMIN_PASSWORD` 未提供则随机生成，仅打印一次），因此
  **生产环境里 `admin` 的密码不再是 `123456`**。已存在启用的超级管理员时
  脚本不会动口令，重新部署不会把运维改过的密码重置掉。
  重置方式：`ADMIN_PASSWORD=新口令 python manage.py ensure_superuser --reset-password`
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
