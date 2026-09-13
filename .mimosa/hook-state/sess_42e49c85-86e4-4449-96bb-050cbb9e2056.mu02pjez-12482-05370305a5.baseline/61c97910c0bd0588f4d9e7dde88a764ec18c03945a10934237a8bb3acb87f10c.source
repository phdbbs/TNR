"""
TNR 业务系统 - URL 路由
所有业务接口的 URL 注册。
"""
from django.urls import path

from . import (
    views_capture, views_transfer, views_treatment, views_material,
    views_release, views_adoption, views_checkin, views_euthanasia,
    views_portal,
)

app_name = 'business'

urlpatterns = [
    # ============================================
    # Task 4: 捕捉登记与主人领回
    # ============================================
    path('captures/', views_capture.capture_list, name='capture_list'),
    path('captures/codes-preview/', views_capture.pet_codes_preview, name='capture_codes_preview'),
    path('captures/create/', views_capture.capture_create, name='capture_create'),
    path('captures/<int:pk>/', views_capture.capture_detail, name='capture_detail'),
    path('captures/<int:pk>/owner-return/', views_capture.owner_return_create, name='owner_return_create'),
    path('owner-returns/', views_capture.owner_return_list, name='owner_return_list'),

    # ============================================
    # Task 5: 转运拆分下发
    # ============================================
    path('transfers/', views_transfer.transfer_list, name='transfer_list'),
    path('transfers/create/', views_transfer.transfer_create, name='transfer_create'),
    path('transfers/<int:pk>/receive/', views_transfer.transfer_receive, name='transfer_receive'),
    path('transfers/<int:pk>/reject/', views_transfer.transfer_reject, name='transfer_reject'),
    path('transfers/<int:pk>/resend/', views_transfer.transfer_resend, name='transfer_resend'),

    # ============================================
    # Task 6: 诊疗与物料库存联动
    # ============================================
    path('treatments/', views_treatment.treatment_list, name='treatment_list'),
    path('treatments/create/', views_treatment.treatment_create, name='treatment_create'),
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
    path('materials/shelter-ledger/', views_material.shelter_stock_ledger, name='shelter_stock_ledger'),
    path('materials/hospital-ledger/', views_material.hospital_stock_ledger, name='hospital_stock_ledger'),

    # ============================================
    # Task 8: 放养闭环
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
    # 在线领养申请
    path('adoptions/apply/', views_adoption.adoption_apply, name='adoption_apply'),
    path('adoptions/my-applications/', views_adoption.my_applications, name='my_applications'),
    path('adoptions/applications/', views_adoption.adoption_application_list, name='adoption_application_list'),
    path('adoptions/applications/<int:pk>/review/', views_adoption.adoption_application_review, name='adoption_application_review'),

    # ============================================
    # Task 10: 回访打卡与黑名单
    # ============================================
    path('checkins/', views_checkin.checkin_list, name='checkin_list'),
    path('checkins/create/', views_checkin.checkin_create, name='checkin_create'),
    path('checkins/<int:pk>/review/', views_checkin.checkin_review, name='checkin_review'),
    path('blacklist/', views_checkin.blacklist_list, name='blacklist_list'),
    path('blacklist/create/', views_checkin.blacklist_create, name='blacklist_create'),
    path('blacklist/check/', views_checkin.blacklist_check, name='blacklist_check'),

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
    path('hall-listings/', views_portal.hospital_hall_listings, name='hospital_hall_listings'),

    # ============================================
    # 宠物全生命周期溯源
    # ============================================
    path('pets/<int:pet_id>/lifecycle/', views_portal.pet_lifecycle, name='pet_lifecycle'),
]
