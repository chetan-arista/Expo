"""
Performance tab (ours, after siteone-crawler's performance analysers; no API key needed): how fast the
crawler got each page, how heavy each page is, and how well its files can be cached.

Rows: internal HTML pages that loaded (2xx), then the internal scripts, stylesheets, images and fonts that
loaded (2xx). Page filters only look at pages, file filters only at files.
  - Response times are what our server measured while crawling, not what real visitors get (Core Web
    Vitals need PageSpeed Insights / Chrome field data). Slowest 10% = above the 90th percentile of the
    pages (nearest rank; needs 10 pages).
  - Page Weight = the page's own HTML plus the known sizes of every file it loads (PageData
    resource_bytes_total + resource_bytes_external, filled by the crawler as it checks each file). Shared
    files count for every page that loads them (siteone counts them once, site-wide, which under-reports).
    Files whose size is unknown (no Content-Length) are counted in "Files Of Unknown Size", never guessed, so
    the weight is a lower bound and only flagged when the known part alone is over the budget.
  - Caching (files only; JSON / XML are not judged): Uncacheable = Cache-Control no-store, or no caching
    header at all (no Cache-Control lifetime, no Expires, no ETag / Last-Modified); Short Cache Lifetime =
    no-cache, a max-age (or Expires) under one day, or only ETag / Last-Modified to revalidate with.
"""
import math
import re
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

from ..registry import Column, Filter, Tab, register, seo
from .internal import (ADDRESS, CONTENT_TYPE, RESPONSE_TIME, STATUS_CODE, category_of, content_kind, headers_of,
                       internal_rows, is_html_doc, memo)
from .page_titles import content_rows

SLOW_MS = 1000
VERY_SLOW_MS = 3000
P90_MIN_PAGES = 10
PAGE_WEIGHT_BUDGET = int(2.5 * 1024 * 1024)     # HTTP Archive mobile median scale
RESOURCE_BUDGET = 75
SHORT_CACHE_SECONDS = 86400
FILE_KINDS = ("javascript", "css", "images", "fonts")

_MAX_AGE = re.compile(r"(?:^|[,\s])(?:s-maxage|max-age)\s*=\s*\"?(\d+)", re.I)


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------
def is_file(r) -> bool:
    return content_kind(r) in FILE_KINDS and category_of(r) == "2xx"


def _page(r) -> bool:
    return is_html_doc(r) and category_of(r) == "2xx" and bool(seo(r).get("is_html"))


def performance_rows(site) -> List[Any]:
    pages = content_rows(site)
    return pages + [r for r in internal_rows(site) if is_file(r)]


# ---------------------------------------------------------------------------------------------
# Response time
# ---------------------------------------------------------------------------------------------
def response_ms(r) -> Optional[int]:
    v = getattr(r, "response_time_ms", None)
    if v is None:
        v = seo(r).get("response_time_ms")
    try:
        return int(v) if v is not None and int(v) > 0 else None
    except (TypeError, ValueError):
        return None


def _nearest_rank(values: List[int], pct: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def performance_summary(site) -> Dict[str, Any]:
    """{"pages", "avg_ms", "p50_ms", "p90_ms", "max_ms"} over the HTML pages that loaded (None values when
    no page has a time)."""
    def build():
        times = [t for t in (response_ms(r) for r in content_rows(site)) if t is not None]
        if not times:
            return {"pages": 0, "avg_ms": None, "p50_ms": None, "p90_ms": None, "max_ms": None}
        return {"pages": len(times), "avg_ms": int(sum(times) / len(times)), "p50_ms": _nearest_rank(times, 50),
                "p90_ms": _nearest_rank(times, 90), "max_ms": max(times)}
    return memo(site, "perf_summary", build)


def _slower_than(ms: int):
    return lambda r, site: _page(r) and (response_ms(r) or 0) > ms


def _above_p90(r, site) -> bool:
    s = performance_summary(site)
    if not _page(r) or s["pages"] < P90_MIN_PAGES or s["p90_ms"] is None:
        return False
    return (response_ms(r) or 0) > s["p90_ms"]


# ---------------------------------------------------------------------------------------------
# Page weight
# ---------------------------------------------------------------------------------------------
def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def page_weight(r) -> Tuple[int, int]:
    """(known bytes: HTML + the files it loads, files of unknown size)."""
    p = seo(r)
    html = _int(p.get("transferred_bytes")) or _int(p.get("size_bytes"))
    known = html + _int(p.get("resource_bytes_total")) + _int(p.get("resource_bytes_external"))
    unknown = max(0, _int(p.get("resources_count")) - _int(p.get("resources_sized")))
    return known, unknown


def _heavy(r, site) -> bool:
    return _page(r) and page_weight(r)[0] > PAGE_WEIGHT_BUDGET


def _many_requests(r, site) -> bool:
    return _page(r) and _int(seo(r).get("resources_count")) > RESOURCE_BUDGET


# ---------------------------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------------------------
def _date(text: str):
    try:
        return parsedate_to_datetime(text) if text else None
    except (TypeError, ValueError, IndexError):
        return None


def cache_lifetime(h: Dict[str, str]) -> Optional[int]:
    """Seconds the file may be reused without asking the server: max-age / s-maxage, else Expires - Date;
    None when neither is given."""
    cc = (h.get("cache-control") or "").lower()
    m = _MAX_AGE.search(cc)
    if m:
        return int(m.group(1))
    exp, date = _date(h.get("expires") or ""), _date(h.get("date") or "")
    if (h.get("expires") or "").strip() and exp is None:
        return 0                                    # an invalid Expires (e.g. "0") means already expired
    if exp is not None:
        try:
            return max(0, int((exp - date).total_seconds())) if date is not None else None
        except Exception:
            return None
    return None


def cache_verdict(r) -> str:
    """"uncacheable" | "short" | "long" | "" (not a file, or its headers were not recorded)."""
    if not is_file(r):
        return ""
    h = headers_of(r)
    if h is None:
        return ""
    cc = (h.get("cache-control") or "").lower()
    if "no-store" in cc:
        return "uncacheable"
    life = cache_lifetime(h)
    validators = bool((h.get("etag") or "").strip() or (h.get("last-modified") or "").strip())
    if "no-cache" in cc:
        return "short"
    if life is None:
        return "short" if validators else "uncacheable"
    return "long" if life >= SHORT_CACHE_SECONDS else "short"


VERDICT_LABEL = {"uncacheable": "Not cacheable", "short": "Under 1 day", "long": "1 day or more"}


def _lifetime_cell(r, site):
    if not is_file(r):
        return ""
    h = headers_of(r)
    life = cache_lifetime(h) if h is not None else None
    return "" if life is None else life


FILTERS = [
    Filter("slow_1s", f"Response Time Over {SLOW_MS // 1000} Second", _slower_than(SLOW_MS), "Warning", "Medium",
           description=f"The page took more than {SLOW_MS // 1000} second to answer the crawler (time to the "
                       "full HTML, measured by our server, not by visitors' browsers). Slow server answers delay "
                       "everything else and use up search engines' crawl time.",
           fix="Check caching (page cache / CDN), slow database queries and plugins on these templates; compare "
               "with the Slowest 10% filter to find the worst ones.",
           short_fix="Speed up the server: page cache, CDN, slow queries", of_rows=_page),
    Filter("slow_3s", f"Response Time Over {VERY_SLOW_MS // 1000} Seconds", _slower_than(VERY_SLOW_MS),
           "Issue", "High",
           description=f"The page took more than {VERY_SLOW_MS // 1000} seconds to answer the crawler. Visitors "
                       "leave and search engines slow down crawling when servers are this slow.",
           fix="Fix these pages first: add full-page caching or a CDN, and profile the server code behind the "
               "template.",
           short_fix="Cache or optimise these pages; they take over 3 s", of_rows=_page),
    Filter("slowest_10pct", "Slowest 10% Of Pages", _above_p90,
           description=f"Pages slower than 90% of the site's pages (above the 90th percentile response time; "
                       f"needs at least {P90_MIN_PAGES} pages). Useful to see which templates are slowest.",
           of_rows=_page),
    Filter("heavy_pages", "Page Weight Over 2.5 MB", _heavy, "Warning", "Medium",
           requires=("pagedata:resource_bytes_total",),
           description="The page plus the files it loads (images, scripts, styles, fonts) weigh more than 2.5 MB "
                       "as far as their sizes are known. Heavy pages load slowly on phones and mobile data.",
           fix="Compress and resize images (WebP/AVIF), remove unused scripts and apps, and lazy-load what is "
               "below the fold.",
           short_fix="Cut page weight: compress images, drop unused scripts", of_rows=_page),
    Filter("many_requests", f"Over {RESOURCE_BUDGET} Files Loaded", _many_requests, "Opportunity", "Low",
           requires=("pagedata:resources_count",),
           description=f"The page loads more than {RESOURCE_BUDGET} files (images, scripts, stylesheets, frames) "
                       "from its HTML. Every file is a request the browser has to make.",
           fix="Combine or remove scripts and stylesheets, drop unused third-party apps, and lazy-load images.",
           short_fix="Load fewer files: drop unused apps, combine scripts", of_rows=_page),
    Filter("uncacheable", "Uncacheable Files", lambda r, site: cache_verdict(r) == "uncacheable", "Warning", "Low",
           requires=("config:crawl_resources",),
           description="Scripts, stylesheets, images or fonts sent with Cache-Control: no-store, or with no "
                       "caching header at all, so browsers download them again on every visit.",
           fix="Send a long Cache-Control lifetime for static files (e.g. max-age=31536000 with versioned file "
               "names).",
           short_fix="Send Cache-Control max-age for static files", of_rows=is_file),
    Filter("short_cache", "Short Cache Lifetime (Under 1 Day)", lambda r, site: cache_verdict(r) == "short",
           "Opportunity", "Low", requires=("config:crawl_resources",),
           description="Static files that browsers may keep for less than a day (or must re-check every time), "
                       "so returning visitors download or re-validate them often.",
           fix="Give static files a long max-age (a year for versioned files, at least a week otherwise).",
           short_fix="Use a long max-age (a week or more) for static files", of_rows=is_file),
]


def _page_cell(fn):
    return lambda r, site: fn(r) if _page(r) else ""


COLUMNS = [
    ADDRESS, CONTENT_TYPE, STATUS_CODE, RESPONSE_TIME,
    Column("size", "Size (Bytes)", lambda r, site: _int(seo(r).get("size_bytes")) or _int(seo(r).get("transferred_bytes"))
           or "", "num"),
    Column("page_weight_kb", "Page Weight (KB)", _page_cell(lambda r: round(page_weight(r)[0] / 1024, 1)), "num"),
    Column("files_loaded", "Files Loaded", _page_cell(lambda r: _int(seo(r).get("resources_count"))), "num"),
    Column("files_unknown", "Files Of Unknown Size", _page_cell(lambda r: page_weight(r)[1]), "num"),
    Column("third_party_kb", "Third-Party Files (KB)",
           _page_cell(lambda r: round(_int(seo(r).get("resource_bytes_external")) / 1024, 1)), "num"),
    Column("cache_control", "Cache-Control",
           lambda r, site: (headers_of(r) or {}).get("cache-control", "") if is_file(r) else ""),
    Column("cache_lifetime", "Cache Lifetime (s)", _lifetime_cell, "num"),
    Column("cache_verdict", "Caching", lambda r, site: VERDICT_LABEL.get(cache_verdict(r), "")),
]

register(Tab(id="performance", name="Performance", order=270, columns=COLUMNS, filters=FILTERS,
             rows=performance_rows,
             description="Server response times, page weight and caching of static files (no API key needed)."))
