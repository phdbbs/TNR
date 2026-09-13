from django.contrib import admin
from .models import (
    Pet, Capture, OwnerReturn, Transfer, Treatment, Material, MaterialTransaction,
    Chip, Release, Adoption, AdoptionApplication, CheckIn, Blacklist, Euthanasia, Message, AdoptionHallListing,
)


@admin.register(Pet)
class PetAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'species', 'gender', 'status', 'district', 'shelter', 'hospital', 'created_at')
    list_filter = ('species', 'gender', 'status', 'district')
    search_fields = ('code', 'name', 'chip_no', 'breed', 'color')
    date_hierarchy = 'created_at'


@admin.register(Capture)
class CaptureAdmin(admin.ModelAdmin):
    list_display = ('id', 'ledger_no', 'shelter_name', 'community_name', 'pet_count', 'operator_name', 'created_at')
    list_filter = ('district', 'status')
    search_fields = ('ledger_no', 'shelter_name', 'community_name', 'address', 'contact_person', 'contact_phone')
    date_hierarchy = 'created_at'


@admin.register(OwnerReturn)
class OwnerReturnAdmin(admin.ModelAdmin):
    list_display = ('id', 'pet_code', 'owner_name', 'owner_phone', 'ledger_no', 'district', 'created_at')
    list_filter = ('district',)
    search_fields = ('pet_code', 'owner_name', 'owner_phone', 'owner_id_card', 'ledger_no')
    date_hierarchy = 'created_at'


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = ('id', 'ledger_no', 'from_shelter_name', 'to_hospital_name', 'pet_count', 'status', 'district', 'created_at')
    list_filter = ('status', 'district')
    search_fields = ('ledger_no', 'from_shelter_name', 'to_hospital_name', 'pet_codes')
    date_hierarchy = 'created_at'


@admin.register(Treatment)
class TreatmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'pet_code', 'hospital_name', 'items_sterilization', 'items_vaccine', 'items_deworming', 'items_chip', 'status', 'district', 'created_at')
    list_filter = ('status', 'district', 'items_sterilization', 'items_vaccine', 'items_deworming', 'items_chip')
    search_fields = ('pet_code', 'hospital_name', 'chip_no', 'vaccine_batch_no', 'deworming_batch_no')
    date_hierarchy = 'created_at'


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category', 'unit', 'batch_no', 'shelter_stock', 'safety_stock', 'expiry_date', 'district')
    list_filter = ('category', 'district')
    search_fields = ('name', 'batch_no', 'supplier', 'specification', 'chip_range_start', 'chip_range_end')


@admin.register(MaterialTransaction)
class MaterialTransactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'type', 'material_name', 'quantity', 'unit', 'from_to', 'ledger_no', 'district', 'date', 'created_at')
    list_filter = ('type', 'district')
    search_fields = ('material_name', 'batch_no', 'supplier', 'from_to', 'ledger_no', 'operator_name')
    date_hierarchy = 'date'


@admin.register(Chip)
class ChipAdmin(admin.ModelAdmin):
    list_display = ('number', 'status', 'pet', 'used_at', 'created_at')
    list_filter = ('status',)
    search_fields = ('number',)
    date_hierarchy = 'created_at'


@admin.register(Release)
class ReleaseAdmin(admin.ModelAdmin):
    list_display = ('id', 'pet_code', 'community_name', 'receiver_name', 'receiver_phone', 'status', 'released_at', 'district', 'created_at')
    list_filter = ('status', 'district')
    search_fields = ('pet_code', 'community_name', 'receiver_name', 'receiver_phone', 'ledger_no')
    date_hierarchy = 'released_at'


@admin.register(Adoption)
class AdoptionAdmin(admin.ModelAdmin):
    list_display = ('id', 'pet_code', 'adopter_name', 'adopter_phone', 'hospital_name', 'status', 'adopted_at', 'district', 'created_at')
    list_filter = ('status', 'district')
    search_fields = ('pet_code', 'adopter_name', 'adopter_phone', 'adopter_id_card', 'ledger_no')
    date_hierarchy = 'adopted_at'


@admin.register(AdoptionApplication)
class AdoptionApplicationAdmin(admin.ModelAdmin):
    list_display = ('id', 'pet_code', 'applicant_name', 'applicant_phone', 'hospital_name', 'status', 'applied_at', 'created_at')
    list_filter = ('status', 'hospital')
    search_fields = ('pet_code', 'applicant_name', 'applicant_phone', 'applicant_id_card', 'reason')
    date_hierarchy = 'created_at'
    actions = ['approve_selected', 'reject_selected']

    def approve_selected(self, request, queryset):
        from django.utils import timezone
        updated = queryset.filter(status='pending').update(
            status='approved', reviewed_by=request.user, reviewed_at=timezone.now())
        if updated:
            self.message_user(request, f'已通过 {updated} 条申请')
    approve_selected.short_description = '通过选中的申请'

    def reject_selected(self, request, queryset):
        from django.utils import timezone
        updated = queryset.filter(status='pending').update(
            status='rejected', reviewed_by=request.user, reviewed_at=timezone.now())
        if updated:
            self.message_user(request, f'已拒绝 {updated} 条申请')
    reject_selected.short_description = '拒绝选中的申请'


@admin.register(CheckIn)
class CheckInAdmin(admin.ModelAdmin):
    list_display = ('id', 'pet_code', 'adopter_name', 'month', 'status', 'created_at')
    list_filter = ('status', 'month')
    search_fields = ('pet_code', 'adopter_name', 'note')
    date_hierarchy = 'created_at'


@admin.register(Blacklist)
class BlacklistAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'id_card', 'phone', 'violation_date', 'district', 'created_at')
    list_filter = ('district',)
    search_fields = ('name', 'id_card', 'phone')
    date_hierarchy = 'violation_date'


@admin.register(Euthanasia)
class EuthanasiaAdmin(admin.ModelAdmin):
    list_display = ('id', 'pet_code', 'hospital_name', 'euthanized_at', 'body_received', 'body_received_by_name', 'district', 'created_at')
    list_filter = ('body_received', 'district')
    search_fields = ('pet_code', 'hospital_name', 'ledger_no', 'body_received_by_name')
    date_hierarchy = 'euthanized_at'


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'type', 'title', 'is_read', 'created_at')
    list_filter = ('type', 'is_read')
    search_fields = ('title', 'content', 'user__username')
    date_hierarchy = 'created_at'


@admin.register(AdoptionHallListing)
class AdoptionHallListingAdmin(admin.ModelAdmin):
    list_display = ('id', 'pet', 'hospital_name', 'is_active', 'published_at', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('hospital_name', 'personality', 'body_condition')
    date_hierarchy = 'published_at'
