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
        if pet.status == 'in_treatment':
            pet.status = 'pending_adopt'
            pet.save(update_fields=['status'])
            AdoptionHallListing.objects.get_or_create(
                pet=pet,
                defaults={
                    'hospital': pet.hospital,
                    'hospital_name': pet.hospital.name if pet.hospital else '',
                    'is_active': True,
                    'published_at': timezone.now().date(),
                }
            )
            count += 1
    return f'Promoted {count} pets to adoptable'
