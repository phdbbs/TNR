"""
TNR 业务系统 - 定时任务
- 诊疗完成5天后宠物自动转'待领养'并上架领养大厅。
- 每月给「已领养但尚未回访打卡」的领养人发回访提醒（第三十七轮）。
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


def send_checkin_reminders(force=False):
    """给「已领养、但本月还没回访打卡」的领养人各发一条提醒。

    这是 `Message.TYPE_CHOICES` 里 ``checkin_reminder``（回访提醒）的
    **唯一真实产生点**。第三十七轮之前，这个类型只有 `seed_data` 造过
    2 条演示数据，真实业务里永远不会产生 —— 即「枚举值存在、但没有任何
    写入路径」，与 `Institution.status`「字段存在但无人读」是同一类缺口
    （这次是反方向：有人读、没人写）。

    :param force: True 时跳过「本月已提醒过」的去重（测试 / 补发用）
    :return: 形如 ``'Sent N checkin reminders'`` 的摘要

    判据细节：

    * **只催「已完成」的领养** —— `pending_claim`（待领出）还没真正交到
      领养人手上，催他打卡是错的。
    * **本月已有 pending / approved 打卡的不催**；`rejected`（被驳回）
      **仍然要催** —— 驳回意味着需要重新提交，那正是提醒的意义。
    * **按 (宠物, 领养人) 组合判定**：同一个人领养多只动物，是**每只**
      都要各自打卡，不能因为「他给 A 打过了」就漏掉 B。
    * **去重按标题**（含月份），保证同一个月不会反复催同一个人。
    """
    from business.models import Adoption, CheckIn, Message

    month = timezone.localdate().strftime('%Y-%m')
    title = f'{month} 回访打卡提醒'

    adoptions = (Adoption.objects
                 .filter(status='completed',
                         adopter__isnull=False,
                         adopter__is_active=True,
                         pet__is_deleted=False)
                 .select_related('adopter', 'pet'))

    # 本月已有有效打卡的 (宠物, 领养人) 组合 —— 一次查出，避免 N+1
    checked = set(
        CheckIn.objects
        .filter(month=month, status__in=('pending', 'approved'))
        .values_list('pet_id', 'adopter_id'))

    sent = 0
    for a in adoptions:
        if (a.pet_id, a.adopter_id) in checked:
            continue
        if not force and Message.objects.filter(
                user_id=a.adopter_id, type='checkin_reminder',
                title=title).exists():
            continue
        pet_label = a.pet_code or (a.pet.name if a.pet else '') or '您的动物'
        Message.objects.create(
            user_id=a.adopter_id,
            type='checkin_reminder',
            title=title,
            content=f'您领养的 {pet_label} 本月还未完成回访打卡，'
                    f'请及时上传回访照片。',
        )
        sent += 1
    return f'Sent {sent} checkin reminders'
