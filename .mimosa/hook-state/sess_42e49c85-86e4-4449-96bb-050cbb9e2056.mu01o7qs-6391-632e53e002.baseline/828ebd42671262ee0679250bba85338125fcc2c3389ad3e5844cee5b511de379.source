"""
Task 13: 政府监管后端 - URL 路由
"""
from django.urls import path

from . import views

app_name = 'supervision'

urlpatterns = [
    # 数据大屏
    path('dashboard/', views.dashboard_stats, name='dashboard_stats'),

    # 机构管理
    path('institutions/', views.institution_list, name='institution_list'),
    path('institutions/create/', views.institution_create, name='institution_create'),
    path('institutions/<int:pk>/edit/', views.institution_edit, name='institution_edit'),
    path('institutions/<int:pk>/toggle/', views.institution_toggle_status, name='institution_toggle_status'),

    # 区县管理
    path('districts/', views.district_list, name='district_list'),
    path('districts/create/', views.district_create, name='district_create'),
    path('districts/<int:pk>/edit/', views.district_edit, name='district_edit'),
    path('districts/<int:pk>/toggle/', views.district_toggle_status, name='district_toggle_status'),
    path('districts/<int:pk>/delete/', views.district_delete, name='district_delete'),

    # 用户管理
    path('users/', views.user_list, name='user_list'),
    path('users/create/', views.user_create, name='user_create'),
    path('users/<int:pk>/toggle/', views.user_toggle_status, name='user_toggle_status'),

    # 监管
    path('business/', views.business_supervision, name='business_supervision'),
    path('materials/', views.material_supervision, name='material_supervision'),
    path('ledger/', views.ledger_center, name='ledger_center'),
    path('logs/', views.operation_logs, name='operation_logs'),
    path('config/', views.system_config, name='system_config'),
]
