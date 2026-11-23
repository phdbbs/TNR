"""批量导出：把一宠一档 lifecycle 数据渲染成**真正的 PDF 文件**。

⚠ 为什么不复用 `templates/print/pet_archive.html`：那是给浏览器打印用的
  A4 HTML，项目没有 HTML→PDF 引擎（见 `views_print` 顶部注释）。批量下载
  不能弹一百次打印对话框，必须服务端产出文件。这里用 reportlab 直接绘制，
  **取数与权限仍走 `build_pet_lifecycle()`** —— 与 JSON 接口和单张打印页
  同源，内容不会漂移；只有版式是独立的一套。

中文字体用 reportlab 自带的 Adobe CID 字体 `STSong-Light`，不需要在镜像里
装字体文件或系统库（Docker 只加一个纯 Python wheel）。
"""
import io
import math
from datetime import datetime
from urllib.parse import urlparse

from django.conf import settings
from django.core.files.storage import default_storage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from business.services_lifecycle import build_pet_lifecycle, event_print_rows

#: 一次批量导出最多多少只。前端必须与这个常量同口径（判据会核对）。
MAX_BATCH_PET_ARCHIVE = 100

PDF_FONT = 'STSong-Light'
pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT))

PAGE_W, PAGE_H = A4
MARGIN = 14 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
LABEL_W = 26 * mm

_INK = colors.HexColor('#1a1a1a')
_MUTED = colors.HexColor('#666666')
_ACCENT = colors.HexColor('#9D2933')
_LIGHT = colors.HexColor('#F7F3EC')
_LINE = colors.HexColor('#AAAAAA')


def _wrap(text, width, size=9):
    """按实际字宽折行（reportlab 的 drawString 不会自动换行）。"""
    cur, lines = '', []
    for ch in str(text if text is not None else ''):
        if ch == '\n':
            lines.append(cur)
            cur = ''
            continue
        trial = cur + ch
        if cur and pdfmetrics.stringWidth(trial, PDF_FONT, size) > width:
            lines.append(cur)
            cur = ch
        else:
            cur = trial
    if cur or not lines:
        lines.append(cur)
    return lines


def _image_reader(url):
    """档案照片 url → `(ImageReader, 文件句柄)`；读不到返回 None。

    ⚠ 必须走 `default_storage`（Storage API），不能直接拼本地路径：业务代码
    禁止引用媒体根路径 —— 切到对象存储后那些路径根本不存在，而本地残留
    会掩盖「迁移没搬干净」。一只照片损坏只画占位框，**不让整批导出失败**。
    """
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path if parsed.scheme else url
    prefix = settings.MEDIA_URL or '/media/'
    rel = path[len(prefix):] if path.startswith(prefix) else path.lstrip('/')
    try:
        handle = default_storage.open(rel, 'rb')
        return ImageReader(handle), handle
    except (OSError, ValueError, NotImplementedError):
        return None


class _Page:
    """极简排版器：维护 y 游标，空间不足自动翻页并补页眉页脚。"""

    def __init__(self, pdf, pet_code, stage, printed_by):
        self.pdf = pdf
        self.pet_code = pet_code
        self.stage = stage
        self.printed_by = printed_by
        self.y = PAGE_H - MARGIN
        self.page_no = 0
        self._new_page()

    def _footer_current(self):
        """页脚必须**在翻页前**画到当前页。

        不能循环 `setPageNumber()` 再 drawString —— 那只是改内部变量，
        所有文字都落在最后一页，前面的页脚全是空白。
        """
        self.pdf.setFont(PDF_FONT, 8)
        self.pdf.setFillColor(_MUTED)
        self.pdf.drawString(MARGIN, 10 * mm,
                            f'打印人：{self.printed_by}    档案编号：{self.pet_code}')
        self.pdf.drawRightString(PAGE_W - MARGIN, 10 * mm, f'第 {self.page_no} 页')
        self.pdf.setFillColor(_INK)

    def _new_page(self):
        if self.page_no:
            self._footer_current()
            self.pdf.showPage()
        self.page_no += 1
        self.y = PAGE_H - MARGIN
        self.pdf.setFont(PDF_FONT, 8)
        self.pdf.setFillColor(_MUTED)
        self.pdf.drawString(MARGIN, PAGE_H - MARGIN + 4 * mm,
                            '襄阳市流浪动物 TNR 管理平台 · 一宠一档档案')
        self.pdf.setFillColor(_INK)

    def need(self, height):
        if self.y - height < MARGIN + 12 * mm:
            self._new_page()

    def text(self, value, x, size=9, color=_INK):
        self.pdf.setFont(PDF_FONT, size)
        self.pdf.setFillColor(color)
        self.pdf.drawString(x, self.y, str(value if value is not None else ''))
        self.pdf.setFillColor(_INK)

    def section(self, title):
        self.need(14 * mm)
        self.y -= 5 * mm
        self.pdf.setStrokeColor(_ACCENT)
        self.pdf.setLineWidth(2)
        self.pdf.line(MARGIN, self.y, MARGIN + 12 * mm, self.y)
        self.text(title, MARGIN + 15 * mm, 11)
        self.y -= 5 * mm

    def kv(self, label, value):
        lines = _wrap(value, CONTENT_W - LABEL_W - 3 * mm)
        self.need(len(lines) * 12 + 3)
        top = self.y
        self.text(label, MARGIN, 9, _MUTED)
        self.pdf.setFont(PDF_FONT, 9)
        for i, line in enumerate(lines):
            self.pdf.drawString(MARGIN + LABEL_W, top - i * 12, line)
        self.y = top - max(len(lines), 1) * 12 - 3

    def photos(self, photos, thumb=26 * mm):
        if not photos:
            return
        gap = 3 * mm
        cols = max(1, int((CONTENT_W + gap) // (thumb + gap)))
        rows = math.ceil(len(photos) / cols)
        self.need(rows * (thumb + 5 * mm) + 4 * mm)
        top = self.y
        for idx, p in enumerate(photos[:MAX_BATCH_PET_ARCHIVE]):
            row, col = divmod(idx, cols)
            x = MARGIN + col * (thumb + gap)
            y = top - row * (thumb + 5 * mm) - thumb
            pair = _image_reader(p.get('url'))
            if pair:
                img, handle = pair
                try:
                    iw, ih = img.getSize()
                    scale = min(thumb / iw, thumb / ih)
                    w, h = iw * scale, ih * scale
                    self.pdf.drawImage(img, x + (thumb - w) / 2, y + (thumb - h) / 2,
                                       width=w, height=h)
                except Exception:
                    self.pdf.rect(x, y, thumb, thumb, stroke=1, fill=0)
                finally:
                    handle.close()
            else:
                self.pdf.rect(x, y, thumb, thumb, stroke=1, fill=0)
            self.pdf.setFont(PDF_FONT, 8)
            self.pdf.setFillColor(_MUTED)
            label = str(p.get('label') or '')
            label = ''.join(_wrap(label, thumb)[:2])
            self.pdf.drawString(x, y - 4 * mm, label)
            self.pdf.setFillColor(_INK)
        self.y = top - rows * (thumb + 5 * mm) - 2 * mm

    def footer(self):
        self._footer_current()


def render_pet_archive_pdf(pet, user, printed_by=''):
    """渲染一只动物的完整档案，返回 PDF 的 bytes。

    数据只从 `build_pet_lifecycle` 取；调用方必须已经用 `pet_for_lifecycle`
    做过权限收敛（与单张打印页同一条规则）。
    """
    data = build_pet_lifecycle(pet, user)
    brief = data['pet_brief']
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"一宠一档档案 {brief['code']}")
    pdf.setAuthor('襄阳市流浪动物 TNR 管理平台')

    page = _Page(pdf, brief['code'], brief['stage_display'],
                 printed_by or getattr(user, 'username', '') or '—')

    # 抬头
    page.text('一宠一档档案', MARGIN, 18)
    page.y -= 8 * mm
    page.text(f"档案编号：{brief['code']}    当前状态：{brief['stage_display']}", MARGIN, 10)
    page.y -= 6 * mm

    page.section('基本信息')
    for label, value in (
        ('昵称', brief.get('name')), ('类别', brief.get('species')),
        ('品种', brief.get('breed')), ('性别', brief.get('gender')),
        ('年龄', brief.get('age')), ('毛色', brief.get('color')),
        ('体重', brief.get('weight')), ('芯片号', brief.get('chip_no')),
        ('所属区县', brief.get('district_name')),
        ('捕捉点', brief.get('shelter_name')),
        ('所在医院', brief.get('hospital_name')),
    ):
        if value:
            page.kv(label, value)

    if data.get('photos'):
        page.section('动物照片')
        page.photos(data['photos'])

    page.section('流转全过程（捕捉 / 转运 / 诊疗 / 手术 / 处方 / 出院 / 领养后打卡）')
    for ev in data['events']:
        page.need(18 * mm)
        page.y -= 2 * mm
        page.pdf.setStrokeColor(_LINE)
        page.pdf.setLineWidth(0.5)
        page.pdf.line(MARGIN, page.y, PAGE_W - MARGIN, page.y)
        page.y -= 5 * mm
        page.text(f"{ev.get('date') or '—'}  {ev.get('type_display') or ev.get('type')}",
                  MARGIN, 10, _ACCENT)
        page.y -= 5 * mm
        for label, value in event_print_rows(ev):
            if value not in (None, '', [], {}):
                page.kv(label, value)
        if ev.get('photos'):
            page.photos(ev['photos'], thumb=20 * mm)
        if ev.get('signature', '').startswith('data:image'):
            page.kv('电子签名', '（见纸质原件）')
        page.y -= 2 * mm

    page.footer()
    pdf.save()
    return buf.getvalue()


def zip_filename(prefix='一宠一档档案批量导出'):
    """带时间戳的 ZIP 文件名（ASCII 安全，中文走 HTTP 头编码）。"""
    return f'{prefix}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
