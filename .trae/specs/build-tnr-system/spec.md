# 流浪动物TNR捕捉绝育领养管理系统 - 全栈开发 Spec

## Why

根据 `tnr_PR.md` 业务定稿与 `solo/` 目录下的 H5 原型，需要将现有"localStorage 演示原型"升级为可上线运行的全栈系统。系统服务城市流浪动物 TNR 全流程治理，覆盖捕捉、转运、诊疗、物料、放归、领养、回访、黑名单、安乐死、政府监管十大业务域。

用户约束：
- 主要操作用户 ≤ 20 人（捕捉点/医院/政府工作人员）
- 宠物主浏览并发 ≤ 100（领养大厅公开访问）
- 单全栈开发，开发者熟悉 Flask，初步倾向 Django + MySQL
- 要求简单部署、低云资源、易后期维护

## 架构评估与选型（定稿）

### 候选方案对比

| 方案 | 开发效率 | 维护成本 | 资源占用 | 适配度 |
|------|----------|----------|----------|--------|
| Flask + MySQL + 自建 | 低（需自建Admin/Auth/ORM） | 中 | 低 | 中 |
| **Django + MySQL + DRF + Django模板（推荐）** | **高** | **低** | **低** | **高** |
| Django + MySQL + Vue/React 前后端分离 | 中 | 高（双端维护） | 中 | 中 |

### 定稿技术栈

- **后端**：Python 3.11 + Django 5.x + Django REST Framework 3.15
- **数据库**：MySQL 8.0（单实例）
- **前端**：Django 模板 + 原型现有 HTML/CSS/JS（保留"水墨丹青"设计系统）+ DRF JSON 接口
- **鉴权**：Django 内置 Session + Auth（多角色 RBAC）
- **文件存储**：本地文件系统（Media），Nginx 直接吐静态
- **部署**：单台 VPS（2核4G）+ Nginx + Gunicorn + MySQL + Supervisor
- **任务调度**：django-q2 或 Django cron（用于"诊疗完成5天自动转待领养"等定时任务）

### 选型理由

1. **Django 的"batteries included"完美匹配 TNR 系统**：本系统是典型 CRUD + 多角色权限系统，Django 自带 Admin/ORM/Auth/表单/中间件能省 40%+ 样板代码
2. **政府监管端可复用 Django Admin**：机构管理、账号权限、日志审计等后台功能用 Django Admin 二次定制，缩短开发周期
3. **保留现有原型设计**：`solo/` 下已有完整 H5 原型（水墨丹青配色 + 组件库），Django 模板可直接复用，无需引入 Vue/React 增加维护负担
4. **DRF 提供清洁 API**：领养人端动态交互（领养大厅、打卡上传）走 JSON 接口，PC 端表单走模板渲染，混合模式灵活
5. **资源占用低**：Django + Gunicorn 单进程多 worker，2核4G VPS 足够承载 100 并发浏览
6. **部署简单**：Nginx + Gunicorn + MySQL 经典组合，Docker Compose 可选，单文件部署脚本即可
7. **开发者从 Flask 迁移成本低**：Django 的 MVT 与 Flask 的 MVC 思想一致，ORM 比 SQLAlchemy 更直观

### 领养人端方案

原型中领养人端为 H5 页面（`adopter.html`）。考虑到：
- 用户要求"简单部署、低云资源"
- 微信小程序需备案、审核、单独维护
- 领养人功能较轻（浏览 + 打卡 + 消息）

**定稿：领养人端采用 H5 移动端页面**（Django 模板 + 响应式），未来如需可包装为微信小程序（复用后端 API）。

## What Changes

### 新增：后端工程
- 新建 Django 项目 `tnr_system/`，含 4 个应用：`accounts`（账号权限）、`core`（宠物/机构/区县等基础数据）、`business`（捕捉/转运/诊疗/物料/放归/领养/回访/黑名单/安乐死）、`supervision`（政府监管/大屏/台账）
- 配置 MySQL 数据库、静态文件、Media 文件、Session
- 配置多角色 RBAC：`gov_city`/`gov_district`/`shelter`/`hospital`/`adopter` 五类角色
- 区县数据隔离：区级政府仅可见本区县数据

### 新增：数据模型
- 基础：District、Institution（shelter/hospital/community 三型）、User
- 业务：Pet、Capture、OwnerReturn、Transfer、Treatment、Material、MaterialTransaction、Chip、Release、Adoption、CheckIn、Blacklist、Euthanasia、Message、AdoptionHallListing
- 字段严格对照 `tnr_PR.md` 与原型 `tnr-data.js` 种子结构

### 新增：后端业务逻辑
- 捕捉登记：批量生成宠物编号（TNR+YYYY+4位序号）、电子签名留存、照片上传
- 转运拆分：一批多医院拆分下发、签收/驳回
- 诊疗+库存联动：绝育病历、疫苗/驱虫/芯片库存自动扣减、芯片永久不可复用
- 物料台账：采购入库→下发→消耗→异动全链路流水
- 放归闭环：原小区物业线上确认
- 领养：线下审核登记、开通领养人账号、关联宠物
- 回访：月度打卡、违规收回、黑名单拦截
- 安乐死：登记备案、扣减活体库存
- 定时任务：诊疗完成5天后状态自动转"待领养"并上架领养大厅

### 新增：前端页面（Django 模板）
- 复用 `solo/css/tnr-traditional.css` 设计系统
- 复用 `solo/js/tnr-common.js` 通用组件（Toast、Modal、确认框）
- 将 `solo/*.html` 原型改造为 Django 模板，替换 localStorage 调用为 DRF 接口
- 四端门户：`index.html`（门户入口）、`shelter.html`、`hospital.html`、`adopter.html`、`government.html`

### 新增：部署配置
- `requirements.txt`、`.env.example`、`gunicorn.conf.py`
- Nginx 配置示例
- 部署脚本 `deploy.sh`（单机一键部署）

## Impact

- **Affected specs**: 无（首个 spec）
- **Affected code**:
  - `solo/` 目录下原型文件将被改造为 Django 模板（保留设计系统）
  - 新建 `tnr_system/` Django 工程目录
  - 新建 `media/` 目录存放上传文件
  - 新建 `static/` 目录存放静态资源

## ADDED Requirements

### Requirement: 多角色账号体系
系统 SHALL 提供 5 类角色账号：市级政府管理员、区级政府管理员、捕捉点操作员、医院操作员、领养人，并支持基于区县的数据隔离。

#### Scenario: 区级政府数据隔离
- **WHEN** 区级政府管理员登录
- **THEN** 仅可见本区县范围内的捕捉、转运、诊疗、领养等所有业务数据

#### Scenario: 领养人开通
- **WHEN** 捕捉点线下审核通过领养人资质
- **THEN** 系统为领养人开通账号，领养人可登录 H5 端查看已领养宠物档案与打卡

### Requirement: 流浪动物捕捉登记
系统 SHALL 支持捕捉点现场录入物业信息、捕捉数量、批量生成宠物唯一编号、上传合影与单只照片、物业电子签名，提交后生成捕捉台账。

#### Scenario: 批量生成编号
- **WHEN** 捕捉点录入捕捉数量为 3
- **THEN** 系统自动生成 3 个唯一宠物编号（格式：TNR+年份+4位序号，如 TNR20250001）

#### Scenario: 主人领回
- **WHEN** 在途宠物被原主人认领
- **THEN** 录入主人信息、电子签字后直接出库归档，终止后续流程

### Requirement: 转运拆分下发
系统 SHALL 支持一批捕捉动物拆分转运至多家医院，单只独立绑定医院，医院端可签收或驳回。

#### Scenario: 拆分转运
- **WHEN** 捕捉点选择 3 只在途宠物，拆分至 2 家医院
- **THEN** 生成 2 张转运交接单，每只宠物独立绑定一家医院

#### Scenario: 医院驳回
- **WHEN** 医院填写驳回理由并驳回转运单
- **THEN** 宠物状态回退，捕捉点可修改信息重新下发

### Requirement: 诊疗与物料库存联动
系统 SHALL 支持医院执行诊疗（绝育/疫苗/驱虫/芯片），其中疫苗/驱虫/芯片自动扣减医院库存，芯片号永久不可复用。

#### Scenario: 芯片植入
- **WHEN** 医院为宠物植入芯片并选择芯片号
- **THEN** 该芯片号标记为已使用且永久不可复用，医院芯片库存-1，宠物档案绑定芯片号

#### Scenario: 疫苗接种扣库存
- **WHEN** 医院勾选疫苗接种并保存
- **THEN** 医院疫苗库存-1，生成用药记录与物料流水

### Requirement: 捕捉点与医院双物料台账
系统 SHALL 维护捕捉点与医院两端独立的物料台账，覆盖采购入库、下发、消耗、异动全链路。

#### Scenario: 物料下发
- **WHEN** 捕捉点向医院下发 50 支疫苗
- **THEN** 捕捉点库存-50，医院待接收+50，医院确认后医院库存+50

#### Scenario: 库存异动
- **WHEN** 医院报备物料过期/破损/盘亏
- **THEN** 库存相应扣减，生成异动台账记录

### Requirement: 放归闭环
系统 SHALL 支持捕捉点回收治愈动物，匹配原小区物业，物业线上确认后完成放归。

#### Scenario: 放归确认
- **WHEN** 捕捉点发起放归并由原物业线上签字确认
- **THEN** 宠物状态变更为"已放归"，生成放归台账

### Requirement: 领养业务
系统 SHALL 汇聚全市医院待领养宠物至领养大厅，领养人线下办理后系统登记领养关系并开通领养人账号。

#### Scenario: 自动上架领养大厅
- **WHEN** 宠物诊疗完成 5 天后
- **THEN** 状态自动变更为"待领养"并上架至领养大厅

#### Scenario: 领养登记
- **WHEN** 捕捉点线下完成领养审核与协议签署
- **THEN** 系统登记领养信息、关联领养人与宠物、开通领养人账号

### Requirement: 回访打卡与黑名单
系统 SHALL 支持领养人月度拍照打卡，捕捉点审核，违规可收回并拉黑。

#### Scenario: 黑名单拦截
- **WHEN** 工作人员录入身份信息时匹配到黑名单
- **THEN** 系统弹窗拦截提示，阻止后续操作

### Requirement: 安乐死处置
系统 SHALL 支持医院登记安乐死处置，尸体交还捕捉点备案，扣减医院活体库存。

### Requirement: 政府监管大屏与台账
系统 SHALL 提供政府监管端数据大屏、全业务台账、物料全局监管、机构管理、权限管理。

#### Scenario: 数据大屏
- **WHEN** 市级政府管理员登录
- **THEN** 可查看全市捕捉/诊疗/放归/领养/安乐死总量及各机构占比

### Requirement: 定时任务
系统 SHALL 提供定时任务：诊疗完成 5 天后宠物自动转"待领养"并上架领养大厅。

### Requirement: 部署与运维
系统 SHALL 支持单机部署（Nginx + Gunicorn + MySQL），提供一键部署脚本，2核4G VPS 可承载 100 并发浏览。

## MODIFIED Requirements

### Requirement: 前端原型升级为 Django 模板
原 `solo/` 目录下的 localStorage 演示原型 SHALL 升级为 Django 模板，数据源由 localStorage 替换为 MySQL + DRF 接口，保留"水墨丹青"设计系统与通用 UI 组件。

## REMOVED Requirements

### Requirement: localStorage 数据层
**Reason**: 原型使用 localStorage 模拟数据持久化，正式系统需替换为 MySQL
**Migration**: 原 `tnr-data.js` 的种子数据结构作为 Django 数据模型与种子数据（fixtures）的设计依据
