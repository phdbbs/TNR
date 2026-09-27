"""第四十四轮：政府端「物料全局监管」的三标签 + 机构筛选（源码级契约）。

界面上的三件事，每一件都容易被后续改动**静默改坏**：

1. **三标签**（库存数据 / 异常数据 / 台账）与「全局台账中心」同一套写法
   （`.tabs` / `.tab-item` + `data-*-tab` + 一个状态字段）。
   少一个标签不会报错，只是那一类数据再也看不到。
2. **机构筛选**在三个标签**都**要有。只做在台账上，库存/异常就筛不动，
   而界面上那个下拉还在 —— 典型的「控件在、功能不在」。
3. **快照标签不挂时间条件**。库存/异常是**当前值**，挂一个起止日期就是
   「能点、能填、没人说得清它在筛什么」的假控件（原先「物料库存明细」上的
   起止日期正是 `renderFilterBar` 自动追加带出来的，筛的是物料建档时间）。

⚠ 本文件用 `blank_comments()`：**只抹注释、保留字符串**。
   直接用 `strip_js` 会把中文标签一起抹掉，`assertIn('库存数据', …)` 恒假；
   完全不动则注释里提到的字段名会让断言恒真。两种都是判据空转。
"""
import re
import sys
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from jslex import blank_comments, match_brace  # noqa: E402

GOV = ROOT / 'templates' / 'portal' / 'gov' / 'portal.html'
TAB_IDS = ('stock', 'alert', 'ledger')
TAB_LABELS = ('库存数据', '异常数据', '台账')


def gov_code():
    return blank_comments(GOV.read_text(encoding='utf-8'))


def block(code, start_pat, limit=6000):
    """从 `start_pat` 匹配处截一段（够用即可，不追求精确配对）。"""
    m = re.search(start_pat, code)
    return None if not m else code[m.start():m.start() + limit]


def method_body(code, start_pat):
    """用**大括号配对**取整个方法体。

    ⚠ 别用「截 N 个字符」——`render_material` 有 6KB+，截 5000 会**截掉 catch**，
    于是「catch 里有没有呈现失败」的断言恒假，看着像功能被删了。
    （这个坑本轮实测踩过。）
    """
    m = re.search(start_pat, code)
    if not m:
        return None
    open_idx = code.index('{', m.end() - 1)
    close_idx = match_brace(code, open_idx)
    return None if close_idx < 0 else code[open_idx:close_idx + 1]


class ThreeTabsTest(SimpleTestCase):
    def setUp(self):
        self.code = gov_code()

    def test_tab_state_and_labels(self):
        blk = block(self.code, r'materialState:\s*\{[^}]*\}')
        self.assertIsNotNone(blk, '找不到 materialState —— 三标签的状态字段没了')
        self.assertRegex(blk, r"tab:\s*'stock'", '默认标签应为「库存数据」')

        tabs = block(self.code, r'materialTabs:\s*\[')
        self.assertIsNotNone(tabs, '找不到 materialTabs')
        for tid in TAB_IDS:
            with self.subTest(tab=tid):
                self.assertIn(f"id: '{tid}'", tabs)
        for label in TAB_LABELS:
            with self.subTest(label=label):
                self.assertIn(f"label: '{label}'", tabs)
        # 顺序就是用户看到的分成，别乱
        self.assertLess(tabs.index("'库存数据'"), tabs.index("'异常数据'"))
        self.assertLess(tabs.index("'异常数据'"), tabs.index("'台账'"))

    def test_renders_tabs_like_ledger_center(self):
        """与「全局台账中心」同一套：`.tabs` + `.tab-item` + `data-material-tab`。"""
        self.assertIn('data-material-tab', self.code)
        self.assertIn("class=\"tab-item ", self.code)
        self.assertIn("class=\"tabs\"", self.code)

    def test_has_content_container(self):
        """标签内容必须渲染进一个**独立容器**，否则切标签会连顶部概览卡一起重建。"""
        self.assertIn("id=\"materialContent\"", self.code)
        self.assertIn("getElementById('materialContent')", self.code)

    def test_snapshot_tabs_have_no_date_range(self):
        """库存/异常是当前快照 ⇒ 必须关掉自动追加的起止日期。"""
        blk = method_body(self.code, r'renderMaterialTab\(\)\s*\{')
        self.assertIsNotNone(blk, '找不到 renderMaterialTab')
        self.assertIn('noDateRange: true', blk,
                      '库存/异常标签又挂上了时间条件 —— 那是个「设了没反应」的假控件')
        self.assertIn("'start_date'", blk, '台账标签丢了开始日期')
        self.assertIn("'end_date'", blk, '台账标签丢了结束日期')
        # 两个快照标签共用一个 else 分支，ledger 单独一个分支
        self.assertIn("tab === 'ledger'", blk)


class InstitutionFilterTest(SimpleTestCase):
    def setUp(self):
        self.code = gov_code()

    def test_three_tabs_share_the_institution_field(self):
        """`instField` 必须被两个分支共用 —— 只写一处就是「有的标签筛不动」。"""
        blk = method_body(self.code, r'renderMaterialTab\(\)\s*\{')
        self.assertIsNotNone(blk)
        self.assertIn("institution_id", blk)
        self.assertGreaterEqual(
            blk.count('instField'), 2,
            '机构筛选只出现在一个分支里 —— 另一个标签的下拉会消失或不起作用')

    def test_dropdown_covers_shelter_and_hospital(self):
        """需求明确：机构包含**捕捉点和医院**。"""
        self.assertRegex(
            self.code,
            r"i\.type === 'shelter' \|\| i\.type === 'hospital'",
            '机构下拉的取值条件变了 —— 捕捉点或医院会从下拉里消失')

    def test_hospital_view_reads_hospital_stocks(self):
        """选医院时库存取 `hospital_stocks[id]`（后端按 get_hospital_stock 算好的）。"""
        blk = method_body(self.code, r'materialView\(instId\)\s*\{')
        self.assertIsNotNone(blk, '找不到 materialView')
        self.assertIn('hospital_stocks', blk)
        self.assertIn('isHospital', blk)
        self.assertRegex(blk, r'\|\|\s*0', '该院没有这个物料的流水时应回落 0')

    def test_stock_column_title_follows_view(self):
        blk = method_body(self.code, r'renderMaterialRows\(bar\)\s*\{')
        self.assertIsNotNone(blk)
        self.assertIn("'该院库存'", blk)
        self.assertIn("'捕捉点库存'", blk)
        self.assertIn('view.isHospital', blk)

    def test_ledger_rows_carry_institution_name(self):
        """台账行的「机构」列：医院流水用 `hospital_name`，捕捉点侧用区县捕捉点名。"""
        blk = method_body(self.code, r'renderMaterialRows\(bar\)\s*\{')
        self.assertIn('hospital_name', blk)
        self.assertIn('shelterByDistrict', blk)
        self.assertIn("title: '机构'", blk)

    def test_empty_state_names_the_institution(self):
        """选了机构却 0 条时，空态要说明是哪家机构 —— 否则像「系统没数据」。"""
        blk = method_body(self.code, r'renderMaterialRows\(bar\)\s*\{')
        self.assertRegex(blk, r'view\.name[\s\S]{0,80}暂无低于安全库存的物料',
                         '异常标签的空态没有写明机构名')


class StrictReadTest(SimpleTestCase):
    """本页的两次取数都必须是**严格读** —— 失败要看得见。

    `getMaterialSupervision()` / `getInstitutions()` 是静默读（非 2xx 返回 `[]`），
    用它们的话服务端一报错，页面就渲染成「暂无物料数据」——
    看起来像「暂时没有数据」，其实是接口挂了。
    """

    def test_uses_strict_readers(self):
        code = gov_code()
        blk = method_body(code, r'async render_material\(\)\s*\{')
        self.assertIsNotNone(blk)
        self.assertIn('getMaterialSupervisionStrict', blk)
        self.assertIn('getInstitutionsStrict', blk)
        self.assertNotIn('getMaterialSupervision()', blk)
        self.assertNotIn('await TNR_API.getInstitutions()', blk)

    def test_failure_is_rendered(self):
        code = gov_code()
        blk = method_body(code, r'async render_material\(\)\s*\{')
        self.assertIsNotNone(blk)
        self.assertIn('catch', blk, 'render_material 没有 try/catch —— 失败会白屏')
        self.assertIn('加载失败', blk, 'catch 里没有把失败呈现给用户（死 catch）')
