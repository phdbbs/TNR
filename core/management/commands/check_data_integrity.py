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
    Adoption, Capture, Euthanasia, Material, MaterialStock, OwnerReturn, Pet,
    Release, Transfer, Treatment,
)
from business.services import (
    get_hospital_stock, get_institution_ledger_total, get_shelter_stock,
    validate_operator_district,
)
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
            ('机构库存与流水合计不一致', self.check_material_stock_vs_ledger),
            ('医院机构库存与现算口径不一致', self.check_hospital_stock_dual_run),
            ('捕捉点库存字段与机构库存合计不一致', self.check_shelter_stock_field),
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
        """捕捉单区县必须与捕捉点区县自洽。

        ⚠ **市级捕捉点是显式豁免**（第四十五轮 C5）：它挂在「全市（市级）」下，
        而它登记的捕捉单归属**实际捕捉区县**（Q2 已确认）。两者本来就**应该**
        不相等 —— 不豁免的话，市级捕捉点每抓一只动物就报一条「脏数据」，
        巡检会被淹没，真正的归属错误反而看不见。
        """
        qs = (Capture.objects.filter(is_deleted=False)
              .exclude(shelter__district__is_city=True)
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
        """宠物档案区县必须与所属捕捉点区县自洽（市级捕捉点豁免，同 `check_capture_district`）。"""
        qs = (Pet.objects.filter(is_deleted=False, shelter__isnull=False)
              .exclude(shelter__district__is_city=True)
              .exclude(district_id=F('shelter__district_id')).order_by('id'))
        return _rows(
            qs,
            lambda p: (f'{p.code}：档案={_district_name(p)} '
                       f'捕捉点={_district_name(p.shelter)}'),
            limit, related=('district', 'shelter__district'))

    @staticmethod
    def check_transfer_district(limit):
        """转运单归属的是**发出捕捉点**的区县（接收医院可以跨区县）。

        ⚠ 市级捕捉点豁免（第四十五轮 C5）：它发出的转运单归属**实际捕捉区县**，
        与捕捉点自身的「全市（市级）」必然不等。
        """
        qs = (Transfer.objects.filter(from_shelter__isnull=False)
              .exclude(from_shelter__district__is_city=True)
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

    # ============================================
    # 机构库存（第四十五轮）
    # ============================================
    @staticmethod
    def check_material_stock_vs_ledger(limit):
        """「已经算好的库存」必须等于「按机构把流水加起来」。

        这是第四十五轮新增的那张 `MaterialStock` 表的**核心判据**：
        它靠「每次入出库在同一个事务里加减那一行」来维护，一旦有哪条路径
        漏了维护（或者有人绕过 `adjust_stock()` 直接写流水），
        界面上的库存就与台账对不上 —— **而且不报错**，只是数字慢慢飘。

        口径（含期初）：

            quantity == opening_quantity + 按机构归属的流水合计

        `opening_quantity` 是这张表诞生之前就已存在的量（`shelter_stock`
        是种子数据直接写字段来的，没有对应流水），不把它算进来，
        判据从第一天起就恒不成立 —— 那样的判据等于没有。
        """
        qs = (MaterialStock.objects
              .select_related('material', 'institution').order_by('id'))
        total = 0
        samples = []
        for row in qs:
            ledger = get_institution_ledger_total(row.material, row.institution)
            expected = row.opening_quantity + ledger
            if row.quantity == expected:
                continue
            total += 1
            if len(samples) < limit:
                samples.append(
                    f'{row.institution.name} / {row.material.name}：'
                    f'表内 {row.quantity}，应为 期初 {row.opening_quantity} + 流水 {ledger} '
                    f'= {expected}（差 {row.quantity - expected}）')
        return total, samples

    @staticmethod
    def check_shelter_stock_field(limit):
        """兼容字段 `Material.shelter_stock` 必须等于「该物料捕捉点库存合计」。

        两个真源并存期的看门狗：
        - 新真源 = `MaterialStock`（按机构）；
        - 旧字段 = `Material.shelter_stock`（区县合计，界面仍有一处在读）。

        写接口在有机构归属时会**按新表重算**这个字段，所以正常情况下恒等。
        不等只有两种可能，都值得人看一眼：

        1. 走了**没有机构归属**的旧写入路径（如政府端采购、直接改字段的脚本）；
        2. 该区县有**多个捕捉点**，而 `shelter_stock` 里还有一份
           **分不下去的存量**（`0020` 迁移与首次建行都**故意不猜**，
           见 `services._opening_quantity_for_new_row()`）。

        ⚠ `get_shelter_stock()` 在「该物料还没有任何捕捉点库存行」时会
        **回退**到旧字段，所以纯存量数据不会在这里误报。
        """
        total = 0
        samples = []
        for material in Material.objects.select_related('district').order_by('id'):
            current = get_shelter_stock(material)
            if current == material.shelter_stock:
                continue
            total += 1
            if len(samples) < limit:
                samples.append(
                    f'{material.name}（{_district_name(material)}）：'
                    f'字段 {material.shelter_stock}，机构库存合计 {current}')
        return total, samples

    @staticmethod
    def check_hospital_stock_dual_run(limit):
        """医院侧**双跑**：新表 `quantity` 必须等于旧算法 `get_hospital_stock()`。

        为什么单列一条（虽然上一条在数学上已经蕴含它）：
        上一条比的是「新表 ↔ 流水」，这一条比的是「**新表 ↔ 旧算法**」——
        医院侧读取暂时仍走旧算法，只有这条判据为零，才有据可依地把读取切过去。
        两者的差异来源不同（一个查漏维护，一个查口径漂移），
        混在一条里会让「到底是哪边错了」变得难判断。
        """
        qs = (MaterialStock.objects.filter(institution__type='hospital')
              .select_related('material', 'institution').order_by('id'))
        total = 0
        samples = []
        for row in qs:
            want = get_hospital_stock(row.material, row.institution)
            if row.quantity == want:
                continue
            total += 1
            if len(samples) < limit:
                samples.append(
                    f'{row.institution.name} / {row.material.name}：'
                    f'机构库存 {row.quantity}，现算 {want}（差 {row.quantity - want}）')
        return total, samples
