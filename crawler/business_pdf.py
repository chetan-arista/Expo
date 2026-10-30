"""
Business Insights PDF: a compact, scannable company profile (2-4 pages) built from a BusinessReport dict
(crawler/ai/business.py), in the look of the Website Audit Report (crawler/pdf_report.py): same colours,
fonts, running header and "Page X of Y" footer.

Page 1: company card, offerings and audience chips, market tiles, competitors.
Then contact & socials, tech stack tiles, the SEO snapshot with the health ring, opportunities and risks,
and the sources behind the claims (SITE = the company's own pages, WEB = Wikipedia/Wikidata,
AI = the model's general knowledge, unverified) as a compact two-column list: facts read from the home
page are counted in one line (page 1 shows them), every other source is listed.
"""
import re
import time
from typing import List, Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape

from reportlab import rl_config
from reportlab.graphics.shapes import Circle, Drawing, String, Wedge
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Flowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BRAND_NAME = "ARACHNIX"
try:   # one palette and font set for every PDF the tool makes
    from .pdf_report import CAT_COLORS, CAT_TINTS, GOOD, GOOD_TINT, INK, LINE, LINK, MUTED, PAPER, WHITE, _fonts
except Exception:  # pragma: no cover - keeps this report working while the audit module is being changed
    INK, MUTED, LINE, PAPER = (colors.HexColor(c) for c in ("#181C1A", "#67716A", "#E3DED3", "#F7F5EF"))
    WHITE, GOOD, GOOD_TINT, LINK = colors.white, colors.HexColor("#244B30"), colors.HexColor("#E7F2EA"), \
        colors.HexColor("#1F5A96")
    CAT_COLORS = {"2xx": colors.HexColor("#2F6B3F"), "3xx": colors.HexColor("#B8741F"),
                  "4xx": colors.HexColor("#9C3345")}
    CAT_TINTS = {"2xx": colors.HexColor("#E7F2EA"), "3xx": colors.HexColor("#FBF1E1"),
                 "4xx": colors.HexColor("#FBE9EC")}

    def _fonts():
        return ("Helvetica", "Helvetica-Bold")
try:   # the ARACHNIX mark in the page header (or the white label of Settings > Branding, P23)
    from .pdf_report import current_brand, draw_brand, draw_header_brand
except Exception:  # pragma: no cover
    draw_brand = draw_header_brand = None
    current_brand = lambda reports_dir=None: None  # noqa: E731

rl_config.useA85 = 0   # binary streams: smaller and faster than ASCII85

AMBER, AMBER_TINT = CAT_COLORS["3xx"], CAT_TINTS["3xx"]
RED, RED_TINT = CAT_COLORS["4xx"], CAT_TINTS["4xx"]
LINK_TINT = colors.HexColor("#E6EEF7")
GOOD_LINE = colors.HexColor("#C8DECE")
HEAD_BG = colors.HexColor("#EFECE4")
FROM_LABEL = {"site": ("SITE", GOOD), "web": ("WEB", LINK), "ai": ("AI · UNVERIFIED", AMBER)}
MAX_SOURCES = 30


def _hex(c) -> str:
    return c.hexval().replace("0x", "#")


def _fit(text: str, font: str, size: float, width: float) -> str:
    """Cut `text` with an ellipsis so it fits `width` points."""
    text = str(text or "")
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    while text and pdfmetrics.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def _esc(text) -> str:
    return escape(str(text or ""))


def _link(url: str, text: Optional[str] = None, limit: int = 70) -> str:
    shown = text if text is not None else url.replace("https://", "").replace("http://", "").rstrip("/")
    if len(shown) > limit:
        shown = shown[:limit - 1] + "…"
    if not str(url).startswith(("http://", "https://")):
        return _esc(shown)
    return f'<link href="{escape(url, {chr(34): "&quot;"})}"><font color="{_hex(LINK)}">{_esc(shown)}</font></link>'


def _styles():
    regular, bold = _fonts()
    return {
        "title": ParagraphStyle("title", fontName=bold, fontSize=24, leading=28, textColor=INK),
        "meta": ParagraphStyle("meta", fontName=regular, fontSize=8.6, leading=12, textColor=MUTED),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=12.5, leading=16, textColor=INK, spaceBefore=2,
                             spaceAfter=5),
        "name": ParagraphStyle("name", fontName=bold, fontSize=18, leading=21, textColor=INK),
        "tagline": ParagraphStyle("tagline", fontName=bold, fontSize=10, leading=13, textColor=GOOD),
        "body": ParagraphStyle("body", fontName=regular, fontSize=8.6, leading=11.6, textColor=INK),
        "bold": ParagraphStyle("bold", fontName=bold, fontSize=8.6, leading=11.6, textColor=INK),
        "small": ParagraphStyle("small", fontName=regular, fontSize=7.4, leading=9.8, textColor=MUTED),
        "label": ParagraphStyle("label", fontName=bold, fontSize=6.4, leading=8.4, textColor=MUTED),
        "value": ParagraphStyle("value", fontName=bold, fontSize=8.6, leading=11, textColor=INK, wordWrap="CJK"),
        "cell": ParagraphStyle("cell", fontName=regular, fontSize=7.6, leading=9.8, textColor=INK, wordWrap="CJK"),
        "cellmuted": ParagraphStyle("cellmuted", fontName=regular, fontSize=7.2, leading=9.4, textColor=MUTED,
                                    wordWrap="CJK"),
        "th": ParagraphStyle("th", fontName=bold, fontSize=6.6, leading=8.5, textColor=MUTED),
        "listhead": ParagraphStyle("listhead", fontName=bold, fontSize=7.4, leading=10, textColor=INK),
        "item": ParagraphStyle("item", fontName=regular, fontSize=8.2, leading=11, textColor=INK, leftIndent=9,
                               firstLineIndent=-9, spaceAfter=2.5),
        "center": ParagraphStyle("center", fontName=regular, fontSize=7.4, leading=9.8, textColor=MUTED,
                                 alignment=TA_CENTER),
    }


def _unlabel(text) -> str:
    """Drop the inline "(from AI knowledge - unverified)" where the section badge already says it."""
    return re.sub(r"\s*\(?from AI knowledge - unverified\)?\.?", "", str(text or ""), flags=re.I).strip()


def _missing(text: str = "Not found on the site") -> str:
    return f"<font color='{_hex(MUTED)}'>{_esc(text)}</font>"


# --------------------------------------------------------------------------------------------
# Flowables
# --------------------------------------------------------------------------------------------
class Chips(Flowable):
    """Rounded tags that wrap onto as many rows as they need (an optional link per tag)."""

    def __init__(self, items: List[str], fill=PAPER, stroke=LINE, text=INK, links: Optional[List[str]] = None,
                 empty: str = "", size: float = 7.8, max_chip: float = 0):
        super().__init__()
        self.items = [str(i).strip() for i in items if str(i or "").strip()]
        self.links = list(links or [])
        self.fill, self.stroke, self.text = fill, stroke, text
        self.empty = empty
        self.size = size
        self.max_chip = max_chip
        self.chip_h = size + 8
        self.gap = 4
        self.pad = 6
        self._layout = []

    def wrap(self, aw, ah):
        regular, _ = _fonts()
        self._layout = []
        x, row = 0.0, 0
        limit = min(aw, self.max_chip or aw)
        for i, t in enumerate(self.items):
            w = min(pdfmetrics.stringWidth(t, regular, self.size) + 2 * self.pad, limit)
            if x > 0 and x + w > aw:
                row, x = row + 1, 0.0
            self._layout.append((x, row, w, t, self.links[i] if i < len(self.links) else None))
            x += w + self.gap
        rows = row + 1 if self.items else (1 if self.empty else 0)
        self.width = aw
        self.height = rows * self.chip_h + max(0, rows - 1) * self.gap
        return aw, self.height

    def draw(self):
        regular, _ = _fonts()
        c = self.canv
        if not self.items:
            if self.empty:
                c.setFont(regular, self.size)
                c.setFillColor(MUTED)
                c.drawString(0, self.chip_h / 2 - self.size * 0.35, self.empty)
            return
        for x, row, w, t, link in self._layout:
            y = self.height - (row + 1) * self.chip_h - row * self.gap
            c.setFillColor(self.fill)
            c.setStrokeColor(self.stroke)
            c.setLineWidth(0.6)
            c.roundRect(x, y, w, self.chip_h, self.chip_h / 2, stroke=1, fill=1)
            c.setFillColor(self.text)
            c.setFont(regular, self.size)
            c.drawString(x + self.pad, y + self.chip_h / 2 - self.size * 0.35,
                         _fit(t, regular, self.size, w - 2 * self.pad + 1))
            if link and str(link).startswith(("http://", "https://")):
                c.linkURL(link, (x, y, x + w, y + self.chip_h), relative=1)


def _score(v) -> Optional[int]:
    """The health score as a number, None when there is none (a report saved before 2.0 holds '' there)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _ring(score: Optional[int], size: float = 31 * mm) -> Drawing:
    """Health score donut (grey with "Not checked" when there was no crawl)."""
    score = _score(score)
    regular, bold = _fonts()
    d = Drawing(size, size)
    cx = cy = size / 2
    r = size / 2 - 1
    thick = size * 0.12
    d.add(Circle(cx, cy, r - thick / 2, fillColor=None, strokeColor=LINE, strokeWidth=thick))
    if score is None:
        d.add(String(cx, cy - size * 0.02, "–", fontName=bold, fontSize=size * 0.22, fillColor=MUTED,
                     textAnchor="middle"))
        d.add(String(cx, cy - size * 0.18, "NOT CHECKED", fontName=bold, fontSize=size * 0.07, fillColor=MUTED,
                     textAnchor="middle"))
        return d
    score = max(0, min(100, int(score)))
    color = CAT_COLORS["2xx"] if score >= 90 else AMBER if score >= 70 else RED
    sweep = 360.0 * score / 100
    if sweep >= 359.5:
        d.add(Circle(cx, cy, r - thick / 2, fillColor=None, strokeColor=color, strokeWidth=thick))
    elif sweep > 0:
        d.add(Wedge(cx, cy, r, 90 - sweep, 90, radius1=r - thick, annular=True, fillColor=color, strokeColor=None))
    d.add(String(cx, cy - size * 0.04, f"{score}%", fontName=bold, fontSize=size * 0.21, fillColor=color,
                 textAnchor="middle"))
    d.add(String(cx, cy - size * 0.19, "HEALTH", fontName=bold, fontSize=size * 0.07, fillColor=MUTED,
                 textAnchor="middle"))
    return d


# --------------------------------------------------------------------------------------------
# Page frame
# --------------------------------------------------------------------------------------------
def _canvas_maker(domain: str, brand: Optional[dict] = None):
    regular, _ = _fonts()

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._chrome(total)
                super().showPage()
            super().save()

        def _chrome(self, total: int):
            w, h = A4
            self.saveState()
            self.setFont(regular, 7)
            self.setFillColor(MUTED)
            self.setStrokeColor(LINE)
            self.setLineWidth(0.5)
            # running header on every page: the ARACHNIX mark + name (page 1 carries the title below it)
            label = "" if self._pageNumber == 1 else "Business Insights"
            if draw_brand is not None:
                if brand and draw_header_brand is not None:      # P23 white label
                    draw_header_brand(self, 15 * mm, h - 10.6 * mm, 3.4 * mm, brand, label, font_size=7.5)
                else:
                    draw_brand(self, 15 * mm, h - 10.6 * mm, 3.4 * mm, label, font_size=7.5)
                self.setFont(regular, 7)
                self.setFillColor(MUTED)
            else:
                self.drawString(15 * mm, h - 10 * mm, f"{BRAND_NAME}  {label}".strip())
            self.drawRightString(w - 15 * mm, h - 10 * mm, domain)
            self.line(15 * mm, h - 11.5 * mm, w - 15 * mm, h - 11.5 * mm)
            self.line(15 * mm, 12 * mm, w - 15 * mm, 12 * mm)
            self.drawString(15 * mm, 8 * mm, str((brand or {}).get("footer") or (brand or {}).get("name")
                                                 or BRAND_NAME)[:120])
            self.drawRightString(w - 15 * mm, 8 * mm, f"Page {self._pageNumber} of {total}")
            self.restoreState()

    return NumberedCanvas


def _box(flowables, width, fill=PAPER, accent=None, pad=8):
    t = Table([[flowables]], colWidths=[width])
    style = [("BACKGROUND", (0, 0), (-1, -1), fill), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
             ("LEFTPADDING", (0, 0), (-1, -1), pad + (2 if accent else 0)), ("RIGHTPADDING", (0, 0), (-1, -1), pad),
             ("TOPPADDING", (0, 0), (-1, -1), pad - 1), ("BOTTOMPADDING", (0, 0), (-1, -1), pad - 1),
             ("VALIGN", (0, 0), (-1, -1), "TOP")]
    if accent is not None:
        style.append(("LINEBEFORE", (0, 0), (0, -1), 3, accent))
    t.setStyle(TableStyle(style))
    return t


def _tiles(cells: List[tuple], cols: int, width: float, s: dict, min_h: float = 0) -> Table:
    """Grid of (LABEL, value_html[, style name]) tiles."""
    rows, row = [], []
    for cell in cells:
        label, value = cell[0], cell[1]
        style = s[cell[2]] if len(cell) > 2 else s["value"]
        row.append([Paragraph(_esc(label), s["label"]), Spacer(1, 1.5), Paragraph(value, style)])
        if len(row) == cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row + [""] * (cols - len(row)))
    t = Table(rows, colWidths=[width / cols] * cols, rowHeights=[min_h] * len(rows) if min_h else None)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), PAPER),
                           ("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE),
                           ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def _heading(text: str, s: dict, badge: str = "", badge_color=AMBER) -> Paragraph:
    extra = (f"&nbsp;&nbsp;<font size='6.6' color='{_hex(badge_color)}'>{_esc(badge)}</font>" if badge else "")
    return Paragraph(_esc(text) + extra, s["h2"])


# --------------------------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------------------------
def _company_card(rep: dict, page_w: float, s: dict):
    c = rep.get("company") or {}
    left = [Paragraph(_esc(c.get("name") or urlparse(rep.get("url", "")).netloc or "Unknown company"), s["name"])]
    if c.get("legal_name"):
        left.append(Paragraph(_esc(c["legal_name"]), s["small"]))
    if c.get("tagline"):
        left += [Spacer(1, 4), Paragraph(_esc(c["tagline"]), s["tagline"])]
    left += [Spacer(1, 4), Paragraph(_esc(c.get("what_they_do")) or _missing("No description found on the site"),
                                     s["body"])]
    facts = [("FOUNDED", c.get("founded")), ("HEADQUARTERS", c.get("headquarters")), ("SIZE", c.get("size")),
             ("INDUSTRY", c.get("industry")), ("WEBSITE", "")]
    right_w = page_w * 0.38
    rows = []
    for label, value in facts:
        if label == "WEBSITE":
            html = _link(rep.get("url", ""), limit=40)
        else:
            html = _esc(value) if value else _missing()
        rows.append([Paragraph(label, s["label"]), Paragraph(html, s["cell"])])
    kv = Table(rows, colWidths=[24 * mm, right_w - 24 * mm - 10])
    kv.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE)]))
    card = Table([[left, kv]], colWidths=[page_w - right_w, right_w])
    card.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), PAPER),
                              ("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LINEBEFORE", (0, 0), (0, -1), 3, INK),
                              ("LINEBEFORE", (1, 0), (1, -1), 0.6, LINE),
                              ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                              ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    return card


def _offer_audience(rep: dict, page_w: float, s: dict) -> List:
    off = Chips(rep.get("offerings") or [], fill=GOOD_TINT, stroke=GOOD_LINE, text=GOOD,
                empty="No offerings found on the site", max_chip=page_w * 0.48)
    aud = Chips(rep.get("audience") or [], fill=LINK_TINT, stroke=colors.HexColor("#C9D8EA"), text=LINK,
                empty="No audience named on the site", max_chip=page_w * 0.48)
    return [KeepTogether([_heading("What they offer", s), off]), Spacer(1, 5 * mm),
            KeepTogether([_heading("Who they serve", s), aud])]


def _market(rep: dict, page_w: float, s: dict) -> List:
    m = rep.get("market") or {}
    unverified = "market" in (rep.get("ai_unverified") or [])
    need_ai = "Needs AI (AI off)" if not rep.get("ai_used") else "Not found"
    clean = _unlabel if unverified else (lambda t: t)
    tiles = _tiles([("SEGMENT", _esc(clean(m.get("segment"))) or _missing("Not found")),
                    ("POSITIONING", _esc(clean(m.get("positioning"))) or _missing(need_ai)),
                    ("GEOGRAPHY", _esc(clean(m.get("geography"))) or _missing("Not found"))], 3, page_w, s)
    out = [_heading("Market", s, "AI KNOWLEDGE · UNVERIFIED" if unverified else ""), tiles]
    if m.get("trends"):
        out += [Spacer(1, 3), Paragraph("TRENDS", s["label"]), Spacer(1, 2),
                Chips([clean(t) for t in m["trends"]], fill=AMBER_TINT, stroke=colors.HexColor("#EBD3AE"), text=INK,
                      max_chip=page_w)]
    return [KeepTogether(out)]


def _competitors(rep: dict, page_w: float, s: dict) -> List:
    comps = rep.get("competitors") or []
    head = _heading("Competitors", s, "AI KNOWLEDGE · UNVERIFIED" if comps else "")
    if not comps:
        msg = ("No competitors listed: competitor research needs AI (AI off - site facts only)."
               if not rep.get("ai_used") else "The AI found no competitors it could name with confidence.")
        return [KeepTogether([head, Paragraph(_missing(msg), s["body"])])]
    rows = [[Paragraph("COMPANY", s["th"]), Paragraph("WEBSITE", s["th"]), Paragraph("NOTE", s["th"])]]
    for comp in comps[:6]:
        url = comp.get("url") or ""
        rows.append([Paragraph(f"<b>{_esc(comp.get('name'))}</b>", s["cell"]),
                     Paragraph(_link(url, limit=34) if url else _missing("-"), s["cell"]),
                     Paragraph(_esc(_unlabel(comp.get("note"))), s["cellmuted"])])
    t = Table(rows, colWidths=[page_w * 0.24, page_w * 0.26, page_w * 0.50], repeatRows=1)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
             ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
             ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
             ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]
    t.setStyle(TableStyle(style))
    return [KeepTogether([head, t])]


def _contact(rep: dict, page_w: float, s: dict) -> List:
    c = rep.get("contact") or {}
    left_w = page_w * 0.56

    def lines(values, mail=False, tel=False):
        if not values:
            return _missing("Not found")
        out = []
        for v in values[:4]:
            if mail:
                out.append(f'<link href="mailto:{_esc(v)}"><font color="{_hex(LINK)}">{_esc(v)}</font></link>')
            elif tel:
                digits = "".join(ch for ch in v if ch.isdigit() or ch == "+")
                out.append(f'<link href="tel:{_esc(digits)}">{_esc(v)}</link>')
            else:
                out.append(_esc(v))
        return "<br/>".join(out)
    rows = [[Paragraph("EMAIL", s["label"]), Paragraph(lines(c.get("emails"), mail=True), s["cell"])],
            [Paragraph("PHONE", s["label"]), Paragraph(lines(c.get("phones"), tel=True), s["cell"])],
            [Paragraph("ADDRESS", s["label"]), Paragraph(_esc(c.get("address")) or _missing("Not found"), s["cell"])]]
    kv = Table(rows, colWidths=[17 * mm, left_w - 17 * mm - 16])
    kv.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 2),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE)]))
    socials = c.get("socials") or {}
    chips = Chips(list(socials.keys()), fill=WHITE, stroke=LINE, text=LINK, links=list(socials.values()),
                  empty="No social profiles linked")
    right = [Paragraph("SOCIAL PROFILES", s["label"]), Spacer(1, 4), chips]
    t = Table([[kv, right]], colWidths=[left_w, page_w - left_w])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), PAPER),
                           ("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LINEBEFORE", (1, 0), (1, -1), 0.6, LINE),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return [KeepTogether([_heading("Contact & socials", s), t])]


def _tech(rep: dict, page_w: float, s: dict) -> List:
    t = rep.get("tech_stack") or {}

    def val(v):
        if isinstance(v, list):
            v = " · ".join(str(x) for x in v if x)
        return _esc(v) if v else _missing("Not detected")
    cells = [("CMS / PLATFORM", val(t.get("cms"))), ("E-COMMERCE", val(t.get("ecommerce"))),
             ("HOSTING", val(t.get("hosting"))), ("CDN", val(t.get("cdn"))),
             ("ANALYTICS & TAGS", val(t.get("analytics")), "cell"), ("OTHER", val(t.get("other")), "cell")]
    return [KeepTogether([_heading("Tech stack", s), _tiles(cells, 3, page_w, s)])]


def _seo(rep: dict, page_w: float, s: dict) -> List:
    snap = rep.get("seo_snapshot") or {}
    score = _score(snap.get("health_score"))
    ring_w = 38 * mm
    checked = snap.get("checked", "crawl" if score is not None else "none")
    pages = snap.get("pages") or 0
    if checked == "crawl":
        scope = f"Share of the <b>{pages:,}</b> crawled HTML pages that load."
    elif checked == "quick":
        scope = f"Not crawled yet: a quick check of <b>{pages:,}</b> key pages only."
    else:
        scope = "Not checked: no pages could be read."
    right = [Paragraph(scope, s["small"]), Spacer(1, 4), Paragraph("TOP ISSUES", s["label"]), Spacer(1, 2)]
    issues = snap.get("top_issues") or []
    if issues:
        right += [Paragraph(f"<font color='{_hex(RED)}'>•</font>&nbsp;{_esc(i)}", s["item"]) for i in issues[:5]]
    else:
        right.append(Paragraph(_missing("None found" if checked != "none" else "Not checked"), s["item"]))
    if snap.get("business_impact"):
        right += [Spacer(1, 3), Paragraph("WHAT IT MEANS FOR THE BUSINESS", s["label"]), Spacer(1, 2),
                  Paragraph(_esc(snap["business_impact"]), s["bold"])]
    t = Table([[_ring(score), right]], colWidths=[ring_w, page_w - ring_w])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), PAPER),
                           ("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return [KeepTogether([_heading("SEO snapshot", s), t])]


def _opp_risk(rep: dict, page_w: float, s: dict) -> List:
    half = page_w / 2

    def column(title, items, color, empty):
        out = [Paragraph(f"<font color='{_hex(color)}'>{_esc(title)}</font>", s["listhead"]), Spacer(1, 3)]
        if not items:
            out.append(Paragraph(_missing(empty), s["item"]))
        for it in items[:6]:
            out.append(Paragraph(f"<font color='{_hex(color)}'>•</font>&nbsp;{_esc(it)}", s["item"]))
        return out
    t = Table([[column("OPPORTUNITIES", rep.get("opportunities") or [], GOOD, "None found"),
                column("RISKS", rep.get("risks") or [], RED, "None found")]], colWidths=[half, half])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, 0), GOOD_TINT),
                           ("BACKGROUND", (1, 0), (1, 0), RED_TINT), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                           ("LINEBEFORE", (0, 0), (0, 0), 3, GOOD), ("LINEBEFORE", (1, 0), (1, 0), 3, RED),
                           ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return [KeepTogether([_heading("Opportunities & risks", s), t])]


def _sources(rep: dict, page_w: float, s: dict) -> List:
    """Where each fact comes from, two to a row: facts read from the home page (shown on page 1) are counted,
    every other source (another page of the site, the web, the AI's knowledge) is listed with its page."""
    srcs = list(rep.get("sources") or [])
    site_host = urlparse(rep.get("url") or "").netloc.lower()

    def on_home(src: dict) -> bool:
        p = urlparse(src.get("url") or "")
        return (src.get("from") == "site" and bool(src.get("url"))
                and (not p.netloc or p.netloc.lower() == site_host) and (p.path or "/") == "/")
    home = [x for x in srcs if on_home(x)]
    listed = [x for x in srcs if not on_home(x)][:MAX_SOURCES]
    out = [_heading("Sources", s),
           Paragraph("<b>SITE</b> = the company's own pages · <b>WEB</b> = Wikipedia/Wikidata · <b>AI</b> = "
                     "the AI model's general knowledge (unverified: check it before using it with a client).",
                     s["small"]), Spacer(1, 3)]
    if not srcs:
        out.append(Paragraph(_missing("No sources recorded."), s["body"]))
        return out
    if home:
        out += [Paragraph(f"<b>{len(home):,} fact{'s' if len(home) != 1 else ''}</b> come from the home page "
                          f"({_link(home[0].get('url') or rep.get('url') or '', '/')}).", s["cell"]), Spacer(1, 3)]
    if listed:
        half = page_w / 2
        cells = []
        for src in listed:
            label, color = FROM_LABEL.get(src.get("from"), FROM_LABEL["ai"])
            url = src.get("url") or ""
            p = urlparse(url)
            shown = (p.path or "/") if p.netloc.lower() == site_host and site_host else None   # own pages: path only
            cells.append(Paragraph(f"<font color='{_hex(color)}'><b>{label}</b></font> {_esc(src.get('claim'))}"
                                   + (f" · {_link(url, shown, limit=48)}" if url else ""), s["cell"]))
        rows = [cells[i:i + 2] + [""] * (2 - len(cells[i:i + 2])) for i in range(0, len(cells), 2)]
        t = Table(rows, colWidths=[half, half])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                               ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE), ("LINEBEFORE", (1, 0), (1, -1), 0.4, LINE),
                               ("TOPPADDING", (0, 0), (-1, -1), 2.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
                               ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]))
        out.append(t)
    return out


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------
def build_business_pdf(report: dict, path: str, brand: Optional[dict] = None) -> str:
    """Write the Business Insights PDF for a BusinessReport dict to `path` and return the path. brand: a white
    label (P23); None = the one saved under Settings > Branding, ARACHNIX's when none is saved."""
    rep = report or {}
    brand = brand if brand is not None else current_brand()
    s = _styles()
    page_w = A4[0] - 30 * mm
    url = rep.get("url") or ""
    domain = urlparse(url).netloc or url or "website"
    story = [Paragraph("Business Insights", s["title"])]
    ai_info = rep.get("ai") or {}
    if rep.get("ai_used"):
        how = f"AI summary ({_esc(ai_info.get('provider') or 'AI')}" + (
            f" · {_esc(ai_info['model'])}" if ai_info.get("model") else "") + ") of site facts"
    else:
        how = _esc(rep.get("note") or "AI off - site facts only")
    meta = [f"<b>Site:</b> {_link(url, limit=60)}" if url else "",
            f"<b>Generated:</b> {_esc(rep.get('generated_at') or time.strftime('%Y-%m-%d %H:%M'))}",
            f"<b>Made from:</b> {how}"]
    story.append(Paragraph(" • ".join(m for m in meta if m), s["meta"]))
    if rep.get("ai_used") and rep.get("note"):
        story += [Spacer(1, 1.5 * mm), Paragraph(_esc(rep["note"]), s["small"])]
    story.append(Spacer(1, 5 * mm))
    story.append(_company_card(rep, page_w, s))
    story.append(Spacer(1, 6 * mm))
    story += _offer_audience(rep, page_w, s)
    story.append(Spacer(1, 6 * mm))
    story += _market(rep, page_w, s)
    story.append(Spacer(1, 6 * mm))
    story += _competitors(rep, page_w, s)
    story.append(Spacer(1, 6 * mm))
    story += _contact(rep, page_w, s)
    story.append(Spacer(1, 6 * mm))
    story += _tech(rep, page_w, s)
    story.append(Spacer(1, 6 * mm))
    story += _seo(rep, page_w, s)
    story.append(Spacer(1, 6 * mm))
    story += _opp_risk(rep, page_w, s)
    story.append(Spacer(1, 7 * mm))
    story += _sources(rep, page_w, s)

    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=16 * mm,
                            bottomMargin=17 * mm, title=f"Business Insights - {domain}",
                            author=(brand or {}).get("name") or BRAND_NAME, creator=BRAND_NAME)
    doc.build(story, canvasmaker=_canvas_maker(domain, brand))
    return path
