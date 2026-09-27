"""第四十四轮：列表「冻结的筛选条 / 表头」不得漂移（源码级契约）。

## 漂移的根因（**实测**出来的，不是推理）

`position: sticky` 元素的**包含块是最近的块级祖先**。所以这种写法：

    '<div id="xxxFilterBar">' + TNR_UI.renderFilterBar(...) + '</div>'

里面那一层 div 与筛选条**一样高** —— `room = parent.height - bar.height = 0`，
筛选条**一点可移动空间都没有**，`position: sticky` 静默失效、跟着内容滚走。
实测：滚到底后 `bar.top` 从 382.97 变成 **-401.03**（正常应钉在 102）。
全站共 **18 处**这么写。

第二处：`body .page-view .filter-bar { top: 46px }` 是为「钉在标签栏下沿」
写的，但**没有标签栏的页面也被套上了** —— 筛选条凭空下移 46px，
上方漏出 46px 的滚动内容（8 个页面受影响）。

第三处：`.std-page-head`（数据总览大屏的页头）是 flex 行，
里面的 `.filter-bar` 作为 flex 子项 sticky 时包含块只有 10px 高。

本文件把三条修法钉住：
  1. 同高包裹层反模式必须 **0 处**（带正/反向对照，判据不空转）；
  2. `renderFilterBar` 必须把 id 写在 `.filter-bar` **自身**上；
  3. CSS 必须有无标签栏归零 + 页头整行冻结两条规则；
  4. `_pinnedHeight()` 必须把 `.std-page-head` 计入被冻结高度。

⚠ 判据建立在 `scripts/jslex.py` 上；那个词法器失真会让这里**静默失真**
（`ScannerSelfCheckTest` 就是为此而写）。
"""
import re
import sys
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from jslex import blank_comments, stripped_kinds  # noqa: E402

PORTALS = ('platform', 'gov', 'shelter', 'hospital', 'adopter')
TEMPLATES = [ROOT / 'templates' / 'portal' / n / 'portal.html' for n in PORTALS]
COMMON_JS = ROOT / 'static' / 'js' / 'tnr-common.js'
CSS = ROOT / 'static' / 'css' / 'tnr-traditional.css'

# 同高包裹层的唯一写法：字符串字面量 + `+ TNR_UI.renderFilterBar(`
WRAPPED = re.compile(r"'<div id=\"(\w+)\">'(\s*\+\s*)TNR_UI\.renderFilterBar\(")

CSS_COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)


def read(p):
    return p.read_text(encoding='utf-8')


def strip_css_comments(src):
    """CSS 注释也提到这些选择器名，不剥掉断言就会**恒真**。

    实测：`tnr-traditional.css` 第 1739 行注释里就写着
    「用 `:not(:has(.tabs))` 把这类页面的偏移归零」—— 不剥注释的话，
    把真正的规则删掉、只留注释，测试照样绿。
    """
    return CSS_COMMENT.sub(lambda m: '\n' * m.group(0).count('\n'), src)


def find_wrapped(src):
    """返回被同高 div 包裹的筛选条 id 列表（跳过注释里的同款写法）。"""
    _, kinds = stripped_kinds(src)
    return [m.group(1) for m in WRAPPED.finditer(src)
            if kinds[m.start()] != 'comment']


class ScannerSelfCheckTest(SimpleTestCase):
    """判据自身的正/反向对照 —— 否则「0 处」可能只是正则写错了。"""

    def test_detects_planted_pattern(self):
        planted = ("html += '<div id=\"xxFilterBar\">'\n"
                   "  + TNR_UI.renderFilterBar([{ key: 'a' }], '')\n"
                   "  + '</div>';\n")
        self.assertEqual(find_wrapped(planted), ['xxFilterBar'])

    def test_ignores_the_pattern_inside_a_comment(self):
        """注释里提到这种写法（本项目注释里确实提了）不算命中。"""
        src = ("// 千万别写成 '<div id=\"xxFilterBar\">' + TNR_UI.renderFilterBar(...)\n"
               "const a = 1;\n")
        self.assertEqual(find_wrapped(src), [])

    def test_accepts_the_new_form(self):
        src = "TNR_UI.renderFilterBar(filters, '', { id: 'xxFilterBar' })\n"
        self.assertEqual(find_wrapped(src), [])

    def test_css_comment_stripper_is_not_vacuous(self):
        css = '/* 用 :not(:has(.tabs)) 归零 */\n.real { color: red; }\n'
        out = strip_css_comments(css)
        self.assertNotIn(':has(.tabs)', out)
        self.assertIn('.real', out)


class NoSameHeightWrapperTest(SimpleTestCase):
    def test_zero_wrapped_filterbars(self):
        bad = {}
        for p in TEMPLATES:
            hits = find_wrapped(read(p))
            if hits:
                bad[p.parent.name] = hits
        self.assertEqual(bad, {}, f'这些筛选条又被同高 div 包裹了（冻结会失效）：{bad}')

    def test_render_filter_bar_puts_id_on_the_bar_itself(self):
        """id 必须写在 `.filter-bar` 元素上，而不是套在外面一层 div 上。"""
        code = blank_comments(read(COMMON_JS))
        self.assertRegex(
            code, r'<div class="filter-bar"\$\{opts\.id',
            'renderFilterBar 没有把 opts.id 写到 .filter-bar 自身上 —— '
            '一旦退回「外面套一层同高 div」，冻结会静默失效')


class StickyCssTest(SimpleTestCase):
    def setUp(self):
        self.css = strip_css_comments(read(CSS))

    def test_tabless_pages_zero_offset(self):
        """没有标签栏的页面，筛选条必须归零，否则上方漏 46px 滚动内容。"""
        self.assertIn('body .page-view:not(:has(.tabs)) .filter-bar', self.css)
        m = re.search(
            r'body \.page-view:not\(:has\(\.tabs\)\) \.filter-bar\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(m, '规则存在但抓不到规则体 —— 断言写法要跟着改')
        self.assertRegex(m.group(1), r'top:\s*0')

    def test_tabs_pinned_at_top_zero(self):
        m = re.search(r'body \.page-view \.tabs\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(m)
        self.assertRegex(m.group(1), r'position:\s*sticky')
        self.assertRegex(m.group(1), r'top:\s*0')

    def test_std_page_head_is_the_sticky_unit(self):
        """页头整行冻结，且行内的筛选条**不能**再自己 sticky（会互相压）。"""
        m = re.search(r'body \.page-view \.std-page-head\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(m, '缺少 .std-page-head 的冻结规则')
        self.assertRegex(m.group(1), r'position:\s*sticky')
        self.assertRegex(m.group(1), r'top:\s*0')

        m2 = re.search(r'body \.page-view \.std-page-head \.filter-bar\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(m2, '缺少「页头里的筛选条不再单独 sticky」的规则')
        self.assertRegex(m2.group(1), r'position:\s*static')


class PinnedHeightTest(SimpleTestCase):
    def test_pinned_height_counts_std_page_head(self):
        """`_pinnedHeight()` 要按「被冻结元素」算列表上方预留高度。

        漏掉 `.std-page-head` 的话，数据总览大屏会把列表算高、
        滚到底时表头正好被冻结的页头压住。
        """
        code = blank_comments(read(COMMON_JS))
        m = re.search(r'_pinnedHeight\s*\(root\)\s*\{', code)
        self.assertIsNotNone(m, '找不到 _pinnedHeight —— 断言要跟着改名')
        body = code[m.end():m.end() + 400]
        self.assertIn('.std-page-head', body)
        self.assertIn('.tabs', body)
        self.assertIn('.filter-bar', body)
