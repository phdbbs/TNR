"""TNR 业务域测试包（2026 重写版）。

旧版单文件端到端测试已移除（可在 git 历史中查阅），本套件按域拆分：
- base.py                  共享夹具与 API 助手（最小化自建数据，不依赖 seed_data）
- test_services.py         服务层单元测试
- test_models.py           模型约束与默认值
- test_capture_views.py    捕捉登记 / 主人领回
- test_transfer_views.py   转运拆分下发 / 签收 / 驳回 / 重发
- test_treatment_views.py  诊疗与库存联动
- test_material_views.py   物料供应链双台账
- test_release_views.py    放养闭环
- test_adoption_views.py   领养登记 / 在线申请 / 领养大厅
- test_checkin_views.py    回访打卡审核
- test_blacklist_views.py  黑名单
- test_euthanasia_views.py 安乐死处置
- test_portal_views.py     门户页面 / 消息 / 溯源
- test_tasks.py            定时任务
- test_integration.py      全生命周期端到端
"""
