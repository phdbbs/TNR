"""
TNR 业务系统 - URL 路由
所有业务接口的 URL 注册。
"""
from django.urls import path

from . import (
    views_capture, views_transfer, views_treatment, views_material,
    views_release, views_adoption, views_checkin, views_euthanasia,
    views_portal, views_chip,
)
# ⚠ 单据打印视图（`views_print`）**不在这里注册** —— 它返回整页 HTML，
#   而本文件下所有路由都在 `/api/business/` 前缀里（契约 = JSON 信封）。
#   注册点在 `tnr_system/urls.py` 的 `/print/<doc>/<pk>/`。

app_name = 'business'

urlpatterns = [
    # ============================================
    # Task 4: 捕捉登记与主人领回
    # ============================================
    path('captures/', views_capture.capture_list, name='capture_list'),
    path('captures/codes-preview/', views_capture.pet_codes_preview, name='capture_codes_preview'),
    path('geocode/reverse/', views_capture.geocode_reverse, name='geocode_reverse'),
    path('geocode/ip/', views_capture.geocode_ip, name='geocode_ip'),
    path('captures/create/', views_capture.capture_create, name='capture_create'),
    path('captures/<int:pk>/', views_capture.capture_detail, name='capture_detail'),
    path('captures/<int:pk>/update/', views_capture.capture_update, name='capture_update'),
    path('captures/<int:pk>/delete/', views_capture.capture_delete, name='capture_delete'),
    path('captures/<int:pk>/owner-return/', views_capture.owner_return_create, name='owner_return_create'),
    path('owner-returns/', views_capture.owner_return_list, name='owner_return_list'),

    # ============================================
    # Task 5: 转运拆分下发
    # ============================================
    path('transfers/', views_transfer.transfer_list, name='transfer_list'),
    path('transfers/create/', views_transfer.transfer_create, name='transfer_create'),
    path('transfers/<int:pk>/receive/', views_transfer.transfer_receive, name='transfer_receive'),
    path('transfers/<int:pk>/reject/', views_transfer.transfer_reject, name='transfer_reject'),
    # 注：transfers/<pk>/resend/（重新下发）已移除 —— 见 views_transfer.transfer_resend 处说明
    path('transfers/<int:pk>/withdraw/', views_transfer.transfer_withdraw, name='transfer_withdraw'),

    # ============================================
    # Task 6: 诊疗与物料库存联动
    # ============================================
    path('treatments/', views_treatment.treatment_list, name='treatment_list'),
    # 注：treatments/create/（Task 6 旧口径「一次请求提交整张单、当场扣库存」）
    # 已移除 —— 第四十七轮。它唯一的界面入口是医院端「快速登记」标签，删除前
    # 已把唯一独有价值（绝育手术病历）并入三步流程的「绝育」项目卡片；
    # 且这条路径「当场扣库存」与三步流程「执行时才扣」是两套会漂移的口径。
    # 读写同一批 `items_*` 列的只剩三步流程一条写路径。
    # v5.1 三步流程（第四十六轮）：库存在「执行」那一刻才扣。
    # 具体路径放在 `<int:pk>` 之前 —— 虽然 `board` / `open` 不会被
    # `<int:pk>` 匹配，但把通配放最后是这条路由表一直以来的约定，
    # 免得将来加一条匹配任意字符串的规则时被它悄悄吃掉。
    path('treatments/board/', views_treatment.treatment_board, name='treatment_board'),
    path('treatments/open/', views_treatment.treatment_open, name='treatment_open'),
    path('treatments/<int:pk>/check/', views_treatment.treatment_check, name='treatment_check'),
    path('treatments/<int:pk>/plan/', views_treatment.treatment_plan, name='treatment_plan'),
    path('treatments/<int:pk>/diagnose/', views_treatment.treatment_diagnose, name='treatment_diagnose'),
    path('treatments/<int:pk>/undiagnose/', views_treatment.treatment_undiagnose, name='treatment_undiagnose'),
    path('treatments/<int:pk>/items/<int:item_id>/execute/',
         views_treatment.treatment_item_execute, name='treatment_item_execute'),
    path('treatments/<int:pk>/items/<int:item_id>/undo/',
         views_treatment.treatment_item_undo, name='treatment_item_undo'),
    path('treatments/<int:pk>/done/', views_treatment.treatment_done, name='treatment_done'),
    path('treatments/<int:pk>/dispose/', views_treatment.treatment_dispose, name='treatment_dispose'),
    # 诊疗后照片（B2）：第三步办结环节的当场留证，独立接口见视图注释。
    path('treatments/<int:pk>/photo/', views_treatment.treatment_photo, name='treatment_photo'),
    path('treatments/<int:pk>/', views_treatment.treatment_detail, name='treatment_detail'),

    # ============================================
    # Task 7: 物料供应链与双台账
    # ============================================
    path('materials/', views_material.material_list, name='material_list'),
    path('materials/purchase/', views_material.purchase_create, name='purchase_create'),
    path('materials/dispatch/', views_material.dispatch_create, name='dispatch_create'),
    path('materials/<int:pk>/receive/', views_material.material_receive, name='material_receive'),
    path('materials/adjustment/', views_material.stock_adjustment, name='stock_adjustment'),
    path('materials/transactions/', views_material.material_transactions, name='material_transactions'),
    # 可下发的接收机构（第四十五轮）——与 `dispatch_create` 共用同一份范围判据，
    # 前端照单渲染就不会再出现「下拉里选得到、一发就 400」。
    path('materials/dispatch-targets/', views_material.dispatch_target_options,
         name='dispatch_target_options'),
    path('materials/shelter-ledger/', views_material.shelter_stock_ledger, name='shelter_stock_ledger'),
    path('materials/hospital-ledger/', views_material.hospital_stock_ledger, name='hospital_stock_ledger'),
    # 物料批次（B5）：只读接口。批次记账收口在 `adjust_stock`（写路径仍走
    # 采购/下发/签收/异动那些接口），这里只提供「本机构的批次明细」与
    # 「FEFO 扣减预览」两个读面，供前端行内展开与表单预检。
    path('materials/<int:pk>/batches/', views_material.material_batches, name='material_batches'),
    path('materials/<int:pk>/batch-preview/', views_material.material_batch_preview,
         name='material_batch_preview'),
    # 芯片（第四十六轮）：只读接口。写路径（入库/下发/签收）留在
    # `materials/purchase|dispatch|<pk>/receive` —— 它们本来就是
    # 「一笔物料业务」的两半，拆开会让「库存扣了、芯片没动」重新出现。
    # `preview` 是 POST 但同样**不写库**（只读预检，见它的 docstring）。
    path('chips/', views_chip.chip_list, name='chip_list'),
    path('chips/available/', views_chip.chip_available, name='chip_available'),
    path('chips/preview/', views_chip.chip_preview, name='chip_preview'),
    path('chips/segments/', views_chip.chip_segments, name='chip_segments'),
    path('chips/trace/', views_chip.chip_trace, name='chip_trace'),

    # ============================================
    # Task 8: 放归闭环
    # ============================================
    path('releases/', views_release.release_list, name='release_list'),
    path('releases/create/', views_release.release_create, name='release_create'),
    path('releases/<int:pk>/confirm/', views_release.release_confirm, name='release_confirm'),

    # ============================================
    # Task 9: 领养业务
    # ============================================
    path('adoptions/', views_adoption.adoption_list, name='adoption_list'),
    path('adoptions/hall/', views_adoption.adoption_hall_list, name='adoption_hall_list'),
    path('adoptions/hall/<int:pk>/', views_adoption.adoption_hall_detail, name='adoption_hall_detail'),
    path('adoptions/<int:pk>/edit-info/', views_adoption.adoption_info_edit, name='adoption_info_edit'),
    path('adoptions/register/', views_adoption.adoption_register, name='adoption_register'),
    path('adoptions/<int:pk>/confirm-claim/', views_adoption.adoption_confirm_claim, name='adoption_confirm_claim'),
    path('adoptions/<int:pk>/reclaim/', views_adoption.adoption_reclaim, name='adoption_reclaim'),
    # 在线领养申请
    path('adoptions/apply/', views_adoption.adoption_apply, name='adoption_apply'),
    path('adoptions/my-applications/', views_adoption.my_applications, name='my_applications'),
    path('adoptions/applications/', views_adoption.adoption_application_list, name='adoption_application_list'),
    path('adoptions/applications/<int:pk>/review/', views_adoption.adoption_application_review, name='adoption_application_review'),
    # 领养人撤回自己的申请（`pending` → `cancelled`）。第四十六轮新增：
    # `AdoptionApplication.status` 里的 `cancelled` 此前没有任何产生端。
    path('adoptions/applications/<int:pk>/cancel/', views_adoption.adoption_application_cancel, name='adoption_application_cancel'),

    # ============================================
    # Task 10: 回访打卡与黑名单
    # ============================================
    path('checkins/', views_checkin.checkin_list, name='checkin_list'),
    path('checkins/create/', views_checkin.checkin_create, name='checkin_create'),
    path('checkins/<int:pk>/review/', views_checkin.checkin_review, name='checkin_review'),
    path('blacklist/', views_checkin.blacklist_list, name='blacklist_list'),
    path('blacklist/create/', views_checkin.blacklist_create, name='blacklist_create'),
    path('blacklist/check/', views_checkin.blacklist_check, name='blacklist_check'),
    path('blacklist/<int:pk>/update/', views_checkin.blacklist_update, name='blacklist_update'),
    path('blacklist/<int:pk>/delete/', views_checkin.blacklist_delete, name='blacklist_delete'),

    # ============================================
    # Task 11: 安乐死处置
    # ============================================
    path('euthanasia/', views_euthanasia.euthanasia_list, name='euthanasia_list'),
    path('euthanasia/create/', views_euthanasia.euthanasia_create, name='euthanasia_create'),
    path('euthanasia/<int:pk>/body-receive/', views_euthanasia.body_receive, name='body_receive'),

    # ============================================
    # 门户配套接口（领养人端）
    # ============================================
    path('portal/adoptions/', views_portal.my_adoptions, name='portal_my_adoptions'),
    path('portal/messages/', views_portal.my_messages, name='portal_my_messages'),
    path('portal/messages/<int:pk>/read/', views_portal.mark_message_read, name='portal_message_read'),

    # ============================================
    # 门户配套接口（医院端）
    # ============================================
    path('pets/', views_portal.hospital_pets, name='hospital_pets'),
    # 一宠一档：动物档案台账（捕捉端「全量台账」与政府端「一宠一档」同源）
    path('pets/archive/', views_portal.pet_archive, name='pet_archive'),
    path('hall-listings/', views_portal.hospital_hall_listings, name='hospital_hall_listings'),

    # ============================================
    # 宠物全生命周期溯源
    # ============================================
    path('pets/<int:pet_id>/lifecycle/', views_portal.pet_lifecycle, name='pet_lifecycle'),
]
