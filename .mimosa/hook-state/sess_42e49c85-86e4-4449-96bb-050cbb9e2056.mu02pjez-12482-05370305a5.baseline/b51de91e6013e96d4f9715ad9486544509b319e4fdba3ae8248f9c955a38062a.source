from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'role', 'district', 'institution', 'phone', 'status', 'is_staff', 'is_superuser')
    list_filter = ('role', 'status', 'is_staff', 'is_superuser', 'district')
    search_fields = ('username', 'phone', 'first_name', 'last_name')
    fieldsets = UserAdmin.fieldsets + (
        ('扩展信息', {'fields': ('role', 'district', 'institution', 'phone', 'status')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('扩展信息', {'fields': ('role', 'district', 'institution', 'phone', 'status')}),
    )
