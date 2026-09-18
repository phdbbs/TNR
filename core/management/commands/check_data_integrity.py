"""数据一致性巡检（**只读**，绝不写库）。

用于部署后自检与日常运维：把「界面上看不出来、但会让某个角色看到错误数据」
的隐性问题一次性列出来。典型形态是**归属字段互相矛盾** ——
账号的区县与所属机构的区县不一致、业务记录的区县与它挂靠对象的区县不一致。
这类问题的共同后果是区县隔离失真：本区县政府看不到本区数据，
或者某个账号看到了别的区县的档案。

用法：
    python manage.py check_data_integrity
    python manage.py check_data_integrity --limit 50

有违规时退出码为 1（便于放进部署脚本 / 定时任务）；无违规则为 0。
"""
import sys

from django.core.management.base import BaseCommand
from django.db.models import F

from accounts.models import User
from business.models import (
    Adoption, Capture, Euthanasia, OwnerReturn, Pet, Release, Transfer,
    Treatment,
)
from business.services import validate_operator_district
from core.models import Institution

# 各检查项统一返回 (总数, 样例列表)
MAX_DETAIL = 200


def _rows(qs, fmt, limit, related=()):
    """把 QuerySet 收敛成 (总数, 前 limit 条描述)。"""
    if related:
        qs = qs.select_related(*related)
    total = qs.count()
    if not total:
        return 0, []
    return total, [fmt(obj) for obj in qs[:limit]]


def _district_name(obj, field='district'):
    district = getattr(obj, field, None)
    if district is None:
        return '（空）'
    return district.name


class Command(BaseCommand):
    help = '只读巡检数据一致性（账号↔机构区县、业务记录↔归属对象区县、逻辑删除孤儿）'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=20,
                            help='每类问题最多列出多少条明细（默认 20）')

    def handle(self, *args, **options):
        limit = max(1, min(int(options['limit']), MAX_DETAIL))

        checks = [
            ('账号区县与机构区县不一致', self.check_user_institution_district),
            ('操作员账号缺少所属机构', self.check_operator_without_institution),
            ('机构缺少业务编号 code', self.check_institution_without_code),
            ('捕捉单区县与捕捉点区县不一致', self.check_capture_district),
            ('宠物档案区县与捕捉单区县不一致', self.check_pet_capture_district),
            ('宠物档案区县与捕捉点区县不一致', self.check_pet_shelter_district),
            ('转运单区县与发出捕捉点区县不一致', self.check_transfer_district),
            ('诊疗记录区县与宠物区县不一致', self.check_treatment_district),
            ('主人领回区县与宠物区县不一致', self.check_owner_return_district),
            ('放养记录区县与宠物区县不一致', self.check_release_district),
            ('领养记录区县与宠物区县不一致', self.check_adoption_district),
            ('安乐死记录区县与宠物区县不一致', self.check_euthanasia_district),
            ('宠物未作废但所属捕捉单已作废', self.check_pet_of_deleted_capture),
        ]

        total_issues = 0
        for title, check in checks:
            try:
                count, samples = check(limit)
            except Exception as exc:  # noqa: BLE001 —— 巡检自身不能因为一条检查崩掉
                self.stdout.write(self.style.ERROR(
                    f'✗ {title}：检查本身出错 —— {exc}'))
                total_issues += 1
                continue

            if count == 0:
                self.stdout.write(f'✓ {title}')
                continue

            total_issues += count
            self.stdout.write(self.style.WARNING(f'! {title}：{count} 条'))
            for line in samples:
                self.stdout.write(f'    - {line}')
            if count > len(samples):
                self.stdout.write(f'    …（其余 {count - len(samples)} 条未列出）')

        self.stdout.write('')
        if total_issues:
            self.stdout.write(self.style.ERROR(f'发现 {total_issues} 条一致性问题'))
            self.stdout.write('本命令只读、不会自动修复；请逐条确认后再处理。')
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS('未发现一致性问题'))

    # ============================================
    # 账号 / 机构
    # ============================================
    @staticmethod
    def check_user_institution_district(limit):
        """账号区县必须与所属机构所在区县自洽（判定逻辑与建号校验共用一份）。

        注意：`validate_operator_district()` **通过时返回 None**，所以不能直接把
        返回值当明细打印 —— 那样会把每一个正常账号都列成问题（写这个命令时
        第一版就是这么错的，实测 9 个正常账号全被误报）。
        """
        qs = (User.objects.filter(institution__isnull=False)
              .select_related('district', 'institution__district').order_by('id'))
        total = 0
        samples = []
        for user in qs:
            err = validate_operator_district(user.role, user.district, user.institution)
            if not err:
                continue
            total += 1
            if len(samples) < limit:
                samples.append(f'{user.username}（{user.get_role_display()}）：{err}')
        return total, samples

    @staticmethod
    def check_operator_without_institution(limit):
        """启用的捕捉点/医院操作员必须挂机构，否则它代表谁都是模糊的。"""
        qs = User.objects.filter(
            role__in=('shelter', 'hospital'), institution__isnull=True,
            is_active=True,
        ).order_by('id')
        return _rows(qs, lambda u: f'{u.username}（{u.get_role_display()}）', limit)

    @staticmethod
    def check_institution_without_code(limit):
        """机构业务编号是去重与引用的稳定键，缺了会让 seed_data 反复插入同名机构。"""
        qs = Institution.objects.filter(code__isnull=True).order_by('id')
        return _rows(qs, lambda i: f'id={i.id} {i.name}（{i.get_type_display()}）',
                     limit, related=('district',))

    # ============================================
    # 业务记录 ↔ 归属对象
    # ============================================
    @staticmethod
    def check_capture_district(limit):
        qs = (Capture.objects.filter(is_deleted=False)
              .exclude(district_id=F('shelter__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda c: (f'{c.ledger_no or c.id}：捕捉单={_district_name(c)} '
                       f'捕捉点={_district_name(c.shelter)}'),
            limit, related=('district', 'shelter__district'))

    @staticmethod
    def check_pet_capture_district(limit):
        qs = (Pet.objects.filter(is_deleted=False, capture__isnull=False)
              .exclude(district_id=F('capture__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda p: (f'{p.code}：档案={_district_name(p)} '
                       f'捕捉单={_district_name(p.capture)}'),
            limit, related=('district', 'capture__district'))

    @staticmethod
    def check_pet_shelter_district(limit):
        qs = (Pet.objects.filter(is_deleted=False, shelter__isnull=False)
              .exclude(district_id=F('shelter__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda p: (f'{p.code}：档案={_district_name(p)} '
                       f'捕捉点={_district_name(p.shelter)}'),
            limit, related=('district', 'shelter__district'))

    @staticmethod
    def check_transfer_district(limit):
        """转运单归属的是**发出捕捉点**的区县（接收医院可以跨区县）。"""
        qs = (Transfer.objects.filter(from_shelter__isnull=False)
              .exclude(district_id=F('from_shelter__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda t: (f'{t.ledger_no or t.id}：转运单={_district_name(t)} '
                       f'发出捕捉点={_district_name(t.from_shelter)}'),
            limit, related=('district', 'from_shelter__district'))

    @staticmethod
    def check_treatment_district(limit):
        qs = (Treatment.objects.all()
              .exclude(district_id=F('pet__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda t: (f'{t.ledger_no or t.id}（{t.pet_code}）：诊疗={_district_name(t)} '
                       f'宠物={_district_name(t.pet)}'),
            limit, related=('district', 'pet__district'))

    @staticmethod
    def check_owner_return_district(limit):
        qs = (OwnerReturn.objects.all()
              .exclude(district_id=F('pet__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda r: (f'{r.ledger_no or r.id}（{r.pet_code}）：领回={_district_name(r)} '
                       f'宠物={_district_name(r.pet)}'),
            limit, related=('district', 'pet__district'))

    @staticmethod
    def check_release_district(limit):
        qs = (Release.objects.all()
              .exclude(district_id=F('pet__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda r: (f'{r.ledger_no or r.id}（{r.pet_code}）：放养={_district_name(r)} '
                       f'宠物={_district_name(r.pet)}'),
            limit, related=('district', 'pet__district'))

    @staticmethod
    def check_adoption_district(limit):
        qs = (Adoption.objects.all()
              .exclude(district_id=F('pet__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda a: (f'{a.ledger_no or a.id}（{a.pet_code}）：领养={_district_name(a)} '
                       f'宠物={_district_name(a.pet)}'),
            limit, related=('district', 'pet__district'))

    @staticmethod
    def check_euthanasia_district(limit):
        qs = (Euthanasia.objects.all()
              .exclude(district_id=F('pet__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda e: (f'{e.ledger_no or e.id}（{e.pet_code}）：处置={_district_name(e)} '
                       f'宠物={_district_name(e.pet)}'),
            limit, related=('district', 'pet__district'))

    # ============================================
    # 逻辑删除
    # ============================================
    @staticmethod
    def check_pet_of_deleted_capture(limit):
        """捕捉单被逻辑删除时，其下宠物档案必须一并作废，否则会继续在医院端流转。"""
        qs = Pet.objects.filter(is_deleted=False, capture__is_deleted=True).order_by('id')
        return _rows(
            qs,
            lambda p: (f'{p.code}：档案未作废，但捕捉单 '
                       f'{p.capture.ledger_no or p.capture_id} 已作废'),
            limit, related=('capture',))
