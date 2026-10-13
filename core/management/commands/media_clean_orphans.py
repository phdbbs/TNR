"""清理 media/ 里数据库已不再引用的孤儿图片。

## 用法

    # 1) 干跑：只出报告，零改动（默认就是这个）
    python manage.py media_clean_orphans

    # 2) 真清：先移送到备份目录（可恢复）
    python manage.py media_clean_orphans --apply

    # 3) 备份确认无误后彻底删除（危险，必须 --yes）
    python manage.py media_clean_orphans --purge --backup-dir <备份目录> --yes

## 为什么是「移到备份」而不是「删」

直接 ``os.remove`` 的一次误判就是永久数据丢失，而判断依据
（「数据库里没有引用」）本身依赖几个不牢靠的假设：

- 历史数据可能是手工导入 / seed 出来的，库里没有对应记录；
- 逻辑删除的记录在某些时刻可能被误清过 ``name`` 字段；
- 生产库与本地库不是同一份，本地看到的「孤儿」在生产可能正被引用。

所以默认走**移动**：``media/photos/X.png`` → ``media_orphans_<ts>/photos/X.png``，
同时写一份 ``MANIFEST.txt`` 记录「原 key → 新位置」。真要恢复，
把文件搬回 ``MEDIA_ROOT`` 下对应相对路径即可，DB 一行都不用改。
确认备份没有价值了，再用 ``--purge`` 彻底删。

## 备份目录为什么必须在 MEDIA_ROOT 之外

如果备份落在 ``media/`` 里面，会**同时**踩三个坑：

1. 下次跑本命令，备份里的文件会被扫成「新的孤儿」—— 报告数字永远不清零；
2. ``media_migrate`` 会把备份文件一起搬上对象存储，白花钱；
3. ``/media/`` 路由下这些文件仍可访问，掩盖「迁移没搬干净」的事实
   （与 ``core.storage.is_local_media`` 的设计意图相悖）。

因此这里是**硬校验**：备份目录解析后落在 ``MEDIA_ROOT`` 内直接报错，
不给「看起来没问题」的机会。

## 判据怎么来的：数据库是真源

孤儿集合 = ``磁盘上的全部文件`` − ``数据库引用的全部文件``。

引用集合由 ``_collect_db_references()`` 得来 —— 它**动态遍历所有
``FileField``/``ImageField``**，不硬编码字段清单（硬编码清单会在有人新增
图片字段时静默漏扫，表现为「新功能的图被当孤儿删掉」且没有任何报错）。

⚠ 软删记录（``is_deleted=True``）的引用**照样算有效**：照片仍是审计凭据，
仍可能被恢复。Django 默认 manager 不过滤软删，所以这里天然满足；
``SoftDeleteReferenceKeptTest`` 会守住这条性质。
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.management.commands.media_migrate import (
    _collect_db_references,
    _human,
    _iter_storage_keys,
)
from core.storage import LocalMediaStorage

#: 备份目录名前缀。放在 MEDIA_ROOT 的同级（项目目录下）。
BACKUP_PREFIX = 'media_orphans_'

#: MANIFEST 文件名。
MANIFEST_NAME = 'MANIFEST.txt'

#: 已知的「一看就知道是什么」的模式，仅用于**报告分组**，
#: 不参与「能不能删」的判断。判断只看 DB 有没有引用。
_PATTERNS = [
    (re.compile(r'^photos/TNR[\d\w-]*\.png$'), 'TNR*.png（历史 / seed 图）'),
    (re.compile(r'^photos/pet\d*_[A-Za-z0-9]+\.png$'), 'pet*_.png（seed 宠物图，单个 264KB）'),
    (re.compile(r'^photos/test-.*$'), 'test-*（自动化测试残留）'),
    (re.compile(r'^\.DS_Store$'), '.DS_Store（Finder 元数据）'),
]


def classify_orphans(orphans):
    """把孤儿按模式分组，返回 ``[(label, [key, ...]), ...]``，未匹配的归「其他」。"""
    groups = {label: [] for _, label in _PATTERNS}
    groups['其他'] = []
    for key in orphans:
        for pattern, label in _PATTERNS:
            if pattern.match(key):
                groups[label].append(key)
                break
        else:
            groups['其他'].append(key)
    # 只返回非空组，顺序固定（其他放最后）
    ordered = [(label, groups[label]) for _, label in _PATTERNS if groups[label]]
    if groups['其他']:
        ordered.append(('其他', groups['其他']))
    return ordered


def _sizes(base, keys):
    """按 key 统计总字节数；文件已消失时按 0 计（不抛异常）。"""
    total = 0
    for k in keys:
        try:
            total += (base / k).stat().st_size
        except OSError:
            pass
    return total


def _write_manifest(backup_dir, rows):
    """写 MANIFEST：一行一条「原 key → 备份相对路径」，并附可读说明头。"""
    lines = [
        '# TNR 孤儿媒体文件清理清单',
        '# 原 key 与数据库 ImageField.name 的命名规则一致（相对 MEDIA_ROOT）。',
        '# 恢复方法：把对应文件搬回 MEDIA_ROOT 下同相对路径，无需改数据库。',
        '#',
        '# 原 key\t备份相对路径',
    ]
    lines += ['%s\t%s' % (k, p) for k, p in rows]
    (Path(backup_dir) / MANIFEST_NAME).write_text(
        '\n'.join(lines) + '\n', encoding='utf-8')

    # 再放一份人话版说明，避免半年后没人知道这目录能删不能删
    (Path(backup_dir) / 'README.txt').write_text(
        'TNR media/ 孤儿图片备份\n'
        '======================\n'
        '\n'
        '来源：manage.py media_clean_orphans --apply\n'
        '时间：%s\n'
        '数量：%d 个\n'
        '\n'
        '这些文件在数据库里已无任何引用（已扫描全部 FileField/ImageField，\n'
        '含逻辑删除记录）。移到这里是为了**可恢复**，不是因为它们有害。\n'
        '\n'
        '删除本目录即永久删除这些文件。\n'
        '确认无需保留时：\n'
        '    python manage.py media_clean_orphans --purge \\\n'
        '        --backup-dir %s --yes\n'
        % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           len(rows), backup_dir),
        encoding='utf-8')


class Command(BaseCommand):
    help = '清理 media/ 中数据库未引用的孤儿图片（默认干跑，先移备份不硬删）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='真正执行清理（移送到备份目录）。不加则只出报告')
        parser.add_argument(
            '--purge', action='store_true',
            help='彻底删除备份目录（危险，必须同时给 --yes 与 --backup-dir）')
        parser.add_argument(
            '--yes', action='store_true',
            help='确认执行危险操作（配合 --purge）')
        parser.add_argument(
            '--rebuild-manifest', action='store_true',
            help='只按备份目录里的现有文件重写 MANIFEST，不移动任何文件。'
                 '用于把多个备份目录合并之后重建清单 —— 合并后清单条数若少于'
                 '文件数，多出来的那些就永久失去溯源记录了')
        parser.add_argument(
            '--backup-dir', default='', metavar='PATH',
            help='备份目录。默认 MEDIA_ROOT 同级下 media_orphans_<时间戳>/')
        parser.add_argument(
            '--source-dir', default='', metavar='PATH',
            help='要清理的媒体目录（默认 settings.MEDIA_ROOT）。本地演练/测试用')
        parser.add_argument(
            '--only', default='', metavar='PREFIX',
            help='只处理该前缀下的孤儿，如 --only photos/')
        parser.add_argument(
            '--limit', type=int, default=0, metavar='N',
            help='最多处理 N 个（分批试跑用）')
        parser.add_argument(
            '--remove-empty-dirs', action='store_true',
            help='清理后删除 media/ 下因此变空的目录')

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        if opts['purge']:
            self._do_purge(opts)
            return

        if opts['rebuild_manifest']:
            self._do_rebuild_manifest(opts)
            return

        media_root = Path(opts['source_dir']).resolve() if opts['source_dir'] \
            else self._media_root()
        # 备份目录必须在 MEDIA_ROOT 之外：落到里面会污染后续扫描与迁移，
        # 也会让 /media/ 路由下的「看起来干净」变成假象。硬校验，不降级。
        backup_dir = self._resolve_backup_dir(media_root, opts['backup_dir'])

        source = LocalMediaStorage(location=str(media_root))
        if not media_root.is_dir():
            raise CommandError('媒体目录不存在：%s' % media_root)

        refs, field_count = _collect_db_references()
        disk = list(_iter_storage_keys(source, opts['only']))
        orphan_keys = sorted(k for k in disk if k not in refs)

        self.stdout.write('孤儿清理（%s）' % ('干跑，零改动' if not opts['apply'] else '执行'))
        self.stdout.write('  媒体目录   %s' % media_root)
        self.stdout.write('  备份目录   %s' % backup_dir)
        self.stdout.write('  文件字段   %d 个（含逻辑删除记录）' % field_count)
        self.stdout.write('  DB 引用    %d 个' % len(refs))
        self.stdout.write('  磁盘文件   %d 个' % len(disk))
        if opts['only']:
            self.stdout.write('  前缀过滤   %s' % opts['only'])
        self.stdout.write('')

        if not orphan_keys:
            self.stdout.write(self.style.SUCCESS('没有孤儿文件，无需清理。'))
            return

        if opts['limit'] and len(orphan_keys) > opts['limit']:
            self.stdout.write(self.style.WARNING(
                '数量上限 %d：本次只处理前 %d 个，剩余 %d 个下次再跑。'
                % (opts['limit'], opts['limit'], len(orphan_keys) - opts['limit'])))
            orphan_keys = orphan_keys[:opts['limit']]

        total_size = _sizes(media_root, orphan_keys)
        self.stdout.write(self.style.WARNING(
            '孤儿文件 %d 个，合计 %s：' % (len(orphan_keys), _human(total_size))))
        for label, keys in classify_orphans(orphan_keys):
            self.stdout.write('  %-26s %6d  %10s'
                              % (label, len(keys), _human(_sizes(media_root, keys))))
        self.stdout.write('')
        self.stdout.write('  样例（前 12）：')
        for k in orphan_keys[:12]:
            self.stdout.write('    %s' % k)
        if len(orphan_keys) > 12:
            self.stdout.write('    … 另有 %d 个' % (len(orphan_keys) - 12))
        self.stdout.write('')

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                '【干跑】以上只是报告，未做任何改动。\n'
                '确认无误后执行：  python manage.py media_clean_orphans --apply'))
            return

        self._do_move(media_root, backup_dir, orphan_keys, opts)

    # ------------------------------------------------------------------
    def _media_root(self):
        from django.conf import settings
        return Path(settings.MEDIA_ROOT).resolve()

    def _resolve_backup_dir(self, media_root, explicit):
        if not explicit:
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            explicit = str(media_root.parent / (BACKUP_PREFIX + stamp))
        backup = Path(explicit).resolve()
        # 备份落在 media/ 内会：① 被下次扫描当成新孤儿 ② 被 media_migrate 搬上云
        # ③ 让 /media/ 路由下「看起来干净」变成假象。所以是硬错误，不提供覆盖。
        if backup == media_root or media_root in backup.parents:
            raise CommandError(
                '备份目录不能位于媒体目录内：%s\n'
                '（否则备份会被当成新的孤儿，也会被 media_migrate 搬上对象存储）'
                % backup)
        return backup

    def _do_move(self, media_root, backup_dir, orphan_keys, opts):
        backup_dir.mkdir(parents=True, exist_ok=True)

        # ⚠ 中断重跑时，备份里可能已经有上一轮移过来的文件，而 MANIFEST
        # 还没写（它在末尾统一落盘）。此时若只把「本轮新移的」写进清单，
        # 中断过的那批就永久失去溯源记录 —— 「可恢复」的承诺在第一次
        # Ctrl-C 之后就失效了。所以先把备份里已有的全部补进来。
        # 用 dict 保证 key 唯一（同一文件不会出现两行）。
        row_map = {}
        for p in sorted(backup_dir.rglob('*')):
            if not p.is_file() or p.name in (MANIFEST_NAME, 'README.txt'):
                continue
            rel = p.relative_to(backup_dir).as_posix()
            row_map[rel] = rel
        recovered = len(row_map)

        self.stdout.write(self.style.WARNING(
            '开始移送到备份目录（文件仍可恢复）：%s' % backup_dir))
        if recovered:
            self.stdout.write(
                '  备份里已有 %d 个文件（上一轮中断留下），将一并计入清单' % recovered)

        moved, skipped, failed = 0, 0, []
        rows = []
        for i, key in enumerate(orphan_keys, 1):
            src = media_root / key
            dst = backup_dir / key
            try:
                # 已移过（上次中断）：补齐 MANIFEST，不重复移动
                if dst.exists():
                    row_map.setdefault(key, key)
                    skipped += 1
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                # 移动后必须核对：源没了 + 目标有了。只信返回值不算「看到」。
                if src.exists() or not dst.exists():
                    raise RuntimeError('移动后校验失败：源仍存在或目标缺失')
                row_map[key] = key
                moved += 1
            except Exception as exc:              # noqa: BLE001 —— 单个失败不中断整批
                failed.append((key, '%s: %s' % (type(exc).__name__, exc)))
                self.stdout.write(self.style.ERROR('  ✗ %s —— %s' % (key, exc)))

            if i % 200 == 0 or i == len(orphan_keys):
                self.stdout.write('  [%d/%d] 已移动 %d，跳过 %d，失败 %d'
                                  % (i, len(orphan_keys), moved, skipped, len(failed)))

        rows = sorted(row_map.items())
        if rows:
            _write_manifest(backup_dir, rows)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            '清理完成：移送 %d，跳过 %d，失败 %d' % (moved, skipped, len(failed))))
        # 清单条数必须等于备份里的文件数，否则说明有文件没被记录（找不回来）。
        manifest = backup_dir / MANIFEST_NAME
        backed_up = len([p for p in backup_dir.rglob('*')
                         if p.is_file() and p.name not in (MANIFEST_NAME, 'README.txt')])
        self.stdout.write('  清单      %d 条（备份文件 %d 个）%s'
                          % (len(rows), backed_up, '' if len(rows) == backed_up
                             else '  ← 不一致，有文件未被记录'))
        self.stdout.write('  清单文件  %s' % manifest)
        self.stdout.write('  说明      %s' % (backup_dir / 'README.txt'))
        if failed:
            self.stdout.write(self.style.WARNING(
                '有 %d 个失败，重跑本命令即可（幂等）；不影响已移送的文件。' % len(failed)))

        if opts['remove_empty_dirs']:
            removed = self._remove_empty_dirs(media_root)
            self.stdout.write('  空目录  %d 个已删除' % removed)

        self.stdout.write(self.style.WARNING(
            '备份未删除。确认这些文件没有保留价值后再执行：\n'
            '  python manage.py media_clean_orphans --purge '
            '--backup-dir %s --yes' % backup_dir))

    def _remove_empty_dirs(self, media_root):
        """自底向上删除空目录。只删**空**目录，非空的一律不碰。"""
        removed = 0
        for p in sorted((x for x in media_root.rglob('*') if x.is_dir()),
                        key=lambda x: len(x.parts), reverse=True):
            try:
                next(p.iterdir())
            except StopIteration:
                p.rmdir()
                removed += 1
            except OSError:
                pass
        return removed

    def _do_rebuild_manifest(self, opts):
        """按备份目录里的现有文件重写 MANIFEST，不移动任何文件。

        ## 为什么需要它

        清理**被中断过一次**之后，续跑会用一个新的时间戳目录（默认备份目录名
        带时间戳），于是文件被分到两个备份目录里，且第一个的 MANIFEST 还没写。
        把两者合并之后，必须重新生成清单才能保证「清单条数 == 文件数」——
        少了就等于有文件失去了溯源记录，而这一点**没有任何报错**。
        """
        if not opts['backup_dir']:
            raise CommandError('请用 --backup-dir 指定要重建清单的备份目录。')
        backup = Path(opts['backup_dir']).resolve()
        if not backup.is_dir():
            raise CommandError('备份目录不存在：%s' % backup)

        files = sorted(
            p for p in backup.rglob('*')
            if p.is_file() and p.name not in (MANIFEST_NAME, 'README.txt'))
        rows = [(p.relative_to(backup).as_posix(),) * 2 for p in files]
        _write_manifest(backup, rows)

        manifest = backup / MANIFEST_NAME
        data_lines = [l for l in manifest.read_text(encoding='utf-8').splitlines()
                      if l and not l.startswith('#')]
        self.stdout.write(self.style.SUCCESS(
            '已重建清单：%d 条 / %d 个文件' % (len(data_lines), len(files))))
        self.stdout.write('  %s' % manifest)
        if len(data_lines) != len(files):
            raise CommandError(
                '清单条数与文件数不一致（%d != %d），请检查备份目录'
                % (len(data_lines), len(files)))

    def _do_purge(self, opts):
        if not opts['yes']:
            raise CommandError(
                '--purge 会**永久删除**备份目录，必须同时给 --yes。')
        if not opts['backup_dir']:
            raise CommandError(
                '请用 --backup-dir 指定要删除的备份目录（避免误删别的）。')
        backup = Path(opts['backup_dir']).resolve()
        if not backup.is_dir():
            raise CommandError('备份目录不存在：%s' % backup)
        manifest = backup / MANIFEST_NAME
        count = sum(1 for _ in manifest.open(encoding='utf-8')) if manifest.exists() else -1
        self.stdout.write(self.style.WARNING(
            '即将永久删除备份目录：%s（记录 %s 条）' % (backup, count if count >= 0 else '无')))
        shutil.rmtree(str(backup))
        self.stdout.write(self.style.SUCCESS('已删除：%s' % backup))
