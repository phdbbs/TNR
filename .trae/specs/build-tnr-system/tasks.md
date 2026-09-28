# Tasks

## 阶段一：工程骨架与基础设施

- [x] Task 1: 初始化 Django 工程与配置
  - [x] SubTask 1.1: 在 `solo/` 下创建 Django 项目 `tnr_system/`，配置 `settings.py`（MySQL、静态文件、Media、Session、中文/时区）
  - [x] SubTask 1.2: 创建 4 个应用：`accounts`、`core`、`business`、`supervision`
  - [x] SubTask 1.3: 编写 `requirements.txt`（Django 5.x、DRF、mysqlclient、Pillow、django-q2、python-dotenv）
  - [x] SubTask 1.4: 编写 `.env.example` 与 `gunicorn.conf.py`
  - [x] SubTask 1.5: 配置项目 URL 路由总入口（四端门户路由分发）

- [x] Task 2: 设计并实现数据模型（`core` + `business`）
  - [x] SubTask 2.1: `core` 模型：District、Institution（含 shelter/hospital/community 三型）、User（扩展 Django Auth，加 role/districtId/institutionId）
  - [x] SubTask 2.2: `business` 模型：Pet、Capture、OwnerReturn、Transfer、Treatment、Material、MaterialTransaction、Chip、Release、Adoption、CheckIn、Blacklist、Euthanasia、Message、AdoptionHallListing
  - [x] SubTask 2.3: 字段严格对照 `tnr-data.js` 种子结构与 `tnr_PR.md`，定义 `__str__`、`Meta.ordering`、索引
  - [x] SubTask 2.4: 编写 migrations 并 `migrate` 成功

- [x] Task 3: 账号权限体系（`accounts`）
  - [x] SubTask 3.1: 自定义 User 模型（AbstractUser 扩展 role/district/institution 字段）
  - [x] SubTask 3.2: 实现登录/登出视图（基于 Django Auth），按角色跳转各自门户
  - [x] SubTask 3.3: 实现区县数据隔离中间件/Manager（区级政府仅见本区数据）
  - [x] SubTask 3.4: 实现角色权限装饰器（`@role_required`）与视图混入
  - [x] SubTask 3.5: 编写种子数据 fixtures（对照 `tnr-data.js` 的区县、机构、用户、物料、宠物等）

## 阶段二：核心业务后端（`business`）

- [x] Task 4: 捕捉登记与主人领回
  - [x] SubTask 4.1: 捕捉登记视图（录入物业信息、捕捉数量、批量生成编号 TNR+年份+4位序号、合影/单只照片上传、物业电子签名）
  - [x] SubTask 4.2: 主人领回登记视图（录入主人信息、电子签字、出库归档、状态终止）
  - [x] SubTask 4.3: 捕捉台账列表与详情视图

- [x] Task 5: 转运拆分下发
  - [x] SubTask 5.1: 转运下发视图（多选在途宠物、拆分至多家医院、生成转运单）
  - [x] SubTask 5.2: 医院端接收/驳回视图（签收改状态为"待诊疗"，驳回回退状态）
  - [x] SubTask 5.3: 转运台账与单据视图

- [x] Task 6: 诊疗与物料库存联动
  - [x] SubTask 6.1: 诊疗操作视图（多选绝育/疫苗/驱虫/芯片，保存生成诊疗档案）
  - [x] SubTask 6.2: 绝育病历录入子表单
  - [x] SubTask 6.3: 疫苗/驱虫接种自动扣减医院库存、生成物料流水
  - [x] SubTask 6.4: 芯片植入：选择可用芯片号、绑定宠物、库存-1、芯片标记永久不可复用

- [x] Task 7: 物料供应链与双台账
  - [x] SubTask 7.1: 捕捉点物料采购入库视图（疫苗/驱虫药常规、芯片支持起止号段批量入库）
  - [x] SubTask 7.2: 捕捉点物料下发出库视图（选医院、选物料、芯片支持号段或具体号码下发）
  - [x] SubTask 7.3: 医院物料入库确认视图
  - [x] SubTask 7.4: 医院库存异动报备（过期/破损/盘亏）视图
  - [x] SubTask 7.5: 捕捉点与医院双库存台账、全流向追溯视图

- [x] Task 8: 放归闭环
  - [x] SubTask 8.1: 治愈动物回收视图（从医院回收、纳入捕捉点在库）
  - [x] SubTask 8.2: 放归登记与原小区物业匹配视图
  - [x] SubTask 8.3: 物业线上确认视图（接收人姓名/电话/电子签字）
  - [x] SubTask 8.4: 放归台账视图，自动核算"接收总数-领养数-安乐死数=放归数"

- [x] Task 9: 领养业务
  - [x] SubTask 9.1: 医院待领养宠物资料维护视图（照片、简介、性格、身体情况）
  - [x] SubTask 9.2: 领养大厅聚合视图（全市待领养宠物列表、详情、领养流程文档）
  - [x] SubTask 9.3: 捕捉点线下领养登记视图（领养人信息、资质审核、承诺书/协议上传）
  - [x] SubTask 9.4: 开通领养人账号、建立领养人-宠物关联

- [x] Task 10: 回访打卡与黑名单
  - [x] SubTask 10.1: 领养人月度打卡视图（上传照片、近况说明、提交打卡）
  - [x] SubTask 10.2: 捕捉点打卡审核视图（按月审核、合格续养/违规收回）
  - [x] SubTask 10.3: 黑名单管理视图（拉黑、列表查询、录入身份信息时自动弹窗拦截）

- [x] Task 11: 安乐死处置
  - [x] SubTask 11.1: 医院安乐死登记视图（原因、病情说明、尸体移交记录）
  - [x] SubTask 11.2: 扣减医院活体库存、状态终止、捕捉点备案视图

- [x] Task 12: 定时任务
  - [x] SubTask 12.1: 配置 django-q2 或 Django cron
  - [x] SubTask 12.2: 实现定时任务：诊疗完成 5 天后宠物状态自动转"待领养"并上架领养大厅

## 阶段三：政府监管端（`supervision`）

- [x] Task 13: 政府监管功能
  - [x] SubTask 13.1: 数据总览大屏视图（捕捉/诊疗/放归/领养/安乐死总量、各机构占比、物料消耗统计图表）
  - [x] SubTask 13.2: 机构管理视图（小区/医院/捕捉点 新增/编辑/启停，含所属区县字段）
  - [x] SubTask 13.3: 账号权限管理视图（角色配置、各端口账号新增/停用/权限分配、下级区县组织）
  - [x] SubTask 13.4: 全业务监管视图（捕捉/转运/诊疗/放归/领养回访监管）
  - [x] SubTask 13.5: 物料全局监管视图（采购/下发/消耗/库存全链路统计溯源、异常预警）
  - [x] SubTask 13.6: 全局台账中心视图（多维度筛选、导出、打印、溯源查询）
  - [x] SubTask 13.7: 系统配置视图（编号规则、字典配置、操作日志、系统参数）

## 阶段四：前端模板改造

- [x] Task 14: 公共资源与门户
  - [x] SubTask 14.1: 将 `solo/css/tnr-traditional.css` 与 `solo/js/tnr-common.js` 迁移至 Django `static/`
  - [x] SubTask 14.2: 改造 `index.html` 为 Django 模板（门户入口、四端链接）
  - [x] SubTask 14.3: 实现统一登录页与按角色跳转

- [x] Task 15: 捕捉点端模板（`shelter.html` 改造）
  - [x] SubTask 15.1: 首页看板（待办、数据概览、库存预警、快捷入口）
  - [x] SubTask 15.2: 捕捉登记页（表单、照片上传、签名面板、批量编号预览）
  - [x] SubTask 15.3: 主人领回页、捕捉台账页
  - [x] SubTask 15.4: 转运拆分下发页、交接记录页
  - [x] SubTask 15.5: 物料采购入库页、下发出库页、库存台账页
  - [x] SubTask 15.6: 动物回收放归页、放归确认页
  - [x] SubTask 15.7: 安乐死备案管理页
  - [x] SubTask 15.8: 领养审核管理页、回访管控与黑名单页
  - [x] SubTask 15.9: 全量台账中心页、个人中心页

- [x] Task 16: 医院端模板（`hospital.html` 改造）
  - [x] SubTask 16.1: 首页看板（待接收、待诊疗、库存预警、待领养维护）
  - [x] SubTask 16.2: 交接接收页（签收/驳回）
  - [x] SubTask 16.3: 诊疗操作页（绝育病历弹窗、疫苗/驱虫/芯片联动）
  - [x] SubTask 16.4: 物料库存页（入库确认、实时库存、异动报备、台账）
  - [x] SubTask 16.5: 领养宠物资料维护页、安乐死处置页
  - [x] SubTask 16.6: 单据台账页、个人中心页

- [x] Task 17: 领养人 H5 端模板（`adopter.html` 改造）
  - [x] SubTask 17.1: 领养大厅首页（卡片列表、详情、领养流程文档）
  - [x] SubTask 17.2: 宠物详情页（捕捉/诊疗/绝育疫苗芯片溯源）
  - [x] SubTask 17.3: 我的领养档案页（全生命周期溯源）
  - [x] SubTask 17.4: 月度回访打卡页（上传照片、近况说明、历史记录）
  - [x] SubTask 17.5: 消息中心页、个人中心页

- [x] Task 18: 政府监管端模板（`government.html` 改造）
  - [x] SubTask 18.1: 数据大屏页（统计卡片、图表，可用 ECharts CDN）
  - [x] SubTask 18.2: 机构管理页、账号权限管理页
  - [x] SubTask 18.3: 全业务监管页、物料全局监管页
  - [x] SubTask 18.4: 全局台账中心页、系统配置页

## 阶段五：部署与验证

- [x] Task 19: 部署配置
  - [x] SubTask 19.1: 编写 `deploy.sh` 一键部署脚本（安装依赖、迁移、收集静态、配置 Nginx/Gunicorn/Supervisor）
  - [x] SubTask 19.2: 编写 Nginx 配置示例（静态/Media 直出、反代 Gunicorn）
  - [x] SubTask 19.3: 编写 `README` 部署说明（VPS 2核4G 推荐配置、域名/SSL 建议）—— 仅作为部署文档内联到 deploy.sh 注释，不单独建 md

- [x] Task 20: 端到端验证
  - [x] SubTask 20.1: 加载 fixtures 种子数据，本地启动验证四端登录
  - [x] SubTask 20.2: 走通主业务闭环：捕捉→转运→诊疗→放归 / 领养 / 安乐死
  - [x] SubTask 20.3: 验证物料库存联动与双台账
  - [x] SubTask 20.4: 验证区县数据隔离与黑名单拦截
  - [x] SubTask 20.5: 验证定时任务（5 天自动转待领养）

# Task Dependencies

- Task 2 依赖 Task 1（工程骨架）
- Task 3 依赖 Task 2（User 模型）
- Task 4–12 依赖 Task 3（账号权限），可在 Task 3 完成后并行推进（不同业务域相互独立）
- Task 13 依赖 Task 4–12（监管端依赖业务数据）
- Task 14–18 依赖对应后端 Task（模板渲染需视图就绪）
- Task 14（公共资源）可与其他后端并行，最早启动
- Task 19 依赖 Task 1（配置文件）
- Task 20 依赖全部 Task 完成
