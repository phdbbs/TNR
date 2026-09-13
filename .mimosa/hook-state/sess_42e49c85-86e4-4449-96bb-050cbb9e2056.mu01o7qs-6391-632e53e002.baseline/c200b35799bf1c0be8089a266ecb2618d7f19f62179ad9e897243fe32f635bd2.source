from django.contrib import admin
from .models import District, Institution


@admin.register(District)
class DistrictAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'code', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'code')
    ordering = ('id',)


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'type', 'district', 'contact', 'phone', 'status')
    list_filter = ('type', 'status', 'district')
    search_fields = ('name', 'address', 'contact', 'phone')
    ordering = ('id',)
