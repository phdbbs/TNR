# Checklist

## 架构与工程
- [x] 架构选型文档化：Django 5.x + MySQL 8.0 + DRF + Django 模板，理由清晰
- [x] Django 工程目录结构规范（4 个应用：accounts/core/business/supervision）
- [x] `requirements.txt` 依赖完整且版本锁定
- [x] `.env.example` 与 `gunicorn.conf.py` 配置就绪
- [x] `settings.py` 配置 MySQL、中文/时区、静态/Media、Session

## 数据模型
- [x] 基础模型（District/Institution/User）字段对照原型种子数据
- [x] 业务模型（Pet/Capture/Transfer/Treatment/Material/Chip/Release/Adoption/CheckIn/Blacklist/Euthanasia 等）字段完整
- [x] 宠物编号生成规则：TNR + 年份 + 4 位序号
- [x] 芯片号永久不可复用约束（数据库 + 业务层双重保障）
- [x] migrations 可成功 migrate

## 账号权限
- [x] 5 类角色：gov_city / gov_district / shelter / hospital / adopter
- [x] 区县数据隔离：区级政府仅见本区数据
- [x] 角色权限装饰器/混入生效
- [x] 登录后按角色跳转各自门户
- [x] 种子数据 fixtures 可正常 loaddata

## 核心业务闭环
- [x] 捕捉登记：批量编号、照片上传、电子签名、生成台账
- [x] 主人领回：录入信息、签字、出库归档
- [x] 转运拆分：一批多医院拆分、签收/驳回
- [x] 诊疗：绝育病历、疫苗/驱虫/芯片库存联动
- [x] 物料供应链：采购入库→下发→消耗→异动全链路
- [x] 捕捉点与医院双库存台账独立且一致
- [x] 放归闭环：回收→匹配原小区→物业线上确认
- [x] 领养：线下审核登记、开通领养人账号、关联宠物
- [x] 回访打卡：月度上传、审核、违规收回
- [x] 黑名单：拉黑、录入身份信息时弹窗拦截
- [x] 安乐死：登记、尸体备案、扣减活体库存
- [x] 定时任务：诊疗完成 5 天自动转待领养并上架

## 政府监管端
- [x] 数据大屏：总量统计、机构占比、物料消耗图表
- [x] 机构管理：小区/医院/捕捉点 CRUD，含所属区县
- [x] 账号权限管理：角色配置、账号新增/停用、下级区县组织
- [x] 全业务监管：捕捉/转运/诊疗/放归/领养回访
- [x] 物料全局监管：全链路统计溯源、异常预警
- [x] 全局台账中心：多维度筛选、导出、打印
- [x] 系统配置：编号规则、字典、操作日志

## 前端模板
- [x] 保留"水墨丹青"设计系统（`tnr-traditional.css`）
- [x] 复用通用 UI 组件（Toast/Modal/确认框，`tnr-common.js`）
- [x] 捕捉点端 9 个菜单页面全部可用
- [x] 医院端 7 个菜单页面全部可用
- [x] 领养人 H5 端 5 个页面全部可用
- [x] 政府监管端 7 个菜单页面全部可用
- [x] 所有表单数据源为后端接口/模板渲染，无 localStorage 残留

## 部署与运维
- [x] `deploy.sh` 一键部署脚本可用
- [x] Nginx 配置示例（静态/Media 直出 + 反代 Gunicorn）
- [x] 2 核 4G VPS 可承载 100 并发浏览（压测或合理估算）
- [x] Supervisor 守护 Gunicorn 进程
- [x] 静态文件 `collectstatic` 正常
- [x] Media 文件上传与访问正常

## 端到端验证
- [x] 四端登录正常，权限隔离正确
- [x] 主业务闭环走通：捕捉→转运→诊疗→放归 / 领养 / 安乐死
- [x] 物料库存联动与双台账一致
- [x] 区县数据隔离生效
- [x] 黑名单拦截生效
- [x] 定时任务（5 天自动转待领养）生效
