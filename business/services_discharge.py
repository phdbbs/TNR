"""出院去向 ↔「建议去向」的唯一对应关系（第五十七轮）。

业务口径（业务方原话）：出院每个去向能选到的动物，必须是**诊疗完成后、
最后一张待办结诊疗单的建议去向就是该去向**的那只 —— 建议「领养」的动物
不能从「死亡 / 领回 / 放归」里被选走；确实要换去向，先改建议去向，
再回去院办理。

判据为什么必须在后端
--------------------
出院候选原先**只**在前端 `hospital.js::dspPendingPets()` 分流，四个写接口
（领养登记 / 死亡登记 / 主人领回 / 运回单）一个都不读 `disposal_advised`。
结果是：界面挡住了，直接 POST 仍然能给一只建议「放归」的动物开领养单，
同一只动物于是同时有两个归宿，而且两张单据都能走到「已办结」。
前端只做展示，判据在这里定义一次，接口与页面都引它。
"""

from .models import Euthanasia, Pet, Treatment
from .services import (
    pet_has_active_adoption, pet_has_approved_application,
    pet_has_pending_release, pet_has_pending_return,
)

#: 出院去向 key → 允许的 `Treatment.disposal_advised` key 集合。
#: 去向 key 与接口/前端共用（`adoption` / `death` / `owner` / `release`）。
#: 「安乐死」与「死亡 / 无害化」共用同一张登记表单（靠 `Euthanasia.kind` 区分），
#: 所以 `death` 接受两个建议 key。
DISCHARGE_ADVICE = {
    'adoption': ('pending_adopt',),
    'death': ('euthanized', 'dead'),
    'owner': ('owner_returned',),
    'release': ('released',),
}

#: 去向 key → 界面文案。错误提示与页签标题共用，避免两边各写一份。
DISCHARGE_LABELS = {
    'adoption': '领养',
    'death': '死亡（含安乐死）',
    'owner': '主人领回',
    'release': '放归',
}

#: 动物已被一张出院单据接管、但还没到终态的状态。
#: 这些状态下改建议去向 = 单据说一个归宿、建议说另一个归宿。
OCCUPYING_PET_STATUSES = ('return_pending', 'pending_release', 'pending_claim')


def disposal_label(key):
    """去向 key → 中文；未知 key 原样返回（不把「查不到」显示成空白）。"""
    key = key or ''
    return dict(Treatment.DISPOSAL_CHOICES).get(key, key)


def pending_treatment(pet):
    """该动物「诊疗完成、尚未办结」的**最新**一张诊疗单（= 待出院），没有则 None。

    取 `-id` 最后一条：一只动物可能有多张诊疗单（复查），
    业务口径要看的是**最后一次办结时给的**建议，不是历史第一张。
    """
    if pet is None:
        return None
    return Treatment.objects.filter(
        pet_id=pet.id, done_at__isnull=False,
        disposed_at__isnull=True).order_by('-id').first()


def advised_disposal(pet):
    """待出院动物当前的建议去向 key；不在待出院流程里返回空串。"""
    treatment = pending_treatment(pet)
    return (treatment.disposal_advised if treatment else '') or ''


def open_euthanasia(pet):
    """尚未出库的死亡 / 安乐死登记（遗体还在院，去向已被它占用）。"""
    if pet is None:
        return None
    return Euthanasia.objects.filter(
        pet_id=pet.id, outbound_at__isnull=True).order_by('-id').first()


def discharge_candidate_error(pet, kind):
    """能否按 `kind` 给这只动物开出院单据 —— 返回错误文案或 None。

    ⚠ 只约束**确实处于待出院**（有一张 `done_at` 已落、`disposed_at` 未落的
    诊疗单）的动物。没走过诊疗流程的动物（捕捉点侧登记、第五十六轮之前的
    存量行）没有建议可比，仍然只由各接口原有的 `Pet.status` 口径把关 ——
    在这里一刀切会让它们**再也办不了出院**。
    """
    if pet is None:
        return '动物档案不存在'
    if kind not in DISCHARGE_ADVICE:
        return f'出院去向「{kind}」不合法'
    treatment = pending_treatment(pet)
    if treatment is None:
        return None
    label = DISCHARGE_LABELS[kind]
    advised = treatment.disposal_advised or ''
    if not advised:
        return (f'{pet.code} 在「诊疗完成」时没有记录建议去向，'
                f'请先在诊疗看板把建议去向补录为「{label}」，再回来办理出院')
    if advised not in DISCHARGE_ADVICE[kind]:
        return (f'{pet.code} 的办结建议去向是「{disposal_label(advised)}」，'
                f'不能按「{label}」办理出院；确需更换去向，请先修改建议去向')
    return None


def advice_edit_error(pet):
    """现在还能不能改建议去向 —— 返回错误文案或 None（第五十七轮）。

    动物一旦被某张出院单据接管（已开出、或已经离院），建议去向就**锁死**：
    此时改建议不会改动任何单据，只会让「单据说领养、建议说放归」同时成立。
    要纠错得先撤回 / 作废那张单据 —— 单据回到可操作状态后，本判据自然放行。
    """
    if pet is None:
        return '动物档案不存在'
    if pet.status in Pet.TERMINAL_STATUSES:
        return (f'动物已出院（{pet.get_status_display()}），建议去向不能再修改；'
                '确需纠错请由平台端维护对应出院单据')
    if pet.status in OCCUPYING_PET_STATUSES:
        return (f'动物当前状态是「{pet.get_status_display()}」，已经在出院单据流程里，'
                '请先撤回或办结该单据再修改建议去向')
    if pet_has_active_adoption(pet):
        return '该动物已有待领出的领养单，请先取消领养登记再修改建议去向'
    if pet_has_approved_application(pet):
        return '该动物已有已批准的领养申请（待出库），请先处理该申请再修改建议去向'
    if pet_has_pending_return(pet):
        return '该动物已在未办结的运回单上，请先撤回运回单再修改建议去向'
    if pet_has_pending_release(pet):
        return '该动物已有未办结的放归单，请先处理放归单再修改建议去向'
    if open_euthanasia(pet) is not None:
        # 死亡登记没有「作废」态：遗体移交凭证可由平台端退回重录，但
        # 「已死亡 / 已安乐死」本身是临床事实，不能靠改建议去向推翻。
        return '该动物已登记死亡 / 安乐死，去向由临床处置决定，建议去向不可修改'
    return None
