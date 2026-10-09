"""把媒体文件从一个存储后端搬到另一个（本地 ↔ 对象存储）。

## 用法

    # 1) 先看清单，什么都不动（默认就是 dry-run）
    python manage.py media_migrate

    # 2) 真搬。可重复执行、可中断重跑（幂等）
    python manage.py media_migrate --apply

    # 3) 核对两边是否一致（搬完后必跑）
    python manage.py media_migrate --verify

    # 4) 确认无误后清理本地残留（危险，双闸门）
    python manage.py media_migrate --prune --yes

    # 回滚：从对象存储搬回本地
    python manage.py media_migrate --reverse --apply

## 设计要点

### 1. 数据库是真源，不是磁盘目录

待迁移清单来自**扫描所有 ``FileField``/``ImageField`` 的实际取值**，
而不是 ``os.walk`` 媒体目录。理由：磁盘上可能有 DB 已不再引用、但一直没删的
孤儿文件；按目录搬会把它们一起搬过去，然后永远不知道哪些能删。
孤儿要不要处理由 ``--include-orphans`` **显式**决定。

⚠ 逻辑删除（``is_deleted=True``）的记录**照样迁移** —— 它们仍可能被恢复、
仍要在监管审计里能调出照片。「软删除的图片不用搬」是个危险假设。

### 2. object key 必须与 DB 里的 ``name`` 逐字一致

这是「一个命令完成迁移、DB 一行不改」的全部前提。因此**不能**用
``Storage.save()``：它会先调 ``get_available_name()``，目标已存在同名文件时
自动改名成 ``abc_AbC123.jpg``，于是 DB 里的 ``photos/abc.jpg`` 就指向了
不存在的位置 —— 图片全部裂开，而命令会「成功」退出。

正确做法是 ``_save(name, content)``（跳过改名），并且**写前先删**：
``FileSystemStorage._save`` 内部用 ``open(path, 'xb')``（独占创建），
文件已存在会抛 ``FileExistsError`` 然后**自动换名重试** —— 同样会破坏 key。
先 delete 掉，两种后端的行为就统一了。

每次写入后都核对 ``target.exists(name)``；对不上就计入失败，不静默放过。

### 3. 源文件永远不删（除非 --prune）

搬运失败时源还在，重跑即可。``--prune`` 要求同时给 ``--yes``，
因为「迁移看起来成功」和「文件真的安全落到对象存储了」是两件事 ——
先跑 ``--verify`` 确认，再决定要不要清理。

### 4. 幂等

目标已存在且**字节数一致**就跳过。所以中断、重跑、增量补齐都安全：
迁移期间服务可以继续跑（新图直接进对象存储），跑完再跑一次就补齐了。
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.storage import (
    LocalMediaStorage,
    is_local_media,
    media_backend,
    media_storage_snapshot,
    object_storage_dependency_error,
    object_storage_missing_env,
)


def _human(n):
    """字节数转可读。"""
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return '%.1f %s' % (n, unit) if unit != 'B' else '%d B' % n
        n /= 1024


def _iter_file_fields():
    """动态发现全部 ``FileField``（含 ``ImageField``）。

    **不硬编码字段清单** —— 硬编码的清单会在有人新增一个图片字段时静默漏掉，
    表现为「新功能的图永远不迁移」，而且没有任何报错。
    """
    from django.apps import apps
    from django.db.models import FileField

    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            if isinstance(field, FileField):
                yield model, field


def _collect_db_references():
    """扫描数据库，返回 ``{name: [引用来源描述, ...]}``。

    同一张图被多条记录引用时（如同一 name 被 Pet 与 Capture 共用）只算一次，
    但把来源都记下来，便于排查。
    """
    refs = {}
    field_count = 0
    for model, field in _iter_file_fields():
        field_count += 1
        qs = (model.objects
              .exclude(**{f'{field.name}__isnull': True})
              .exclude(**{field.name: ''})
              .only('pk', field.name))
        for obj in qs.iterator():
            ff = getattr(obj, field.name, None)
            name = getattr(ff, 'name', '') or ''
            if not name:
                continue
            refs.setdefault(name, []).append(
                '%s#%s.%s' % (model.__name__, obj.pk, field.name))
    return refs, field_count


def _iter_storage_keys(storage, prefix=''):
    """遍历一个 storage 里已有的全部 key（用于找孤儿文件）。

    只实现了本地磁盘分支 —— 迁移的主方向是 local → 对象存储，
    而孤儿文件只可能出现在**源**（本地）那一侧。
    """
    location = getattr(storage, 'location', None)
    if not location:
        return
    base = Path(location)
    if not base.is_dir():
        return
    for p in sorted(base.rglob('*')):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        if prefix and not rel.startswith(prefix):
            continue
        yield rel


def _put_keeping_name(target, name, source):
    """把 ``source.open(name)`` 的内容写到 ``target`` 的**同一个 key** 上。

    返回实际写入的 key。与 ``Storage.save()`` 的区别见模块 docstring 第 2 点。
    """
    # 写前先删：否则 FileSystemStorage 的独占创建会触发自动改名
    if target.exists(name):
        target.delete(name)

    with source.open(name, 'rb') as fh:
        if hasattr(target, '_save'):
            written = target._save(name, fh)
        else:  # 极老的第三方后端可能没有 _save
            written = target.save(name, fh)
            if written != name:
                raise RuntimeError(
                    '目标后端把 key 从 %r 改成了 %r —— 会破坏数据库引用，已中止'
                    % (name, written))

    if written != name:
        raise RuntimeError(
            '写入后 key 变成了 %r（期望 %r）—— 已中止以免数据库引用失配'
            % (written, name))
    if not target.exists(name):
        raise RuntimeError('写入后目标里仍找不到 %r' % name)
    return written


class Command(BaseCommand):
    help = '在本地存储与对象存储之间搬运媒体文件（默认 dry-run，幂等）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='真正执行搬运。不加则只列清单（dry-run）')
        parser.add_argument(
            '--verify', action='store_true',
            help='只核对源与目标是否一致，不搬运')
        parser.add_argument(
            '--reverse', action='store_true',
            help='反向：从当前 storage 搬回本地 media/（回滚用）')
        parser.add_argument(
            '--prune', action='store_true',
            help='搬运成功后删除源文件（危险，必须同时给 --yes）')
        parser.add_argument(
            '--yes', action='store_true',
            help='确认执行危险操作（配合 --prune）')
        parser.add_argument(
            '--include-orphans', action='store_true',
            help='把源里存在、但数据库未引用的文件也纳入搬运')
        parser.add_argument(
            '--only', default='', metavar='PREFIX',
            help='只处理该前缀下的文件，如 --only photos/')
        parser.add_argument(
            '--limit', type=int, default=0, metavar='N',
            help='最多处理 N 个（试跑用）')
        parser.add_argument(
            '--batch', type=int, default=200, metavar='N',
            help='每处理 N 个报告一次进度（默认 200）')
        parser.add_argument(
            '--status', action='store_true',
            help='只打印当前存储配置快照，不做任何扫描')
        parser.add_argument(
            '--source-dir', default='', metavar='PATH',
            help='源目录（默认 settings.MEDIA_ROOT）。仅在本地演练时用')
        parser.add_argument(
            '--target-dir', default='', metavar='PATH',
            help='目标目录。指定时忽略 TNR_MEDIA_STORAGE 配置，直接搬到该目录 —— '
                 '用于**不接对象存储也能完整演练**迁移流程（dry-run→apply→verify→prune）')

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        if opts['status']:
            self._print_status()
            return

        source, target = self._resolve_endpoints(opts)
        self._print_endpoints(source, target)

        if opts['verify']:
            self._do_verify(source, target, opts)
            return

        refs, field_count = _collect_db_references()
        names = sorted(n for n in refs
                       if not opts['only'] or n.startswith(opts['only']))

        orphans = []
        if opts['include_orphans']:
            known = set(names)
            orphans = [k for k in _iter_storage_keys(source, opts['only'])
                       if k not in known]
            names = sorted(set(names) | set(orphans))

        if opts['limit'] and len(names) > opts['limit']:
            names = names[:opts['limit']]

        self.stdout.write(
            '扫描数据库：%d 个文件字段，%d 个被引用的文件'
            % (field_count, len(refs)))
        if opts['include_orphans']:
            self.stdout.write('孤儿文件（DB 未引用）：%d 个' % len(orphans))
        if opts['only']:
            self.stdout.write('前缀过滤：%s' % opts['only'])
        if opts['limit']:
            self.stdout.write('数量上限：%d' % opts['limit'])
        self.stdout.write('')

        if not names:
            self.stdout.write(self.style.WARNING('没有需要处理的文件。'))
            return

        pending, skipped, missing = self._classify(source, target, names)

        self.stdout.write('  待搬运   %d' % len(pending))
        self.stdout.write('  已存在   %d  （目标已有且大小一致，跳过）' % len(skipped))
        if missing:
            self.stdout.write(self.style.WARNING(
                '  源缺失   %d  （数据库有引用，但源里找不到）' % len(missing)))
        self.stdout.write('')

        if missing:
            for n in missing[:20]:
                self.stdout.write(self.style.WARNING('  ⚠ 源缺失 %s' % n))
            if len(missing) > 20:
                self.stdout.write('  … 另有 %d 个' % (len(missing) - 20))
            self.stdout.write('')

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                '【dry-run】以上只是清单，未做任何改动。'
                '确认无误后加 --apply 执行。'))
            for n in pending[:20]:
                self.stdout.write('  → %s' % n)
            if len(pending) > 20:
                self.stdout.write('  … 另有 %d 个' % (len(pending) - 20))
            return

        ok, failed = self._do_copy(source, target, pending, opts)
        self.stdout.write('')
        self.stdout.write('搬运完成：成功 %d，失败 %d，跳过 %d'
                          % (ok, len(failed), len(skipped)))
        for name, err in failed[:20]:
            self.stdout.write(self.style.ERROR('  ✗ %s —— %s' % (name, err)))

        if opts['prune']:
            self._do_prune(source, target, names, opts, ok, failed)

        if failed:
            raise CommandError(
                '%d 个文件搬运失败，源文件已保留。修好后重跑本命令即可（幂等）。'
                % len(failed))

    # ------------------------------------------------------------------
    def _print_status(self):
        snap = media_storage_snapshot()
        self.stdout.write('当前媒体存储配置：')
        for k, v in snap.items():
            self.stdout.write('  %-22s %s' % (k, v))
        # 依赖只在**对象存储模式**下才是必需的：本地存储不碰 django-storages。
        # 无条件检查会在纯本地环境里报一个吓人但无关的错误。
        if snap['kind'] == 'object':
            err = object_storage_dependency_error()
            if err:
                self.stdout.write(self.style.ERROR('  %s' % err))

    def _print_endpoints(self, source, target):
        self.stdout.write('媒体存储迁移')
        self.stdout.write('  源   %s' % self._describe(source))
        self.stdout.write('  目标 %s' % self._describe(target))
        self.stdout.write('')

    def _describe(self, storage):
        name = type(storage).__module__ + '.' + type(storage).__name__
        loc = getattr(storage, 'location', None)
        extra = '  location=%s' % loc if loc else ''
        return '%s%s' % (name, extra)

    # ------------------------------------------------------------------
    def _resolve_endpoints(self, opts):
        """决定源与目标 storage。

        三种情形，优先级从高到低：

        1. ``--target-dir``：目标是一个本地目录。**这条路径不要求配对象存储**，
           用于在本地完整演练迁移流程（以及自动化测试）。源取 ``--source-dir``
           或 ``settings.MEDIA_ROOT``。
        2. ``--reverse``：源 = 当前 storage，目标 = 本地 ``media/``（回滚）。
        3. 默认：源 = 本地 ``media/``，目标 = 当前 ``STORAGES['default']``。
        """
        from django.conf import settings
        from django.core.files.storage import FileSystemStorage, storages

        # ---- 1) 本地演练：两边都是目录 --------------------------------
        if opts['target_dir']:
            src_dir = opts['source_dir'] or str(settings.MEDIA_ROOT)
            tgt_dir = opts['target_dir']
            if Path(src_dir).resolve() == Path(tgt_dir).resolve():
                raise CommandError('源目录与目标目录是同一个：%s' % src_dir)
            return (FileSystemStorage(location=src_dir),
                    FileSystemStorage(location=tgt_dir))

        current = storages['default']
        local = (FileSystemStorage(location=opts['source_dir'])
                 if opts['source_dir'] else LocalMediaStorage())

        # ---- 2) 回滚：对象存储 → 本地 ---------------------------------
        if opts['reverse']:
            if is_local_media():
                raise CommandError(
                    '当前已经是本地存储（TNR_MEDIA_STORAGE=%s），没有可回滚的对象存储。'
                    % media_backend())
            return current, local

        # ---- 3) 默认：本地 → 当前 storage ----------------------------
        if is_local_media():
            raise CommandError(
                '当前媒体后端就是本地存储（TNR_MEDIA_STORAGE=%s），'
                '没有可搬运的目标。\n'
                '请先在 .env 里设好 TNR_MEDIA_STORAGE 与桶/密钥，再执行本命令。\n'
                '（配置项见 .env.example 的「媒体文件存储」一节）\n'
                '若只是想演练流程，用 --target-dir 指定一个本地目录即可。'
                % media_backend())

        err = object_storage_dependency_error()
        if err:
            raise CommandError(err)
        missing = object_storage_missing_env()
        if missing:
            raise CommandError(
                '对象存储配置不完整，缺少：%s\n见 .env.example 的「媒体文件存储」一节。'
                % '、'.join(missing))

        return local, current

    # ------------------------------------------------------------------
    def _classify(self, source, target, names):
        """把待处理清单分成 待搬运 / 已存在 / 源缺失 三组。"""
        pending, skipped, missing = [], [], []
        for name in names:
            if not source.exists(name):
                missing.append(name)
                continue
            if target.exists(name) and self._same_size(source, target, name):
                skipped.append(name)
            else:
                pending.append(name)
        return pending, skipped, missing

    @staticmethod
    def _same_size(a, b, name):
        try:
            return a.size(name) == b.size(name)
        except Exception:
            return False

    # ------------------------------------------------------------------
    def _do_copy(self, source, target, pending, opts):
        ok, failed = 0, []
        total = len(pending)
        batch = max(1, opts['batch'])
        for i, name in enumerate(pending, 1):
            try:
                size = source.size(name)
                _put_keeping_name(target, name, source)
                ok += 1
                if i % batch == 0 or i == total:
                    self.stdout.write('  [%d/%d] %s' % (i, total, _human(size)))
            except Exception as exc:          # noqa: BLE001 —— 单个失败不中断整批
                failed.append((name, '%s: %s' % (type(exc).__name__, exc)))
                self.stdout.write(self.style.ERROR('  ✗ %s —— %s' % (name, exc)))
        return ok, failed

    # ------------------------------------------------------------------
    def _do_verify(self, source, target, opts):
        refs, field_count = _collect_db_references()
        names = sorted(n for n in refs
                       if not opts['only'] or n.startswith(opts['only']))
        self.stdout.write('核对 %d 个字段引用的 %d 个文件…'
                          % (field_count, len(names)))

        missing_target, size_mismatch, missing_source, ok = [], [], [], 0
        for name in names:
            if not source.exists(name):
                missing_source.append(name)
                continue
            if not target.exists(name):
                missing_target.append(name)
                continue
            if not self._same_size(source, target, name):
                size_mismatch.append(name)
                continue
            ok += 1

        self.stdout.write('  一致     %d' % ok)
        self.stdout.write('  目标缺失 %d' % len(missing_target))
        self.stdout.write('  大小不符 %d' % len(size_mismatch))
        self.stdout.write('  源缺失   %d' % len(missing_source))

        for label, items in (('目标缺失', missing_target),
                             ('大小不符', size_mismatch),
                             ('源缺失', missing_source)):
            for n in items[:10]:
                self.stdout.write(self.style.WARNING('  ⚠ %s %s' % (label, n)))
            if len(items) > 10:
                self.stdout.write('  … %s 另有 %d 个' % (label, len(items) - 10))

        if missing_target or size_mismatch:
            raise CommandError('核对未通过：目标与源不一致，请重跑 --apply 补齐。')
        self.stdout.write(self.style.SUCCESS('核对通过。'))

    # ------------------------------------------------------------------
    def _do_prune(self, source, target, names, opts, ok, failed):
        if not opts['yes']:
            self.stdout.write(self.style.WARNING(
                '已搬运 %d 个，但**未清理源文件** —— --prune 必须同时给 --yes。'
                % ok))
            return
        if failed:
            self.stdout.write(self.style.WARNING(
                '有 %d 个文件搬运失败，**已跳过清理**（避免删掉还没搬走的源文件）。'
                % len(failed)))
            return

        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            '⚠ 开始清理源文件。这一步会**真的删除**本地 media/ 下的文件。'))

        # ⚠ 逐个**重新核对目标里确实有同名文件**，才允许删源。
        # 只信「刚才那轮搬运成功了」是不够的：本轮可能大部分是**跳过**的
        # （上一轮搬过），而跳过是基于「大小一致」的推断 —— 万一同名文件是
        # 别人手工放进去的、内容其实不同，只凭推断就删源等于把唯一副本删掉。
        # 删除是不可逆的，宁可多一次 exists() 调用。
        pruned, kept, errors = 0, 0, 0
        for name in names:
            try:
                if not source.exists(name):
                    continue
                if not target.exists(name):
                    kept += 1
                    self.stdout.write(self.style.WARNING(
                        '  ⊘ 目标里没有，保留源文件：%s' % name))
                    continue
                source.delete(name)
                pruned += 1
            except Exception as exc:          # noqa: BLE001
                errors += 1
                self.stdout.write(self.style.ERROR('  ✗ 删除失败 %s —— %s' % (name, exc)))

        self.stdout.write('清理完成：删除 %d，保留 %d，失败 %d'
                          % (pruned, kept, errors))
        if kept:
            self.stdout.write(self.style.WARNING(
                '有 %d 个文件在目标里找不到，**源文件已保留**。'
                '先跑 --verify 查明原因，别手工删。' % kept))
        if errors:
            self.stdout.write(self.style.WARNING(
                '有删除失败的残留文件，可重跑本命令（已搬走的会再删一次）。'))
