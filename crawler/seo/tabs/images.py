"""
Images tab (Screaming Frog "Images"): every image URL of the site, with how the pages use it.

Rows: internal URLs the crawl requested that answered with an image Content-Type (image/*), the
same URLs the Internal tab files under Images, then every other <img src> seen on a crawled page
that was not requested itself (external images, or internal ones when resources are not crawled).
As in Screaming Frog, the Content-Type decides: a .avif that sent no Content-Type is "Other" and a
404 HTML page at a .png address is HTML, so neither is an Images row (the reference lists 213 =
209 internal + 4 external). An external image that the link checker requested is shown as that
ExternalLink when it answered with an image type; one never checked is an ImageRef with no status
("not checked").

The alt and size filters are about the <img> elements on pages (2xx HTML pages only): an image
counts when ANY page uses it without alt text / alt attribute / width and height, as Screaming
Frog's "Image Details" view shows per page. Over 100 KB needs the image's size, from the body, the
Content-Length header or the transferred bytes; for an external image, from the Content-Length the
link checker recorded (ExternalLink.size_bytes). When no size could be known (images not requested, or no
size sent) and nothing was flagged, Over 100 kB is "Not checked" with the reason (size_skip_reason), never 0.

srcset and <picture> (P11): every candidate file of an <img> (its src, each srcset candidate and the srcset of the
<source> elements of its <picture>) is an image entry of its own (PageData images[] "from" src | srcset | picture |
rendered, "element" = the <img>), listed as a row and requested when resources are crawled, so size, status and
format count each candidate. Alt text, width and height belong to the <img> element: those filters read one entry
per element (primary_images: its src or rendered entry, else its first candidate), so an <img> without alt and with
three srcset candidates counts once.
CDN hosts (CrawlConfig.cdn_hosts): images on those hosts are the site's own (internal rows, requested and checked).

IMG Inlinks counts every <img> that loads the image, like Screaming Frog: a logo shown twice on one
page counts twice. "Missing Alt On (Pages)" counts distinct pages.

Background Images: images a page shows through CSS (background / background-image url(...)) found in
style="" attributes and <style> blocks of the HTML (PageData background_images) and, for pages rendered in
Chrome, the backgrounds applied after the CSS and scripts ran (rendered["background_images"]). Background
images that no <img> uses are listed as rows too.
Incorrectly Sized Images: from pages rendered in Chrome (rendered["img_sizes"]): an image whose file holds
far more pixels than it is shown at (more than 25 % of its pixels never displayed, and at least 4 KB to save
when the file size is known; Lighthouse's "properly size images" idea). Needs JavaScript rendering
(Configuration > Rendering); images that appear only after rendering are listed as rows too.
"""
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ...url_utils import get_domain, normalize_url
from ..registry import Column, Filter, Tab, pref_number, register, rendered_content, seo, threshold_filter
from .internal import (ADDRESS, CONTENT_TYPE, INDEXABILITY, INDEXABILITY_STATUS, category_of, content_kind,
                       external_checked, header, internal_rows, is_internal_url, memo)

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")
MAX_IMAGE_BYTES = 100 * 1024      # default "Over 100 kB" (Preferences image_max_kb, P14)
MAX_ALT_CHARS = 100               # default "Alt Text Over 100 Characters" (Preferences alt_max_chars)
OVERSIZED_PIXELS_PCT = 25.0       # share of an image's pixels never shown above which it is incorrectly sized
OVERSIZED_MIN_BYTES = 4096        # ... when the file size is known, at least this much to save


@dataclass
class ImageRef:
    """An <img src> seen on a page but never requested. Shaped like an unchecked ExternalLink
    (url + domain, no status), so the shared helpers give it category "" ("not checked")."""
    url: str
    domain: str = ""
    internal: bool = False
    status_code: Optional[int] = None
    http_status: str = ""
    content_type: str = ""


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------
def _extension(url: str) -> str:
    try:
        return os.path.splitext(urlparse(url).path.lower())[1]
    except Exception:
        return ""


def is_image_result(r) -> bool:
    """A requested URL that answered with an image Content-Type (Internal > Images)."""
    return content_kind(r) == "images"


def _listed(r) -> bool:
    """An <img src> found on a page belongs in the tab when it answered as an image or was never
    requested (status unknown); a requested URL that answered anything else does not."""
    return is_image_result(r) or category_of(r) == ""


CANDIDATE_FROM = ("srcset", "picture")      # entries that are another file of an <img>, not the element itself


def primary_images(images: Any) -> List[dict]:
    """One image entry per <img> element of a page (PageData images[]): the element's src (or rendered) entry,
    else its first entry (an <img> with only a srcset). Entries without "element" (pages read before P11) are
    each their own element."""
    chosen: Dict[Any, dict] = {}
    order: List[Any] = []
    for i, img in enumerate(images or ()):
        if not isinstance(img, dict):
            continue
        el = img.get("element")
        key = el if isinstance(el, int) else ("entry", i)
        cur = chosen.get(key)
        if cur is None:
            chosen[key] = img
            order.append(key)
        elif cur.get("from") in CANDIDATE_FROM and img.get("from") not in CANDIDATE_FROM:
            chosen[key] = img
    return [chosen[k] for k in order]


def _occurrences(site, primary: bool = False) -> Dict[str, List[Tuple[str, dict]]]:
    """image src -> [(page url, <img> dict)] over every 2xx HTML page, in crawl/document order; `primary`: one entry
    per <img> element only (primary_images)."""
    out: Dict[str, List[Tuple[str, dict]]] = {}
    for page in site.pages:
        if category_of(page) != "2xx":
            continue
        images = seo(page).get("images") or ()
        for img in (primary_images(images) if primary else images):
            try:
                src = img.get("src") or ""
            except Exception:
                continue
            if src:
                out.setdefault(src, []).append((page.url, img))
    return out


def occurrences(site) -> Dict[str, List[Tuple[str, dict]]]:
    return memo(site, "img_occurrences", lambda: _occurrences(site))


def element_occurrences(site) -> Dict[str, List[Tuple[str, dict]]]:
    """occurrences() with one entry per <img> element (alt text, width and height are the element's)."""
    return memo(site, "img_element_occurrences", lambda: _occurrences(site, primary=True))


def _norm(url: str) -> str:
    try:
        return normalize_url(url) or url
    except Exception:
        return url


def _backgrounds(site) -> Dict[str, List[str]]:
    """background image url -> pages (2xx HTML) that show it through CSS, from the HTML and, for rendered
    pages, from Chrome's computed styles."""
    out: Dict[str, List[str]] = {}
    for page in site.pages:
        if category_of(page) != "2xx":
            continue
        urls = list(seo(page).get("background_images") or [])
        rd = rendered_content(page)   # not the destination of a redirect that fired in Chrome
        if rd is not None:
            urls += [_norm(u) for u in rd.get("background_images") or [] if isinstance(u, str)]
        for u in urls:
            if isinstance(u, str) and u and not u.startswith("data:"):
                lst = out.setdefault(u, [])
                if page.url not in lst:
                    lst.append(page.url)
    return out


def backgrounds(site) -> Dict[str, List[str]]:
    return memo(site, "img_backgrounds", lambda: _backgrounds(site))


def _rendered_sizes(site) -> Dict[str, List[Tuple[str, dict]]]:
    """image url -> [(page url, {"natural_w", "natural_h", "display_w", "display_h", "wasted_pct"})] from the
    pages rendered in Chrome."""
    out: Dict[str, List[Tuple[str, dict]]] = {}
    for page in site.pages:
        rd = rendered_content(page)
        if rd is None or category_of(page) != "2xx":
            continue
        for e in rd.get("img_sizes") or ():
            src = e.get("src") if isinstance(e, dict) else None
            if src and not str(src).startswith("data:"):
                out.setdefault(_norm(src), []).append((page.url, e))
    return out


def rendered_sizes(site) -> Dict[str, List[Tuple[str, dict]]]:
    return memo(site, "img_rendered_sizes", lambda: _rendered_sizes(site))


def occurrences_of(site, r) -> List[Tuple[str, dict]]:
    return occurrences(site).get(getattr(r, "url", ""), [])


def element_occurrences_of(site, r) -> List[Tuple[str, dict]]:
    return element_occurrences(site).get(getattr(r, "url", ""), [])


def _external_by_url(site) -> Dict[str, Any]:
    """url -> ExternalLink, preferring one the link checker requested."""
    out: Dict[str, Any] = {}
    for e in site.externals or ():
        u = getattr(e, "url", "")
        if u and (u not in out or (external_checked(e) and not external_checked(out[u]))):
            out[u] = e
    return out


def _image_rows(site) -> List[Any]:
    rows: List[Any] = []
    seen = set()
    for r in internal_rows(site):
        if is_image_result(r):
            rows.append(r)
            seen.add(r.url)
    externals = None
    extra = [u for u in list(backgrounds(site)) + list(rendered_sizes(site)) if u not in occurrences(site)]
    for src in list(occurrences(site)) + list(dict.fromkeys(extra)):
        if src in seen:
            continue
        seen.add(src)
        r = site.by_url.get(src)
        if r is None:
            try:
                r = site.by_url.get(normalize_url(src) or "")
            except Exception:
                r = None
        if r is None:
            if externals is None:
                externals = _external_by_url(site)
            r = externals.get(src)
            if r is None:
                try:
                    r = externals.get(normalize_url(src) or "")
                except Exception:
                    r = None
        if r is None:
            try:
                domain = get_domain(src) or ""
            except Exception:
                domain = ""
            r = ImageRef(url=src, domain=domain, internal=is_internal_url(site, src))
        if getattr(r, "url", "") in seen and r.url != src:
            continue  # src normalized onto a URL already listed
        seen.add(getattr(r, "url", src))
        if _listed(r):
            rows.append(r)
    return rows


def image_rows(site) -> List[Any]:
    return memo(site, "image_rows", lambda: _image_rows(site))


# ---------------------------------------------------------------------------------------------
# Cells and tests
# ---------------------------------------------------------------------------------------------
def image_size(r) -> Optional[int]:
    """Bytes of the image: body length, else transferred bytes, else the Content-Length header, else
    the size the external link checker recorded (None if unknown)."""
    p = seo(r)
    for v in (p.get("size_bytes"), p.get("transferred_bytes")):
        try:
            if v and int(v) > 0:
                return int(v)
        except Exception:
            continue
    cl = header(r, "content-length").strip()
    if cl.isdigit():
        return int(cl)
    v = getattr(r, "size_bytes", None)
    return int(v) if isinstance(v, int) and v > 0 else None


def pages_using(site, r) -> int:
    """Distinct pages that show the image."""
    return len({page for page, _ in occurrences_of(site, r)})


def img_inlinks(site, r) -> int:
    """Every <img> that loads the image, repeats on one page included (Screaming Frog's IMG Inlinks)."""
    return len(occurrences_of(site, r))


def _alt(img: dict):
    alt = img.get("alt")
    return None if alt is None else str(alt).strip()


def _pages_where(site, r, test) -> int:
    return len({page for page, img in element_occurrences_of(site, r) if test(img)})


def missing_alt_pages(site, r) -> int:
    """Pages that use the image (as an <img> element's own file) with no alt attribute or an empty one."""
    return _pages_where(site, r, lambda img: not _alt(img))


def _any_img(test):
    """A test on the <img> elements that show this file as their own (one entry per element, P11)."""
    return lambda r, site: any(test(img) for _, img in element_occurrences_of(site, r))


def background_pages(site, r) -> List[str]:
    return list(backgrounds(site).get(getattr(r, "url", ""), []))


def size_entries(site, r) -> List[Tuple[str, dict]]:
    return rendered_sizes(site).get(getattr(r, "url", ""), [])


def _wasted(e: dict) -> float:
    try:
        return float(e.get("wasted_pct") or 0)
    except (TypeError, ValueError):
        return 0.0


def worst_size(site, r) -> Optional[dict]:
    """The rendered use of the image that wastes the largest share of its pixels."""
    entries = size_entries(site, r)
    return max((e for _, e in entries), key=_wasted) if entries else None


def _incorrectly_sized(r, site) -> bool:
    e = worst_size(site, r)
    if e is None or _wasted(e) <= OVERSIZED_PIXELS_PCT:
        return False
    size = image_size(r)
    return size is None or size * _wasted(e) / 100.0 >= OVERSIZED_MIN_BYTES


def _px(e: Optional[dict], w: str, h: str) -> str:
    if not e or not e.get(w) or not e.get(h):
        return ""
    return f"{int(e.get(w))} x {int(e.get(h))}"


def max_image_bytes(site) -> int:
    """The image size limit of this crawl in bytes (Preferences image_max_kb, default 100 kB)."""
    try:
        return int(float(pref_number(site, "image_max_kb")) * 1024)
    except (TypeError, ValueError):
        return MAX_IMAGE_BYTES


def _over_100_kb(r, site) -> bool:
    size = image_size(r)
    return size is not None and size > max_image_bytes(site) and category_of(r) == "2xx"


def size_skip_reason(site) -> str:
    """Why Over 100 kB could not be checked ("" when it could): no image of the crawl has a known size because the
    site's images were not requested (resource crawling off) or none sent a size. A crawl that knows some sizes is
    judged on those (intel/checks.py adds the caveat); a crawl where the site's own images were not requested is
    never "none found" (P11)."""
    rows = image_rows(site)
    if not rows or any(_over_100_kb(r, site) for r in rows):
        return ""                 # nothing to check, or something was found (judged with a caveat)
    cfg = getattr(site, "config", None)
    resources_on = bool(getattr(cfg, "crawl_resources", True)) if cfg is not None else True
    unrequested = [r for r in rows if category_of(r) == ""]
    own_unrequested = sum(1 for r in unrequested if is_internal_url(site, getattr(r, "url", "")))
    sized = sum(1 for r in rows if category_of(r) == "2xx" and image_size(r) is not None)
    if own_unrequested and not resources_on:
        known = f" Only {sized:,} image{'s' if sized != 1 else ''} on other sites had a known size." if sized else ""
        return (f"{own_unrequested:,} image{'s' if own_unrequested != 1 else ''} of this site "
                f"{'were' if own_unrequested != 1 else 'was'} not requested (resource crawling was off), so "
                f"{'their' if own_unrequested != 1 else 'its'} file size is unknown.{known}")
    if sized:
        return ""
    if unrequested:
        return ("No image was requested (resource crawling or the external link check was off), so no image file "
                "size is known.")
    if any(category_of(r) == "2xx" for r in rows):
        return "The images that loaded sent no file size (no body and no Content-Length header), so sizes are unknown."
    return ""


def _skip_reason(site, filter_id: str) -> str:
    if filter_id == "over_100_kb":
        return size_skip_reason(site)
    return ""


FILTERS = [
    threshold_filter("over_100_kb", "image_max_kb", "Over {n} kB", _over_100_kb,
                     issue_type="Opportunity", priority="Medium",
                     description="The image file is larger than {n} KB. Big images are one of the most common "
                                 "reasons pages load slowly, especially on phones. {n} KB is a rule of thumb, not "
                                 "a hard limit.",
                     fix="Compress the image, resize it to the largest size it is actually shown at, and use a "
                         "modern format such as WebP or AVIF."),
    Filter("missing_alt_text", "Missing Alt Text", _any_img(lambda img: _alt(img) == ""),
           issue_type="Issue", priority="Low",
           description="At least one page shows this image with an alt attribute that is empty "
                       "(alt=\"\"). Screen readers then skip it and search engines get no description. "
                       "That is right for purely decorative images, but not for images that carry "
                       "meaning. Open the image to see which pages use it this way.",
           fix="Write a short alt text that says what the image shows or does. Leave alt=\"\" only on "
               "decorative images (or move those into CSS backgrounds)."),
    Filter("missing_alt_attribute", "Missing Alt Attribute", _any_img(lambda img: _alt(img) is None),
           issue_type="Issue", priority="Low",
           description="At least one page shows this image with no alt attribute at all. Every <img> "
                       "should have one: descriptive text for meaningful images, or alt=\"\" for "
                       "decorative ones. Open the image to see which pages use it this way.",
           fix="Add an alt attribute to the <img> tag: a short description of the image, or an empty "
               "alt=\"\" if it is only decoration."),
    threshold_filter("alt_text_over_100_characters", "alt_max_chars", "Alt Text Over {n} Characters",
                     lambda r, site: any(len(_alt(img) or "") > pref_number(site, "alt_max_chars")
                                         for _, img in element_occurrences_of(site, r)),
                     issue_type="Opportunity", priority="Low",
                     description="At least one page gives this image an alt text longer than {n} characters. Long "
                                 "alt text is tiring to hear in a screen reader and often stuffed with keywords.",
                     fix="Shorten the alt text to a clear one-sentence description of the image. Put longer "
                         "explanations in the page text or a caption."),
    Filter("background_images", "Background Images", lambda r, site: bool(background_pages(site, r)),
           description="Images that pages show as a CSS background (background-image) rather than with an <img> "
                       "tag. They cannot carry alt text and search engines rarely index them as images, so keep "
                       "meaningful pictures (products, people, diagrams) in <img> tags. Found in the HTML's "
                       "styles and, for pages rendered in Chrome, in the styles applied after loading."),
    Filter("incorrectly_sized", "Incorrectly Sized Images", _incorrectly_sized,
           issue_type="Opportunity", priority="Medium", requires=("rendering:img_sizes",),
           description=f"The image file is much larger than the size it is shown at: more than "
                       f"{OVERSIZED_PIXELS_PCT:.0f}% of its pixels are never displayed (measured in Chrome on the "
                       "pages that show it, including high-density screens). Visitors download bytes they never "
                       "see.",
           fix="Serve the image at the size it is displayed (see Displayed Size against Real Size), or add "
               "srcset/sizes so browsers pick a smaller file on smaller screens.",
           short_fix="Resize the image to its displayed size or add srcset"),
    Filter("missing_size_attributes", "Missing Size Attributes",
           _any_img(lambda img: not (img.get("has_width") and img.get("has_height"))),
           issue_type="Opportunity", priority="Low",
           description="At least one page shows this image without both width and height attributes on "
                       "the <img> tag. The browser cannot reserve space before the image loads, so the "
                       "page jumps around (layout shift, which hurts Core Web Vitals).",
           fix="Add width and height attributes with the image's real pixel size to the <img> tag; CSS "
               "can still scale it, and the browser keeps the right shape while it loads."),
]

COLUMNS = [
    ADDRESS, CONTENT_TYPE,
    Column("size", "Size (Bytes)", lambda r, site: image_size(r) or "", "num"),
    Column("img_inlinks", "IMG Inlinks", lambda r, site: img_inlinks(site, r), "num"),
    Column("missing_alt_pages", "Missing Alt On (Pages)", lambda r, site: missing_alt_pages(site, r), "num"),
    Column("background_pages", "Background On (Pages)", lambda r, site: len(background_pages(site, r)) or "", "num"),
    Column("real_size", "Real Size (px)", lambda r, site: _px(worst_size(site, r), "natural_w", "natural_h")),
    Column("displayed_size", "Displayed Size (px)", lambda r, site: _px(worst_size(site, r), "display_w", "display_h")),
    Column("wasted_pct", "Pixels Not Shown (%)",
           lambda r, site: round(_wasted(worst_size(site, r)), 1) if worst_size(site, r) else "", "num"),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="images", name="Images", order=120, columns=COLUMNS, filters=FILTERS, rows=image_rows,
             skip_reason=_skip_reason))
