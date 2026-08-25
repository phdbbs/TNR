# 演示账号规范（部署/调试必读）

> **经验记录**：每次部署或调试启动本系统时，都必须确保下列演示账号可用。
> 账号由 `python manage.py seed_data` 创建，该命令是**幂等**的
> （内部全部使用 `get_or_create`，已存在的账号和数据会跳过），
> 因此可以、也应该在每次启动流程中重复执行。

## 演示账号清单（统一密码：`123456`）

| 账号 | 角色 | 归属 |
|------|------|------|
| `admin` | 市级政府管理员 | 全市 |
| `cy_gov` | 区级政府管理员 | 朝阳区 |
| `hd_gov` | 区级政府管理员 | 海淀区 |
| `cy_shelter` | 捕捉点操作员 | 朝阳区流浪动物捕捉点 |
| `hd_shelter` | 捕捉点操作员 | 海淀区流浪动物捕捉点 |
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

- `seed_data` 只在账号**不存在**时创建并设置密码为 `123456`；
  若某账号被人为改过密码需要重置，可在 Django shell 中执行：
  ```python
  from accounts.models import User
  u = User.objects.get(username='cy_shelter')
  u.set_password('123456')
  u.save()
  ```
- 需要全新演示数据时可用 `python manage.py seed_data --flush`
  （**会清空全部业务数据后重建**，仅限演示/开发环境使用）。
