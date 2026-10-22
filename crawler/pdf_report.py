"""
Visual PDF issue report: one summary page (score, category tiles, health bar, top issues,
worst pages) followed by one card per broken URL with cause, fix, link position and screenshots.
"""
import os
import time
from collections import Counter
from typing import Dict, List, Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .issues import CATEGORY_LABELS, ISSUE_CATEGORIES, classify_result, diagnose, issue_sort_key

MAX_DETAILED_ISSUES = 60

INK = colors.HexColor("#181C1A")
MUTED = colors.HexColor("#67716A")
LINE = colors.HexColor("#E8E4DB")
PAPER = colors.HexColor("#FAF9F5")
CAT_COLORS = {
    "2xx": colors.HexColor("#3D794F"),
    "3xx": colors.HexColor("#C2812E"),
    "4xx": colors.HexColor("#9C3345"),
    "5xx": colors.HexColor("#734399"),
    "error": colors.HexColor("#4D5650"),
}
CAT_TINTS = {
    "2xx": colors.HexColor("#E9F4ED"),
    "3xx": colors.HexColor("#FDF5E6"),
    "4xx": colors.HexColor("#FDECEE"),
    "5xx": colors.HexColor("#F5EFFC"),
    "error": colors.HexColor("#EEF0EE"),
}
SEVERITY_COLORS = {
    "critical": colors.HexColor("#734399"),
    "high": colors.HexColor("#9C3345"),
    "medium": colors.HexColor("#C2812E"),
    "low": colors.HexColor("#67716A"),
}
# Regions that are not visible on the rendered page
OFF_PAGE_REGIONS = {"XML sitemap", "Page <head> metadata", "JavaScript bundle", "Inline script"}

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
                _FONTS = ("ReportSans", "ReportSans-Bold")
                break
            except Exception:
                continue
    return _FONTS


def _styles():
    regular, bold = _fonts()
    return {
        "title": ParagraphStyle("title", fontName=bold, fontSize=22, leading=26, textColor=INK),
        "subtitle": ParagraphStyle("subtitle", fontName=regular, fontSize=9.5, leading=13, textColor=MUTED),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=12.5, leading=16, textColor=INK, spaceBefore=4, spaceAfter=6),
        "body": ParagraphStyle("body", fontName=regular, fontSize=8.6, leading=11.5, textColor=INK),
        "small": ParagraphStyle("small", fontName=regular, fontSize=7.6, leading=10, textColor=MUTED),
        "label": ParagraphStyle("label", fontName=bold, fontSize=6.8, leading=9, textColor=MUTED),
        "url": ParagraphStyle("url", fontName=regular, fontSize=7.8, leading=10, textColor=colors.HexColor("#244B30"),
                              wordWrap="CJK"),
        "code": ParagraphStyle("code", fontName="Courier", fontSize=6.8, leading=8.6, textColor=INK, wordWrap="CJK",
                               backColor=PAPER, borderPadding=3),
        "card_title": ParagraphStyle("card_title", fontName=bold, fontSize=10.5, leading=13, textColor=INK),
        "chip": ParagraphStyle("chip", fontName=bold, fontSize=9, leading=11, textColor=colors.white, alignment=TA_CENTER),
        "tile_num": ParagraphStyle("tile_num", fontName=bold, fontSize=17, leading=20, alignment=TA_CENTER),
        "tile_label": ParagraphStyle("tile_label", fontName=regular, fontSize=7, leading=9, textColor=MUTED,
                                     alignment=TA_CENTER),
        "caption": ParagraphStyle("caption", fontName=bold, fontSize=7, leading=9, textColor=MUTED),
    }


def _link(url: str, text: Optional[str] = None, limit: int = 140) -> str:
    shown = text if text is not None else url
    if len(shown) > limit:
        shown = shown[: limit - 1] + "\u2026"
    if not url.startswith(("http://", "https://")):
        return escape(shown)
    return f'<link href="{escape(url, {chr(34): "&quot;"})}">{escape(shown)}</link>'


def _position_text(result, pos: Optional[dict]) -> str:
    region = result.link_region or "Not recorded"
    if pos and pos.get("found"):
        top, height = pos.get("top", 0), max(1, pos.get("page_height", 1))
        pct = min(100, round(top / height * 100))
        extra = " \u00b7 inside a hidden/collapsed menu" if pos.get("hidden") else ""
        return f"{region} \u00b7 {top:,}px from top ({pct}% down the page){extra}"
    if pos is not None and pos.get("found") is False:
        return f"{region} \u00b7 not visible in the rendered page (may be added/removed by JavaScript)"
    return region


def _wireframe(region: str, pos: Optional[dict], width: float, height: float) -> Drawing:
    """Mini page sketch with the link's region highlighted (used when no screenshot exists)."""
    regular, bold = _fonts()
    d = Drawing(width, height)
    d.add(Rect(0, 0, width, height, fillColor=PAPER, strokeColor=LINE, strokeWidth=0.8, rx=4, ry=4))
    if region in OFF_PAGE_REGIONS:
        d.add(String(width / 2, height / 2 + 4, "Not visible on the page", fontName=bold, fontSize=9,
                     fillColor=INK, textAnchor="middle"))
        d.add(String(width / 2, height / 2 - 9, f"Link comes from: {region}", fontName=regular, fontSize=7.5,
                     fillColor=MUTED, textAnchor="middle"))
        return d

    pad = 6
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    hi = colors.HexColor("#F6C2C9")
    base = colors.HexColor("#EBE7DE")
    bands = {  # name: (x, y_from_top, w, h) as fractions of inner box
        "Header": (0, 0, 1, 0.13),
        "Navigation menu": (0, 0.15, 1, 0.07),
        "Breadcrumb": (0, 0.24, 1, 0.04),
        "Main content": (0, 0.30, 0.71, 0.52),
        "Sidebar": (0.73, 0.30, 0.27, 0.52),
        "Footer": (0, 0.85, 1, 0.15),
    }
    target = region if region in bands else ("Main content" if region else None)
    for name, (fx, fy, fw, fh) in bands.items():
        x = pad + fx * inner_w
        y = pad + inner_h - (fy + fh) * inner_h
        is_target = name == target
        d.add(Rect(x, y, fw * inner_w, fh * inner_h, fillColor=hi if is_target else base,
                   strokeColor=CAT_COLORS["4xx"] if is_target else None, strokeWidth=1.2 if is_target else 0,
                   rx=2, ry=2))
        if fh * inner_h > 9:
            d.add(String(x + 4, y + fh * inner_h / 2 - 3, name, fontName=bold if is_target else regular,
                         fontSize=6.5, fillColor=CAT_COLORS["4xx"] if is_target else MUTED))
    if pos and pos.get("found") and pos.get("page_height"):
        frac = min(0.98, max(0.02, pos["top"] / pos["page_height"]))
        y = pad + inner_h - frac * inner_h
        d.add(Rect(pad - 3, y - 2, inner_w + 6, 4, fillColor=CAT_COLORS["4xx"], strokeColor=None))
    return d


def _image_cell(path: Optional[str], width: float, fallback):
    if path and os.path.exists(path):
        try:
            img = Image(path)
            ratio = img.imageHeight / float(img.imageWidth or 1)
            img.drawWidth, img.drawHeight = width, min(width * ratio, width * 0.75)
            return img
        except Exception:
            pass
    return fallback


def _tile(value, label, cat=None):
    s = _styles()
    num_style = ParagraphStyle("n", parent=s["tile_num"], textColor=CAT_COLORS.get(cat, INK))
    return [Paragraph(str(value), num_style), Paragraph(label, s["tile_label"])]


def build_pdf_report(
    results: List,
    filepath: str,
    stats: Optional[dict] = None,
    evidence: Optional[Dict[str, dict]] = None,
    evidence_dir: Optional[str] = None,
    screenshot_note: str = "",
) -> str:
    stats = stats or {}
    evidence = evidence or {}
    s = _styles()
    regular, bold = _fonts()
    page_w = A4[0] - 30 * mm - 12  # frame has 6pt padding on each side

    start_url = stats.get("start_url") or (results[0].url if results else "")
    domain = urlparse(start_url).netloc or start_url

    counts = Counter(classify_result(r) for r in results)
    total = len(results)
    issues = sorted([r for r in results if classify_result(r) in ISSUE_CATEGORIES], key=issue_sort_key)
    healthy = counts["2xx"] + counts["3xx"]
    score = round(healthy / total * 100) if total else 100

    story = []
    story.append(Paragraph("Site Health Report", s["title"]))
    story.append(Paragraph(
        f"{escape(domain)} \u00b7 {time.strftime('%d %b %Y, %H:%M')} \u00b7 "
        f"{total:,} pages checked in {stats.get('elapsed_seconds', 0)}s", s["subtitle"]))
    story.append(Spacer(1, 7 * mm))

    # Score + KPI tiles
    score_cat = "2xx" if score >= 90 else ("3xx" if score >= 70 else "4xx")
    tiles = [
        _tile(f"{score}%", "Health score", score_cat),
        _tile(f"{counts['2xx']:,}", "Healthy", "2xx"),
        _tile(f"{counts['3xx']:,}", "Redirects", "3xx"),
        _tile(f"{counts['4xx']:,}", "Broken (4xx)", "4xx"),
        _tile(f"{counts['5xx']:,}", "Server (5xx)", "5xx"),
        _tile(f"{counts['error']:,}", "Network errors", "error"),
    ]
    tile_table = Table([tiles], colWidths=[page_w / 6] * 6, rowHeights=[17 * mm])
    tile_style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, LINE),
        ("BACKGROUND", (0, 0), (0, 0), CAT_TINTS[score_cat]),
    ]
    tile_table.setStyle(TableStyle(tile_style))
    story.append(tile_table)
    story.append(Spacer(1, 4 * mm))

    # Stacked health bar
    bar = Drawing(page_w, 12 * mm)
    x = 0.0
    for cat in ("2xx", "3xx", "4xx", "5xx", "error"):
        if total and counts[cat]:
            w = page_w * counts[cat] / total
            bar.add(Rect(x, 5 * mm, w, 5 * mm, fillColor=CAT_COLORS[cat], strokeColor=colors.white, strokeWidth=0.5))
            x += w
    if not total:
        bar.add(Rect(0, 5 * mm, page_w, 5 * mm, fillColor=LINE, strokeColor=None))
    lx = 0.0
    for cat in ("2xx", "3xx", "4xx", "5xx", "error"):
        bar.add(Rect(lx, 0.6 * mm, 2.4 * mm, 2.4 * mm, fillColor=CAT_COLORS[cat], strokeColor=None))
        label = f"{CATEGORY_LABELS[cat]} {counts[cat]:,}"
        bar.add(String(lx + 3.5 * mm, 1 * mm, label, fontName=regular, fontSize=7, fillColor=MUTED))
        lx += page_w / 5
    story.append(bar)
    story.append(Spacer(1, 6 * mm))

    if not issues:
        story.append(Paragraph("No broken links or server errors were found. \u2705", s["h2"]))
    else:
        # Top issue types
        type_counts = Counter()
        type_meta = {}
        for r in issues:
            d = diagnose(r)
            key = (d["code_label"], d["title"])
            type_counts[key] += 1
            type_meta[key] = d
        rows = [[Paragraph("ISSUE", s["label"]), Paragraph("STATUS", s["label"]),
                 Paragraph("PAGES", s["label"]), Paragraph("SEVERITY", s["label"])]]
        for (code_label, title), n in type_counts.most_common(8):
            sev = type_meta[(code_label, title)]["severity"]
            rows.append([
                Paragraph(f"<b>{escape(title)}</b>", s["body"]),
                Paragraph(escape(code_label), s["body"]),
                Paragraph(f"<b>{n}</b>", s["body"]),
                Paragraph(f'<font color="{SEVERITY_COLORS[sev].hexval().replace("0x", "#")}"><b>{sev.upper()}</b></font>',
                          s["body"]),
            ])
        # Pages that contain the most broken links
        parent_counts = Counter(r.parent_url for r in issues if (r.parent_url or "").startswith("http"))
        prow = [[Paragraph("PAGE WITH BROKEN LINKS", s["label"]), Paragraph("LINKS", s["label"])]]
        for parent, n in parent_counts.most_common(8):
            prow.append([Paragraph(_link(parent, limit=70), s["url"]), Paragraph(f"<b>{n}</b>", s["body"])])
        if len(prow) == 1:
            prow.append([Paragraph("Broken URLs came from sitemaps or direct input", s["small"]), ""])

        half = (page_w - 6 * mm) / 2
        t1 = Table(rows, colWidths=[half * 0.36, half * 0.27, half * 0.16, half * 0.21])
        t2 = Table(prow, colWidths=[half * 0.84, half * 0.16])
        for t in (t1, t2):
            t.setStyle(TableStyle([
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
                ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ]))
        story.append(Table(
            [[Paragraph("Top issues", s["h2"]), "", Paragraph("Where they are", s["h2"])], [t1, "", t2]],
            colWidths=[half, 6 * mm, half],
            style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]),
        ))

    if screenshot_note:
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph(f"<b>Note:</b> {escape(screenshot_note)}", s["small"]))

    # ---------------- Issue cards ----------------
    if issues:
        story.append(PageBreak())
        detailed = issues[:MAX_DETAILED_ISSUES]
        current_cat = None
        for idx, r in enumerate(detailed, start=1):
            cat = classify_result(r)
            if cat != current_cat:
                current_cat = cat
                story.append(Paragraph(
                    f'<font color="{CAT_COLORS[cat].hexval().replace("0x", "#")}">\u25a0</font> '
                    f"{CATEGORY_LABELS[cat]} \u00b7 {sum(1 for i in issues if classify_result(i) == cat)}", s["h2"]))
            story.append(_issue_card(r, idx, evidence.get(r.url) or {}, evidence_dir, page_w, s))
            story.append(Spacer(1, 5 * mm))

        rest = issues[MAX_DETAILED_ISSUES:]
        if rest:
            story.append(PageBreak())
            story.append(Paragraph(f"More issues ({len(rest):,})", s["h2"]))
            rows = [[Paragraph(h, s["label"]) for h in ("STATUS", "BROKEN URL", "FOUND ON", "LINK TEXT")]]
            for r in rest:
                rows.append([
                    Paragraph(escape(r.http_status or "Error"), s["body"]),
                    Paragraph(_link(r.url, limit=90), s["url"]),
                    Paragraph(_link(r.parent_url or "-", limit=70), s["url"]),
                    Paragraph(escape((r.anchor_text or "-")[:40]), s["small"]),
                ])
            t = Table(rows, colWidths=[page_w * 0.14, page_w * 0.38, page_w * 0.32, page_w * 0.16], repeatRows=1)
            t.setStyle(TableStyle([
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
                ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(t)

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(regular, 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 9 * mm, f"Site Health Report \u00b7 {domain}")
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    doc = SimpleDocTemplate(filepath, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"Site Health Report - {domain}", author="Website Crawler")
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return filepath


def _issue_card(r, idx: int, ev: dict, evidence_dir: Optional[str], page_w: float, s: dict):
    card_w = page_w
    page_w = card_w - 16  # content width inside the card's 8pt side padding
    d = diagnose(r) or {}
    cat = classify_result(r)
    color = CAT_COLORS[cat]
    pos = ev.get("position")

    chip = Table([[Paragraph(escape(str(r.status_code or "ERR")), s["chip"])]], colWidths=[16 * mm],
                 rowHeights=[8 * mm], style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), color),
                                                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    head = Table(
        [[chip, [Paragraph(f"{idx}. {escape(d.get('title', 'Issue'))} "
                           f'<font size="8" color="#67716A">\u00b7 {escape(d.get("severity", "").upper())}</font>',
                           s["card_title"]),
                 Paragraph(_link(r.url), s["url"])]]],
        colWidths=[19 * mm, page_w - 19 * mm],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]),
    )

    why = [Paragraph("WHY", s["label"]), Paragraph(escape(d.get("summary", "")), s["body"]), Spacer(1, 2)]
    for cause in d.get("causes", []):
        why.append(Paragraph(f"\u2022 {escape(cause)}", s["small"]))
    why += [Spacer(1, 4), Paragraph("FIX", s["label"]), Paragraph(f"<b>{escape(d.get('fix', ''))}</b>", s["body"])]

    where = [Paragraph("FOUND ON", s["label"]), Paragraph(_link(r.parent_url or "-"), s["url"]), Spacer(1, 3)]
    if r.anchor_text:
        where += [Paragraph("LINK TEXT", s["label"]), Paragraph(f"\u201c{escape(r.anchor_text[:90])}\u201d", s["body"]),
                  Spacer(1, 3)]
    where += [Paragraph("POSITION", s["label"]), Paragraph(escape(_position_text(r, pos)), s["body"])]
    if r.link_html:
        where += [Spacer(1, 3), Paragraph(escape(r.link_html[:200]), s["code"])]

    col = (page_w - 4 * mm) / 2
    info = Table([[why, "", where]], colWidths=[col, 4 * mm, col],
                 style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    def shot(name):
        return os.path.join(evidence_dir, name) if (evidence_dir and name) else None

    img_w = col - 2
    sketch = _wireframe(r.link_region or "", pos, img_w, img_w * 0.55)
    left = _image_cell(shot(ev.get("parent_shot")), img_w, sketch)
    no_shot = Table([[Paragraph("No screenshot captured", s["small"])]], colWidths=[img_w], rowHeights=[img_w * 0.55],
                    style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), PAPER), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                                      ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    right = _image_cell(shot(ev.get("error_shot")), img_w, no_shot)
    shots = Table(
        [[Paragraph("WHERE THE LINK IS", s["caption"]), "", Paragraph("WHAT VISITORS SEE", s["caption"])],
         [left, "", right]],
        colWidths=[col, 4 * mm, col],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                          ("BOTTOMPADDING", (0, 0), (-1, 0), 2)]),
    )

    card = Table([[head], [info], [shots]], colWidths=[card_w])
    card.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
        ("LINEBEFORE", (0, 0), (0, -1), 3, color),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, LINE),
    ]))
    return KeepTogether(card)
