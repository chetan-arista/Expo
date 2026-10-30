"""
Website Audit Report (PDF): direct and compact, every finding kept, long text and full lists left to Excel.

Page 1  KPI tiles that add up to the internal URLs, the status bar and the equation (2xx + 3xx + 4xx ... =
        internal URLs, then the URLs crawled as the dashboard counts them), "Fix first" (the five most valuable root causes, with the page that explains each) and
        one line per area "at a glance" (broken links, SEO checks, re-check, content, connected data).
Then    every SEO issue on one line (type, priority, URLs, %, a short fix, an example, the page of its
        picture); every broken internal URL and every broken link to another site on one line each, with
        its re-check verdict; the fix plan (two lines per root cause); content findings; one compact table
        per connected Google source (Search Console, Analytics, PageSpeed); then the evidence: broken links
        grouped by cause and place (the site's error page shown once) and one picture per SEO issue type, as
        half-page tiles (search previews and addresses full width). It ends with a pointer to the Excel
        workbook, which has every URL.
The status buckets use the same names as the dashboard and the Excel workbook. "Parent link" is the page
that holds a link. Page references ("p7") are filled in when the document is saved, so page 1 can point
forward.
"""
import os
import re
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape

from reportlab import rl_config
from reportlab.graphics.shapes import Circle, Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    CondPageBreak, Flowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .issues import (CATEGORY_LABELS, CATEGORY_SHORT, ISSUE_CATEGORIES, classify_result, found_in_code,
                     health_score, issue_sort_key)

# Binary streams instead of ASCII85: about a quarter faster to build and 15 % smaller (pure-Python reportlab)
rl_config.useA85 = 0

SECTION_ROOM = 120        # points a section needs on the page it starts on (else it starts on the next one)
MAX_EVIDENCE_CARDS = 60   # broken-link evidence tiles (a tile can hold a whole group of same-cause links)
MAX_ISSUE_ROWS = 150
MAX_EXTERNAL_ROWS = 10      # the compact PDF lists the 10 most-linked; Excel has every one
MAX_GROUP_ROWS = 8        # URLs listed in one broken-link tile (the rest are counted)
FIX_FIRST = 5

INK = colors.HexColor("#181C1A")
MUTED = colors.HexColor("#67716A")
LINE = colors.HexColor("#E3DED3")
PAPER = colors.HexColor("#F7F5EF")
WHITE = colors.white
GOOD = colors.HexColor("#244B30")
GOOD_TINT = colors.HexColor("#E7F2EA")
LINK = colors.HexColor("#1F5A96")
HEAD_BG = colors.HexColor("#EFECE4")
ZEBRA = colors.HexColor("#FBFAF7")
CAT_COLORS = {
    "2xx": colors.HexColor("#2F6B3F"),
    "3xx": colors.HexColor("#B8741F"),
    "4xx": colors.HexColor("#9C3345"),
    "5xx": colors.HexColor("#734399"),
    "error": colors.HexColor("#4D5650"),
    "blocked": colors.HexColor("#8A7F6B"),
}
CAT_TINTS = {
    "2xx": colors.HexColor("#E7F2EA"),
    "3xx": colors.HexColor("#FBF1E1"),
    "4xx": colors.HexColor("#FBE9EC"),
    "5xx": colors.HexColor("#F3ECFA"),
    "error": colors.HexColor("#ECEEEC"),
    "blocked": colors.HexColor("#F1EEE6"),
}
EQUATION_LABELS = {"2xx": "2xx", "3xx": "3xx", "4xx": "4xx", "5xx": "5xx", "error": "no response",
                   "blocked": "blocked"}
CAT_BAR_LABEL = {"2xx": "2XX OK", "3xx": "3XX", "4xx": "4XX", "5xx": "5XX", "error": "ERR", "blocked": "BLOCKED"}

_FONTS = None


def _fonts():
    """Register a Unicode TTF if one is available, else fall back to built-in Helvetica."""
    global _FONTS
    if _FONTS:
        return _FONTS
    candidates = [
        ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ]
    _FONTS = ("Helvetica", "Helvetica-Bold")
    for regular, bold in candidates:
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("ReportSans", regular))
                pdfmetrics.registerFont(TTFont("ReportSans-Bold", bold))
                # <b> in a paragraph uses the family's bold face (without the family it stayed regular)
                pdfmetrics.registerFontFamily("ReportSans", normal="ReportSans", bold="ReportSans-Bold",
                                              italic="ReportSans", boldItalic="ReportSans-Bold")
                _FONTS = ("ReportSans", "ReportSans-Bold")
                break
            except Exception:
                continue
    return _FONTS


CELL = 6.9   # table text size (pt)


def _styles():
    regular, bold = _fonts()
    return {
        "title": ParagraphStyle("title", fontName=bold, fontSize=21, leading=25, textColor=INK),
        "meta": ParagraphStyle("meta", fontName=regular, fontSize=8.4, leading=11.5, textColor=MUTED),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=12, leading=15, textColor=INK, spaceBefore=4,
                             spaceAfter=3, keepWithNext=1),
        "label": ParagraphStyle("label", fontName=bold, fontSize=7.2, leading=9.4, textColor=MUTED, spaceAfter=2,
                                keepWithNext=1),
        "body": ParagraphStyle("body", fontName=regular, fontSize=8.2, leading=11, textColor=INK),
        "bold": ParagraphStyle("bold", fontName=bold, fontSize=8.2, leading=11, textColor=INK),
        "small": ParagraphStyle("small", fontName=regular, fontSize=7.2, leading=9.4, textColor=MUTED),
        "intro": ParagraphStyle("intro", fontName=regular, fontSize=7.2, leading=9.4, textColor=MUTED, spaceAfter=4),
        "cell": ParagraphStyle("cell", fontName=regular, fontSize=CELL, leading=8.6, textColor=INK),
        "cellb": ParagraphStyle("cellb", fontName=bold, fontSize=CELL, leading=8.6, textColor=INK),
        "cellmuted": ParagraphStyle("cellmuted", fontName=regular, fontSize=CELL, leading=8.6, textColor=MUTED),
        "url": ParagraphStyle("url", fontName=regular, fontSize=CELL, leading=8.6, textColor=LINK),
        "num": ParagraphStyle("num", fontName=bold, fontSize=CELL, leading=8.6, textColor=INK, alignment=2),
        "nummuted": ParagraphStyle("nummuted", fontName=regular, fontSize=CELL, leading=8.6, textColor=MUTED,
                                   alignment=2),
        "th": ParagraphStyle("th", fontName=bold, fontSize=6.2, leading=7.8, textColor=MUTED),
        "thr": ParagraphStyle("thr", fontName=bold, fontSize=6.2, leading=7.8, textColor=MUTED, alignment=2),
        "glance_k": ParagraphStyle("glance_k", fontName=bold, fontSize=7, leading=10, textColor=MUTED),
        "glance_v": ParagraphStyle("glance_v", fontName=regular, fontSize=8, leading=10.4, textColor=INK),
        "code": ParagraphStyle("code", fontName="Courier", fontSize=6.4, leading=8, textColor=INK, wordWrap="CJK"),
        "tilehead": ParagraphStyle("tilehead", fontName=bold, fontSize=8, leading=10, textColor=WHITE),
        "tilemeta": ParagraphStyle("tilemeta", fontName=regular, fontSize=6.6, leading=8.4, textColor=WHITE),
        "tile_num": ParagraphStyle("tile_num", fontName=bold, fontSize=18, leading=21, alignment=TA_CENTER),
        "tile_label": ParagraphStyle("tile_label", fontName=bold, fontSize=6.2, leading=7.8, textColor=MUTED,
                                     alignment=TA_CENTER),
    }


def _hex(c) -> str:
    return c.hexval().replace("0x", "#")


def _link(url: str, text: Optional[str] = None, limit: int = 120) -> str:
    shown = text if text is not None else url
    if len(shown) > limit:
        shown = shown[: limit - 1] + "\u2026"
    if not url.startswith(("http://", "https://")):
        return escape(shown)
    return f'<link href="{escape(url, {chr(34): "&quot;"})}">{escape(shown)}</link>'


def _fit(text: str, font: str, size: float, width: float) -> str:
    """Cut `text` with an ellipsis so it fits `width` points."""
    text = str(text or "")
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:   # longest prefix that fits with the ellipsis
        mid = (lo + hi + 1) // 2
        if pdfmetrics.stringWidth(text[:mid] + "\u2026", font, size) <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "\u2026"


def _cut(text: str, n: int) -> str:
    """`text` cut at a word boundary to at most `n` characters (with an ellipsis when cut)."""
    text = " ".join(str(text or "").split())
    if len(text) <= n:
        return text
    cut = text[: n - 1].rsplit(" ", 1)[0].rstrip(",;:(-")
    return (cut or text[: n - 1]) + "\u2026"


def _duration(seconds) -> str:
    try:
        sec = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    return f"{sec // 60}:{sec % 60:02d}" if sec < 3600 else f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _plural(n: int, word: str, many: str = "") -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def _crawled_short(totals: Dict[str, int]) -> str:
    """How the URLs crawled add up to the internal URLs, in few words: " (+3 external, -1 blocked)"
    (exporter.crawl_totals; "" when they are the same number)."""
    parts = []
    if totals.get("external"):
        parts.append(f"+{totals['external']:,} external")
    blocked = int(totals.get("internal_blocked") or 0) + int(totals.get("external_blocked") or 0)
    if blocked:
        parts.append(f"-{blocked:,} blocked")
    if totals.get("external_not_checked"):
        parts.append(f"-{totals['external_not_checked']:,} not checked")
    return f" ({', '.join(parts)})" if parts else ""


def _type_count(n: int, issue_type: str) -> str:
    """"5 issues", "1 warning", "22 opportunities"."""
    word = issue_type.lower()
    return _plural(n, word, "opportunities" if word == "opportunity" else "")


def _short_url(url: str, host: str = "") -> str:
    """The path (and query) of a URL on the audited host, else host + path without the scheme."""
    p = urlparse(url or "")
    if not p.scheme:
        return url or ""
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    if host and p.netloc.lower() == host.lower():
        return path
    return p.netloc + (path if path != "/" else "")


def _url_cell(url: str, host: str, width: float, s: dict, style: str = "url", extra: str = "") -> Paragraph:
    """One-line linked URL (path on the audited host) cut to fit `width`, plus muted `extra` (e.g. "+3")."""
    regular, _ = _fonts()
    more = f" {extra}" if extra else ""
    text = _fit(_short_url(url, host), regular, CELL, width - 8 - pdfmetrics.stringWidth(more, regular, CELL))
    html = _link(url, text) if (url or "").startswith("http") else escape(text)
    if extra:
        html += f" <font color='{_hex(MUTED)}'>{escape(extra)}</font>"
    return Paragraph(html, s[style])


def _one_line(text: str, width: float, s: dict, style: str = "cell", bold: bool = False) -> Paragraph:
    regular, b = _fonts()
    return Paragraph(escape(_fit(text, b if bold else regular, CELL, width - 8)), s[style])


def _is_image(r) -> bool:
    return (getattr(r, "link_tag", "") or "").lower() in ("img", "source", "picture")


def _clicked(r) -> str:
    """What the visitor clicks to reach this URL, in words."""
    if found_in_code(r):
        greyed = getattr(r, "code_greyed", None) or {}
        if greyed.get("label"):
            return f"“{greyed['label']}” greyed out (not clickable)"
        return "(in code, not clickable)" if r.link_region == "Found in code, not clickable" else "(link in JavaScript)"
    if r.anchor_text:
        return r.anchor_text
    if r.link_region == "XML sitemap" or not (r.parent_url or "").startswith("http"):
        return "(direct navigation)"
    if r.link_region == "Page <head> metadata":
        return "(link in page <head>)"
    if not r.link_tag:
        source = (r.discovery_source or "").lower()
        return "(found while logged in)" if source.startswith("authenticated") else "(direct navigation)"
    if _is_image(r):
        return "(image shown on the page, no alt text)"
    return "(no link text)"


def _boxed_elsewhere(r, pin: Optional[dict]) -> bool:
    """True when the pinpointed picture boxes another copy of the link than the one the crawl recorded (the
    first in the HTML, here hidden on screen, e.g. in a collapsed mobile menu): the tile then describes the
    boxed copy, so its text and its picture point at the same element."""
    return bool(pin and pin.get("url") == getattr(r, "url", None) and pin.get("recorded") == "other_hidden"
                and pin.get("selector"))


def _live_sources(r, linking: Optional[tuple], error_urls: set) -> Optional[tuple]:
    """found_on() without the broken URL itself and without pages that are broken too (a 404 page whose own
    menu links to its address is not a page anyone can edit or visit). Falls back like found_on()."""
    if not linking:
        return linking
    first, pages = linking
    url = getattr(r, "url", "")
    live = [p for p in pages if p != url and p not in error_urls]
    if len(live) == len(pages):
        return linking
    return (live[0] if live else (getattr(r, "parent_url", "") or "")), live


def _status_label(r) -> str:
    if r.status_code:
        return r.http_status or str(r.status_code)
    return r.http_status or "Error"


def _broken_fix(r) -> str:
    """What to do about a broken internal URL, in a few words."""
    cat = classify_result(r)
    if found_in_code(r):
        return "Not clickable today: remove the path from the code or build the page"
    if cat == "4xx":
        if _is_image(r):
            return "Missing image: re-upload or remove it"
        if getattr(r, "resource_type", "page") == "file":
            return "File missing: re-upload it or fix the link"
        if r.status_code in (401, 403):
            return "Access refused: open it to visitors or remove the link"
        return "Page gone: fix the link or 301 it to a live page"
    if cat == "5xx":
        return "Server error: check the server logs, then re-check"
    return "No answer: check the address and the server, then re-check"


def _short_fix(row: dict) -> str:
    """The issue's one-line fix: the row's short_fix, else the tab registry's (rows saved before it existed),
    else the first clause of `fix` (cut at ':', '(' or ';'), about 70 characters."""
    short = " ".join(str(row.get("short_fix") or "").split())
    if not short and row.get("tab") and row.get("filter"):
        try:
            from .seo.registry import get_tab, short_fix_of
            tab = get_tab(row["tab"])
            f = next((f for f in tab.filters if f.id == row["filter"]), None) if tab else None
            short = short_fix_of(f) if f is not None else ""
        except Exception:
            short = ""
    if short:
        return _cut(short, 72)
    fix = " ".join(str(row.get("fix") or "").split())
    if not fix:
        return ""
    first = re.split(r"(?<=[.!?])\s", fix, maxsplit=1)[0]
    first = re.split(r"\s*[:(;]\s*", first, maxsplit=1)[0].rstrip(" .,")
    return _cut(first, 72)


def _fit_image(path: Optional[str], max_w: float, max_h: float):
    """The picture scaled to fit max_w x max_h points, keeping its proportions (None when missing)."""
    if not path or not os.path.exists(path):
        return None
    try:
        img = Image(path)
        w, h = float(img.imageWidth or 1), float(img.imageHeight or 1)
        scale = min(max_w / w, max_h / h)
        img.drawWidth, img.drawHeight = w * scale, h * scale
        img.hAlign = "LEFT"
        return img
    except Exception:
        return None


# --------------------------------------------------------------------------------------------
# Page references: "p7" next to an item, pointing at the page of an anchor further on
# --------------------------------------------------------------------------------------------
class _Refs:
    """Anchors (page and height, recorded while pages are laid out) and the places that print their page
    number (drawn when the document is saved, when every anchor's page is known)."""

    def __init__(self):
        self.anchors: Dict[str, tuple] = {}     # key -> (page, y)
        self.slots: List[tuple] = []            # (page, x, y, key, size, align_right, width)

    def page(self, key: str) -> Optional[int]:
        return self.anchors.get(key, (None,))[0]


class _Anchor(Flowable):
    """Zero-size marker: the page it lands on is what references to `key` print (and link to)."""

    def __init__(self, refs: _Refs, *keys: str):
        super().__init__()
        self.refs, self.keys = refs, keys
        self.width = self.height = 0

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        x, y = self.canv.absolutePosition(0, 0)
        for key in self.keys:
            self.refs.anchors.setdefault(key, (self.canv.getPageNumber(), y))


class _PageRef(Flowable):
    """Prints "p7" (the page of anchor `key`, linked to it) or "–" when the anchor never got placed."""

    def __init__(self, refs: _Refs, key: str, width: float = 20, size: float = CELL, right: bool = False):
        super().__init__()
        self.refs, self.key, self.size, self.right = refs, key, size, right
        self.width, self.height = width, size * 1.25

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        x, y = self.canv.absolutePosition(0, self.size * 0.3)
        self.refs.slots.append((self.canv.getPageNumber(), x, y, self.key, self.size, self.right, self.width))


# --------------------------------------------------------------------------------------------
# Drawn evidence (no screenshot): "found on this page" -> "leads to"
# --------------------------------------------------------------------------------------------
def _browser(d: Drawing, x: float, y: float, w: float, h: float, url: str, border, regular: str):
    d.add(Rect(x, y, w, h, rx=4, ry=4, fillColor=WHITE, strokeColor=border, strokeWidth=0.9))
    bar = 11
    d.add(Rect(x, y + h - bar, w, bar, rx=4, ry=4, fillColor=HEAD_BG, strokeColor=None))
    d.add(Rect(x, y + h - bar, w, 4, fillColor=HEAD_BG, strokeColor=None))
    for i, c in enumerate(("#E0645C", "#E5B33C", "#5CB85C")):
        d.add(Circle(x + 6 + i * 6, y + h - bar / 2, 1.8, fillColor=colors.HexColor(c), strokeColor=None))
    pill_x, pill_w = x + 26, w - 32
    d.add(Rect(pill_x, y + h - bar + 2.2, pill_w, bar - 4.4, rx=3, ry=3, fillColor=WHITE, strokeColor=LINE,
               strokeWidth=0.5))
    d.add(String(pill_x + 3, y + h - bar + 4, _fit(url.replace("https://", "").replace("http://", ""),
                                                  regular, 4.6, pill_w - 6),
                 fontName=regular, fontSize=4.6, fillColor=MUTED))


def _evidence_drawing(r, width: float, group_n: int = 1) -> Drawing:
    """A sketch of the parent page with the link highlighted where it sits, an arrow, and the answer."""
    regular, bold = _fonts()
    cat = classify_result(r)
    color = CAT_COLORS.get(cat, INK)
    tint = CAT_TINTS.get(cat, PAPER)
    height = 112
    d = Drawing(width, height)
    d.add(Rect(0, 0, width, height, rx=5, ry=5, fillColor=PAPER, strokeColor=LINE, strokeWidth=0.7))
    pad, gap = 7, 18
    win_w = (width - 2 * pad - gap) / 2
    win_h = 88
    win_y = 8
    lx, rx = pad, pad + win_w + gap
    d.add(String(lx, height - 10, "PARENT LINK", fontName=bold, fontSize=5.4, fillColor=MUTED))
    d.add(String(rx, height - 10, "LEADS TO", fontName=bold, fontSize=5.4, fillColor=MUTED))
    parent = r.parent_url if (r.parent_url or "").startswith("http") else "(no referring page)"
    _browser(d, lx, win_y, win_w, win_h, parent, LINE, regular)
    body_top = win_y + win_h - 11
    for i, frac in enumerate((0.62, 0.8, 0.5, 0.72, 0.66, 0.4)):
        yy = body_top - 10 - i * 11
        if yy < win_y + 6:
            break
        d.add(Rect(lx + 8, yy, (win_w - 16) * frac, 3.2, rx=1.5, ry=1.5, fillColor=colors.HexColor("#E9E6DE"),
                   strokeColor=None))
    region_frac = {"Header": 0.08, "Navigation menu": 0.14, "Breadcrumb": 0.22, "Main content": 0.5,
                   "Sidebar": 0.5, "Footer": 0.86, "Form": 0.55, "Page body": 0.5}
    frac = 0.45 if found_in_code(r) else region_frac.get(r.link_region, 0.5)
    hl_h = 20
    hl_y = body_top - 6 - frac * (win_h - 11 - hl_h - 8) - hl_h
    hl_y = max(win_y + 4, min(hl_y, body_top - hl_h - 3))
    d.add(Rect(lx + 5, hl_y, win_w - 10, hl_h, rx=3, ry=3, fillColor=tint, strokeColor=color, strokeWidth=1))
    if found_in_code(r):
        main = (f"{group_n} paths in code" if group_n > 1 else f"\u201c{urlparse(r.url).path}\u201d in code")
    else:
        main = r.anchor_text or _clicked(r)
    d.add(String(lx + 10, hl_y + 7, _fit(main, bold, 6.2, win_w - 22), fontName=bold, fontSize=6.2, fillColor=color))
    ay = win_y + win_h / 2
    d.add(Line(lx + win_w + 3, ay, rx - 5, ay, strokeColor=color, strokeWidth=1.1))
    d.add(Polygon(points=[rx - 2, ay, rx - 7, ay + 3, rx - 7, ay - 3], fillColor=color, strokeColor=None))
    _browser(d, rx, win_y, win_w, win_h, r.url, color, regular)
    big = str(r.status_code) if r.status_code else ("TIMEOUT" if "timeout" in (r.http_status or "").lower() else "ERROR")
    d.add(String(rx + 8, body_top - 28, big, fontName=bold, fontSize=22 if len(big) <= 3 else 13, fillColor=color))
    d.add(String(rx + 8, body_top - 40, _fit(_status_label(r), bold, 6.2, win_w - 16), fontName=bold, fontSize=6.2,
                 fillColor=INK))
    return d


# --------------------------------------------------------------------------------------------
# Brand: the ARACHNIX mark, drawn as vectors (the same geometry as the dashboard's logo: a spider in a hexagonal web,
# its body a magnifying glass with a small network in the lens)
# --------------------------------------------------------------------------------------------
BRAND_NAME = "ARACHNIX"
BRAND_DARK = colors.HexColor("#17304D")     # navy: legs, the network in the lens
BRAND_LIGHT = colors.HexColor("#22808F")    # teal: web strands and nodes
BRAND_BODY = colors.HexColor("#1D6475")     # deep teal: head, lens, abdomen
_AX_NODES = ((32, 5), (55.4, 18.5), (55.4, 45.5), (32, 59), (8.6, 45.5), (8.6, 18.5))
_AX_LEGS_L = (((27.5, 28.5), (20, 19.5), (17, 13.7)), ((27, 31), (15, 27), (8.6, 23.6)),
              ((27, 34), (15, 37), (8.6, 39.4)), ((28.5, 38), (24, 48.5), (21, 52.6)))


def current_brand(reports_dir: Optional[str] = None) -> Optional[dict]:
    """The white label of Settings > Branding (P23) when one is set, else None (the ARACHNIX look, unchanged)."""
    try:
        from .settings_store import branding
        b = branding(reports_dir)
    except Exception:
        return None
    return b if b.get("custom") else None


def draw_header_brand(c, x: float, y: float, size: float, brand: Optional[dict], label: str = "",
                      font_size: float = 7.5) -> float:
    """The running header's mark: ARACHNIX's own (draw_brand) or the client's logo and company name (P23)."""
    if not brand:
        return draw_brand(c, x, y, size, label, font_size=font_size)
    regular, bold = _fonts()
    tx = x
    path = brand.get("logo_path") or ""
    if path:
        try:
            w0, h0 = float(brand.get("logo_width") or 0), float(brand.get("logo_height") or 0)
            h = size * 1.15
            w = min(h * (w0 / h0), 30 * mm) if w0 and h0 else h
            c.drawImage(path, x, y - size * 0.07, width=w, height=h, mask="auto", preserveAspectRatio=True)
            tx = x + w + size * 0.35
        except Exception:
            tx = x
    name = str(brand.get("company_name") or brand.get("name") or BRAND_NAME)[:60]
    c.saveState()
    c.setFillColor(_accent(brand) or INK)
    c.setFont(bold, font_size)
    ty = y + (size - font_size * 0.7) / 2
    c.drawString(tx, ty, name)
    tx += pdfmetrics.stringWidth(name, bold, font_size)
    if label:
        gap = font_size * 0.7
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.line(tx + gap, y + size * 0.12, tx + gap, y + size * 0.88)
        tx += gap * 2
        c.setFillColor(MUTED)
        c.setFont(regular, font_size)
        c.drawString(tx, ty, label)
        tx += pdfmetrics.stringWidth(label, regular, font_size)
    c.restoreState()
    return tx


def _accent(brand: Optional[dict]):
    value = str((brand or {}).get("accent_color") or "") if (brand or {}).get("accent_custom") else ""
    try:
        return colors.HexColor(value) if value else None
    except Exception:
        return None


def draw_brand(c, x: float, y: float, size: float, label: str = "", font: str = "", font_size: float = 7.5,
               color=None) -> float:
    """Draws the ARACHNIX mark (a spider in its hexagonal web, the body a lens) with its bottom-left corner at
    (x, y), `size` points square, then "ARACHNIX" (+ `label`) to its right. Returns the x after the text."""
    regular, bold = _fonts()
    c.saveState()
    c.translate(x, y + size)
    c.scale(size / 64.0, -size / 64.0)          # the 64-unit, y-down box of the SVG
    c.setLineCap(1)
    c.setLineJoin(1)
    web = c.beginPath()                         # the web: hexagon + the threads above the head and below the abdomen
    web.moveTo(*_AX_NODES[0])
    for n in _AX_NODES[1:]:
        web.lineTo(*n)
    web.close()
    web.moveTo(32, 5.6)
    web.lineTo(32, 19)
    web.moveTo(32, 51.5)
    web.lineTo(32, 58.4)
    c.setStrokeColor(BRAND_LIGHT)
    c.setLineWidth(1.9)
    c.drawPath(web, stroke=1, fill=0)
    legs = list(_AX_LEGS_L) + [tuple((64 - px, py) for px, py in leg) for leg in _AX_LEGS_L]
    lp = c.beginPath()
    for root, knee, tip in legs:
        lp.moveTo(*root)
        lp.lineTo(*knee)
        lp.lineTo(*tip)
    c.setStrokeColor(BRAND_DARK)
    c.setLineWidth(3.0)
    c.drawPath(lp, stroke=1, fill=0)
    c.setFillColor(BRAND_LIGHT)
    for nx, ny in _AX_NODES:
        c.circle(nx, ny, 3.1, stroke=0, fill=1)
    for leg in legs:
        c.circle(leg[1][0], leg[1][1], 1.7, stroke=0, fill=1)
    c.setFillColor(BRAND_BODY)
    c.setStrokeColor(BRAND_BODY)
    c.ellipse(26.7, 18.9, 37.3, 28.3, stroke=0, fill=1)     # head
    ab = c.beginPath()                          # abdomen
    ab.moveTo(28.2, 44.6)
    ab.lineTo(35.8, 44.6)
    ab.lineTo(32, 51.8)
    ab.close()
    c.drawPath(ab, stroke=0, fill=1)
    c.setLineWidth(4)
    c.line(39.3, 40.3, 43.4, 44.4)              # the lens handle
    c.setFillColor(colors.white)
    c.setLineWidth(3.3)
    c.circle(32, 33, 8.4, stroke=1, fill=1)     # the lens
    net = c.beginPath()                         # the network in the lens
    net.moveTo(32, 28.6)
    net.lineTo(36, 35.8)
    net.lineTo(28, 35.8)
    net.close()
    net.moveTo(32, 28.6)
    net.lineTo(32, 33.2)
    net.moveTo(36, 35.8)
    net.lineTo(32, 33.2)
    net.lineTo(28, 35.8)
    c.setStrokeColor(BRAND_DARK)
    c.setLineWidth(1.1)
    c.drawPath(net, stroke=1, fill=0)
    c.setFillColor(BRAND_DARK)
    for nx, ny, r in ((32, 28.6, 1.7), (36, 35.8, 1.7), (28, 35.8, 1.7), (32, 33.2, 1.3)):
        c.circle(nx, ny, r, stroke=0, fill=1)
    c.restoreState()
    tx = x + size + size * 0.35
    ty = y + (size - font_size * 0.7) / 2
    c.saveState()
    c.setFillColor(color or INK)
    c.setFont(font or bold, font_size)
    c.drawString(tx, ty, BRAND_NAME)
    tx += pdfmetrics.stringWidth(BRAND_NAME, font or bold, font_size)
    if label:                                   # a thin rule, then the report name (no extra words)
        gap = font_size * 0.7
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.line(tx + gap, y + size * 0.12, tx + gap, y + size * 0.88)
        tx += gap * 2
        c.setFillColor(MUTED)
        c.setFont(regular, font_size - 0.5)
        c.drawString(tx, ty, label)
        tx += pdfmetrics.stringWidth(label, regular, font_size - 0.5)
    c.restoreState()
    return tx


# --------------------------------------------------------------------------------------------
# Page frame: running header, "Page X of Y" footer, page references
# --------------------------------------------------------------------------------------------
def _canvas_maker(domain: str, refs: Optional[_Refs] = None, brand: Optional[dict] = None):
    regular, bold = _fonts()
    footer = (str(brand.get("footer") or brand.get("name") or BRAND_NAME)[:120] if brand else BRAND_NAME)

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
                if refs is not None:
                    self._references()
                super().showPage()
            super().save()

        def _references(self):
            """Bookmarks for the anchors on this page and the page numbers printed on it. Both are made here,
            page by page as the file is written: earlier, every bookmark would point at the first page."""
            page = self._pageNumber
            for key, (p, y) in refs.anchors.items():
                if p == page:
                    self.bookmarkHorizontalAbsolute(key, y + 14)
            for n, (p, x, y, key, size, right, width) in enumerate(refs.slots):
                if p != page:
                    continue
                target = refs.page(key)
                text = f"p{target}" if target else "\u2013"
                w = pdfmetrics.stringWidth(text, bold, size)
                x0 = x + width - w - 2 if right else x
                self.setFont(bold if target else regular, size)
                self.setFillColor(LINK if target else MUTED)
                self.drawString(x0, y, text)
                if target:
                    # named explicitly: the annotation counter is part of the per-page state restored above
                    self.linkRect("", key, (x0 - 1, y - 2, x0 + w + 1, y + size), relative=0, name=f"PageRef{n}")

        def _chrome(self, total: int):
            w, _ = A4
            self.saveState()
            self.setFont(regular, 7)
            self.setFillColor(MUTED)
            self.setStrokeColor(LINE)
            self.setLineWidth(0.5)
            # running header on every page: the ARACHNIX mark + name (page 1 carries the report title below it)
            draw_header_brand(self, 15 * mm, A4[1] - 10.6 * mm, 3.4 * mm, brand,
                              "" if self._pageNumber == 1 else "Website Audit Report", font_size=7.5)
            self.setFont(regular, 7)
            self.setFillColor(MUTED)
            self.drawRightString(w - 15 * mm, A4[1] - 10 * mm, domain)
            self.line(15 * mm, A4[1] - 11.5 * mm, w - 15 * mm, A4[1] - 11.5 * mm)
            self.line(15 * mm, 12 * mm, w - 15 * mm, 12 * mm)
            self.drawString(15 * mm, 8 * mm, footer)
            self.drawRightString(w - 15 * mm, 8 * mm, f"Page {self._pageNumber} of {total}")
            self.restoreState()

    return NumberedCanvas


def _table(rows, widths, zebra=True, header=True, pad=2.6):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 3.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 1), (-1, -1), 0.35, LINE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG)]
    if zebra:
        for i in range(2, len(rows), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
    t.setStyle(TableStyle(style))
    return t


def _type_color(ty: str):
    return ISSUE_TYPE_COLORS.get(ty, INK)


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------
def build_pdf_report(
    results: List,
    filepath: str,
    stats: Optional[dict] = None,
    evidence: Optional[Dict[str, dict]] = None,
    evidence_dir: Optional[str] = None,
    screenshot_note: str = "",
    seo_issues: Optional[List[dict]] = None,
    site=None,
    external_links: Optional[List] = None,
    seo_evidence: Optional[dict] = None,
    validation: Optional[dict] = None,
    intel: Optional[dict] = None,
    content: Optional[dict] = None,
    seo_evidence_dir: Optional[str] = None,
    dismissed_urls: Optional[Dict[str, List[str]]] = None,
    checks: Optional[List[dict]] = None,
    integrations: Optional[List[dict]] = None,
    seo_index=None,
    brand: Optional[dict] = None,
) -> str:
    """brand: a white label (settings_store.branding(), P23); None = the one saved under Settings > Branding, the
    ARACHNIX look when none is saved.
    seo_issues: rows of registry.TabIndex.issues (name, issue_type, priority, urls, pct, description,
    fix, short_fix, optional "examples": [url]); when given, every issue gets one line. site (analysis.Site)
    lets each broken URL list every page that links to it; external_links adds the broken links to other
    websites.
    Optional sections, drawn only when the data is passed:
      seo_evidence  seo_evidence.capture_issue_evidence() result ((tab, filter) or "tab/filter" keys):
                    one picture per issue; pictures live in seo_evidence_dir (default evidence_dir).
                    Broken-link tiles use its boxed parent-page pictures too.
      validation    GET /api/validate/status: re-check counts on page 1 and a verdict per broken link.
      intel         GET /api/intel/errors: "Fix first" on page 1 and the fix plan (root-cause groups).
      content       GET /api/intel/content: thin, off-topic and address/content mismatches.
      checks        intel.checks.check_statuses() (dismissals applied): Passed / Failed / Not checked counts.
      integrations  integrations.report_summary(site): one compact table per connected source (Search
                    Console, Google Analytics 4, PageSpeed Insights); nothing is drawn for sources that
                    are not connected, and the section is left out when none is.
      seo_index     the TabIndex: the integration tables say how many issues each listed page has.
    Dismissals: seo_issues is the overview with the team's dismissals applied, so a pictured issue missing
    from it (dismissed completely) gets no picture; dismissed_urls ({"tab:filter": [urls]}, from
    dismiss.apply_dismissals) keeps dismissed URLs out of the pictures and the per-page issue counts."""
    from .exporter import crawl_totals, found_on, is_broken_external
    stats = stats or {}
    evidence = evidence or {}
    seo_ev = _norm_seo_evidence(seo_evidence)
    shots_dir = seo_evidence_dir or evidence_dir
    verdicts = (validation or {}).get("results") or {}
    s = _styles()
    regular, bold = _fonts()
    page_w = A4[0] - 30 * mm
    refs = _Refs()

    start_url = stats.get("start_url") or (results[0].url if results else "")
    domain = urlparse(start_url).netloc or start_url
    host = domain
    counts = Counter(classify_result(r) for r in results)
    total = len(results)
    issues = sorted([r for r in results if classify_result(r) in ISSUE_CATEGORIES], key=issue_sort_key)
    health = health_score(results)
    external = stats.get("total_external", 0) or 0
    external_links = list(external_links or [])
    broken_ext = [e for e in external_links if is_broken_external(e)]
    # pages that are broken themselves (a 404 page's own menu linking to its address) are not counted as
    # pages with broken links: nobody can visit or edit them
    error_urls = {r.url for r in issues}
    linking: Dict[str, tuple] = {}
    for r in issues if site is not None else ():
        linking[r.url] = _live_sources(r, found_on(site, r), error_urls)
    groups = _evidence_groups(issues, linking)
    pins = {it.get("url"): it for key in BROKEN_KEYS for it in seo_ev.get(key, []) if it.get("shot")}
    issue_rows = list(seo_issues or [])

    story: List = []
    ctx = _Ctx(s=s, page_w=page_w, refs=refs, host=host, start_url=start_url, stats=stats, counts=counts,
               total=total, external=external, health=health, issues=issues, linking=linking,
               verdicts=verdicts, broken_ext=broken_ext, external_links=external_links,
               issue_rows=issue_rows, seo_issues=seo_issues, intel=intel, checks=checks,
               validation=validation, content=content, integrations=integrations,
               screenshot_note=screenshot_note, totals=crawl_totals(results, external_links),
               brand=brand if brand is not None else current_brand())
    story += _summary_page(ctx)
    story.append(PageBreak())   # page 1 is the summary only
    if seo_issues is not None:
        story += _issues_section(issue_rows, page_w, s, refs, host, bool(groups))
    if issues:
        story += _broken_internal_section(issues, linking, verdicts, page_w, s, host)
    if broken_ext:
        story += _broken_external_section(broken_ext, site, page_w, s, verdicts, error_urls, host)
    if intel and intel.get("groups"):
        story += _fix_plan_section(intel, page_w, s, refs, host, issue_rows)
    if content and any(content.get(k) for k in ("mismatches", "thin", "off_topic")):
        story += _content_section(content, page_w, s, host)
    if integrations and any(i.get("connected") for i in integrations):
        story += _integrations_section(integrations, page_w, s, host,
                                       _issues_by_url(issue_rows, seo_index, dismissed_urls))

    # ---- Evidence ----
    tiles_ev = []
    if groups:
        tiles_ev = _broken_tiles(groups[:MAX_EVIDENCE_CARDS], evidence, evidence_dir, pins, shots_dir, verdicts,
                                 linking, page_w, s, refs, host)
    seo_tiles, seo_intro = [], []
    if seo_ev:
        seo_tiles, seo_intro = _seo_tiles(_without_dismissed(seo_ev, seo_issues, dismissed_urls), seo_issues,
                                          shots_dir, page_w, s, refs, host,
                                          skip=set(BROKEN_KEYS) if groups else set())
    if tiles_ev or seo_tiles:
        story.append(CondPageBreak(260))   # a new page only when little room is left
    if tiles_ev:
        rows = _grid(tiles_ev, page_w)
        intro = _broken_evidence_intro(groups, evidence, evidence_dir, pins, shots_dir, page_w, s, host)
        story += [KeepTogether(intro + rows[:1])] + rows[1:]   # the heading never ends a page alone
        if len(groups) > MAX_EVIDENCE_CARDS:
            story.append(Paragraph(f"Pictures shown for the {MAX_EVIDENCE_CARDS} most severe groups; the table above "
                                   "and the Excel workbook list every broken URL.", s["small"]))
    if seo_tiles:
        rows = _grid(seo_tiles, page_w)
        story += [KeepTogether(seo_intro + rows[:1])] + rows[1:]

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        f"<b>Every URL is in the Excel workbook:</b> Internal URLs (all {total:,}), Issues Overview (full "
        "descriptions and fixes), Issue URLs (every URL per issue), Broken Links &amp; Errors, Broken External "
        "Links, Fix Plan, Validation and Checks.", s["small"]))

    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    partial = f"{filepath}.partial"
    doc = SimpleDocTemplate(partial, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=16 * mm, bottomMargin=17 * mm,
                            title=f"Website Audit Report - {domain}",
                            author=(ctx.brand or {}).get("name") or BRAND_NAME, creator=BRAND_NAME)
    try:
        doc.build(story, canvasmaker=_canvas_maker(domain, refs, ctx.brand))
        os.replace(partial, filepath)
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    return filepath


# --------------------------------------------------------------------------------------------
# Page 1: summary (its inputs, then the page)
# --------------------------------------------------------------------------------------------
@dataclass
class _Ctx:
    """What the summary page reads, gathered once by build_pdf_report()."""
    s: dict
    page_w: float
    refs: "_Refs"
    host: str
    start_url: str
    stats: dict
    counts: Counter
    total: int
    external: int
    health: dict
    issues: List
    linking: Dict[str, tuple]
    verdicts: dict
    broken_ext: List
    external_links: List
    issue_rows: List[dict]
    seo_issues: Optional[List[dict]]
    intel: Optional[dict]
    checks: Optional[List[dict]]
    validation: Optional[dict]
    content: Optional[dict]
    integrations: Optional[List[dict]]
    screenshot_note: str
    totals: Optional[dict] = None        # exporter.crawl_totals: Total URLs Crawled as the dashboard counts it
    brand: Optional[dict] = None         # P23 white label (None = ARACHNIX)


def _summary_page(v: "_Ctx") -> List:
    """Title, KPI tiles that add up, the status bar, "Fix first" and "At a glance" (≈200 words)."""
    s, stats, counts, total = v.s, v.stats, v.counts, v.total
    page_w, refs = v.page_w, v.refs
    regular, bold = _fonts()
    out: List = [Paragraph("Website Audit Report", s["title"])]
    if v.brand and v.brand.get("cover_line"):
        out.append(Paragraph(escape(str(v.brand["cover_line"])[:200]), s["meta"]))
    meta = [f"<b>Site:</b> {_link(v.start_url, limit=70)}", f"<b>Generated:</b> {time.strftime('%d %b %Y %H:%M')}",
            f"<b>Duration:</b> {_duration(stats.get('elapsed_seconds'))}",
            f"<b>Crawl:</b> {'logged in' if stats.get('auth_used') else 'public (not logged in)'}"]
    out.append(Paragraph(" \u2022 ".join(meta), s["meta"]))
    try:       # an SEO check module failed to load: its issues are missing from this report
        from .seo.registry import checks_warning
        missing = checks_warning()
    except Exception as e:
        missing = f"WARNING: checks incomplete - the SEO checks could not be loaded ({type(e).__name__})."
    if missing:
        out.append(Spacer(1, 1.5 * mm))
        out.append(Paragraph(f"<font color='{_hex(CAT_COLORS['5xx'])}'><b>{escape(missing)}</b></font>", s["body"]))
    if stats.get("limit_reached") or stats.get("not_crawled_max_depth"):
        notes = [n for n in stats.get("limitations") or [] if "Max URLs" in n or "Max depth" in n]
        for note in notes[:2]:
            out.append(Spacer(1, 1.5 * mm))
            out.append(Paragraph(f"<font color='{_hex(CAT_COLORS['3xx'])}'><b>Partial crawl:</b></font> "
                                 f"{escape(_cut(note, 160))}", s["body"]))
    out.append(Spacer(1, 4 * mm))

    # KPI tiles: the status tiles add up to INTERNAL (the dashboard's tile of the same name)
    tiles = [("INTERNAL", total, INK)] + [(CATEGORY_SHORT[c], counts[c], CAT_COLORS[c])
                                         for c in ("2xx", "3xx", "4xx", "5xx", "error")]
    if counts["blocked"]:
        tiles.append((CATEGORY_SHORT["blocked"], counts["blocked"], CAT_COLORS["blocked"]))
    tiles.append(("EXTERNAL", v.external, INK))
    cells = [[Paragraph(f"{n:,}", ParagraphStyle("n", parent=s["tile_num"], textColor=c)),
              Paragraph(label, s["tile_label"])] for label, n, c in tiles]
    tt = Table([cells], colWidths=[page_w / len(tiles)] * len(tiles), rowHeights=[16 * mm])
    tt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                            ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE), ("BACKGROUND", (0, 0), (-1, -1), PAPER)]))
    out.append(tt)
    out.append(Spacer(1, 2.5 * mm))

    # Status bar
    bar_h = 13
    bar = Drawing(page_w, bar_h)
    x = 0.0
    for cat in ("2xx", "3xx", "4xx", "5xx", "error", "blocked"):
        if not (total and counts[cat]):
            continue
        w = page_w * counts[cat] / total
        bar.add(Rect(x, 0, w, bar_h, fillColor=CAT_COLORS[cat], strokeColor=WHITE, strokeWidth=0.6))
        full = f"{CAT_BAR_LABEL[cat]}  {counts[cat]:,} ({counts[cat] / total:.0%})"
        if pdfmetrics.stringWidth(full, bold, 6.8) + 10 < w:
            bar.add(String(x + 5, 3.9, full, fontName=bold, fontSize=6.8, fillColor=WHITE))
        elif pdfmetrics.stringWidth(f"{counts[cat]:,}", bold, 6.8) + 6 < w:
            bar.add(String(x + w / 2, 3.9, f"{counts[cat]:,}", fontName=bold, fontSize=6.8, fillColor=WHITE,
                           textAnchor="middle"))
        x += w
    if not total:
        bar.add(Rect(0, 0, page_w, bar_h, fillColor=LINE, strokeColor=None))
    out.append(bar)
    out.append(Spacer(1, 1.5 * mm))
    health = v.health
    parts = " + ".join(f"{counts[c]:,} {EQUATION_LABELS[c]}" for c in CATEGORY_LABELS if counts[c])
    score_color = _hex(CAT_COLORS["2xx"] if health["score"] >= 90 else CAT_COLORS["3xx"] if health["score"] >= 70
                       else CAT_COLORS["4xx"])
    totals = v.totals
    crawled = ""
    if totals:   # the dashboard's Total URLs Crawled: internal + external, minus the URLs never requested
        crawled = f" \u00b7 <b>{totals['crawled']:,} URLs crawled</b>{_crawled_short(totals)}"
    out.append(Paragraph(
        f"{parts} = <b>{total:,} internal URLs</b>{crawled} \u00b7 "
        f"<font color='{score_color}'><b>Health {health['score']}%</b>"
        f"</font> ({health['pages_ok']:,} of {_plural(health['pages'], 'page')} load) \u00b7 "
        f"{_plural(health['files'], 'file')} checked, not scored", s["small"]))
    out.append(Spacer(1, 5 * mm))

    # Fix first: the most valuable root causes (else the top issues), each with the page that explains it
    rows = _fix_first_rows(v)
    if rows:
        out.append(Paragraph("FIX FIRST", s["label"]))
        widths = [14, page_w - 14 - 40 - 46 - 26, 40, 46, 26]
        trows = [[Paragraph(h, s["th"]) for h in ("#", "WHAT TO FIX", "URLS", "EFFORT", "SEE")]]
        trows[0][2] = Paragraph("URLS", s["thr"])
        for i, (title, note, urls, effort, key) in enumerate(rows, start=1):
            trows.append([Paragraph(str(i), s["cellmuted"]), _title_note(title, note, widths[1], s),
                          Paragraph(f"{urls:,}" if urls is not None else "", s["num"]),
                          Paragraph(escape(effort), s["cell"]), _PageRef(refs, key, width=20)])
        t = _table(trows, widths, zebra=False)
        t.setStyle(TableStyle([("BACKGROUND", (0, 1), (-1, -1), WHITE),
                               ("LINEBEFORE", (0, 1), (0, -1), 2.5, CAT_COLORS["4xx"])]))
        out.append(t)
        out.append(Spacer(1, 5 * mm))

    # At a glance: one line per area
    lines = _glance_lines(v)
    if lines:
        out.append(Paragraph("AT A GLANCE", s["label"]))
        g = Table([[Paragraph(k, s["glance_k"]), Paragraph(val, s["glance_v"])] for k, val in lines],
                  colWidths=[24 * mm, page_w - 24 * mm])
        g.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -2), 0.35, LINE),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                               ("LEFTPADDING", (0, 0), (-1, -1), 2), ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                               ("BACKGROUND", (0, 0), (0, -1), PAPER)]))
        out.append(g)
    return out


def _fix_first_rows(v: "_Ctx") -> List[tuple]:
    """(title, note, URLs, effort, anchor key): the top root-cause groups of the fix plan (note: where the rest of
    an issue's URLs went, see _split_notes), else the most urgent issues."""
    groups = _ranked_groups(v.intel)
    if groups:
        notes = _split_notes(groups, v.issue_rows)
        return [(str(g.get("title") or ""), notes.get(i, ""), int(g.get("urls_count") or 0),
                 str(g.get("effort") or "").capitalize(), f"fix:{i}")
                for i, g in enumerate(groups[:FIX_FIRST], start=1)]
    rows = []
    for i, row in enumerate(v.issue_rows[:FIX_FIRST], start=1):
        rows.append((str(row.get("name") or ""), "", int(row.get("urls") or 0), str(row.get("priority") or ""),
                     f"issue:{i}"))
    if not rows and v.issues:
        rows.append((f"{_plural(len(v.issues), 'broken internal URL')}", "", len(v.issues), "", "broken"))
    return rows


def _ranked_groups(intel: Optional[dict]) -> List[dict]:
    """The fix plan's root-cause groups in fix-first order: position i (from 1) is the "#" shown for a group in
    "Fix first", the Fix plan table and its page references (the Excel Fix Order)."""
    return sorted((intel or {}).get("groups") or [],
                  key=lambda g: (int(g.get("rank") or 999), -int(g.get("impact") or 0)))


def _split_notes(groups: List[dict], issue_rows: Optional[List[dict]]) -> Dict[int, str]:
    """{position: note} for a group named after the one issue it holds that has only part of that issue's URLs,
    because the fix plan moved the others to groups with their own cause (a deleted image is taken out of
    "Internal Client Error (4xx)"): "1 more URL in #12". Without it the same name shows 2 URLs in Fix first and
    3 in All issues. The fix plan's own helper (intel.root_cause.split_notes), so the dashboard and Excel say
    the same."""
    try:
        from .intel.root_cause import split_notes
        return split_notes(groups, issue_rows)
    except Exception:
        pass
    totals = {str(r.get("name") or ""): int(r.get("urls") or 0) for r in issue_rows or []}
    holders: Dict[str, List[tuple]] = {}
    for i, g in enumerate(groups, start=1):
        for name, n in (g.get("issue_urls") or {}).items():
            holders.setdefault(str(name), []).append((i, int(n or 0)))
    out: Dict[int, str] = {}
    for i, g in enumerate(groups, start=1):
        held = g.get("issue_urls") or {}
        if len(held) != 1:
            continue
        name, n = next(iter(held.items()))
        name, n = str(name), int(n or 0)
        title, total = str(g.get("title") or "").strip(), totals.get(name, 0)
        if not name or not (title == name or title.startswith(name + ":")) or not 0 < n < total:
            continue
        others = [(j, m) for j, m in holders.get(name, []) if j != i and m > 0]
        rest = sum(m for _, m in others)
        if others and n + rest == total:
            where = ", ".join(f"#{j}" for j, _ in others[:3]) + (f" +{len(others) - 3}" if len(others) > 3 else "")
            out[i] = f"{rest:,} more URL{'s' if rest != 1 else ''} in {where}"
        else:
            out[i] = f"{n:,} of its {total:,} URLs"
    return out


def _title_note(title: str, note: str, width: float, s: dict) -> Paragraph:
    """One line: the bold title, cut to fit, then the muted note in full."""
    regular, bold = _fonts()
    tail = f" · {note}" if note else ""
    room = width - 8 - (pdfmetrics.stringWidth(tail, regular, CELL) if tail else 0)
    html = escape(_fit(title, bold, CELL, room))
    if tail:
        html += f"<font name='{regular}' color='{_hex(MUTED)}'>{escape(tail)}</font>"
    return Paragraph(html, s["cellb"])


def _glance_lines(v: "_Ctx") -> List[tuple]:
    """("LABEL", html) per area: broken links, SEO checks, re-check, content, login, connected data, notes."""
    out = []
    issues, linking, verdicts, broken_ext = v.issues, v.linking, v.verdicts, v.broken_ext
    # Broken links
    if issues:
        n_pages = len({p for _, pages in linking.values() for p in pages})
        n_links = sum(len(pages) for _, pages in linking.values())
        text = f"<b>{_plural(len(issues), 'internal URL')}</b>"
        if n_pages:
            text += f" on {_plural(n_pages, 'page')}" + (f" ({_plural(n_links, 'link')})" if n_links != n_pages else "")
        text += _verdict_tally([r.url for r in issues], verdicts)
    else:
        text = "No broken internal URLs"
    if broken_ext:
        text += f" \u00b7 <b>{len(broken_ext):,} external</b>" + _verdict_tally([e.url for e in broken_ext], verdicts,
                                                                                external=True)
    elif any((getattr(e, "http_status", "") or "").strip() for e in v.external_links):
        text += " \u00b7 no broken external links"
    out.append(("BROKEN LINKS", text))
    # SEO checks
    rows = v.issue_rows
    if v.seo_issues is not None:
        by = Counter(r.get("issue_type") or "" for r in rows)
        text = (f"<b>{len(rows):,} flagged</b>: " + " \u00b7 ".join(
            f"<font color='{_hex(_type_color(t))}'>{_type_count(by.get(t, 0), t)}</font>"
            for t in ("Issue", "Warning", "Opportunity")))
        tally = _checks_tally(v.checks)
        if tally:
            text += f" \u00b7 {tally}"
        out.append(("SEO CHECKS", text))
    # Re-check
    validation = v.validation
    if validation and (validation.get("results") or (validation.get("summary") or {})):
        summ = validation.get("summary") or {}
        c = {k: int(summ.get(k) or 0) for k in VERDICT_LABELS}
        if not any(c.values()):
            c = dict(Counter(e.get("verdict") for e in (validation.get("results") or {}).values()
                             if e.get("verdict") in VERDICT_LABELS))
        n = sum(c.values())
        text = f"<b>{_plural(n, 'URL')}</b> requested again: " + " \u00b7 ".join(
            f"<font color='{_hex(VERDICT_COLORS[k])}'>{c.get(k, 0):,} {VERDICT_LABELS[k].lower()}</font>"
            for k in VERDICT_LABELS)
        if summ.get("not_checked"):
            text += f" \u00b7 {int(summ['not_checked']):,} not checked"
        out.append(("RE-CHECK", text))
        exposed = [g for g in validation.get("login_guard") or [] if g.get("exposed")]
        if exposed:
            out.append(("LOGIN", f"<font color='{_hex(CAT_COLORS['4xx'])}'><b>{_plural(len(exposed), 'page')} meant "
                                 "for logged-in users opened without logging in</b></font>: "
                        + ", ".join(_link(g.get("url", ""), _short_url(g.get("url", ""), v.host), limit=50)
                                    for g in exposed[:3]) + (f" +{len(exposed) - 3}" if len(exposed) > 3 else "")))
    else:
        out.append(("RE-CHECK", "Not run: start the live re-check from the dashboard to confirm each error"))
    # Content
    content = v.content
    if content is not None:
        thin, off, mism = (len(content.get(k) or []) for k in ("thin", "off_topic", "mismatches"))
        if thin or off or mism:
            out.append(("CONTENT", " \u00b7 ".join(p for p in (
                _plural(thin, "thin page") if thin else "", f"{off:,} off-topic" if off else "",
                _plural(mism, "address/content mismatch", "address/content mismatches") if mism else "") if p)))
        else:
            out.append(("CONTENT", "No thin, off-topic or mismatched pages"))
    # Connected data: only when at least one source is connected (each source says whether it is)
    integ = v.integrations or []
    if any(i.get("connected") for i in integ):
        names = {"gsc": "Search Console", "ga4": "Analytics", "psi": "PageSpeed"}
        out.append(("DATA", " \u00b7 ".join(
            f"{names.get(i.get('source'), i.get('title', ''))}: "
            + ("<b>connected</b>" if i.get("connected") else "not connected") for i in integ)))
    if v.screenshot_note:
        out.append(("NOTE", escape(_cut(v.screenshot_note, 180))))
    return out


def _verdict_tally(urls: List[str], verdicts: dict, external: bool = False) -> str:
    """", all confirmed" / ": 8 confirmed, 10 open in a browser" from the live re-check ("" when not re-checked)."""
    seen = Counter(_verdict_of(verdicts, u) for u in urls)
    seen.pop("", None)
    if not seen:
        return " (not re-checked)" if external else ""
    n = sum(seen.values())
    if seen.get("confirmed") == len(urls):
        return ", all confirmed"
    words = {"confirmed": "confirmed", "not_reproduced": "open in a browser" if external else "not reproduced",
             "intermittent": "intermittent", "fixed": "fixed"}
    text = ", ".join(f"{seen[k]:,} {words[k]}" for k in VERDICT_LABELS if seen.get(k))
    if n < len(urls):
        text += f", {len(urls) - n:,} not re-checked"
    return ": " + text


def _checks_tally(checks: Optional[List[dict]]) -> str:
    """Passed, failed, not checked, not applicable (and dismissed) counts: the dashboard's Checks chips, counted the
    same way. A check with nothing of its kind on the site (no hreflang, no AMP) is "not applicable", not "not
    checked"; a failed check the team dismissed completely is "dismissed", not failed."""
    if not checks:
        return ""
    c: Counter = Counter()
    for x in checks:
        if x.get("not_applicable"):
            c["not_applicable"] += 1
        elif x.get("dismissed") and x.get("status") == "failed":
            c["dismissed"] += 1
        else:
            c[x.get("status") or "not_checked"] += 1
    parts = [f"{c['passed']:,} passed", f"{c['failed']:,} failed", f"{c['not_checked']:,} not checked"]
    if c["not_applicable"]:
        parts.append(f"{c['not_applicable']:,} not applicable")
    if c["dismissed"]:
        parts.append(f"{c['dismissed']:,} dismissed")
    return " \u00b7 ".join(parts)


# --------------------------------------------------------------------------------------------
# All issues, one line each
# --------------------------------------------------------------------------------------------
ISSUE_TYPE_COLORS = {"Issue": CAT_COLORS["4xx"], "Warning": CAT_COLORS["3xx"], "Opportunity": LINK}
PRIORITY_COLORS = {"High": CAT_COLORS["4xx"], "Medium": CAT_COLORS["3xx"], "Low": MUTED}
MAX_SEO_ISSUE_ROWS = 120


def _only_indexable(row: dict) -> bool:
    try:
        from .seo.registry import get_tab
        tab = get_tab(row.get("tab"))
        f = next((f for f in tab.filters if f.id == row.get("filter")), None) if tab else None
        return bool(f is not None and f.only_indexable)
    except Exception:
        return False


def _issues_section(rows: List[dict], page_w: float, s: dict, refs: _Refs, host: str, broken_tiles: bool) -> List:
    n = len(rows)
    by = Counter(r.get("issue_type") or "" for r in rows)
    out: List = [Spacer(1, 2 * mm), Paragraph(
        f"All issues \u00b7 {n:,} <font size='8.5' color='{_hex(MUTED)}'>("
        + " \u00b7 ".join(_type_count(by.get(t, 0), t) for t in ("Issue", "Warning", "Opportunity")) + ")</font>",
        s["h2"])]
    if not rows:
        out.append(Paragraph("No SEO issues, warnings or opportunities were flagged on this crawl.", s["small"]))
        return out
    # The % is the registry's "% of Total" (the dashboard's "% of URLs", Excel's Issues Overview): the URLs as a
    # share of every URL the check covers. A check run on indexable pages only (dagger) still divides by all of
    # them, so the dagger marks the count, not the %.
    out.append(Paragraph("Most urgent first. URLS = URLs affected (\u2020 indexable pages only), % = their share of all "
                         "URLs of that kind (indexable or not), PIC = page of its picture.", s["intro"]))
    widths = [16, 140, 48, 34, 34, 26, 0, 72, 20]
    widths[6] = page_w - sum(widths)
    head = ["#", "ISSUE", "TYPE", "PRIO", "URLS", "%", "FIX", "E.G.", "PIC"]
    table = [[Paragraph(h, s["thr"] if h in ("URLS", "%") else s["th"]) for h in head]]
    for i, row in enumerate(rows[:MAX_SEO_ISSUE_ROWS], start=1):
        ty, pr = row.get("issue_type") or "", row.get("priority") or ""
        key = (row.get("tab"), row.get("filter"))
        urls = int(row.get("urls") or 0)
        examples = [u for u in row.get("examples") or [] if u]
        pic = "ev:broken" if (broken_tiles and key in BROKEN_KEYS) else f"pic:{key[0]}/{key[1]}"
        dagger = f"<font color='{_hex(MUTED)}'>\u2020</font>" if _only_indexable(row) else ""
        table.append([
            [_Anchor(refs, f"issue:{i}"), Paragraph(str(i), s["cellmuted"])],
            Paragraph(escape(str(row.get("name", ""))), s["cellb"]),
            Paragraph(f"<font color='{_hex(_type_color(ty))}'>{escape(ty)}</font>", s["cell"]),
            Paragraph(f"<font color='{_hex(PRIORITY_COLORS.get(pr, INK))}'>{escape(pr)}</font>", s["cell"]),
            Paragraph(f"{urls:,}{dagger}", s["num"]),
            Paragraph(f"{float(row.get('pct') or 0):.1f}", s["nummuted"]),
            Paragraph(escape(_short_fix(row)), s["cell"]),
            _url_cell(examples[0], host, widths[7], s) if examples else Paragraph("", s["cell"]),
            _PageRef(refs, pic, width=18),
        ])
    out.append(_table(table, widths))
    if n > MAX_SEO_ISSUE_ROWS:
        out.append(Paragraph(f"Showing the {MAX_SEO_ISSUE_ROWS} most urgent of {n:,}; the Excel workbook has every "
                             "issue.", s["small"]))
    return out


# --------------------------------------------------------------------------------------------
# Broken links: internal and external, one line each
# --------------------------------------------------------------------------------------------
def _broken_internal_section(issues: List, linking: Dict[str, tuple], verdicts: dict, page_w: float, s: dict,
                             host: str) -> List:
    n = len(issues)
    n_pages = len({p for _, pages in linking.values() for p in pages})
    head = f"Broken internal URLs \u00b7 {n:,}" + (f" on {_plural(n_pages, 'page')}" if n_pages else "")
    out: List = [CondPageBreak(SECTION_ROOM), Spacer(1, 4 * mm), Paragraph(head, s["h2"]),
                 Paragraph("PARENT LINK = the page that holds the link (+ how many more pages link to it). RE-CHECK = "
                           "the live re-check after the crawl (– = not re-checked).", s["intro"])]
    widths = [26, 134, 118, 80, 0, 58]
    widths[4] = page_w - sum(widths)
    rows = [[Paragraph(h, s["th"]) for h in ("CODE", "BROKEN URL", "PARENT LINK", "CLICKED", "WHAT TO DO",
                                             "RE-CHECK")]]
    for r in issues[:MAX_ISSUE_ROWS]:
        first, pages = linking.get(r.url) or (r.parent_url or "", [])
        first = first or r.parent_url or ""
        more = len(pages) - 1
        v = _verdict_of(verdicts, r.url)
        rows.append([
            Paragraph(f"<font color='{_hex(CAT_COLORS[classify_result(r)])}'><b>"
                      f"{escape(str(r.status_code or 'ERR'))}</b></font>", s["cell"]),
            _url_cell(r.url, host, widths[1], s),
            _url_cell(first, host, widths[2], s, extra=f"+{more:,}" if more > 0 else "") if first.startswith("http")
            else _one_line(first or "(start URL)", widths[2], s, "cellmuted"),
            _one_line(_clicked(r), widths[3], s),
            Paragraph(escape(_broken_fix(r)), s["cell"]),
            Paragraph(f"<font color='{_hex(VERDICT_COLORS[v])}'>{VERDICT_LABELS[v]}</font>" if v else
                      f"<font color='{_hex(MUTED)}'>–</font>", s["cell"]),
        ])
    out.append(_table(rows, widths))
    if n > MAX_ISSUE_ROWS:
        out.append(Paragraph(f"Showing the {MAX_ISSUE_ROWS} most severe of {n:,}; the Excel workbook (Broken Links "
                             "&amp; Errors) has every row.", s["small"]))
    return out


def _broken_external_section(broken: List, site, page_w: float, s: dict, verdicts: Optional[dict] = None,
                             error_urls: Optional[set] = None, host: str = "") -> List:
    """Broken links to other websites: status, URL, parent link and how many pages link to it, re-check verdict
    (a site that refuses only automated checkers opens fine in a browser: nothing to fix)."""
    from .exporter import found_on
    verdicts = verdicts or {}
    out: List = [CondPageBreak(SECTION_ROOM), Spacer(1, 4 * mm), Paragraph(f"Broken links to other websites \u00b7 {len(broken):,}", s["h2"]),
                 Paragraph("Not part of the crawled-URL counts. \u201cOpens in a browser\u201d = the other site only "
                           "refuses automated checkers: nothing to fix. – = not re-checked.", s["intro"])]
    widths = [64, 0, 140, 30, 74]
    widths[1] = page_w - sum(widths)
    rows = [[Paragraph(h, s["th"]) for h in ("STATUS", "EXTERNAL URL", "PARENT LINK", "PAGES", "RE-CHECK")]]
    rows[0][3] = Paragraph("PAGES", s["thr"])
    for e in broken[:MAX_EXTERNAL_ROWS]:
        first, pages = (_live_sources(e, found_on(site, e), error_urls or set()) if site is not None
                        else (e.parent_url, []))
        first = first or e.parent_url or ""
        v = _verdict_of(verdicts, e.url)
        label = {"confirmed": "Confirmed", "not_reproduced": "Opens in a browser", "intermittent": "Intermittent",
                 "fixed": "Fixed"}.get(v, "")
        rows.append([
            Paragraph(f"<font color='{_hex(CAT_COLORS.get(_ext_cat(e), INK))}'><b>"
                      f"{escape(_fit(getattr(e, 'http_status', '') or 'No response', _fonts()[1], CELL, 58))}"
                      "</b></font>", s["cell"]),
            _url_cell(e.url, host, widths[1], s),
            _url_cell(first, host, widths[2], s) if first.startswith("http") else _one_line(first, widths[2], s),
            Paragraph(f"{max(len(pages), 1 if first else 0):,}", s["num"]),
            Paragraph(f"<font color='{_hex(VERDICT_COLORS[v])}'>{label}</font>" if v else
                      f"<font color='{_hex(MUTED)}'>–</font>", s["cell"]),
        ])
    out.append(_table(rows, widths))
    if len(broken) > MAX_EXTERNAL_ROWS:
        out.append(Paragraph(f"Showing {MAX_EXTERNAL_ROWS} of {len(broken):,}; Excel (Broken External Links) has "
                             "them all, with every parent link.", s["small"]))
    return out


def _ext_cat(e) -> str:
    from .exporter import row_category
    return row_category(e) or "error"


# --------------------------------------------------------------------------------------------
# Fix plan, content, connected data
# --------------------------------------------------------------------------------------------
MAX_FIX_GROUPS = 20       # rows of the compact PDF; the Excel workbook (Fix Plan) has every group
FIX_STEPS_GROUPS = 10     # groups whose fix steps are printed (the rest: one line each)
MAX_MISMATCH_ROWS = 15


def _plain_group(g: dict, names: Dict[str, str]) -> bool:
    """A group that is one issue under its own name (or the catch-all "Other issues"): no shared cause beyond what
    the issues list already says."""
    title = str(g.get("title") or "").strip()
    keys = [str(k) for k in g.get("issues") or []]     # issue names (or "tab:filter" keys)
    return title.lower() == "other issues" or (len(keys) == 1 and title in (keys[0], names.get(keys[0])))


def _short_checked(label: str) -> str:
    """The CHECKED cell of the compact PDF in one word where the lead explains it (Seen / Likely)."""
    return {"Seen in crawl data": "Seen", "Likely cause (from crawl data)": "Likely"}.get(label, label)


def _fix_plan_section(intel: dict, page_w: float, s: dict, refs: _Refs, host: str = "",
                      issue_rows: Optional[List[dict]] = None) -> List:
    """Root-cause groups, two lines each (what, URLs, impact, effort, checked; then the fix and an example) for the
    FIX_STEPS_GROUPS most valuable, one line for every other shared cause. Groups that are a single issue under its
    own name are only counted: the issues list has them, Excel (Fix Plan) their steps."""
    ranked = _ranked_groups(intel)
    if not ranked:
        return []
    split = _split_notes(ranked, issue_rows)
    names = {f"{r.get('tab')}:{r.get('filter')}": str(r.get("name") or "") for r in issue_rows or []}
    eligible = [(i, g) for i, g in enumerate(ranked, start=1)
                if i <= FIX_STEPS_GROUPS or not _plain_group(g, names)]
    listed = eligible[:MAX_FIX_GROUPS]
    more = len(eligible) - len(listed)          # shared causes past the compact PDF's rows (Excel has them)
    plain = len(ranked) - len(eligible)
    groups = ranked
    plat = intel.get("platform") or {}
    lead = "One fix clears every URL in a group; most valuable first."
    if plat.get("cms"):
        lead += f" Platform: <b>{escape(str(plat['cms']))}</b>"
        if plat.get("plugins"):
            lead += " with " + escape(", ".join(str(p) for p in plat["plugins"][:4]))
        lead += "."
    lead += " Written with AI help." if intel.get("ai_used") else " AI off: site facts only."
    lead += " CHECKED: Seen = in the crawl data, Likely = the likely cause."
    out: List = [CondPageBreak(SECTION_ROOM), Spacer(1, 4 * mm),
                 Paragraph(f"Fix plan \u00b7 {_plural(len(groups), 'group')}", s["h2"]), Paragraph(lead, s["intro"])]
    verified_c = {"confirmed": GOOD, "partly": CAT_COLORS["3xx"], "unverified": MUTED}
    widths = [16, 0, 34, 62, 40, 104]
    widths[1] = page_w - sum(widths)
    rows = [[Paragraph(h, s["th"]) for h in ("#", "ROOT CAUSE", "URLS", "IMPACT", "EFFORT", "CHECKED")]]
    rows[0][2] = Paragraph("URLS", s["thr"])
    spans = []
    for i, g in listed:
        impact = max(0, min(100, int(g.get("impact") or 0)))
        bar = Drawing(32, 5)
        bar.add(Rect(0, 0, 32, 5, fillColor=LINE, strokeColor=None))
        bar.add(Rect(0, 0, 32 * impact / 100.0, 5, fillColor=CAT_COLORS["4xx"], strokeColor=None))
        ver = str(g.get("verified") or "unverified")
        ver_text = _short_checked(str(g.get("verified_label") or ver.capitalize()))
        imp = Table([[bar, Paragraph(f"{impact}", s["cellmuted"])]], colWidths=[36, 22])
        imp.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        rows.append([
            [_Anchor(refs, f"fix:{i}"), Paragraph(str(i), s["cellmuted"])],
            Paragraph(f"<b>{escape(str(g.get('title', '')))}</b>"
                      + (f" <font color='{_hex(MUTED)}'>· {escape(split[i])}</font>" if i in split else ""),
                      s["cell"]),
            Paragraph(f"{int(g.get('urls_count') or 0):,}", s["num"]),
            imp,
            Paragraph(escape(str(g.get("effort", "")).capitalize()), s["cell"]),
            Paragraph(f"<font color='{_hex(verified_c.get(ver, MUTED))}'>{escape(_cut(ver_text, 60))}</font>",
                      s["cell"]),
        ])
        if i > FIX_STEPS_GROUPS:
            continue      # every group is listed; the steps of the less valuable ones are in Excel (Fix Plan)
        steps = [str(x) for x in (g.get("platform_steps") or [])[:2]] or [str(x) for x in (g.get("fix_steps") or [])[:2]]
        fix = _cut("; ".join(st.strip().rstrip(".") for st in steps if st.strip()), 85)
        bits = []
        if fix:
            bits.append(f"<font color='{_hex(GOOD)}'><b>Fix:</b></font> {escape(fix)}")
        samples = [u for u in g.get("sample_urls") or [] if u][:1]
        if samples:
            bits.append("e.g. " + _link(samples[0], _cut(_short_url(samples[0], host), 60)))
        if bits:
            rows.append(["", Paragraph(" \u00b7 ".join(bits), s["cellmuted"]), "", "", "", ""])
            spans.append(len(rows) - 1)
    table = _table(rows, widths, zebra=False)
    table.setStyle(TableStyle([st for r in spans for st in (
        ("SPAN", (1, r), (-1, r)), ("TOPPADDING", (0, r), (-1, r), 0), ("BOTTOMPADDING", (0, r), (-1, r), 4),
        ("LINEBELOW", (0, r - 1), (-1, r - 1), 0.4, WHITE), ("NOSPLIT", (0, r - 1), (-1, r)))]))
    out.append(table)
    notes = []
    in_excel = ([f"steps below #{FIX_STEPS_GROUPS}"] if len(listed) > FIX_STEPS_GROUPS else []) +         ([_plural(more, "more shared cause")] if more else [])
    if in_excel:
        notes.append("Excel workbook (Fix Plan): " + " and ".join(in_excel) + ".")
    if plain:
        notes.append(f"{_plural(plain, 'more group is a single issue', 'more groups are single issues')} already in "
                     "All issues.")
    if notes:
        out.append(Paragraph(" ".join(notes), s["small"]))
    return out


def _content_section(content: dict, page_w: float, s: dict, host: str = "") -> List:
    """Thin and off-topic pages and pages whose address promises one thing and whose content delivers another."""
    mism = sorted(content.get("mismatches") or [], key=lambda m: -float(m.get("confidence") or 0))
    thin = list(content.get("thin") or [])
    off = list(content.get("off_topic") or [])
    head = " \u00b7 ".join(p for p in (_plural(len(thin), "thin page") if thin else "",
                                       f"{len(off):,} off-topic" if off else "",
                                       _plural(len(mism), "address/content mismatch", "address/content mismatches")
                                       if mism else "") if p)
    out: List = [CondPageBreak(SECTION_ROOM), Spacer(1, 4 * mm), Paragraph(f"Content \u00b7 {head}", s["h2"]),
                 Paragraph("Reviewed with AI help." if content.get("ai_used") else "AI off: site facts only.",
                           s["intro"])]
    widths = [170, 0]
    widths[1] = page_w - sum(widths)
    rows = [[Paragraph("PAGE", s["th"]), Paragraph("FINDING AND WHAT TO DO", s["th"])]]
    for t in thin[:10]:
        note = re.sub(r"^\d[\d,]* words:\s*", "", str(t.get("note", "")))
        rows.append([_url_cell(str(t.get("url", "")), host, widths[0], s),
                     Paragraph(f"<b>Thin, {int(t.get('words') or 0):,} words</b>"
                               + (f": {escape(_cut(note, 150))}" if note else ""), s["cell"])])
    for o in off[:10]:
        rows.append([_url_cell(str(o.get("url", "")), host, widths[0], s),
                     Paragraph("<b>Off-topic</b>: " + escape(_cut(str(o.get("note", "")), 150)), s["cell"])])
    for m in mism[:MAX_MISMATCH_ROWS]:
        rows.append([_url_cell(str(m.get("url", "")), host, widths[0], s),
                     Paragraph(f"<b>Address says {escape(str(m.get('url_intent', '')))}, page delivers "
                               f"{escape(str(m.get('content_intent', '')))}</b> "
                               f"({float(m.get('confidence') or 0):.0%} sure): "
                               + escape(_cut(str(m.get("suggestion", "")), 130)), s["cell"])])
    out.append(_table(rows, widths))
    left = max(0, len(thin) - 10) + max(0, len(off) - 10) + max(0, len(mism) - MAX_MISMATCH_ROWS)
    if left:
        out.append(Paragraph(f"{left:,} more in the dashboard and the Excel workbook (Content Intelligence).",
                             s["small"]))
    return out


def _issues_by_url(rows: List[dict], index, dismissed_urls: Optional[Dict[str, List[str]]]) -> Dict[str, List[str]]:
    """URL -> names of the issues flagging it (the overview's issues, dismissed URLs left out); {} without index."""
    out: Dict[str, List[str]] = {}
    if index is None:
        return out
    for row in rows:
        tr = index.tabs.get(row.get("tab"))
        if tr is None:
            continue
        gone = set((dismissed_urls or {}).get(f"{row.get('tab')}:{row.get('filter')}") or ())
        for r in tr.filter_rows.get(row.get("filter"), []):
            url = getattr(r, "url", "")
            if url and url not in gone:
                out.setdefault(url, []).append(str(row.get("name") or ""))
    return out


def _integrations_section(sources: List[dict], page_w: float, s: dict, host: str,
                          by_url: Dict[str, List[str]]) -> List:
    """One compact table per connected source: Search Console pages by clicks, Analytics landing pages by
    sessions (each with the issues found on the page), PageSpeed Core Web Vitals and its top opportunities."""
    out: List = [CondPageBreak(SECTION_ROOM), Spacer(1, 4 * mm), Paragraph("Connected data", s["h2"])]
    missing = [i for i in sources if not i.get("connected")]
    if missing:
        out.append(Paragraph("Not connected: " + ", ".join(escape(str(i.get("title") or i.get("source")))
                                                           for i in missing)
                             + " (Configuration \u203a API Access).", s["intro"]))
    for src in sources:
        if not src.get("connected"):
            continue
        tot = src.get("totals") or {}
        cols = list(src.get("columns") or [])
        title = escape(str(src.get("title") or src.get("source") or ""))
        note = escape(str(src.get("note") or ""))
        if src.get("source") == "psi":
            line = (f"{_plural(int(tot.get('tested') or 0), 'page')} tested: Core Web Vitals "
                    f"<b>{int(tot.get('cwv_passed') or 0):,} passed</b> \u00b7 {int(tot.get('cwv_failed') or 0):,} failed"
                    f" \u00b7 {int(tot.get('cwv_no_data') or 0):,} without field data")
            if tot.get("avg_performance") is not None:
                line += f" \u00b7 average performance score {int(tot['avg_performance'])}/100"
            if tot.get("not_tested"):
                line += f" \u00b7 {int(tot['not_tested']):,} could not be tested"
        elif src.get("source") == "gsc":
            line = (f"<b>{int(tot.get('clicks') or 0):,} clicks</b> on {_plural(int(tot.get('pages_with_clicks') or 0), 'page')}"
                    f" \u00b7 {int(tot.get('non_indexable_with_impressions') or 0):,} non-indexable pages with "
                    f"impressions \u00b7 {int(tot.get('orphans') or 0):,} pages in Search Console but not found by "
                    "the crawl")
        else:
            line = (f"<b>{int(tot.get('sessions') or 0):,} sessions</b> on "
                    f"{_plural(int(tot.get('pages_with_sessions') or 0), 'page')} \u00b7 "
                    f"{int(tot.get('orphans') or 0):,} pages with sessions but not found by the crawl")
        block: List = [Paragraph(f"<b>{title}</b>" + (f" <font color='{_hex(MUTED)}'>\u00b7 {note}</font>" if note else ""),
                                 s["body"]), Paragraph(line, s["small"]), Spacer(1, 1 * mm)]
        rows_in = list(src.get("rows") or [])[:10]
        if rows_in and cols:
            page_col = cols[0].lower() == "page"
            with_issues = page_col and bool(by_url)
            n_num = len(cols) - 1
            widths = [0] + [max(40.0, pdfmetrics.stringWidth(c.upper(), _fonts()[1], 6.2) + 10) for c in cols[1:]]
            widths += [150] if with_issues else []
            widths[0] = page_w - sum(widths)
            head = [Paragraph(escape(cols[0].upper()), s["th"])] + [
                Paragraph(escape(c.upper()), s["thr"]) for c in cols[1:]] + (
                [Paragraph("ISSUES ON THE PAGE (MOST URGENT)", s["th"])] if with_issues else [])
            table = [head]
            for row in rows_in:
                first = str(row[0]) if row else ""
                cells = [_url_cell(first, host, widths[0], s) if page_col else _one_line(first, widths[0], s)]
                for val in list(row[1:1 + n_num]) + [""] * max(0, n_num - len(row) + 1):
                    cells.append(Paragraph(_fmt(val), s["num"]))
                if with_issues:
                    names = by_url.get(first) or []
                    cells.append(_one_line(f"{len(names)} · {names[0]}" if names else "none", widths[-1], s,
                                           "cell" if names else "cellmuted"))
                table.append(cells)
            block.append(_table(table, widths))
        out.append(KeepTogether(block))
        out.append(Spacer(1, 3 * mm))
    return out


def _fmt(val) -> str:
    if val is None or val == "":
        return "\u2013"
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, int):
        return f"{val:,}"
    if isinstance(val, float):
        return f"{val:,.1f}" if abs(val) < 1000 else f"{val:,.0f}"
    return escape(str(val))


# --------------------------------------------------------------------------------------------
# Evidence tiles (half page wide, two per row; wide pictures take a full row)
# --------------------------------------------------------------------------------------------
GAP = 8
TILE_PAD = 5


def _tile(head: List, body: List, color, width: float) -> Table:
    """A bordered tile: a coloured header (flowables) and a white body (flowables)."""
    t = Table([[head], [body]], colWidths=[width])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), color), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), TILE_PAD), ("RIGHTPADDING", (0, 0), (-1, -1), TILE_PAD),
                           ("TOPPADDING", (0, 0), (-1, 0), 3.5), ("BOTTOMPADDING", (0, 0), (-1, 0), 3.5),
                           ("TOPPADDING", (0, 1), (-1, 1), TILE_PAD), ("BOTTOMPADDING", (0, 1), (-1, 1), TILE_PAD)]))
    return t


def _grid(tiles: List[tuple], page_w: float) -> List:
    """Rows of tiles: (flowable, "half" | "full"). Two half tiles share a row (the next half tile is pulled
    forward over at most two full ones, so rows are not left half empty)."""
    out: List = []
    half_w = (page_w - GAP) / 2
    pending = list(tiles)
    while pending:
        tile, kind = pending.pop(0)
        if kind == "half":
            j = next((k for k, (_, kk) in enumerate(pending[:3]) if kk == "half"), None)
            mate = pending.pop(j)[0] if j is not None else ""
            row = Table([[tile, "", mate]], colWidths=[half_w, GAP, half_w])
        else:
            row = Table([[tile]], colWidths=[page_w])
        row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), GAP)]))
        out.append(row)
    return out


def _evidence_groups(issues: List, linking: Dict[str, tuple]) -> List[List]:
    """Broken URLs grouped by cause and place, in severity order: paths read from the same JavaScript file,
    missing images (one group per status), and links per parent page."""
    groups: "OrderedDict[tuple, List]" = OrderedDict()
    for r in issues:
        cat = classify_result(r)
        if found_in_code(r) and cat == "4xx":
            key = ("code", r.status_code, r.source_file or "inline")
        elif _is_image(r):
            key = ("image", cat, r.status_code)
        else:
            first = (linking.get(r.url) or (r.parent_url or "", []))[0] or r.parent_url or ""
            key = ("link", cat, r.status_code, first)
        groups.setdefault(key, []).append(r)
    return list(groups.values())


def _error_picture(groups, evidence, evidence_dir, pins, shots_dir) -> tuple:
    """(file, url, how many page URLs share it): what visitors get at a broken page, shown once."""
    found: "OrderedDict[str, List[str]]" = OrderedDict()
    for g in groups:
        for r in g:
            if getattr(r, "resource_type", "page") == "file" or _is_image(r) or found_in_code(r):
                continue
            ev = evidence.get(r.url) or {}
            path = None
            if ev.get("error_shot") and evidence_dir:
                path = os.path.join(evidence_dir, ev["error_shot"])
            elif (pins.get(r.url) or {}).get("error_shot"):
                path = os.path.join(shots_dir or evidence_dir or "", pins[r.url]["error_shot"])
            if path and os.path.exists(path):
                found.setdefault(path, []).append(r.url)
    if not found:
        return None, "", 0
    path, urls = next(iter(found.items()))
    return path, urls[0], (len(urls) if len(found) == 1 else 0)


def _broken_evidence_intro(groups, evidence, evidence_dir, pins, shots_dir, page_w: float, s: dict,
                           host: str) -> List:
    n = sum(len(g) for g in groups)
    head = Paragraph(f"Evidence \u00b7 where the broken links are ({_plural(n, 'URL')}, "
                     f"{_plural(len(groups), 'group')})", s["h2"])
    shots = any(pins.get(r.url) or (evidence.get(r.url) or {}).get("parent_shot") for g in groups for r in g)
    text = ("One tile per cause and place. In each picture the <font color='#2563EB'><b>blue 1</b></font> is the "
            "part of the page the link sits in and the <font color='#E11D48'><b>red 2</b></font> is the link."
            if shots else "One tile per cause and place, with a sketch of where the link sits (no screenshots were "
                          "taken for this report).")
    path, url, same = _error_picture(groups, evidence, evidence_dir, pins, shots_dir)
    if not path:
        return [head, Paragraph(text, s["small"]), Spacer(1, 2 * mm)]
    half = (page_w - GAP) / 2
    img = _fit_image(path, half - 2 * TILE_PAD, 95)
    where = _short_url(url, host)
    caption = (f"all {same:,} broken pages show this error page (e.g. {where})." if same > 1
               else f"the error page at {where}.")
    left = [Paragraph(text, s["small"]), Spacer(1, 2 * mm),
            Paragraph("<b>What visitors see</b> (right): " + escape(caption), s["small"])]
    row = Table([[left, "", [img] if img is not None else ""]], colWidths=[half, GAP, half])
    row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOX", (2, 0), (2, 0), 0.6, LINE),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return [head, row, Spacer(1, GAP)]


def _where_line(r, pin: Optional[dict], host: str) -> str:
    """"Parent link /blog/x/ · line 902 · Main content · <img src=...>" for the tile of a broken URL."""
    parent = (pin or {}).get("page") if pin and pin.get("page") else (r.parent_url or "")
    bits = ["Parent link " + (_link(parent, _cut(_short_url(parent, host), 46)) if parent.startswith("http")
                              else escape(parent or "(start URL)"))]
    if found_in_code(r):
        bits.append("JavaScript " + escape(_cut((r.source_file or "inline script").rsplit("/", 1)[-1], 40)))
        return " \u00b7 ".join(bits)
    if getattr(r, "link_line", 0):
        bits.append(f"line {r.link_line}")
    region = (pin or {}).get("region") or r.link_region or ""
    if region:
        bits.append(escape(region))
    html = (pin or {}).get("html") if _boxed_elsewhere(r, pin) else (r.link_html or (pin or {}).get("html") or "")
    if html:
        bits.append(f"<font name='Courier' size='6.2'>{escape(_cut(html, 90))}</font>")
    if _boxed_elsewhere(r, pin):
        bits.append("also hidden earlier in the HTML")
    return " \u00b7 ".join(bits)


def _recheck_text(group: List, verdicts: dict) -> str:
    """"re-check: confirmed (404 ×3)" for one URL, "re-check: 6 confirmed" for a group, "" when not re-checked."""
    seen = Counter(_verdict_of(verdicts, r.url) for r in group)
    seen.pop("", None)
    if not seen:
        return ""
    if len(group) == 1:
        v = next(iter(seen))
        entry = (verdicts or {}).get(group[0].url) or {}
        codes = Counter(str(a.get("status") or "no answer") for a in entry.get("attempts") or [])
        detail = ", ".join(f"{c} \u00d7{k}" if k > 1 else c for c, k in codes.most_common(2))
        return (f"re-check: <font color='{_hex(VERDICT_COLORS[v])}'>{VERDICT_LABELS[v].lower()}</font>"
                + (f" ({escape(detail)})" if detail else ""))
    return "re-check: " + ", ".join(f"<font color='{_hex(VERDICT_COLORS[k])}'>{seen[k]:,} {VERDICT_LABELS[k].lower()}"
                                    "</font>" for k in VERDICT_LABELS if seen.get(k))


def _broken_tiles(groups: List[List], evidence, evidence_dir, pins, shots_dir, verdicts, linking, page_w: float,
                  s: dict, refs: _Refs, host: str) -> List[tuple]:
    regular, bold = _fonts()
    half = (page_w - GAP) / 2
    inner = half - 2 * TILE_PAD
    out = []
    for gi, group in enumerate(groups):
        first = group[0]
        n = len(group)
        cat = classify_result(first)
        color = CAT_COLORS.get(cat, INK)
        code = str(first.status_code or "ERR")
        if found_in_code(first) and cat == "4xx":
            title = f"{_plural(n, 'path')} in the site's JavaScript, not clickable" if n > 1 else \
                f"{_short_url(first.url, host)} \u00b7 in JavaScript, not clickable"
        elif _is_image(first):
            title = f"{n:,} missing images" if n > 1 else f"Missing image {_short_url(first.url, host).rsplit('/', 1)[-1]}"
        elif n > 1:
            parent = (linking.get(first.url) or (first.parent_url or "", []))[0] or first.parent_url or ""
            title = f"{n:,} broken links on {_short_url(parent, host)}"
        else:
            title = _short_url(first.url, host)
        head = [Paragraph(escape(_fit(f"{code} \u00b7 {title}", bold, 8, inner)), s["tilehead"])]
        if gi == 0:
            head.insert(0, _Anchor(refs, "ev:broken", "broken"))
        # the picture: the first URL of the group that has one (pinpointed, else the crawl-end screenshot)
        pin = next((pins.get(r.url) for r in group if pins.get(r.url)), None)
        lead = next((r for r in group if pins.get(r.url) is pin), first) if pin else first
        img = None
        if pin:
            img = _fit_image(os.path.join(shots_dir or evidence_dir or "", pin.get("shot") or ""), inner, 150)
        if img is None:
            ev = next(((evidence.get(r.url) or {}) for r in group if (evidence.get(r.url) or {}).get("parent_shot")), {})
            if ev and evidence_dir:
                lead = next((r for r in group if (evidence.get(r.url) or {}) is ev), first)
                img = _fit_image(os.path.join(evidence_dir, ev["parent_shot"]), inner, 150)
        body: List = [img if img is not None else _evidence_drawing(first, inner, group_n=n), Spacer(1, 2)]
        body.append(Paragraph(_where_line(lead, pin, host), s["cell"]))
        if n > 1:
            for r in group[:MAX_GROUP_ROWS]:
                parent, pages = linking.get(r.url) or (r.parent_url or "", [])
                parent = parent or r.parent_url or ""
                name = _short_url(r.url, host)
                if _is_image(r):
                    name = name.rsplit("/", 1)[-1] or name
                more = f" +{len(pages) - 1:,}" if len(pages) > 1 else ""
                body.append(Paragraph(
                    "\u2022 " + _link(r.url, _fit(name, regular, CELL, inner * 0.45)) + f" <font color='{_hex(MUTED)}'>on "
                    + "</font>" + (_link(parent, _fit(_short_url(parent, host), regular, CELL, inner * 0.42))
                                   if parent.startswith("http") else escape(parent or "-"))
                    + (f"<font color='{_hex(MUTED)}'>{more}</font>" if more else ""), s["cell"]))
            if n > MAX_GROUP_ROWS:
                body.append(Paragraph(f"+{n - MAX_GROUP_ROWS:,} more (Excel: Broken Links &amp; Errors)", s["cellmuted"]))
        fix = f"<font color='{_hex(GOOD)}'><b>Fix:</b></font> {escape(_broken_fix(first))}"
        rc = _recheck_text(group, verdicts)
        body.append(Paragraph(fix + (f" \u00b7 {rc}" if rc else ""), s["cell"]))
        out.append((_tile(head, body, color, half), "half"))
    return out


# --------------------------------------------------------------------------------------------
# Pinpointed SEO evidence
# --------------------------------------------------------------------------------------------
BROKEN_KEYS = (("response_codes", "internal_client_error"), ("response_codes", "internal_server_error"),
               ("response_codes", "internal_no_response"))
MAX_SEO_EVIDENCE_CARDS = 40
VERDICT_LABELS = {"confirmed": "Confirmed", "not_reproduced": "Not reproduced", "intermittent": "Intermittent",
                  "fixed": "Fixed"}
VERDICT_COLORS = {"confirmed": CAT_COLORS["4xx"], "not_reproduced": GOOD, "intermittent": CAT_COLORS["3xx"],
                  "fixed": LINK}
PIN_ISSUE = colors.HexColor("#E11D48")
PIN_PARENT = colors.HexColor("#2563EB")
# Issue types that show the same element (or the same response headers) of the same page: one tile names them all
MERGE_GROUPS = ((("page_titles", "over_60_characters"), ("page_titles", "over_561_pixels")),
                (("page_titles", "below_30_characters"), ("page_titles", "below_200_pixels")),
                (("meta_description", "over_155_characters"), ("meta_description", "over_985_pixels")),
                (("meta_description", "below_70_characters"), ("meta_description", "below_400_pixels")),
                tuple(("security", f) for f in ("missing_hsts", "missing_csp", "missing_xcto", "missing_xfo",
                                                 "missing_referrer_policy")))
FULL_WIDTH_MODES = ("serp", "url")


def _norm_seo_evidence(data) -> Dict[tuple, List[dict]]:
    """capture_issue_evidence() output with (tab, filter) keys; "tab/filter" string keys are accepted."""
    out: Dict[tuple, List[dict]] = {}
    for k, v in (data or {}).items():
        if isinstance(k, (tuple, list)) and len(k) == 2:
            key = (str(k[0]), str(k[1]))
        elif isinstance(k, str) and "/" in k:
            key = tuple(k.split("/", 1))
        else:
            continue
        items = [it for it in (v or []) if isinstance(it, dict) and it.get("shot")]
        if items:
            out[key] = items
    return out


def _without_dismissed(seo_ev: Dict[tuple, List[dict]], seo_issues: Optional[List[dict]],
                       dismissed_urls: Optional[Dict[str, List[str]]]) -> Dict[tuple, List[dict]]:
    """The SEO evidence without pictures of URLs the team dismissed (pictures are made once, dismissals can
    come later). With dismissed_urls the exact URLs drop out; without it, an overview row that lost URLs to
    a dismissal (its "dismissed_urls" count) keeps only pictures of the URLs it still gives as examples."""
    rows = {(i.get("tab"), i.get("filter")): i for i in seo_issues or []}
    out: Dict[tuple, List[dict]] = {}
    for key, items in seo_ev.items():
        if dismissed_urls is not None:
            gone = set(dismissed_urls.get(f"{key[0]}:{key[1]}") or ())
            items = [it for it in items if it.get("url") not in gone]
        else:
            n = (rows.get(key) or {}).get("dismissed_urls")
            try:
                n = len(n) if isinstance(n, (list, tuple, set)) else int(n or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                keep = set(rows[key].get("examples") or ())
                items = [it for it in items if it.get("url") in keep]
        if items:
            out[key] = items
    return out


def _verdict_of(verdicts: dict, url: str) -> str:
    v = ((verdicts or {}).get(url) or {}).get("verdict") or ""
    return v if v in VERDICT_LABELS else ""


def _issue_meta(key: tuple, seo_issues: Optional[List[dict]]) -> dict:
    """Name, type, priority, URL count, description and fix of an issue (from the overview rows or
    the tab registry)."""
    for i in seo_issues or []:
        if (i.get("tab"), i.get("filter")) == key:
            return i
    try:
        from .seo.registry import get_tab, short_fix_of
        tab = get_tab(key[0])
        f = next((f for f in tab.filters if f.id == key[1]), None) if tab else None
        if f is not None:
            return {"tab": key[0], "filter": key[1], "name": f"{tab.name}: {f.name}", "issue_type": f.issue_type or "",
                    "priority": f.priority or "", "urls": None, "description": f.description, "fix": f.fix,
                    "short_fix": short_fix_of(f)}
    except Exception:
        pass
    return {"tab": key[0], "filter": key[1], "name": f"{key[0]}: {key[1]}", "issue_type": "", "priority": "",
            "urls": None, "description": "", "fix": ""}


def _seo_tiles(seo_ev: Dict[tuple, List[dict]], seo_issues: Optional[List[dict]], shots_dir: Optional[str],
               page_w: float, s: dict, refs: _Refs, host: str, skip: set) -> tuple:
    """(tiles, intro flowables): one tile per pictured SEO issue in the order of the issues list; issue types
    showing the same element of the same page share a tile."""
    def path(name):
        return os.path.join(shots_dir, name) if (shots_dir and name) else name

    order = [(i.get("tab"), i.get("filter")) for i in seo_issues or []]
    if seo_issues is not None:
        # the overview is the list of issues (dismissals applied): a pictured issue that is not in it was
        # dismissed completely (or is no longer flagged) and gets no picture
        keys = [k for k in order if k in seo_ev]
    else:
        keys = list(seo_ev)
    keys = [k for k in keys if k not in skip and any(os.path.exists(path(it["shot"])) for it in seo_ev[k])]
    merged: Dict[tuple, List[tuple]] = {}       # first key of a group -> the other keys shown in its tile
    for group in MERGE_GROUPS:
        present = [k for k in keys if k in group]
        if len(present) > 1:
            lead = present[0]
            mates = [k for k in present[1:] if seo_ev[k][0].get("url") == seo_ev[lead][0].get("url")]
            if mates:
                merged[lead] = mates
    gone = {k for mates in merged.values() for k in mates}
    keys = [k for k in keys if k not in gone]
    if not keys:
        return [], []
    intro = [Paragraph(f"Evidence \u00b7 where each SEO issue is ({_plural(len(keys), 'picture')})", s["h2"]),
             Paragraph(f"<font color='{_hex(PIN_PARENT)}'><b>Blue 1</b></font> = where it is on the page, "
                       f"<font color='{_hex(PIN_ISSUE)}'><b>red 2</b></font> and up = the problem. Titles and "
                       "descriptions show as a search preview. Under each: the URL (+ how many more) and the fix.",
                       s["intro"])]
    tiles = []
    for key in keys[:MAX_SEO_EVIDENCE_CARDS]:
        group = [key] + merged.get(key, [])
        metas = [_issue_meta(k, seo_issues) for k in group]
        item = next(it for it in seo_ev[key] if os.path.exists(path(it["shot"])))
        full = (item.get("mode") or "") in FULL_WIDTH_MODES
        width = page_w if full else (page_w - GAP) / 2
        tile = _seo_tile(metas, item, path(item["shot"]), width, s, refs, host, group)
        if tile is not None:
            tiles.append((tile, "full" if full else "half"))
    if len(keys) > MAX_SEO_EVIDENCE_CARDS:
        tiles.append((Paragraph(f"Pictures shown for the {MAX_SEO_EVIDENCE_CARDS} most urgent issues; the dashboard "
                                "shows the rest.", s["small"]), "full"))
    return tiles, intro


def _merged_name(metas: List[dict]) -> str:
    """"Page Titles: Over 60 Characters + Over 561 Pixels"; for missing headers "Security: Missing
    Content-Security-Policy, X-Frame-Options headers"."""
    names = [str(m.get("name", "")) for m in metas]
    if len(names) == 1:
        return names[0]
    tab = names[0].split(":", 1)[0]
    parts = [n.split(":", 1)[-1].strip() for n in names]
    if all(p.startswith("Missing ") and p.endswith(" Header") for p in parts):
        return f"{tab}: Missing " + ", ".join(p[len("Missing "):-len(" Header")] for p in parts) + " headers"
    return f"{tab}: " + " + ".join(parts)


def _seo_tile(metas: List[dict], item: dict, shot: str, width: float, s: dict, refs: _Refs, host: str,
              keys: List[tuple]):
    regular, bold = _fonts()
    inner = width - 2 * TILE_PAD
    serp = (item.get("mode") or "") == "serp"
    # a search preview reads well at two thirds of the page: its caption goes beside it
    img = _fit_image(shot, inner * 0.66 if serp else inner, 150 if serp else 200 if width > 300 else 165)
    if img is None:
        return None
    meta = metas[0]
    ty, pr = meta.get("issue_type") or "", meta.get("priority") or ""
    name = _merged_name(metas)
    counts = [int(m["urls"]) for m in metas if m.get("urls") is not None]
    if len(counts) > 1 and len(set(counts)) == 1:
        urls = f"{counts[0]:,} URLs each"
    else:
        urls = (" + ".join(f"{n:,}" for n in counts) + " URLs") if counts else ""
    head = [_Anchor(refs, *[f"pic:{k[0]}/{k[1]}" for k in keys]),
            Paragraph(escape(name), s["tilehead"]),
            Paragraph(escape(" \u00b7 ".join(b for b in (ty, urls) if b)), s["tilemeta"])]
    url = item.get("url") or ""
    page = item.get("page") or url
    total = int(meta.get("urls") or 0)
    more = f" (+{total - 1:,})" if total > 1 else ""
    shown = _link(url, _fit(_short_url(url, host), regular, CELL, inner * 0.6)) if url.startswith("http") else \
        escape(_cut(url, 60))
    caption = shown + (f"<font color='{_hex(MUTED)}'>{more}</font>" if more else "")
    if page and page != url and page.startswith("http"):
        caption += " \u00b7 parent link " + _link(page, _fit(_short_url(page, host), regular, CELL, inner * 0.5))
    fixes = list(dict.fromkeys(f for f in (_short_fix(m) for m in metas) if f))   # merged issues: each fix once
    fix = _cut("; ".join(fixes), 160)
    if serp:
        side = [Paragraph(caption, s["cell"])]
        if fix:
            side += [Spacer(1, 3), Paragraph(f"<font color='{_hex(GOOD)}'><b>Fix:</b></font> {escape(fix)}", s["cell"])]
        row = Table([[img, side]], colWidths=[inner * 0.66 + 8, inner * 0.34 - 8])
        row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        return _tile(head, [row], _type_color(ty), width)
    if fix:
        caption += f" \u00b7 <font color='{_hex(GOOD)}'><b>Fix:</b></font> {escape(fix)}"
    body = [img, Spacer(1, 2), Paragraph(caption, s["cell"])]
    return _tile(head, body, _type_color(ty), width)
