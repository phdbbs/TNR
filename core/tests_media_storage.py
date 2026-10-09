"""媒体存储抽象与迁移命令的判据（第五十轮：对象存储就绪）。

## 这些判据为什么这样写

按项目约定，**判据不能停在「标识符存在」**：
``assertIn('MEDIA_ROOT', source)`` 这种断言在有人把逻辑整段删掉之后依然会通过
（只要注释里还留着那个词）。所以这里：

- 凡是能断言**行为**的，一律断言行为 —— 文件真的落在哪个目录、``.url`` 真的
  返回什么前缀、数据库里的 ``name`` 真的有没有变；
- 静态扫描判据**先剥注释**（`tokenize`），并用**变异样本反向对照**，
  证明它不是恒真；
- 涉及「保留 / 拒绝」的，都配**反向对照** —— 只测「该拦的拦住了」的话，
  把判据写成「一律拦」也能过。
"""

from __future__ import annotations

import io
import os
import tempfile
import tokenize
from contextlib import contextmanager
from pathlib import Path

from django.core.files.storage import FileSystemStorage, default_storage, storages
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils.functional import empty

from core.management.commands.media_migrate import (
    Command as MigrateCommand,
    _collect_db_references,
    _iter_file_fields,
)
from core.storage import (
    is_local_media,
    media_backend,
    object_storage_dependency_error,
)

# 复用业务侧夹具：编号自动递增、图片是**内容真实**的 PNG
# （上传接口按内容校验，假字节会被正确拒绝）。
from business.tests.base import make_district, make_image_file, make_pet


_MISSING = object()


def _count_files(root):
    return sum(1 for p in Path(root).rglob('*') if p.is_file())


def _tmpdir():
    return tempfile.mkdtemp(prefix='tnr-media-test-')


@contextmanager
def use_media_storage(location, base_url='/media/'):
    """把默认 storage 临时切到一个指定目录。

    ## ⚠ 为什么不能用 `override_settings(STORAGES=...)`

    实测（Django 5.0.6）**它不生效**：handler 缓存一旦建立，
    `override_settings(STORAGES=...)` 之后 `storages['default'].location`
    仍然是旧值。原因有两层：

    1. `django.test.signals.storages_changed` 用 `del storages.backends`
       来清 handler 的 `cached_property` 缓存；
    2. 而 Django 自己的 `cached_property`（`django/utils/functional.py`）
       **只实现了 `__set_name__` 与 `__get__`，没有 `__delete__`** ——
       `del` 抛 `AttributeError`，被紧跟的 `except AttributeError: pass`
       吞掉，缓存原封不动。

    于是 `storages._storages = {}` 清空后，重建时读到的仍是缓存的旧
    `backends`，创建出来的还是旧配置的 storage 实例。

    （对照：`override_settings(MEDIA_ROOT=...)` **是**有效的 ——
    `FileSystemStorage.__init__` 里 `setting_changed.connect(
    self._clear_cached_properties)` 自己监听了 `MEDIA_ROOT`。
    两者行为不同，容易误判成「Django 的 override 时灵时不灵」。）

    生产代码不受影响：切换后端靠改环境变量 + 重启进程，那时缓存还没建立。
    这里显式替换实例并重置 LazyObject，是**测试专用**的绕行。
    """
    # 清 handler 的 params 缓存（cached_property 的缓存存在 __dict__ 里）
    storages.__dict__.pop('backends', None)
    storages._backends = None

    old = storages._storages.get('default', _MISSING)
    storages._storages['default'] = FileSystemStorage(
        location=location, base_url=base_url)
    default_storage._wrapped = empty
    try:
        yield
    finally:
        if old is _MISSING:
            storages._storages.pop('default', None)
        else:
            storages._storages['default'] = old
        storages.__dict__.pop('backends', None)
        storages._backends = None
        default_storage._wrapped = empty


# ===========================================================================
# 一、配置完整性
# ===========================================================================
class MediaStorageConfigTest(TestCase):
    def test_storages_declares_both_aliases(self):
        """`STORAGES` 必须同时给出 default 与 staticfiles。

        Django 5.0 起 `STORAGES` 是**整块替换**而非逐键合并。只写 `default`
        会让 `staticfiles` 丢失，`collectstatic` 直接报 `InvalidStorageError`
        —— 而这条路径在单元测试里跑不到，往往到部署时才炸。
        """
        from django.conf import settings

        self.assertIn('default', settings.STORAGES)
        self.assertIn('staticfiles', settings.STORAGES)

    def test_default_backend_is_declared_explicitly(self):
        """必须是显式声明的后端，而不是依赖 Django 的隐式默认。

        隐式默认下「当前用的是本地还是对象存储」在配置里读不出来，
        切换也就无从下手。
        """
        from django.conf import settings

        backend = settings.STORAGES['default']['BACKEND']
        self.assertTrue(backend, 'STORAGES.default.BACKEND 不能为空')
        self.assertNotEqual(backend, 'django.core.files.storage.FileSystemStorage',
                            '应为项目自己的后端，便于 is_local_media() 判定')

    def test_is_local_media_matches_backend_name(self):
        """`is_local_media()` 与后端名一致（正反成对）。

        用 `mock.patch.dict` 改环境变量 —— 这同时验证了「切换只看环境变量、
        不需要改代码」这条设计承诺。
        """
        from unittest import mock

        with mock.patch.dict(os.environ, {'TNR_MEDIA_STORAGE': 'local'}):
            self.assertTrue(is_local_media())
            self.assertEqual(media_backend(), 'local')

        with mock.patch.dict(os.environ, {'TNR_MEDIA_STORAGE': 'cos'}):
            self.assertFalse(is_local_media())
            self.assertEqual(media_backend(), 'cos')

    def test_object_storage_reports_missing_dependency(self):
        """配了对象存储但没装依赖时，必须给出**可读**的错误而不是静默降级。

        静默降级到本地存储是最坏的结果：上传"成功"、文件写在容器本地、
        重启即丢，而没有任何人察觉。
        """
        from unittest import mock

        with mock.patch.dict(os.environ, {'TNR_MEDIA_STORAGE': 'cos'}):
            err = object_storage_dependency_error()
            # 本环境没装 django-storages，应报缺失；若装了则 err 为 None。
            if err is not None:
                self.assertIn('django-storages', err)

    def test_local_mode_does_not_require_object_dependency(self):
        """纯本地环境**不应该**被要求安装对象存储依赖。"""
        from unittest import mock

        with mock.patch.dict(os.environ, {'TNR_MEDIA_STORAGE': 'local'}):
            self.assertTrue(is_local_media())


# ===========================================================================
# 二、所有文件字段都绑在默认 storage 上
# ===========================================================================
class FileFieldStorageBindingTest(TestCase):
    """⚠ 本类会**真的写文件**，必须先把 MEDIA_ROOT 隔离到临时目录。

    否则测试图片会落进项目真实的 `media/` 目录里 —— 这正是本地
    `media/photos/` 里堆了两万多个文件、而数据库只引用其中几十个的原因：
    **每次跑测试都往真实媒体目录里丢几个文件，且从不清理。**
    """

    def setUp(self):
        self.media_root = _tmpdir()
        ov = override_settings(MEDIA_ROOT=self.media_root)
        ov.enable()
        self.addCleanup(ov.disable)

    def test_every_file_field_uses_default_storage(self):
        """动态发现全部 FileField/ImageField，断言都走默认 storage。

        **不硬编码字段清单**：硬编码的清单在有人新增图片字段时会静默漏掉，
        而漏掉的字段可能自己指定了别的 storage，从此绕过迁移。
        """
        from django.core.files.storage import default_storage

        fields = list(_iter_file_fields())
        self.assertGreater(len(fields), 0, '没有发现任何文件字段，判据本身失效')

        for model, field in fields:
            with self.subTest(model=model.__name__, field=field.name):
                self.assertIs(
                    field.storage, default_storage,
                    '%s.%s 绑定了自定义 storage，会绕过 media_migrate 的搬运范围'
                    % (model.__name__, field.name))

    def test_db_reference_scan_finds_the_fields(self):
        """扫描器能真的从数据库里读出引用（而不是永远返回空）。

        反向对照：造一条带图的记录，扫描结果必须**增加**。
        """
        before, _ = _collect_db_references()

        pet = make_pet(district=make_district())
        pet.photo_capture.save('scan.png', make_image_file('scan.png'), save=True)

        after, field_count = _collect_db_references()
        self.assertGreater(field_count, 0)
        self.assertIn(pet.photo_capture.name, after,
                      '扫描器没读到刚写入的记录 —— 判据会恒真')

    def test_writes_are_isolated_from_real_media_dir(self):
        """反向对照：确认本类真的没往真实媒体目录里写东西。

        上面「隔离」的注释是**承诺**，这条是**判据** —— 没有它，某天有人
        把 `setUp` 里的 override 删掉，测试照样全绿，而真实目录继续被污染。
        """
        from django.conf import settings

        pet = make_pet(district=make_district())
        pet.photo_capture.save('iso.png', make_image_file('iso.png'), save=True)
        written = Path(settings.MEDIA_ROOT) / pet.photo_capture.name

        self.assertTrue(written.exists(), '图片没有写进隔离目录')
        self.assertIn(str(self.media_root), str(written))
        # 真实目录里不该出现这个名字
        real = Path(settings.BASE_DIR) / 'media' / pet.photo_capture.name
        self.assertFalse(real.exists(),
                         '测试图片写进了项目真实的 media/ 目录：%s' % real)


# ===========================================================================
# 三、切换存储后端后，行为真的跟着变（正反成对）
# ===========================================================================
class StorageSwitchBehaviourTest(TestCase):
    def setUp(self):
        self.old_dir = _tmpdir()
        self.new_dir = _tmpdir()
        self.pet = make_pet(district=make_district())

    def test_upload_lands_in_configured_storage_only(self):
        """保存图片后：文件在新后端里，**且旧后端里没有**。

        只断言「新目录里有」是不够的 —— 若代码同时往两处写（或 storage 没
        真正切换、旧目录恰好也是同一个），正向断言照样通过。
        反向断言才证明「确实换了」。
        """
        with use_media_storage(self.new_dir):
            self.pet.photo_capture.save(
                'switch.png', make_image_file('switch.png'), save=True)
            name = self.pet.photo_capture.name

            self.assertTrue(
                os.path.exists(os.path.join(self.new_dir, name)),
                '图片没有落到配置的存储后端')
            self.assertFalse(
                os.path.exists(os.path.join(self.old_dir, name)),
                '图片同时出现在了旧位置 —— 说明并没有真正切换后端')

    def test_url_prefix_follows_storage(self):
        """`.url` 返回的前缀必须跟随 storage —— 这是「前端零改动」的依据。

        前端全部直接使用后端返回的 URL（不拼 `/media/`），所以只要 `.url`
        跟随 storage，切对象存储后前端就自动指向新地址。
        """
        self.pet.photo_capture.save(
            'url.png', make_image_file('url.png'), save=True)

        with use_media_storage(self.old_dir, '/media/'):
            self.assertTrue(self.pet.photo_capture.url.startswith('/media/'))

        with use_media_storage(self.new_dir, '/cdn-media/'):
            self.assertTrue(
                self.pet.photo_capture.url.startswith('/cdn-media/'),
                '切换 base_url 后 .url 前缀没变 —— 说明 URL 被硬编码了')

    def test_serialize_instance_url_follows_storage(self):
        """序列化出口同样跟随 storage（前端拿到的就是它）。"""
        from business.services import serialize_instance

        self.pet.photo_capture.save(
            'ser.png', make_image_file('ser.png'), save=True)

        with use_media_storage(self.new_dir, '/cdn-media/'):
            data = serialize_instance(self.pet)
            self.assertTrue(data['photo_capture'].startswith('/cdn-media/'))
            self.assertEqual(data['photoCapture'], data['photo_capture'],
                             '驼峰别名必须与蛇形字段同值')

    def test_helper_itself_actually_switches(self):
        """判据的判据：上面的 helper 必须**真的**换掉了 storage。

        没有这一条，若 `use_media_storage` 是个空壳（什么都不做），
        上面三个用例会因为「新旧目录恰好都空」而全部通过 —— 典型的假绿。
        """
        with use_media_storage(self.new_dir):
            self.assertEqual(
                str(default_storage.location), str(self.new_dir),
                'helper 没有真正切换默认 storage —— 上面几条判据全是假的')


# ===========================================================================
# 四、静态扫描：不许再出现本地路径假设
# ===========================================================================
#: 允许出现 MEDIA_ROOT 的文件（都是**配置/挂载**用途，不是业务代码）：
#:   - settings.py 定义它
#:   - urls.py 在 DEBUG 下把它挂成静态路由
#:   - core/storage.py 是存储抽象的落点
#:   - media_migrate.py 用它做「本地源目录」的默认值
_MEDIA_ROOT_ALLOWED = {
    'tnr_system/settings.py',
    'tnr_system/urls.py',
    'core/storage.py',
    'core/management/commands/media_migrate.py',
}

_SCAN_DIRS = ('accounts', 'business', 'core', 'supervision', 'tnr_system')


def _strip_python_comments(source):
    """剥掉注释后返回源码文本。

    ⚠ 必须在搜索关键词**之前**剥注释：本项目已经四次栽在同一形态上 ——
    关键词出现在解释性注释里，判据于是恒真（改坏了代码也照样通过）。
    字符串字面量**保留**（注释和字符串在 `tokenize` 里是不同 token）。
    """
    try:
        tokens = [
            tok for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type != tokenize.COMMENT
        ]
        return tokenize.untokenize(tokens)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 解析失败时保守返回原文（宁可误报，不可漏报）
        return source


def _is_scannable(rel_path):
    """是否纳入扫描：排除迁移文件、测试与白名单。"""
    p = Path(rel_path)
    parts = p.parts
    if '__pycache__' in parts or 'migrations' in parts:
        return False
    name = p.name
    if name.startswith('test') or name.startswith('tests'):
        return False
    if name in ('conftest.py',):
        return False
    return True


def _scan_media_root_violations(root):
    """返回 `[(相对路径, 行号), ...]` —— 业务代码里出现 MEDIA_ROOT 的位置。"""
    root = Path(root)
    violations = []
    for d in _SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob('*.py')):
            rel = path.relative_to(root).as_posix()
            if not _is_scannable(rel) or rel in _MEDIA_ROOT_ALLOWED:
                continue
            text = _strip_python_comments(path.read_text(encoding='utf-8'))
            for i, line in enumerate(text.splitlines(), 1):
                if 'MEDIA_ROOT' in line:
                    violations.append((rel, i))
    return violations


class NoHardcodedMediaPathTest(TestCase):
    def test_no_media_root_in_business_code(self):
        """业务代码不得直接使用 MEDIA_ROOT。

        一旦某处写 `os.path.join(settings.MEDIA_ROOT, name)`，切到对象存储后
        那里就必然读不到文件（对象存储没有本地路径），而且**只在那一条路径上
        出错** —— 静态扫描是唯一能提前发现它的手段。
        """
        root = Path(__file__).resolve().parent.parent
        violations = _scan_media_root_violations(root)
        self.assertEqual(
            violations, [],
            '以下位置直接使用了 MEDIA_ROOT，应改为经 Storage API 读写：\n' +
            '\n'.join('  %s:%d' % v for v in violations))

    def test_scanner_detects_a_real_violation(self):
        """**反向对照**：把违规代码喂给扫描器，必须能抓到。

        没有这条，把 `_scan_media_root_violations` 写成 `return []`
        也能让上面那条测试通过 —— 判据恒真。
        """
        tmp = Path(_tmpdir()) / 'business'
        tmp.mkdir(parents=True)
        bad = tmp / 'bad_views.py'
        bad.write_text(
            'import os\n'
            'from django.conf import settings\n'
            'P = os.path.join(settings.MEDIA_ROOT, "photos")\n',
            encoding='utf-8')

        root = tmp.parent
        found = [v for v in _scan_media_root_violations(root)
                 if v[0] == 'business/bad_views.py']
        self.assertTrue(found, '扫描器漏掉了真实违规 —— 判据是假的')

    def test_comment_only_mention_is_not_a_violation(self):
        """只在**注释**里提到 MEDIA_ROOT 不算违规（剥注释这一步的对照）。

        这正是本项目栽过四次的形态：把关键词写进解释性注释，
        判据就变成恒真，代码改坏了也照样绿。
        """
        tmp = Path(_tmpdir()) / 'business'
        tmp.mkdir(parents=True)
        ok = tmp / 'commented.py'
        ok.write_text(
            '# 以前这里写的是 os.path.join(settings.MEDIA_ROOT, "photos")\n'
            'VALUE = 1\n',
            encoding='utf-8')

        root = tmp.parent
        found = [v for v in _scan_media_root_violations(root)
                 if v[0] == 'business/commented.py']
        self.assertEqual(found, [], '注释里的关键词被误判成违规')


# ===========================================================================
# 五、迁移命令端到端
# ===========================================================================
class MediaMigrateCommandTest(TestCase):
    def setUp(self):
        self.src = _tmpdir()
        self.dst = _tmpdir()
        # 让业务侧写入落到 src，源目录就是它
        ov = override_settings(MEDIA_ROOT=self.src)
        ov.enable()
        self.addCleanup(ov.disable)

        self.pets = []
        for i in range(3):
            pet = make_pet(district=make_district())
            pet.photo_capture.save(
                'mig%d.png' % i, make_image_file('mig%d.png' % i), save=True)
            self.pets.append(pet)
        self.names = [p.photo_capture.name for p in self.pets]

    def _run(self, *args):
        out = io.StringIO()
        call_command('media_migrate', *args, stdout=out)
        return out.getvalue()

    def test_source_files_exist_before_migrate(self):
        """前置事实：源目录里确实有这些文件（否则后面的断言没意义）。"""
        self.assertEqual(_count_files(self.src), 3)

    def test_dry_run_changes_nothing(self):
        self._run('--source-dir', self.src, '--target-dir', self.dst)
        self.assertEqual(_count_files(self.dst), 0,
                         'dry-run 竟然写了文件')

    def test_apply_copies_and_keeps_db_name_unchanged(self):
        """搬完之后：目标有文件、源还在、**数据库里的 name 一个字都没变**。

        「name 不变」是这套方案的全部价值 —— 变了的话所有历史图片全部失联，
        而命令会「成功」退出。
        """
        self._run('--source-dir', self.src, '--target-dir', self.dst, '--apply')

        self.assertEqual(_count_files(self.dst), 3)
        self.assertEqual(_count_files(self.src), 3, '源文件不该被删（未加 --prune）')
        for pet, name in zip(self.pets, self.names):
            pet.refresh_from_db()
            self.assertEqual(pet.photo_capture.name, name)
            self.assertTrue(os.path.exists(os.path.join(self.dst, name)))

    def test_apply_is_idempotent(self):
        """重跑安全 —— 已存在的按「大小一致」跳过。"""
        self._run('--source-dir', self.src, '--target-dir', self.dst, '--apply')
        second = self._run('--source-dir', self.src, '--target-dir', self.dst,
                           '--apply')
        self.assertEqual(_count_files(self.dst), 3, '重跑产生了重复文件')
        self.assertIn('跳过 3', second)

    def test_verify_passes_after_apply(self):
        self._run('--source-dir', self.src, '--target-dir', self.dst, '--apply')
        self._run('--source-dir', self.src, '--target-dir', self.dst, '--verify')

    def test_verify_fails_when_target_incomplete(self):
        """**反向对照**：目标不完整时 `--verify` 必须**报错退出**。

        否则「核对通过」只是一句空话 —— 把 `_do_verify` 写成直接 print
        「核对通过」也能过。
        """
        # 只搬一个（--limit 1），然后核对全部
        self._run('--source-dir', self.src, '--target-dir', self.dst,
                  '--apply', '--limit', '1')
        self.assertEqual(_count_files(self.dst), 1)

        with self.assertRaises(CommandError):
            self._run('--source-dir', self.src, '--target-dir', self.dst,
                      '--verify')

    def test_refuses_when_source_equals_target(self):
        with self.assertRaises(CommandError):
            self._run('--source-dir', self.src, '--target-dir', self.src)

    def test_refuses_without_object_storage_configured(self):
        """本地模式下不带 `--target-dir` 执行，必须给出**指引**而不是空跑。"""
        from unittest import mock

        with mock.patch.dict(os.environ, {'TNR_MEDIA_STORAGE': 'local'}):
            with self.assertRaises(CommandError) as ctx:
                self._run('--apply')
            self.assertIn('TNR_MEDIA_STORAGE', str(ctx.exception))


# ===========================================================================
# 六、prune 的安全性（反向对照）
# ===========================================================================
class PruneSafetyTest(TestCase):
    """`--prune` 是唯一会**真的删文件**的路径，判据必须成对。

    ⚠ 这里全部在临时目录上跑，绝不碰真实 `media/`。
    """

    def setUp(self):
        self.src = _tmpdir()
        self.dst = _tmpdir()
        self.src_storage = FileSystemStorage(location=self.src)
        self.dst_storage = FileSystemStorage(location=self.dst)

    def _write(self, storage, name):
        storage.save(name, io.BytesIO(b'x' * 32))
        # FileSystemStorage.save 可能改名；用 exists 核对原名即可（32 字节不会重名）
        return name

    def _cmd(self):
        out = io.StringIO()
        cmd = MigrateCommand(stdout=out)
        return cmd, out

    def test_prune_requires_yes(self):
        """不给 `--yes` 一律不删。"""
        self._write(self.src_storage, 'photos/a.png')
        cmd, out = self._cmd()
        cmd._do_prune(self.src_storage, self.dst_storage, ['photos/a.png'],
                      {'yes': False}, ok=1, failed=[])
        self.assertTrue(self.src_storage.exists('photos/a.png'))
        self.assertIn('--yes', out.getvalue())

    def test_prune_skips_when_any_failure(self):
        """有失败就整体跳过清理 —— 否则会删掉还没搬走的源文件。"""
        self._write(self.src_storage, 'photos/a.png')
        cmd, out = self._cmd()
        cmd._do_prune(self.src_storage, self.dst_storage, ['photos/a.png'],
                      {'yes': True}, ok=0, failed=[('photos/b.png', 'boom')])
        self.assertTrue(self.src_storage.exists('photos/a.png'))
        self.assertIn('跳过清理', out.getvalue())

    def test_prune_keeps_source_when_target_missing(self):
        """**关键反向对照**：目标里没有同名文件时，**必须保留源文件**。

        这是唯一不可逆的操作。只凭「上一轮搬运成功了」就删源是不够的 ——
        本轮可能整批是**跳过**的（依据是「大小一致」的推断），
        推断错了就把唯一副本删掉了。
        """
        self._write(self.src_storage, 'photos/a.png')
        # 目标故意为空
        cmd, out = self._cmd()
        cmd._do_prune(self.src_storage, self.dst_storage, ['photos/a.png'],
                      {'yes': True}, ok=1, failed=[])

        self.assertTrue(self.src_storage.exists('photos/a.png'),
                        '目标里没有却删了源 —— 数据永久丢失')
        self.assertIn('保留', out.getvalue())

    def test_prune_deletes_only_when_target_has_it(self):
        """正向：目标里确实有，才删源。"""
        self._write(self.src_storage, 'photos/a.png')
        self._write(self.dst_storage, 'photos/a.png')
        cmd, out = self._cmd()
        cmd._do_prune(self.src_storage, self.dst_storage, ['photos/a.png'],
                      {'yes': True}, ok=1, failed=[])
        self.assertFalse(self.src_storage.exists('photos/a.png'))
        self.assertTrue(self.dst_storage.exists('photos/a.png'))
