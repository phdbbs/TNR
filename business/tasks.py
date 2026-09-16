"""
TNR 业务系统 - 定时任务
诊疗完成5天后宠物自动转'待领养'并上架领养大厅。
"""
from datetime import timedelta

from django.utils import timezone

from business.models import Treatment, Pet, AdoptionHallListing


def auto_promote_to_adoptable(force=False):
    """诊疗完成5天后宠物自动转'待领养'并上架领养大厅。

    此函数可由 django-q2 定时调度执行，也可通过 management command 手动触发。
    :param force: True 时跳过5天时间限制，处理所有已完成诊疗记录（用于测试或补偿）
    """
    if force:
        treatments = Treatment.objects.filter(status='completed')
    else:
        cutoff = timezone.now() - timedelta(days=5)
        treatments = Treatment.objects.filter(
            status='completed',
            created_at__lte=cutoff
        )
    count = 0
    for t in treatments:
        pet = t.pet
        # 已作废的档案不再自动上架
        if pet is None or pet.is_deleted:
            continue
        if pet.status == 'in_treatment':
            pet.status = 'pending_adopt'
            pet.save(update_fields=['status'])
            # 已存在上架记录时必须显式置回 is_active：
            # 领养完成时记录会被下架，get_or_create 不会重新激活它，
            # 会导致宠物已是「待领养」却不出现在领养大厅（幽灵待领养）。
            listing = AdoptionHallListing.objects.filter(pet=pet).first()
            if listing:
                listing.is_active = True
                if pet.hospital:
                    listing.hospital = pet.hospital
                    listing.hospital_name = pet.hospital.name
                listing.save(update_fields=['is_active', 'hospital', 'hospital_name'])
            else:
                AdoptionHallListing.objects.create(
                    pet=pet,
                    hospital=pet.hospital,
                    hospital_name=pet.hospital.name if pet.hospital else '',
                    is_active=True,
                    published_at=timezone.localdate(),
                )
            count += 1
    return f'Promoted {count} pets to adoptable'
