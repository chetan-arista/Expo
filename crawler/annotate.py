"""
Pinpointed evidence pictures (Pillow) for the PDF and the dashboard.

  draw(image_path, boxes, out_path)          a screenshot with numbered boxes: blue = where it is on the
                                             page (parent), red = the problem; a thin connector joins
                                             them, the rest of the page is lightly dimmed, and the
                                             picture is cropped to the boxes (with context) and scaled
                                             down for the PDF.
  render_source_snippet(snippet, highlight, out_path)   a "view source" panel with the offending tag
                                             boxed (or a dashed line where a missing tag belongs);
                                             also used for response headers and page addresses.
  render_serp_mock(title, url, description, problem, out_path)   a search-result preview with the
                                             title or description boxed and Google's cut-off marked.

A box is {"n": int, "kind": "issue"|"parent", "label": str, "rect": [x, y, w, h]} in pixels of the
input picture. Every function returns the written file's path, or None when it could not draw (it
never raises), and can append the boxes as drawn (pixels of the written picture) to `boxes_out`.

TILE is the profile of pictures the PDF shows half-page wide (about 245 pt): a tighter crop, about 820 px,
JPEG quality 62 and badges / labels drawn larger, so they stay readable at that size.
SOURCE_TILE is the same for view-source and response-header panels (a narrower panel, larger labels).
"""
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Pillow comes with reportlab; without it no pictures are drawn
    Image = ImageDraw = ImageFont = None

ISSUE = (225, 29, 72)          # red: the problem
PARENT = (37, 99, 235)         # blue: where it is
ISSUE_TINT = (253, 226, 230)
PARENT_TINT = (225, 236, 254)
INK = (24, 28, 26)
MUTED = (103, 113, 106)
WHITE = (255, 255, 255)
MAX_WIDTH = 1100               # px width of written pictures (the PDF shows them about 510 pt wide)
MIN_CROP = (820, 400)          # smallest crop around the boxes, so the element keeps its context
MAX_CROP = (1000, 640)         # larger crops are cut down around the problem boxes (readable in the PDF)
JPEG_QUALITY = 70
# Half-page-wide pictures in the PDF: crop closer to the element, write about 820 px, labels about 1.8x larger
TILE = {"min_crop": (680, 380), "max_crop": (840, 540), "max_width": 820, "quality": 62, "unit_scale": 1.8}
SOURCE_TILE = {"width": 600, "scale": 1.0, "label_scale": 1.3}

Box = Dict[str, Any]

_FONT_DIRS = ["C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation",
              "/usr/share/fonts/dejavu", "/Library/Fonts", "/System/Library/Fonts/Supplemental"]
_FONT_FILES = {
    "sans": ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf"],
    "bold": ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf"],
    "mono": ["consola.ttf", "cour.ttf", "DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "Courier New.ttf"],
    "serp": ["arial.ttf", "LiberationSans-Regular.ttf", "Arial.ttf", "DejaVuSans.ttf"],   # results use Arial
}
_REPORTLAB_FONTS = {"sans": "Vera.ttf", "bold": "VeraBd.ttf", "mono": "com_____.pfb", "serp": "Vera.ttf"}


def available() -> bool:
    return Image is not None


@lru_cache(maxsize=64)
def _font(kind: str, size: int):
    """A TrueType font of `kind` (sans, bold, mono): system fonts first, then the fonts that ship
    with reportlab, then Pillow's built-in font."""
    size = max(6, int(size))
    paths = [os.path.join(d, f) for f in _FONT_FILES.get(kind, ()) for d in _FONT_DIRS]
    try:
        import reportlab
        paths.append(os.path.join(os.path.dirname(reportlab.__file__), "fonts", _REPORTLAB_FONTS[kind]))
    except Exception:
        pass
    for path in paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _text_w(font, text: str) -> float:
    try:
        return font.getlength(text)
    except Exception:
        return len(text) * getattr(font, "size", 10) * 0.6


def _fit(font, text: str, width: float) -> str:
    """Cut `text` with an ellipsis so it fits `width` pixels."""
    text = text or ""
    if _text_w(font, text) <= width:
        return text
    while text and _text_w(font, text + "\u2026") > width:
        text = text[:-1]
    return text + "\u2026"


def _wrap(font, text: str, width: float, max_lines: int = 0) -> List[str]:
    """Word-wrap to `width` pixels; words longer than a line are broken."""
    lines: List[str] = []
    cur = ""
    for word in (text or "").split():
        trial = f"{cur} {word}".strip()
        if _text_w(font, trial) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while _text_w(font, word) > width:
            cut = max(1, len(_fit(font, word, width)) - 1)
            lines.append(word[:cut])
            word = word[cut:]
        cur = word
    if cur:
        lines.append(cur)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(font, lines[-1] + " \u2026", width)
    return lines


# --------------------------------------------------------------------------------------------
# Boxes, badges, labels
# --------------------------------------------------------------------------------------------
def _rect(b: Box) -> Optional[Tuple[float, float, float, float]]:
    try:
        x, y, w, h = (float(v) for v in b.get("rect")[:4])
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, x + w, y + h


def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _inside(r, size, pad: float = 0) -> bool:
    return r[0] >= pad and r[1] >= pad and r[2] <= size[0] - pad and r[3] <= size[1] - pad


def _clamp_rect(r, size):
    return (max(0.0, r[0]), max(0.0, r[1]), min(float(size[0]), r[2]), min(float(size[1]), r[3]))


def _dim(img, keep: List[Tuple[float, float, float, float]], alpha: int = 64):
    """Darken everything outside the `keep` rectangles a little."""
    shade = Image.new("L", img.size, alpha)
    sd = ImageDraw.Draw(shade)
    for r in keep:
        sd.rectangle([int(r[0]), int(r[1]), int(r[2]), int(r[3])], fill=0)
    black = Image.new("RGB", img.size, (15, 23, 42))
    img.paste(black, (0, 0), shade)


def annotate_image(img, boxes: Sequence[Box], dim: bool = True, unit: Optional[float] = None,
                   connector: bool = True) -> List[Box]:
    """Draws `boxes` on a PIL image in place (rects in this image's pixels). Returns the boxes with
    their rects as drawn (padded and clipped to the image)."""
    if img is None or not boxes:
        return []
    u = unit or max(0.75, img.width / 1000.0)
    pad = 4 * u
    items = []
    for b in boxes:
        r = _rect(b)
        if r is None:
            continue
        r = _clamp_rect((r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad), img.size)
        if r[2] - r[0] < 2 or r[3] - r[1] < 2:
            continue
        items.append((b, r))
    if not items:
        return []
    parents = [r for b, r in items if b.get("kind") == "parent"]
    issues = [r for b, r in items if b.get("kind") != "parent"]
    if dim:
        _dim(img, parents or issues)

    d = ImageDraw.Draw(img)
    radius = int(6 * u)
    # parents first so a nested problem box stays on top
    ordered = sorted(items, key=lambda it: 0 if it[0].get("kind") == "parent" else 1)
    for b, r in ordered:
        color = PARENT if b.get("kind") == "parent" else ISSUE
        width = max(2, int((3 if b.get("kind") == "parent" else 3.5) * u))
        dashed = bool(b.get("dashed"))
        box = [int(r[0]), int(r[1]), int(r[2]), int(r[3])]
        if dashed:
            _dashed_rect(d, box, color, width, int(10 * u))
        else:
            d.rounded_rectangle(box, radius=radius, outline=WHITE, width=width + 2)
            d.rounded_rectangle(box, radius=radius, outline=color, width=width)

    # Badge + label per box. Thin boxes (a link, a line of source) get them on the right, outside the
    # box; tall boxes (an image, a page area) at the top-left corner. Nothing is placed on another
    # badge or label, and labels avoid the other problem boxes.
    br = 15 * u
    lab_font = _font("bold", int(15 * u))
    num_font = _font("bold", int(17 * u))
    lab_h = 25 * u
    gap = 4 * u
    placed: List[Tuple[float, float, float, float]] = []
    centers: Dict[int, Tuple[float, float]] = {}
    radii: Dict[int, float] = {}
    pills: Dict[int, Tuple[Tuple[float, float, float, float], str]] = {}
    W, H = img.size
    first_issue = next((i for i, (b, _) in enumerate(items) if b.get("kind") != "parent"), -1)

    def fits(rect, others=()) -> bool:
        return _inside(rect, img.size, 1) and not any(_overlaps(rect, p) for p in list(placed) + list(others))

    for i, (b, r) in enumerate(items):
        text = (b.get("label") or "").strip()
        text = _fit(lab_font, text, min(W * 0.6, 440 * u)) if text else ""
        lw = _text_w(lab_font, text) + 14 * u if text else 0
        obstacles = [rr for j, (bb, rr) in enumerate(items) if j != i and bb.get("kind") != "parent"]
        # thin boxes (a link, a line of source) and dashed ones (missing lines) keep their label off the box
        thin = ((r[3] - r[1]) < 2.4 * lab_h or bool(b.get("dashed"))) and b.get("kind") != "parent"
        mid = (r[1] + r[3]) / 2
        tl = (r[0], r[1])
        right = (r[2] + br + gap, mid)
        rad = br
        # (badge centre, where the label goes): "badge_r"/"badge_l" beside the badge, "below"/"above" the box
        if b.get("kind") != "parent" and not text and i != first_issue:
            # more of the same problem: a small badge on a corner, so a row of links stays readable
            rad = br * 0.72
            plans = [((r[2], r[1]), None), ((r[0], r[1]), None), ((r[2], r[3]), None), ((r[0], r[3]), None)]
        elif thin:
            plans = [(right, "badge_r"), (tl, "below"), (tl, "above"), (tl, "badge_r"), (tl, None)]
        else:
            plans = [(tl, "badge_r"), (tl, "below"), ((r[2], r[1]), "badge_l"),
                     ((r[0] + br + gap, r[1] + br + gap), "badge_r"), (tl, None)]
        chosen = None
        for (cx, cy), side in plans:
            cx = min(max(rad + 1, cx), W - rad - 1)
            cy = min(max(rad + 1, cy), H - rad - 1)
            badge = (cx - rad, cy - rad, cx + rad, cy + rad)
            if not fits(badge):
                continue
            if not text or side is None:
                chosen = ((cx, cy), None)
                break
            lx, ly = {"badge_r": (cx + br + gap, cy - lab_h / 2), "badge_l": (cx - br - gap - lw, cy - lab_h / 2),
                      "below": (r[0], r[3] + gap), "above": (r[0], r[1] - lab_h - gap)}[side]
            lx = min(max(2.0, lx), W - lw - 2)
            pill = (lx, ly, lx + lw, ly + lab_h)
            if fits(pill, obstacles) and not _overlaps(pill, badge):
                chosen = ((cx, cy), pill)
                break
        if chosen is None:
            cx = min(max(rad + 1, tl[0]), W - rad - 1)
            cy = min(max(rad + 1, tl[1]), H - rad - 1)
            chosen = ((cx, cy), None)
        (cx, cy), pill = chosen
        centers[i] = (cx, cy)
        radii[i] = rad
        placed.append((cx - rad, cy - rad, cx + rad, cy + rad))
        if pill is not None:
            pills[i] = (pill, text)
            placed.append(pill)

    # Connector from the parent's badge to the first problem box. A problem inside its parent box needs
    # none (the nesting shows where it is, and a line would cross the page text).
    parent_idx = [i for i, (b, _) in enumerate(items) if b.get("kind") == "parent"]
    if parent_idx and connector:
        pr = items[parent_idx[0]][1]
        p = centers[parent_idx[0]]
        for i, (b, r) in enumerate(items):
            if b.get("kind") == "parent":
                continue
            nested = r[0] >= pr[0] - 2 and r[1] >= pr[1] - 2 and r[2] <= pr[2] + 2 and r[3] <= pr[3] + 2
            c = centers[i]
            if not nested and abs(c[0] - p[0]) + abs(c[1] - p[1]) > 3 * br:
                d.line([p, c], fill=WHITE, width=max(2, int(3.5 * u)))
                d.line([p, c], fill=PARENT, width=max(1, int(1.8 * u)))
            break  # one connector keeps the picture readable

    small_font = _font("bold", int(12.5 * u))
    for i, (b, r) in enumerate(items):
        color = PARENT if b.get("kind") == "parent" else ISSUE
        cx, cy = centers[i]
        rad = radii.get(i, br)
        font = num_font if rad >= br else small_font
        d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=color, outline=WHITE, width=max(2, int(2 * u)))
        label = str(b.get("n", i + 1))
        tw = _text_w(font, label)
        d.text((cx - tw / 2, cy - font.size * 0.62), label, font=font, fill=WHITE)
        if i in pills:
            (x, y, x1, y1), text = pills[i]
            d.rounded_rectangle([int(x), int(y), int(x1), int(y1)], radius=int(lab_h / 2), fill=color,
                                outline=WHITE, width=max(1, int(1.5 * u)))
            d.text((x + 7 * u, y + (lab_h - lab_font.size) / 2 - 2 * u), text, font=lab_font, fill=WHITE)

    out = []
    for b, r in items:
        nb = dict(b)
        nb["rect"] = [round(r[0]), round(r[1]), round(r[2] - r[0]), round(r[3] - r[1])]
        out.append(nb)
    return out


def _dashed_rect(d, box, color, width: int, dash: int):
    x0, y0, x1, y1 = box
    for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
        length = max(abs(bx - ax), abs(by - ay))
        steps = max(1, int(length // (dash * 2)) + 1)
        for s in range(steps):
            t0 = s * 2 * dash / max(1, length)
            t1 = min(1.0, (s * 2 * dash + dash) / max(1, length))
            if t0 >= 1:
                break
            d.line([(ax + (bx - ax) * t0, ay + (by - ay) * t0), (ax + (bx - ax) * t1, ay + (by - ay) * t1)],
                   fill=color, width=width)


def _span(lo: float, hi: float, want: float, limit: float, keep_lo: float, keep_hi: float) -> Tuple[float, float]:
    """A window of length `want` inside [0, limit] around [lo, hi] that always holds [keep_lo, keep_hi]."""
    want = min(limit, max(want, keep_hi - keep_lo))
    start = lo if hi - lo > want else (lo + hi) / 2 - want / 2   # a wide parent: start at its edge
    start = min(start, keep_lo)                  # never cut the problem boxes
    start = max(start, keep_hi - want)
    start = min(max(0.0, start), limit - want)
    return start, start + want


def _roi(rects: List[Tuple[float, float, float, float]], size: Tuple[int, int],
         focus: Optional[List[Tuple[float, float, float, float]]] = None,
         min_size: Tuple[int, int] = MIN_CROP, max_size: Tuple[int, int] = MAX_CROP) -> Tuple[int, int, int, int]:
    """The region to keep: every box plus a margin, at least `min_size` (the page around the element is
    context) and at most `max_size` unless the problem boxes (`focus`) need more; a wide parent box
    (a full-width menu) is then cut at the edges rather than shrinking the element."""
    W, H = size
    margin = 48
    focus = focus or rects
    fx0 = min(r[0] for r in focus) - margin
    fy0 = min(r[1] for r in focus) - margin
    fx1 = max(r[2] for r in focus) + margin
    fy1 = max(r[3] for r in focus) + margin
    x0 = min(r[0] for r in rects) - margin
    y0 = min(r[1] for r in rects) - margin
    x1 = max(r[2] for r in rects) + margin
    y1 = max(r[3] for r in rects) + margin
    want_w = min(max(x1 - x0, min_size[0]), max(max_size[0], fx1 - fx0))
    want_h = min(max(y1 - y0, min_size[1], want_w * 0.42), max(max_size[1], fy1 - fy0))
    left, right = _span(x0, x1, want_w, W, max(0, fx0), min(W, fx1))
    top, bottom = _span(y0, y1, want_h, H, max(0, fy0), min(H, fy1))
    return int(left), int(top), int(right), int(bottom)


def _save(img, out_path: str, quality: int = JPEG_QUALITY) -> Optional[str]:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        if out_path.lower().endswith((".jpg", ".jpeg")):
            img.convert("RGB").save(out_path, "JPEG", quality=quality, optimize=True, progressive=True)
        else:
            # flat drawings: a small palette keeps the PNG tiny (older Pillow: plain RGB)
            try:
                small = img.convert("RGB").quantize(colors=96, method=Image.Quantize.MEDIANCUT)
            except Exception:
                small = img.convert("RGB")
            small.save(out_path, "PNG", optimize=True)
        return out_path
    except Exception:
        return None


def draw(image_path: str, boxes: Sequence[Box], out_path: str, dim: bool = True, crop: bool = True,
         max_width: int = MAX_WIDTH, quality: int = JPEG_QUALITY, boxes_out: Optional[list] = None,
         crop_out: Optional[list] = None, min_crop: Tuple[int, int] = MIN_CROP,
         max_crop: Tuple[int, int] = MAX_CROP, unit_scale: float = 1.0) -> Optional[str]:
    """Annotate a screenshot: red rounded boxes for problems, blue for the parent area, numbered
    badges, a connector from parent to problem and a light dim of the rest. With `crop`, keeps the
    region around the boxes (at least `min_crop` pixels, for context; at most `max_crop` unless the
    problem needs more) and scales it to `max_width`. `unit_scale` enlarges badges and labels (pictures
    shown small, see TILE). `crop_out` receives [x, y, w, h, scale] of the region kept, in the input
    picture's pixels."""
    if Image is None:
        return None
    try:
        img = Image.open(image_path)
        img.load()
        img = img.convert("RGB")
    except Exception:
        return None
    rects = [r for r in (_rect(b) for b in boxes or ()) if r is not None]
    left = top = 0
    if crop and rects:
        focus = [r for b, r in ((b, _rect(b)) for b in boxes or ()) if r is not None and b.get("kind") != "parent"]
        left, top, right, bottom = _roi(rects, img.size, focus=focus or None, min_size=min_crop, max_size=max_crop)
        img = img.crop((left, top, right, bottom))
    scale = min(1.0, max_width / float(img.width or 1))
    if scale < 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    moved = []
    for b in boxes or ():
        r = _rect(b)
        if r is None:
            continue
        nb = dict(b)
        nb["rect"] = [(r[0] - left) * scale, (r[1] - top) * scale, (r[2] - r[0]) * scale, (r[3] - r[1]) * scale]
        moved.append(nb)
    drawn = annotate_image(img, moved, dim=dim, unit=max(0.75, img.width / 1000.0) * max(0.5, unit_scale))
    if crop_out is not None:
        crop_out.extend([left, top, round(img.width / scale), round(img.height / scale), round(scale, 4)])
    path = _save(img, out_path, quality)
    if path and boxes_out is not None:
        boxes_out.extend(drawn)
    return path


# --------------------------------------------------------------------------------------------
# "View source" panel
# --------------------------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"(<!--.*?-->)|(</?[A-Za-z][\w:-]*)|(\s[\w:@.-]+)(?==)|(\"[^\"]*\"|'[^']*')|(/?>)", re.S)
_CODE_COLORS = {1: (35, 110, 37), 2: (136, 18, 128), 3: (153, 69, 0), 4: (26, 26, 166), 5: (136, 18, 128)}

Highlight = Union[str, Dict[str, Any]]


def _colour_runs(line: str) -> List[Tuple[int, int, Tuple[int, int, int]]]:
    """(start, end, colour) runs of one source line, view-source style."""
    runs, pos = [], 0
    for m in _TOKEN_RE.finditer(line):
        if m.start() > pos:
            runs.append((pos, m.start(), INK))
        group = next(i for i in range(1, 6) if m.group(i) is not None)
        runs.append((m.start(), m.end(), _CODE_COLORS[group]))
        pos = m.end()
    if pos < len(line):
        runs.append((pos, len(line), INK))
    return runs


def _wrap_chars(font, line: str, width: float, max_rows: int) -> List[Tuple[int, int]]:
    """(start, end) character spans of the rows a long line wraps into."""
    spans, start, acc = [], 0, 0.0
    cache: Dict[str, float] = {}
    for i, ch in enumerate(line):
        w = cache.get(ch)
        if w is None:
            w = cache[ch] = _text_w(font, ch)
        if acc + w > width and i > start:
            spans.append((start, i))
            start, acc = i, 0.0
            if len(spans) >= max_rows:
                return spans
        acc += w
    spans.append((start, len(line)))
    return spans[:max_rows]


def render_source_snippet(html_snippet: Union[str, Sequence[str]], highlight: Union[Highlight, Sequence[Highlight]],
                          out_path: str, title: str = "", line_numbers: Optional[Sequence[Optional[int]]] = None,
                          missing: Union[str, Sequence[str]] = "", missing_at: int = -1, label: str = "",
                          parent_label: str = "",
                          boxes_out: Optional[list] = None, width: int = 1000, max_rows: int = 26,
                          note: str = "", scale: float = 0.0, label_scale: float = 1.0) -> Optional[str]:
    """A view-source panel of `html_snippet` (lines) with the `highlight` boxed in red and numbered.
    highlight: substrings to find, or {"line": i, "start": s, "end": e, "label": str} spans. `missing`
    draws a dashed red line (a tag that should be there) at line index `missing_at` (-1 = before
    </head>, else at the end); a list draws one line each, boxed together. The whole panel is the blue parent box (`parent_label`). `scale` sets the
    text size (default width / 1000, so a narrower panel keeps 15 px code with scale=1.0) and
    `label_scale` enlarges the badges and labels (see SOURCE_TILE)."""
    if Image is None:
        return None
    try:
        lines = html_snippet.splitlines() if isinstance(html_snippet, str) else [str(x) for x in html_snippet]
        lines = [ln.replace("\t", "  ").rstrip() for ln in lines] or [""]
        ghosts = [missing] if isinstance(missing, str) else [str(m) for m in missing or ()]
        ghosts = [g for g in ghosts if g]
        missing = ghosts[0] if ghosts else ""
        hls = highlight if isinstance(highlight, (list, tuple)) else ([highlight] if highlight else [])
        nums = list(line_numbers) if line_numbers else list(range(1, len(lines) + 1))
        nums += [None] * (len(lines) - len(nums))

        u = scale or width / 1000.0
        lu = u * max(0.5, label_scale)            # size unit of the badges and labels
        mono = _font("mono", int(15 * u))
        small = _font("sans", int(13 * u))
        row_h = int(23 * u)
        gutter = int(70 * u)
        code_x = gutter + int(12 * u)
        code_w = width - code_x - int(18 * u)
        # room for the parent's label on the panel's top edge and for a problem label under the last line,
        # so neither covers the code: the panel starts at panel_top, its first line `pad_top` lower
        pad_top = int(13 * lu) if parent_label else 0
        panel_top = int(54 * u)
        top = panel_top + pad_top
        below = int(32 * lu)

        # Spans to box: (line index, start, end, label)
        spans: List[Tuple[int, int, int, str]] = []
        for h in hls:
            if isinstance(h, dict):
                li = int(h.get("line", -1))
                if 0 <= li < len(lines):
                    s = max(0, int(h.get("start", 0)))
                    e = min(len(lines[li]), int(h.get("end", len(lines[li]))))
                    if e > s:
                        spans.append((li, s, e, str(h.get("label") or "")))
                continue
            needle = str(h)
            for li, ln in enumerate(lines):
                k = ln.find(needle)
                if k < 0:
                    k = ln.lower().find(needle.lower())
                if k >= 0 and needle:
                    spans.append((li, k, k + len(needle), ""))
                    break

        # Rows after wrapping: highlighted lines may take up to 4 rows, others 1 (cut with an ellipsis)
        hl_lines = {s[0] for s in spans}
        if missing and missing_at < 0:
            missing_at = next((i for i, ln in enumerate(lines) if "</head" in ln.lower()), len(lines))
        missing_at = min(max(0, missing_at), len(lines)) if missing else -1
        rows: List[Tuple[Optional[int], int, int, int]] = []   # (line index or None for the ghost line, start, end, first)
        for li, ln in enumerate(lines):
            if li == missing_at:
                rows += [(None, gi, 0, 1) for gi in range(len(ghosts))]
            parts = _wrap_chars(mono, ln, code_w, 4 if li in hl_lines else 1)
            for pi, (s, e) in enumerate(parts):
                rows.append((li, s, e, 1 if pi == 0 else 0))
        if missing and missing_at >= len(lines):
            rows += [(None, gi, 0, 1) for gi in range(len(ghosts))]
        if len(rows) > max_rows:
            # keep the rows around the first highlight (or the ghost line)
            focus = next((i for i, r in enumerate(rows) if r[0] is None or r[0] in hl_lines), 0)
            first = max(0, min(focus - max_rows // 3, len(rows) - max_rows))
            rows = rows[first:first + max_rows]

        note_rows = _wrap(small, note, width - 40 * u, 2) if note else []
        height = top + len(rows) * row_h + max(int(26 * u), below) + len(note_rows) * int(18 * u) + int(10 * u)
        img = Image.new("RGB", (width, height), WHITE)
        d = ImageDraw.Draw(img)

        # Window bar with the address
        d.rectangle([0, 0, width, int(40 * u)], fill=(239, 236, 228))
        for i, c in enumerate(((224, 100, 92), (229, 179, 60), (92, 184, 92))):
            cx = int((18 + i * 16) * u)
            d.ellipse([cx - 5 * u, 20 * u - 5 * u, cx + 5 * u, 20 * u + 5 * u], fill=c)
        pill = [int(76 * u), int(8 * u), width - int(14 * u), int(32 * u)]
        d.rounded_rectangle(pill, radius=int(12 * u), fill=WHITE, outline=(227, 222, 211))
        d.text((pill[0] + 12 * u, pill[1] + 4 * u), _fit(small, title or "view-source", pill[2] - pill[0] - 24 * u),
               font=small, fill=MUTED)
        d.rectangle([0, panel_top - int(6 * u), gutter, top + len(rows) * row_h + int(6 * u)], fill=(247, 245, 239))

        span_rects: List[Tuple[Tuple[float, float, float, float], str]] = []
        ghost_rect = None
        cw: Dict[str, float] = {}

        def x_of(text: str) -> float:
            return sum(cw.setdefault(ch, _text_w(mono, ch)) for ch in text)

        for ri, (li, s, e, first) in enumerate(rows):
            y = top + ri * row_h
            if li is None:
                ghost = "+ " + ghosts[s]
                text = _fit(mono, ghost, code_w)
                gw = x_of(text)
                one = (code_x - 4 * u, y, code_x + gw + 6 * u, y + row_h - 2 * u)
                d.rectangle(one, fill=ISSUE_TINT)
                ghost_rect = one if ghost_rect is None else (min(ghost_rect[0], one[0]), min(ghost_rect[1], one[1]),
                                                             max(ghost_rect[2], one[2]), max(ghost_rect[3], one[3]))
                d.text((code_x, y + 3 * u), text, font=mono, fill=ISSUE)
                continue
            ln = lines[li]
            if first and nums[li] is not None:
                num = str(nums[li])
                d.text((gutter - 8 * u - _text_w(small, num), y + 4 * u), num, font=small, fill=MUTED)
            cut = e < len(ln) and li not in hl_lines
            # highlight fills behind the text
            for (sl, ss, se, _) in spans:
                if sl != li or se <= s or ss >= e:
                    continue
                a, b = max(ss, s), min(se, e)
                x0 = code_x + x_of(ln[s:a])
                x1 = code_x + x_of(ln[s:b])
                d.rectangle([x0 - 2 * u, y + 1 * u, x1 + 2 * u, y + row_h - 2 * u], fill=ISSUE_TINT)
            x = code_x
            for (rs, re_, colour) in _colour_runs(ln):
                a, b = max(rs, s), min(re_, e)
                if b <= a:
                    continue
                piece = ln[a:b]
                d.text((x, y + 3 * u), piece, font=mono, fill=colour)
                x += x_of(piece)
            if cut:
                d.text((x + 2 * u, y + 3 * u), "\u2026", font=mono, fill=MUTED)

        # Problem rectangles per highlight span (union of its rows)
        for (sl, ss, se, lab) in spans:
            xs, ys = [], []
            for ri, (li, s, e, _) in enumerate(rows):
                if li != sl or se <= s or ss >= e:
                    continue
                a, b = max(ss, s), min(se, e)
                y = top + ri * row_h
                xs += [code_x + x_of(lines[li][s:a]), code_x + x_of(lines[li][s:b])]
                ys += [y, y + row_h - 2 * u]
            if xs:
                span_rects.append(((min(xs) - 3 * u, min(ys), max(xs) + 3 * u, max(ys)), lab))

        if note_rows:
            ny = top + len(rows) * row_h + max(int(16 * u), below - int(10 * u))
            for i, nl in enumerate(note_rows):
                d.text((20 * u, ny + i * 18 * u), nl, font=small, fill=MUTED)

        boxes: List[Box] = []
        n = 1
        if parent_label is not None:
            panel = (gutter + 2 * u, panel_top - 4 * u, width - 8 * u, top + len(rows) * row_h)
            boxes.append({"n": n, "kind": "parent", "label": parent_label,
                          "rect": [panel[0], panel[1], panel[2] - panel[0], panel[3] - panel[1]]})
            n += 1
        if ghost_rect is not None:
            boxes.append({"n": n, "kind": "issue", "label": label or "missing", "dashed": True,
                          "rect": [ghost_rect[0], ghost_rect[1], ghost_rect[2] - ghost_rect[0],
                                   ghost_rect[3] - ghost_rect[1]]})
            n += 1
        for i, (r, lab) in enumerate(span_rects[:8]):
            boxes.append({"n": n, "kind": "issue", "label": lab or (label if i == 0 else ""),
                          "rect": [r[0], r[1], r[2] - r[0], r[3] - r[1]]})
            n += 1
        drawn = annotate_image(img, boxes, dim=False, unit=lu, connector=False)
        path = _save(img, out_path)
        if path and boxes_out is not None:
            boxes_out.extend(drawn)
        return path
    except Exception:
        return None


# --------------------------------------------------------------------------------------------
# Search result preview
# --------------------------------------------------------------------------------------------
def _cut_at(text: str, limit_px: float, measure) -> int:
    """Index where `text` passes `limit_px` (len(text) when it fits)."""
    for i in range(1, len(text) + 1):
        if measure(text[:i]) > limit_px:
            return i - 1
    return len(text)


def _px(text: str, kind: str) -> float:
    try:
        from .seo.text_metrics import pixel_width
        return float(pixel_width(text, kind))
    except Exception:
        return len(text) * (9.0 if kind == "title" else 6.5)


def render_serp_mock(title: str, url: str, description: str, problem: str, out_path: str,
                     focus: str = "title", others: Optional[Sequence[Tuple[str, str, str]]] = None,
                     title_limit_px: int = 561, desc_limit_px: int = 985, parent_label: str = "",
                     boxes_out: Optional[list] = None, short_limit_px: int = 0,
                     label_scale: float = 1.0) -> Optional[str]:
    """A desktop search-result preview of one page, with the title (focus="title") or the description
    (focus="description") boxed in red and labelled `problem`. Text past Google's cut-off is marked.
    `others`: more results (title, url, description) shown below with their titles boxed too
    (duplicates). `short_limit_px` shades the unused room of a too-short title or description.
    `label_scale` enlarges the badges and labels."""
    if Image is None:
        return None
    try:
        s = 1.4                                    # 1 px of a real result = 1.4 px here
        W = 1000
        col = int(600 * s)
        x0 = int(40)
        f_site = _font("serp", int(14 * s))
        f_crumb = _font("serp", int(12 * s))
        f_title = _font("serp", int(20 * s))
        f_desc = _font("serp", int(14 * s))
        f_note = _font("sans", int(10.5 * s))
        img = Image.new("RGB", (W, 1600), WHITE)
        d = ImageDraw.Draw(img)
        boxes: List[Box] = []

        def result(t: str, u_: str, desc: str, y: int, main: bool) -> Tuple[int, Tuple[float, float, float, float]]:
            from urllib.parse import urlparse
            p = urlparse(u_ or "")
            host = p.netloc or u_ or ""
            # favicon, site name, breadcrumb
            r_ = int(15 * s)
            d.ellipse([x0, y + 2, x0 + 2 * r_, y + 2 + 2 * r_], fill=(241, 243, 244), outline=(218, 220, 224))
            letter = (host.replace("www.", "")[:1] or "?").upper()
            d.text((x0 + r_ - _text_w(f_site, letter) / 2, y + 2 + r_ - f_site.size * 0.6), letter, font=f_site,
                   fill=(95, 99, 104))
            tx = x0 + 2 * r_ + 14
            d.text((tx, y), _fit(f_site, host.replace("www.", ""), col - 60), font=f_site, fill=(32, 33, 36))
            crumbs = " \u203a ".join([f"{p.scheme or 'https'}://{host}"] + [c for c in (p.path or "").split("/") if c][:3])
            d.text((tx, y + int(20 * s)), _fit(f_crumb, crumbs, col - 60), font=f_crumb, fill=(77, 81, 86))
            y += int(48 * s)
            block_top = y - int(48 * s)

            # title
            title_top = y
            t = " ".join((t or "").split())
            if not t:
                shown, rest = "", ""
                d.text((x0, y), "(no title: Google writes one from the page text)", font=f_title,
                       fill=(154, 160, 166))
                title_right = x0 + _text_w(f_title, "(no title: Google writes one from the page text)")
            else:
                k = _cut_at(t, title_limit_px, lambda x: _px(x, "title"))
                if k < len(t):
                    shown = t[:k].rstrip()
                    rest = t[k:].strip()
                    # Google drops the last partial word and adds an ellipsis
                    if " " in shown:
                        shown = shown[:shown.rfind(" ")]
                        rest = t[len(shown):].strip()
                    shown += " \u2026"
                else:
                    shown, rest = t, ""
                d.text((x0, y), shown, font=f_title, fill=(26, 13, 171))
                title_right = x0 + _text_w(f_title, shown)
                if short_limit_px and focus == "title" and main:
                    room = x0 + short_limit_px * s
                    if room > title_right + 10:
                        _dashed_rect(d, [int(title_right + 8), y + 4, int(room), y + int(26 * s)], (46, 125, 50), 2, 8)
                        d.text((title_right + 16, y + int(7 * s)), "unused room", font=f_note, fill=(46, 125, 50))
                        title_right = room
            y += int(30 * s)
            if rest and main and focus == "title":
                for i, line in enumerate(_wrap(f_note, "Not shown: \u201c" + rest + "\u201d", col, 2)):
                    d.text((x0, y + i * int(15 * s)), line, font=f_note, fill=ISSUE)
                y += int(32 * s)
            title_box = (x0 - 6, title_top - 4, max(title_right, x0 + 120) + 6, y - 4)

            # description
            y += 10
            desc_top = y + 2
            dd = " ".join((desc or "").split())
            if not dd:
                d.text((x0, y + 2), "(no meta description: Google picks text from the page)", font=f_desc,
                       fill=(154, 160, 166))
                y += int(24 * s)
                desc_right = x0 + _text_w(f_desc, "(no meta description: Google picks text from the page)")
                drest = ""
            else:
                k = _cut_at(dd, desc_limit_px, lambda x: _px(x, "description"))
                shown_d, drest = (dd, "") if k >= len(dd) else (dd[:k].rstrip(), dd[k:].strip())
                if drest and " " in shown_d:
                    shown_d = shown_d[:shown_d.rfind(" ")]
                    drest = dd[len(shown_d):].strip()
                lines = _wrap(f_desc, shown_d + (" \u2026" if drest else ""), col)
                desc_right = x0
                for i, line in enumerate(lines[:4]):
                    d.text((x0, y + 2 + i * int(22 * s)), line, font=f_desc, fill=(77, 81, 86))
                    desc_right = max(desc_right, x0 + _text_w(f_desc, line))
                y += 2 + len(lines[:4]) * int(22 * s)
                if short_limit_px and focus == "description" and main:
                    room = desc_limit_px * s
                    used = _px(shown_d, "description") * s
                    if room - used > 40:
                        last_w = _text_w(f_desc, lines[-1]) if lines else 0
                        yy = y - int(22 * s)
                        end_x = min(x0 + col, x0 + last_w + (room - used))
                        _dashed_rect(d, [int(x0 + last_w + 8), yy + 2, int(end_x), yy + int(20 * s)], (46, 125, 50), 2, 8)
                        d.text((x0 + last_w + 16, yy + int(4 * s)), "unused room", font=f_note, fill=(46, 125, 50))
                        desc_right = max(desc_right, end_x)
            if drest and main and focus == "description":
                for i, line in enumerate(_wrap(f_note, "Not shown: \u201c" + drest + "\u201d", col, 2)):
                    d.text((x0, y + 4 + i * int(15 * s)), line, font=f_note, fill=ISSUE)
                y += int(34 * s)
            desc_box = (x0 - 6, desc_top - 4, max(desc_right, x0 + 120) + 6, y + 2)
            block = (x0 - 14, block_top - 10, x0 + col + 14, y + 10)
            return y + int(26 * s), (title_box if focus != "description" else desc_box) + (block,)

        lift = int(14 * max(1.0, label_scale))     # room for the parent's label on the result's top edge
        y = 46 + lift
        y, (bx0, by0, bx1, by1, block) = result(title, url, description, y, True)
        block = (block[0], block[1] - lift, block[2], block[3])
        boxes.append({"n": 1, "kind": "parent", "label": parent_label,
                      "rect": [block[0], block[1], block[2] - block[0], block[3] - block[1]]})
        boxes.append({"n": 2, "kind": "issue", "label": problem, "rect": [bx0, by0, bx1 - bx0, by1 - by0]})
        nxt = 3
        for (t2, u2, d2) in list(others or [])[:2]:
            y += 4
            y, (cx0, cy0, cx1, cy1, _) = result(t2, u2, d2, y, False)
            from urllib.parse import urlparse
            boxes.append({"n": nxt, "kind": "issue", "label": "same text on " + (urlparse(u2).path or "/"),
                          "rect": [cx0, cy0, cx1 - cx0, cy1 - cy0]})
            nxt += 1
        # room under the last result for a problem label that cannot sit beside its box
        img = img.crop((0, 0, W, min(img.height, int(y + 10 + 24 * max(1.0, label_scale)))))
        drawn = annotate_image(img, boxes, dim=False, unit=max(0.5, label_scale))
        path = _save(img, out_path)
        if path and boxes_out is not None:
            boxes_out.extend(drawn)
        return path
    except Exception:
        return None
