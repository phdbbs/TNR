from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('', views.dashboard_redirect, name='dashboard_redirect'),
    path('api/me/', views.api_me, name='api_me'),
]
