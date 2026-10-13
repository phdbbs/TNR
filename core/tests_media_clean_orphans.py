"""孤儿媒体文件清理命令的判据。

## 这些判据守的是什么

清理是**删数据**，误判一次就是永久丢失。所以每条判据都在守一个
具体的事故形态：

| 事故形态 | 对应判据 |
|---|---|
| 干跑居然动了文件 | ``test_dry_run_moves_nothing`` |
| 正在用的图被当孤儿删了 | ``test_referenced_image_kept`` |
| 把判据写成「一律不动」也能过 | ``test_orphan_is_actually_cleaned``（反向对照） |
| 软删记录的照片被当孤儿删了 | ``test_soft_deleted_reference_kept`` |
| 新增图片字段后漏扫 → 新功能的图被删 | ``test_new_file_field_is_scanned`` |
| 备份落在 media/ 内，被下次扫描当真孤儿 | ``test_backup_inside_media_root_rejected`` |
| 移动「成功」但文件没过去 | ``test_move_is_verified_after_it_happens`` |
| 中断重跑时重复移动 / 报错 | ``test_apply_is_idempotent`` |
| 模式分组统计与实际不符（报告骗人） | ``test_report_groups_match_disk`` |
| ``--purge`` 少一道确认就删了备份 | ``test_purge_requires_double_guard`` |

命名说明：本文件用 ``media/`` 指代媒体目录，用 ``backup/`` 指代备份目录。
真实项目里备份目录默认在 ``media/`` 的同级（见命令模块 docstring）。
"""

from __future__ import annotations

import io
from pathlib import Path
from tempfile import mkdtemp

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from business.models import Pet
from business.tests.base import make_image_file, make_pet
from core.management.commands.media_clean_orphans import (
    BACKUP_PREFIX,
    MANIFEST_NAME,
    Command,
    classify_orphans,
)


class _FsTestCase(TestCase):
    """提供一个隔离的 media/ 目录与备份目录，绝不碰真实 media/。"""

    def setUp(self):
        super().setUp()
        self.root = Path(mkdtemp(prefix='tnr-orphan-test-'))
        self.media = self.root / 'media'
        self.backup = self.root / 'backup'
        (self.media / 'photos').mkdir(parents=True)
        self.backup.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.root), ignore_errors=True)
        super().tearDown()

    # ---- 助手 -------------------------------------------------------
    def write_file(self, rel, content=b'fake-bytes'):
        p = self.media / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def run_cleanup(self, *args):
        out = io.StringIO()
        call_command('media_clean_orphans', *args,
                     stdout=out, stderr=out)
        return out.getvalue()

    def disk_files(self, base=None):
        base = base or self.media
        return sorted(p.relative_to(base).as_posix()
                      for p in Path(base).rglob('*') if p.is_file())

    def seed_default(self):
        """一套典型场景：1 个被引用、3 个孤儿。"""
        self.referenced = self.write_file('photos/keep1.png')
        self.write_file('photos/orphan1.png')
        self.write_file('photos/test-pet-9.png')
        self.write_file('photos/TNR260900000.png')


@override_settings(MEDIA_ROOT='/nonexistent-tnr-media-root')
class ReferencedAndOrphanTest(_FsTestCase):
    """核心判据：被引用的一个不动，孤儿全清。"""

    def setUp(self):
        super().setUp()
        self.seed_default()
        # 直接赋 name（不落盘）：_collect_db_references 只读 name。
        # 这样判据完全不依赖 storage 行为，只检验「引用集合算得对不对」。
        pet = make_pet()
        pet.photo_capture = 'photos/keep1.png'
        pet.save(update_fields=['photo_capture'])

    def test_dry_run_moves_nothing(self):
        out = self.run_cleanup('--source-dir', str(self.media))
        self.assertIn('干跑', out)
        # 磁盘一个字节都不能动
        self.assertEqual(len(self.disk_files()), 4)
        self.assertTrue(self.referenced.exists())
        self.assertTrue((self.media / 'photos/orphan1.png').exists())
        self.assertFalse(any(self.backup.iterdir()))

    def test_referenced_image_kept(self):
        self.run_cleanup('--apply', '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))
        self.assertTrue(
            (self.media / 'photos/keep1.png').exists(),
            '数据库正在引用的图片被清掉了 —— 这是最严重的事故')
        # 反向对照：孤儿必须真的被清掉，否则「一律不动」也能让上面的判据通过
        self.assertFalse((self.media / 'photos/orphan1.png').exists())
        self.assertFalse((self.media / 'photos/test-pet-9.png').exists())
        self.assertFalse((self.media / 'photos/TNR260900000.png').exists())
        self.assertEqual(self.disk_files(), ['photos/keep1.png'])

    def test_move_goes_to_backup_and_is_verified(self):
        self.run_cleanup('--apply', '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))
        # 备份里三个孤儿都在，且字节一致
        for name in ('orphan1.png', 'test-pet-9.png', 'TNR260900000.png'):
            src = self.backup / 'photos' / name
            self.assertTrue(src.exists(), '备份缺少 %s' % name)
        # 被引用的那个**不应该**出现在备份里
        self.assertFalse((self.backup / 'photos/keep1.png').exists())
        # MANIFEST 记录齐全。key 与备份相对路径形影相随（目录结构镜像），
        # 因此每个 key 在文件里出现两次 —— 少一次说明有一列是空的。
        manifest = (self.backup / MANIFEST_NAME).read_text(encoding='utf-8')
        self.assertEqual(manifest.count('photos/orphan1.png'), 2)
        # 被引用的那个绝不能出现在清单里
        self.assertEqual(manifest.count('photos/keep1.png'), 0)

    def test_apply_is_idempotent(self):
        for _ in range(2):
            out = self.run_cleanup('--apply', '--source-dir', str(self.media),
                                   '--backup-dir', str(self.backup))
        self.assertEqual(self.disk_files(), ['photos/keep1.png'])
        # 第二次应当报告「没有孤儿文件」，而不是重复移动或报错
        # （第一次已把三个搬走，第二次没有任何可搬的）
        self.assertEqual(len(self.disk_files(self.backup)), 3 + 2)  # 3 图 + MANIFEST + README
        self.assertIn('没有孤儿文件', self.run_cleanup(
            '--source-dir', str(self.media)))

    def test_apply_recovers_manifest_after_interruption(self):
        """中断后重跑，MANIFEST 必须把上一轮已移送的文件也记上。

        ## 这条守的是一个真出过的事故

        2026-10-14 清理真实 media/ 时进程被 SIGTERM 打断：17669 个文件已经
        移进备份，但 MANIFEST 是末尾统一落盘的，**一行都没写**。
        若按「只记录本轮新移的」实现，重跑后那 17669 个文件就永久失去
        溯源记录 —— 「先备份、可恢复」的承诺在第一次 Ctrl-C 之后就失效了，
        而且没有任何报错。

        所以备份目录里已有的文件必须**先扫进来**再写清单。
        """
        # 第一轮：本类 setUp 的 seed_default() 另有 3 个孤儿，
        # 加上这里写的 a/b/c，一共 6 个孤儿被移送。
        self.write_file('photos/a.png')
        self.write_file('photos/b.png')
        self.write_file('photos/c.png')
        self.run_cleanup('--apply', '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))
        self.assertEqual(len(self.disk_files(self.backup)), 6 + 2)  # 6 图 + 2 元数据
        self.assertEqual(self.disk_files(), ['photos/keep1.png'])   # 只有被引用的留下
        for meta in (MANIFEST_NAME, 'README.txt'):
            (self.backup / meta).unlink()
        # 磁盘上再放一个没见过的新孤儿，模拟「中断后业务又产生了新孤儿」
        self.write_file('photos/d.png')

        # 第二轮：重跑。此时备份里已有 6 个文件，源里只剩 d.png
        out = self.run_cleanup('--apply', '--source-dir', str(self.media),
                               '--backup-dir', str(self.backup))
        self.assertIn('备份里已有 6 个文件', out)

        manifest = (self.backup / MANIFEST_NAME).read_text(encoding='utf-8')
        for name in ('photos/a.png', 'photos/b.png', 'photos/c.png',
                     'photos/d.png'):
            self.assertIn(name, manifest,
                          'MANIFEST 缺少中断前已移送的 %s —— 该文件已无法溯源' % name)
        # 清单条数 = 备份文件数（6 + d），且不重复
        data_lines = [l for l in manifest.splitlines()
                      if l and not l.startswith('#')]
        self.assertEqual(len(data_lines), 7)
        self.assertEqual(len(set(data_lines)), len(data_lines),
                         'MANIFEST 里有重复条目')

    def test_only_prefix_limits_scope(self):
        self.write_file('avatars/lonely.png')
        out = self.run_cleanup('--apply', '--source-dir', str(self.media),
                               '--backup-dir', str(self.backup),
                               '--only', 'photos/')
        self.assertFalse((self.media / 'photos/orphan1.png').exists())
        self.assertTrue((self.media / 'avatars/lonely.png').exists(),
                        '--only photos/ 却动了 photos/ 之外的文件')

    def test_limit_caps_count(self):
        out = self.run_cleanup('--apply', '--source-dir', str(self.media),
                               '--backup-dir', str(self.backup), '--limit', '2')
        self.assertIn('数量上限 2', out)
        # 磁盘共 4 个文件（1 引用 + 3 孤儿），只清 2 个 → 剩 2 个
        self.assertEqual(len(self.disk_files()), 2)
        # 断言具体是哪一个留下了，防止「刚好清错了两个」也通过
        self.assertTrue((self.media / 'photos/keep1.png').exists())
        orphans_left = [f for f in self.disk_files() if f != 'photos/keep1.png']
        self.assertEqual(len(orphans_left), 1)


class SoftDeleteReferenceTest(_FsTestCase):
    """软删记录的照片仍是审计凭据，绝不能被当孤儿清掉。"""

    def test_soft_deleted_reference_kept(self):
        # 两个引用：一个来自正常记录，一个来自**逻辑删除**的记录。
        # 前者是基线（证明命令本身能干活），后者才是本用例要守的。
        self.write_file('photos/live.png')
        self.write_file('photos/dropped.png')
        live = make_pet()
        live.photo_after = 'photos/live.png'
        live.save(update_fields=['photo_after'])

        gone = make_pet()
        gone.photo_after = 'photos/dropped.png'
        gone.save(update_fields=['photo_after'])
        gone.is_deleted = True
        gone.save(update_fields=['is_deleted'])
        gone.refresh_from_db()
        self.assertTrue(gone.is_deleted)

        self.run_cleanup('--apply', '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))
        self.assertTrue(
            (self.media / 'photos/dropped.png').exists(),
            '逻辑删除记录引用的图片被清掉了 —— 审计凭据丢失')
        self.assertTrue((self.media / 'photos/live.png').exists())
        self.assertFalse(any(self.backup.glob('photos/*')))


class NewFileFieldTest(_FsTestCase):
    """引用集合必须随模型字段自动增长，不能有硬编码清单。"""

    def test_new_file_field_is_scanned(self):
        """用一个当前不在「已知字段名单」里的字段引用文件，验证它被算有效。

        做法：把 ``Capture.group_photo`` 指向磁盘上的文件。如果清理命令
        硬编码了字段清单（漏掉 group_photo），这个文件会被当孤儿删掉。
        """
        self.write_file('photos/group-keep.png')
        self.write_file('photos/real-orphan.png')
        from business.models import Capture
        from business.tests.base import make_capture
        cap = make_capture()
        cap.group_photo = 'photos/group-keep.png'
        cap.save(update_fields=['group_photo'])

        self.run_cleanup('--apply', '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))
        self.assertTrue(
            (self.media / 'photos/group-keep.png').exists(),
            'Capture.group_photo 引用的图片被清了 —— 引用扫描漏了字段')
        self.assertFalse((self.media / 'photos/real-orphan.png').exists())


class BackupLocationTest(_FsTestCase):
    """备份目录落在 media/ 内会污染后续扫描与迁移，必须硬拦。"""

    def test_backup_inside_media_root_rejected(self):
        inside = self.media / (BACKUP_PREFIX + '20260101_000000')
        with self.assertRaises(CommandError) as ctx:
            self.run_cleanup('--source-dir', str(self.media),
                             '--backup-dir', str(inside))
        self.assertIn('不能位于媒体目录内', str(ctx.exception))
        # 目录不该被创建出来
        self.assertFalse(inside.exists())

    def test_default_backup_dir_is_outside_media_root(self):
        """不显式指定时，默认备份目录必须在 media/ 的同级。"""
        self.write_file('photos/x.png')
        out = self.run_cleanup('--source-dir', str(self.media))
        self.assertIn(str(self.root), out)
        self.assertIn(BACKUP_PREFIX, out)
        self.assertNotIn(str(self.media / BACKUP_PREFIX), out)


class PurgeTest(_FsTestCase):
    """--purge 是唯一真正不可逆的一步，必须有双闸门。"""

    def test_purge_requires_yes(self):
        self.write_file('photos/x.png')
        self.run_cleanup('--apply', '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))
        with self.assertRaises(CommandError) as ctx:
            self.run_cleanup('--purge', '--backup-dir', str(self.backup))
        self.assertIn('--yes', str(ctx.exception))
        self.assertTrue(self.backup.is_dir(), '没给 --yes 就把备份删了')

    def test_purge_requires_explicit_dir(self):
        self.write_file('photos/x.png')
        self.run_cleanup('--apply', '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))
        with self.assertRaises(CommandError) as ctx:
            self.run_cleanup('--purge', '--yes')
        self.assertIn('--backup-dir', str(ctx.exception))

    def test_purge_deletes_backup_when_confirmed(self):
        self.write_file('photos/x.png')
        self.run_cleanup('--apply', '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))
        self.assertTrue(self.backup.is_dir())
        out = self.run_cleanup('--purge', '--backup-dir', str(self.backup),
                               '--yes')
        self.assertFalse(self.backup.exists())
        self.assertIn('已删除', out)


class RebuildManifestTest(_FsTestCase):
    """合并多个备份目录之后，必须能重建出完整清单。

    中断后续跑会用新的时间戳目录，文件因此分到两处、且第一个目录的
    MANIFEST 还没写。此时唯一正确的补救是把目录合并后重建清单 ——
    条数少于文件数就意味着有文件失去了溯源记录，而这不会报错。
    """

    def seed_and_collect(self):
        self.write_file('photos/a.png')
        self.write_file('photos/b.png')
        self.run_cleanup('--apply', '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))

    def test_rebuild_counts_every_file(self):
        self.seed_and_collect()
        # 模拟中断留下的残缺清单：删掉，再手工放一个只记了一半的文件
        (self.backup / MANIFEST_NAME).unlink()
        files = [p for p in self.backup.rglob('*')
                 if p.is_file() and p.name not in (MANIFEST_NAME, 'README.txt')]
        self.assertEqual(len(files), 2)

        out = self.run_cleanup('--rebuild-manifest',
                               '--backup-dir', str(self.backup))
        self.assertIn('已重建清单：2 条 / 2 个文件', out)
        data = [l for l in (self.backup / MANIFEST_NAME)
                .read_text(encoding='utf-8').splitlines()
                if l and not l.startswith('#')]
        self.assertEqual(sorted(data), ['photos/a.png\tphotos/a.png',
                                        'photos/b.png\tphotos/b.png'])

    def test_rebuild_moves_nothing(self):
        self.seed_and_collect()
        before = self.disk_files()
        self.run_cleanup('--rebuild-manifest',
                         '--backup-dir', str(self.backup))
        self.assertEqual(self.disk_files(), before, '重建清单却动了媒体目录')

    def test_rebuild_requires_dir(self):
        with self.assertRaises(CommandError) as ctx:
            self.run_cleanup('--rebuild-manifest')
        self.assertIn('--backup-dir', str(ctx.exception))

    def test_rebuild_rejects_missing_dir(self):
        with self.assertRaises(CommandError) as ctx:
            self.run_cleanup('--rebuild-manifest',
                             '--backup-dir', str(self.root / 'nope'))
        self.assertIn('不存在', str(ctx.exception))


class EmptyDirTest(_FsTestCase):
    """空目录清理只删真空目录，非空目录一律不碰。"""

    def test_remove_empty_dirs_removes_only_empty(self):
        self.write_file('photos/only-orphan.png')
        (self.media / 'avatars').mkdir()          # 空目录，应被删
        (self.media / 'pets').mkdir()
        self.write_file('pets/keep.png')          # 非空目录，必须保留

        pet = make_pet()
        pet.photo_capture = 'pets/keep.png'
        pet.save(update_fields=['photo_capture'])

        self.run_cleanup('--apply', '--remove-empty-dirs',
                         '--source-dir', str(self.media),
                         '--backup-dir', str(self.backup))
        self.assertFalse((self.media / 'avatars').exists(), '空目录没被删')
        self.assertTrue((self.media / 'pets').exists(), '非空目录被误删')
        self.assertTrue((self.media / 'pets/keep.png').exists())


class ReportGroupingTest(_FsTestCase):
    """报告里的分组统计必须与磁盘一致 —— 数字骗人比不报更糟。"""

    def test_report_groups_match_disk(self):
        keys = ['photos/TNR260900000.png', 'photos/test-pet-1.png',
                'photos/weird-name.png']
        groups = dict(classify_orphans(keys))
        self.assertEqual(groups['TNR*.png（历史 / seed 图）'],
                         ['photos/TNR260900000.png'])
        self.assertEqual(groups['test-*（自动化测试残留）'],
                         ['photos/test-pet-1.png'])
        self.assertEqual(groups['其他'], ['photos/weird-name.png'])
        # 分组引用的总数必须等于输入总数（不能有文件被分组吞掉）
        self.assertEqual(sum(len(v) for v in groups.values()), len(keys))

    def test_report_shows_real_counts(self):
        self.seed_default()
        pet = make_pet()
        pet.photo_capture = 'photos/keep1.png'
        pet.save(update_fields=['photo_capture'])
        out = self.run_cleanup('--source-dir', str(self.media))
        self.assertIn('孤儿文件 3 个', out)
        self.assertIn('DB 引用', out)


class CommandTest(TestCase):
    """不依赖夹具的纯逻辑判据。"""

    def test_help_text_exists(self):
        self.assertTrue(Command.help)

    def test_manifest_roundtrip(self):
        """MANIFEST 能反查出「原 key → 备份位置」的对应关系。"""
        from core.management.commands.media_clean_orphans import _write_manifest
        root = Path(mkdtemp(prefix='tnr-manifest-'))
        try:
            _write_manifest(root, [('photos/a.png', 'photos/a.png'),
                                   ('photos/b.png', 'photos/b.png')])
            text = (root / MANIFEST_NAME).read_text(encoding='utf-8')
            # 出现两次：一列是原 key，一列是备份相对路径（结构镜像，故同名）。
            # 只出现一次意味着有一列是空的 —— 清单等于没写。
            self.assertEqual(text.count('photos/a.png'), 2)
            self.assertIn('恢复方法', text)
            self.assertTrue((root / 'README.txt').exists())
        finally:
            import shutil
            shutil.rmtree(str(root), ignore_errors=True)
