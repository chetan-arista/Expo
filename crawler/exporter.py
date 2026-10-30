import csv
import io
import math
import os
import re
import time
import uuid
import zipfile
from copy import copy
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from .engine import CrawlResult, ExternalLink
from .issues import CATEGORY_LABELS, ISSUE_CATEGORIES, classify_result, diagnose, issue_sort_key, health_score, found_in_code

# Downloadable report scopes: key -> (sheet title, filter, sheet layout)
EXPORT_CATEGORIES = {
    "all": ("Internal URLs", lambda r: True, "internal"),
    "2xx": (CATEGORY_LABELS["2xx"], lambda r: classify_result(r) == "2xx", "internal"),
    "3xx": (CATEGORY_LABELS["3xx"], lambda r: classify_result(r) == "3xx", "internal"),
    "4xx": (CATEGORY_LABELS["4xx"], lambda r: classify_result(r) == "4xx", "issue"),
    "5xx": (CATEGORY_LABELS["5xx"], lambda r: classify_result(r) == "5xx", "issue"),
    "error": (CATEGORY_LABELS["error"], lambda r: classify_result(r) == "error", "issue"),
    "issues": ("Broken URLs (4xx, 5xx, none)", lambda r: classify_result(r) in ISSUE_CATEGORIES, "issue"),
    "blocked": (CATEGORY_LABELS["blocked"], lambda r: classify_result(r) == "blocked", "internal"),
    "external": ("External Links", None, "external"),
}

NAVY_FILL = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
PURPLE_FILL = PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid")
ORANGE_FILL = PatternFill(start_color="D97706", end_color="D97706", fill_type="solid")
RED_HEADER_FILL = PatternFill(start_color="991B1B", end_color="991B1B", fill_type="solid")
LABEL_FILL = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

WHITE_BOLD_FONT = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
REGULAR_FONT = Font(name="Segoe UI", size=10)
BOLD_FONT = Font(name="Segoe UI", size=10, bold=True)
LINK_FONT = Font(name="Segoe UI", size=10, color="1D4ED8", underline="single")
SUMMARY_TITLE_FONT = Font(name="Segoe UI", size=14, bold=True, color="FFFFFF")

CATEGORY_FILLS = {
    "2xx": PatternFill(start_color="DEF7EC", end_color="DEF7EC", fill_type="solid"),
    "3xx": PatternFill(start_color="FEF08A", end_color="FEF08A", fill_type="solid"),
    "4xx": PatternFill(start_color="FDE8E8", end_color="FDE8E8", fill_type="solid"),
    "5xx": PatternFill(start_color="FFE4E6", end_color="FFE4E6", fill_type="solid"),
    "error": PatternFill(start_color="FFE4E6", end_color="FFE4E6", fill_type="solid"),
    "blocked": PatternFill(start_color="F1EEE6", end_color="F1EEE6", fill_type="solid"),
}
SLATE_FILL = PatternFill(start_color="475569", end_color="475569", fill_type="solid")

THIN_BORDER = Border(
    left=Side(style='thin', color='E2E8F0'),
    right=Side(style='thin', color='E2E8F0'),
    top=Side(style='thin', color='E2E8F0'),
    bottom=Side(style='thin', color='E2E8F0')
)

PARENT_LINK = "Parent Link"     # the page a URL was found on (was "Found On"; the data keys keep parent_url)
INTERNAL_HEADERS = [
    "Index", "Page Title", "URL", PARENT_LINK, "Link Text (Clicked Element)",
    "Link Location", "HTTP Status", "Status Code", "Category", "Redirect Destination",
    "Depth", "Discovery Source", "Content Type", "Latency (ms)", "Error Notes",
]
ISSUE_HEADERS = [
    "Index", "HTTP Status", "Issue", "Severity", "Broken URL", PARENT_LINK,
    "Pages Linking Here", "All Linking Pages",
    "Link Text (Clicked Element)", "Link Location", "Element HTML", "Why It Happens",
    "Likely Causes", "How To Fix", "Latency (ms)",
]
EXTERNAL_HEADERS = [
    "Index", "External Destination URL", "External Domain", PARENT_LINK,
    "Anchor Text / Label", "Validation Status", "Discovery Source",
]
BROKEN_EXTERNAL_HEADERS = [
    "Index", "Status", "External URL", "Domain", PARENT_LINK, "Pages Linking Here",
    "All Linking Pages", "Anchor Text / Label", "How To Fix",
]
LINK_COLUMNS = {
    "internal": ("URL", PARENT_LINK, "Redirect Destination"),
    "issue": ("Broken URL", PARENT_LINK),
    "external": ("External Destination URL", PARENT_LINK),
    "broken_external": ("External URL", PARENT_LINK),
}
LINKING_PAGES_SHOWN = 50   # pages listed in an "All Linking Pages" cell (the count column has the total)
CENTERED = {"Index", "Status Code", "Depth", "Latency (ms)", "Severity", "Category"}

# SEO tab grids (crawler/seo/registry.py) in Excel
TAB_SHEET_ROW_CAP = 50_000          # rows per tab sheet inside the full workbook
INLINKS_ROW_CAP = 1_000_000         # Excel holds 1,048,576 rows per sheet
EXCEL_CELL_MAX = 32_000             # Excel's per-cell limit is 32,767 characters
TEAL_FILL = PatternFill(start_color="115E59", end_color="115E59", fill_type="solid")
ISSUE_TYPE_FILLS = {
    "Issue": PatternFill(start_color="FDE8E8", end_color="FDE8E8", fill_type="solid"),
    "Warning": PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
    "Opportunity": PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid"),
}
PRIORITY_FILLS = {
    "High": PatternFill(start_color="FDE8E8", end_color="FDE8E8", fill_type="solid"),
    "Medium": PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
    "Low": PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid"),
}
ISSUES_OVERVIEW_HEADERS = ["Issue Name", "Issue Type", "Issue Priority", "URLs", "% of Total", "Description",
                           "How To Fix"]
INLINK_HEADERS = ["Source", "Destination", "Anchor Text", "Nofollow", "Link Position", "Source Line",
                  "Destination Status Code", "Destination Status", "Type", "Path Type", "Rel"]
# Bulk export of the pages behind every URL of one tab filter (Screaming Frog's "... Inlinks" exports)
SOURCE_HEADERS = ["Destination", "Source (Page To Edit)", "Type", "Anchor / Alt Text", "Alt Attribute",
                  "Nofollow", "Link Position", "Source Line", "Destination Status Code", "Destination Status"]
ISSUE_URL_HEADERS = ["Issue Name", "Issue Type", "Issue Priority", "URL", "Status Code", "Tab", "Filter"]
ISSUE_URL_SHEET_CAP = 200_000       # rows in the full workbook's "Issue URLs" sheet
_SHEET_BAD_CHARS = re.compile(r"[\[\]:*?/\\]")

# Sheets from the follow-up jobs (validation, crawl intelligence, checks, dismissed issues)
GREEN_FILL = PatternFill(start_color="DEF7EC", end_color="DEF7EC", fill_type="solid")
RED_FILL = PatternFill(start_color="FDE8E8", end_color="FDE8E8", fill_type="solid")
AMBER_FILL = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
GREY_FILL = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
RESULT_FILLS = {
    "Passed": GREEN_FILL, "Failed": RED_FILL, "Not checked": GREY_FILL, "Not applicable": GREY_FILL,
    "Confirmed": RED_FILL, "Intermittent": AMBER_FILL, "Not reproduced": GREEN_FILL, "Fixed": GREEN_FILL,
    "Exposed": RED_FILL, "Protected": GREEN_FILL, "Public": GREY_FILL, "Not verified": GREY_FILL,
    "Wrong page type": AMBER_FILL, "Thin content": AMBER_FILL, "Off topic": AMBER_FILL,
}
CHECK_LABELS = {"passed": "Passed", "failed": "Failed", "not_checked": "Not checked"}
CHECK_ORDER = {"failed": 0, "not_checked": 1, "passed": 2}
VERDICT_LABELS = {"confirmed": "Confirmed", "intermittent": "Intermittent", "not_reproduced": "Not reproduced",
                  "fixed": "Fixed"}
VERDICT_ORDER = {"confirmed": 0, "intermittent": 1, "not_reproduced": 2, "fixed": 3}
CHECKS_HEADERS = ["Check", "Result", "Why", "URLs", "Issue Type", "Priority", "Tab", "Filter"]
VALIDATION_HEADERS = ["URL", "Verdict", "Reported", "Status Now", "Attempts", "Link Still On Page",
                      "Linking Page", "Note", "Checked At"]
LOGIN_GUARD_HEADERS = ["Member Page", "Result", "Logged-Out Status", "Note", "Warnings"]
FIX_PLAN_HEADERS = ["Fix Order", "Fix", "Kind", "URLs", "Impact (0-100)", "Effort", "Verified", "Issues",
                    "Root Cause", "Why It Happens", "How To Fix", "Platform Steps", "Prevent It", "Example URLs"]
CONTENT_HEADERS = ["Finding", "URL", "URL Promises", "Page Delivers", "Confidence", "Words", "Evidence / Note",
                   "Suggestion"]
DISMISSED_HEADERS = ["Issue", "Tab", "Filter", "Pattern", "URLs Covered", "Note", "Dismissed At"]


# ---------------------------------------------------------------------------------------------
# SEO tab grids: one source of rows for the API (/api/crawl/tab) and every Excel export
# ---------------------------------------------------------------------------------------------
def json_value(v: Any) -> Any:
    """A cell value the API can serialize: primitives as-is, sequences as lists, anything else as text."""
    if v is None:
        return ""
    if isinstance(v, bool) or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else ""
    if isinstance(v, (list, tuple, set)):
        return [json_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): json_value(x) for k, x in v.items()}
    return str(v)


def text_value(v: Any) -> str:
    """A cell value as one line of text (search, Excel)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (list, tuple, set)):
        return " | ".join(text_value(x) for x in v if x not in (None, ""))
    if isinstance(v, dict):
        return ", ".join(f"{k}: {text_value(x)}" for k, x in v.items())
    return str(v)


def xl_value(v: Any) -> Any:
    """A cell value Excel accepts: numbers stay numbers, text loses control characters and is capped."""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (int, float)):
        return v if not isinstance(v, float) or math.isfinite(v) else ""
    s = ILLEGAL_CHARACTERS_RE.sub("", text_value(v))
    return s[:EXCEL_CELL_MAX]


def row_category(r) -> str:
    """'2xx' | '3xx' | '4xx' | '5xx' | 'error' | 'blocked' | '' from the HTTP status alone."""
    try:
        from .seo.tabs.internal import category_of
        return category_of(r) or ""
    except Exception:
        try:
            return classify_result(r) if hasattr(r, "resource_type") else ""
        except Exception:
            return ""


def row_status_code(r) -> Optional[int]:
    """The URL's own status code as Screaming Frog reports it (a redirect shows 301, not the 200 it
    leads to); 0 for no response / blocked, None for an external link that was never checked."""
    try:
        from .seo.tabs.internal import first_status_code
        return first_status_code(r)
    except Exception:
        return getattr(r, "status_code", None)


def row_status_text(r) -> str:
    """Reason phrase of the URL's own response ("OK", "Moved Permanently", "Blocked by robots.txt")."""
    try:
        from .seo.tabs.internal import status_text
        return status_text(r)
    except Exception:
        return getattr(r, "http_status", "") or ""


_FOUND_ON = re.compile(r"\b(?:first\s+)?found\s+on\b", re.I)


def user_label(label: str) -> str:
    """A column label as the user sees it: "Found On" / "First Found On" is called "Parent Link"."""
    return _FOUND_ON.sub(PARENT_LINK, label or "") if label else label


def tab_grid(site, index, tab_id: str, filter_id: str = "all") -> Tuple[List[dict], Optional[List[dict]]]:
    """Columns and rows of one tab grid: ([{"id","label","kind"}], [{"url","category","cells"}]).
    Rows is None when the tab or filter does not exist."""
    from .seo.registry import cell_value
    tr = index.tabs.get(tab_id) if index is not None else None
    if tr is None:
        return [], None
    tab = tr.tab
    columns = [{"id": c.id, "label": user_label(c.label), "kind": c.kind} for c in tab.columns]
    items = tr.rows if filter_id in ("", "all", None) else tr.filter_rows.get(filter_id)
    if items is None:
        return columns, None
    rows = [{"url": getattr(r, "url", "") or "", "category": row_category(r),
             "cells": {c.id: json_value(cell_value(c, r, site)) for c in tab.columns}} for r in items]
    return columns, rows


_RESOURCE_TYPES = {"image": "Image", "script": "JavaScript", "css": "CSS", "iframe": "Iframe"}


def resource_type_label(kind: Any) -> str:
    """The Type a resource reference shows in every inlink and outlink list (Image, JavaScript, CSS, Iframe,
    Other): the URL details pane, All Inlinks / All Outlinks and the per-filter Inlinks exports."""
    return _RESOURCE_TYPES.get(str(kind or ""), "Other")


def _memo(site, key: str, build):
    store = site.__dict__.setdefault("_tab_memo", {})
    if key not in store:
        store[key] = build()
    return store[key]


def _norm_url(url: str) -> str:
    try:
        from .url_utils import normalize_url
        return normalize_url(url) or url
    except Exception:
        return url


def source_index(site) -> Dict[str, List[dict]]:
    """url -> every way a crawled page points at it, internal and external targets alike:
    {"source", "type" (Hyperlink | Image | Background Image | JavaScript | CSS | Iframe | Other | HTTP Redirect),
    "anchor" (link text or image alt), "alt" (None = no alt attribute; images only), "nofollow",
    "region", "line", "raw" (the href / src as written), "rel" (a hyperlink's rel values, "stylesheet" for a
    stylesheet, else "")}. An HTTP Redirect entry's source is the redirecting URL, not a page. The other inlink
    types (canonical, hreflang, rel next / prev, refreshes, form actions...) are in other_sources_of: they do not
    count as inlinks in the tabs (Site.other_inlinks). Built once per Site."""
    def build():
        out: Dict[str, List[dict]] = {}
        for page in site.pages:
            p = getattr(page, "seo", None) or {}
            for link in p.get("links") or ():
                url = link.get("url") or ""
                if url:
                    out.setdefault(url, []).append({
                        "source": page.url, "type": "Hyperlink", "anchor": link.get("anchor") or "", "alt": None,
                        "nofollow": bool(link.get("nofollow")), "region": link.get("region") or "",
                        "line": int(link.get("line") or 0), "raw": link.get("raw") or "", "rel": link_rel(link)})
            alts: Dict[str, Any] = {}
            for img in p.get("images") or ():
                if img.get("src") and img["src"] not in alts:
                    alts[img["src"]] = img.get("alt")
            seen = set()
            for res in p.get("resources") or ():
                url = res.get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                kind = _RESOURCE_TYPES.get(res.get("type") or "", "Other")
                alt = (alts[url] if url in alts else res.get("alt")) if kind == "Image" else None
                out.setdefault(url, []).append({
                    "source": page.url, "type": kind, "anchor": (alt or "") if kind == "Image" else "",
                    "alt": alt, "nofollow": False, "region": res.get("region") or "",
                    "line": int(res.get("line") or 0), "raw": res.get("raw") or "", "rel": resource_rel(res)})
            # CSS background images of the page's HTML (style="" and <style> blocks): the page shows them too
            for bg in p.get("background_images") or ():
                if isinstance(bg, str) and bg and bg not in seen and not bg.startswith("data:"):
                    seen.add(bg)
                    out.setdefault(bg, []).append({
                        "source": page.url, "type": "Background Image", "anchor": "", "alt": None,
                        "nofollow": False, "region": "CSS background image", "line": 0, "raw": bg, "rel": ""})
        for r in site.results:
            if row_category(r) != "3xx":
                continue
            dest = getattr(r, "redirect_destination", None)
            if dest and dest != "-":
                out.setdefault(_norm_url(dest), []).append({
                    "source": r.url, "type": "HTTP Redirect", "anchor": "", "alt": None, "nofollow": False,
                    "region": "HTTP redirect", "line": 0, "raw": dest, "rel": ""})
        return out
    if site is None:
        return {}
    return _memo(site, "source_index", build)


def sources_of(site, url: str) -> List[dict]:
    idx = source_index(site)
    return idx.get(url) or idx.get(_norm_url(url)) or []


def link_rel(link: Any) -> str:
    """A hyperlink's rel values ("nofollow sponsored"), "" when it has none (seo.analysis.link_rel)."""
    from .seo.analysis import link_rel as rel_of
    return rel_of(link)


def resource_rel(res: Any) -> str:
    """The rel a resource reference was written with: "stylesheet" for <link rel=stylesheet>, else ""."""
    if isinstance(res, dict) and res.get("tag") == "link" and res.get("type") == "css":
        return "stylesheet"
    return ""


def other_type_label(kind: Any) -> str:
    """The Type shown for a Site.other_inlinks entry ("Canonical", "Hreflang", "Rel Next", ...)."""
    from .seo.analysis import OTHER_INLINK_TYPES
    return OTHER_INLINK_TYPES.get(str(kind or ""), "Link Element")


def _other_entry(e: dict, target: str) -> dict:
    """A Site.other_inlinks entry in the source_index shape (type = its label, anchor = the hreflang code)."""
    lang = str(e.get("lang") or "")
    return {"source": e.get("source") or "", "type": other_type_label(e.get("type")),
            "type_id": e.get("type") or "", "anchor": f"hreflang={lang}" if lang else "", "alt": None,
            "nofollow": False, "region": e.get("region") or "", "line": int(e.get("line") or 0),
            "raw": e.get("raw") or "", "rel": e.get("rel") or "", "via": e.get("via") or "",
            "method": e.get("method") or "", "target": target}


def other_sources_of(site, url: str) -> List[dict]:
    """Every other way a crawled URL points at `url` (Site.other_inlinks: canonical, hreflang, rel next / prev,
    meta refresh, Refresh header, JavaScript redirect, mobile alternate, form action, other link elements), in the
    source_index entry shape with "type" = the label and "type_id" = the Site.other_inlinks type. These do not count
    as inlinks in the tabs (Link Score, Inlinks, Unique Inlinks); the panes and the All Inlinks export list them."""
    other = getattr(site, "other_inlinks", None) if site is not None else None
    if not other or not url:
        return []
    entries = other.get(url)
    key = url
    if entries is None:
        key = _norm_url(url)
        entries = other.get(key)
    return [_other_entry(e, key) for e in entries or ()]


def other_outlinks_of(site, r) -> List[dict]:
    """The same entries seen from the page that holds them (the Outlinks pane and the All Outlinks export); each
    carries "target" (the URL pointed at)."""
    try:
        from .seo.analysis import other_links_of
        return [_other_entry(e, target) for target, e in other_links_of(r)]
    except Exception:
        return []


def pane_inlinks(site, url: str) -> List[dict]:
    """The other inlink types of a URL for the URL details Inlinks pane (GET /api/crawl/url, W5), in the pane's
    shape: {"source", "anchor", "nofollow", "region", "line", "type", "rel"}."""
    return [{"source": e["source"], "anchor": e["anchor"], "nofollow": False, "region": e["region"],
             "line": e["line"], "type": e["type"], "rel": e["rel"]} for e in other_sources_of(site, url)]


def pane_outlinks(site, r) -> List[dict]:
    """The other link types a page points with, for the URL details Outlinks pane (W5): {"url", "anchor",
    "nofollow", "internal", "region", "type", "rel"} (the pane adds the status of each target)."""
    return [{"url": e["target"], "anchor": e["anchor"], "nofollow": False,
             "internal": is_internal_target(site, e["target"]), "region": e["region"], "type": e["type"],
             "rel": e["rel"]} for e in other_outlinks_of(site, r)]


def linking_pages(site, url: str, max_hops: int = 5) -> List[str]:
    """The pages to edit for a URL: every crawled page that links to it or embeds it. Links that reach
    it through redirects are traced back to the pages holding them (a page linking to /old, which
    redirects to a 404, is a page with a broken link). Empty when nothing crawled points at it."""
    if site is None or not url:
        return []
    pages: List[str] = []
    seen_pages, seen_urls = set(), {url}
    frontier = [url]
    for _ in range(max_hops):
        nxt = []
        for u in frontier:
            for e in sources_of(site, u):
                src = e.get("source") or ""
                if not src:
                    continue
                if e.get("type") == "HTTP Redirect":
                    if src not in seen_urls:
                        seen_urls.add(src)
                        nxt.append(src)
                elif src not in seen_pages:
                    seen_pages.add(src)
                    pages.append(src)
        if not nxt:
            break
        frontier = nxt
    return pages


def _is_error_page(site, url: str) -> bool:
    """A crawled URL that is itself broken (4xx, 5xx, no response): not a page anyone can edit or visit."""
    if site is None:
        return False
    broken = _memo(site, "error_pages", lambda: {u for u, r in (getattr(site, "by_url", None) or {}).items()
                                                 if row_category(r) in ISSUE_CATEGORIES})
    return url in broken


def found_on(site, r) -> Tuple[str, List[str]]:
    """(page shown as "Parent Link", all linking pages) for a broken URL: the first real page that links
    to it, falling back to the URL that led the crawler there when no crawled page links to it
    (start URL, XML sitemap, JavaScript code). The URL itself and pages that are broken too (a 404 template
    whose menu links to its own address) are not linking pages, the same rule as the PDF."""
    url = getattr(r, "url", "")
    pages = [p for p in linking_pages(site, url) if p != url and not _is_error_page(site, p)]
    return (pages[0] if pages else (getattr(r, "parent_url", "") or "")), pages


def pages_cell(pages: List[str]) -> str:
    """Linking pages for one Excel cell (one per line, capped at LINKING_PAGES_SHOWN)."""
    shown = "\n".join(pages[:LINKING_PAGES_SHOWN])
    more = len(pages) - LINKING_PAGES_SHOWN
    return shown + (f"\n... and {more:,} more" if more > 0 else "")


def is_broken_external(e) -> bool:
    """An external link the checker requested that answered 4xx/5xx or not at all (robots.txt-blocked
    and never-checked links are not broken)."""
    return row_category(e) in ISSUE_CATEGORIES


def crawl_totals(results, external_links) -> Dict[str, int]:
    """Screaming Frog's Summary counts, the same as the dashboard Overview's Summary rows:
    encountered = internal + external URLs; crawled = encountered minus the URLs never requested
    (internal and external URLs blocked by robots.txt, external links not checked). A URL that was
    requested but got no answer counts as crawled. Keys: encountered, internal, external, internal_blocked,
    external_blocked, external_not_checked, crawled."""
    internal = list(results or ())
    externals = list(external_links or ())
    ext_cats = [row_category(e) for e in externals]
    out = {"internal": len(internal), "external": len(externals),
           "internal_blocked": sum(1 for r in internal if row_category(r) == "blocked"),
           "external_blocked": sum(1 for c in ext_cats if c == "blocked"),
           "external_not_checked": sum(1 for c in ext_cats if c == "")}
    out["encountered"] = out["internal"] + out["external"]
    out["crawled"] = max(0, out["encountered"] - out["internal_blocked"] - out["external_blocked"]
                         - out["external_not_checked"])
    return out


def crawled_note(totals: Dict[str, int]) -> str:
    """How "Total URLs Crawled" adds up, e.g. "38 internal + 2 external URLs, minus 1 blocked by robots.txt"."""
    text = f"{totals['internal']:,} internal + {totals['external']:,} external URLs"
    blocked = totals["internal_blocked"] + totals["external_blocked"]
    minus = []
    if blocked:
        minus.append(f"{blocked:,} blocked by robots.txt")
    if totals["external_not_checked"]:
        minus.append(f"{totals['external_not_checked']:,} external not checked")
    return text + (f", minus {' and '.join(minus)}" if minus else ", all requested")


def content_type_split(results) -> str:
    """The Internal tab's split of the internal URLs by the Content-Type each answered with, e.g.
    "34 HTML, 3 Images, 1 Unknown = 38" (every URL in exactly one bucket, so it adds up)."""
    results = list(results or ())
    try:
        from .seo.tabs.internal import CONTENT_KINDS, content_kind
    except Exception:
        return f"{len(results):,} URLs"
    counts: Dict[str, int] = {}
    for r in results:
        try:
            k = content_kind(r)
        except Exception:
            k = "unknown"
        counts[k] = counts.get(k, 0) + 1
    parts = [f"{counts[k]:,} {name}" for k, name in CONTENT_KINDS if counts.get(k)]
    return (", ".join(parts) or "0") + f" = {len(results):,} (as the Internal tab files them)"


def pages_vs_html_note(results, health: dict) -> str:
    """Why the health score's page count differs from the HTML count of the Internal tab: the score counts
    links to pages, the tab files URLs by the Content-Type they answered with. "" when both agree."""
    try:
        from .seo.tabs.internal import content_kind
        from .issues import is_page
    except Exception:
        return ""
    html = files_html = pages_other = 0
    for r in results or ():
        try:
            k = content_kind(r)
        except Exception:
            k = "unknown"
        page = is_page(r)
        html += 1 if k == "html" else 0
        files_html += 1 if (k == "html" and not page) else 0
        pages_other += 1 if (k != "html" and page) else 0
    if not files_html and not pages_other:
        return ""
    text = (f"The Health Score counts {health.get('pages', 0):,} links to pages; {html:,} URLs answered with HTML "
            "(the HTML type above).")
    if files_html:
        one = files_html == 1
        text += (f" {files_html:,} image or file {'address' if one else 'addresses'} answered with an HTML page "
                 f"(usually an error page), so {'it counts' if one else 'they count'} under HTML.")
    if pages_other:
        text += (f" {pages_other:,} page {'link' if pages_other == 1 else 'links'} did not answer with HTML "
                 "(no Content-Type sent, no response, or a file such as a PDF).")
    return text


def external_recheck_note(external_links, validation: Optional[dict]) -> str:
    """", of which N open normally for visitors (live re-check)" for the broken external links the
    validation re-checked; "" when none was re-checked."""
    results = (validation or {}).get("results") or {}
    broken = [e for e in external_links or [] if is_broken_external(e)]
    seen = [results[e.url] for e in broken if isinstance(results.get(e.url), dict)]
    if not seen:
        return ""
    fine = sum(1 for v in seen if v.get("verdict") in ("not_reproduced", "fixed"))
    return (f", of which {fine:,} open normally for visitors in the live re-check (the other site refuses only "
            f"automated checkers) and {len(seen) - fine:,} are confirmed")


EXTERNAL_FIX_NO_RESPONSE = ("The other site gave no answer: open the link in a browser. If the site is gone, "
                            "remove the link or point it to a working page; if it works, it is probably "
                            "blocking crawlers and can stay.")
EXTERNAL_FIX_ERROR = ("Open the link in a browser. If the page is gone, replace the link with a working page "
                      "that covers the same thing, or remove it (a 403 can be the other site blocking crawlers).")


def broken_external_rows(site, external_links) -> List[list]:
    """BROKEN_EXTERNAL_HEADERS rows: every checked external link that answered 4xx/5xx or not at all."""
    rows = []
    for i, e in enumerate([e for e in external_links or [] if is_broken_external(e)], start=1):
        first, pages = found_on(site, e)
        first = first or getattr(e, "parent_url", "") or ""
        rows.append([i, getattr(e, "http_status", "") or getattr(e, "validation_status", ""), e.url, e.domain,
                     first, len(pages) or (1 if first else 0), pages_cell(pages or ([first] if first else [])),
                     e.anchor_text or "-",
                     EXTERNAL_FIX_NO_RESPONSE if getattr(e, "status_code", None) is None else EXTERNAL_FIX_ERROR])
    return rows


def is_internal_target(site, url: str) -> bool:
    """A link target on the crawled site: crawled, or not a recorded external URL and on the site's host."""
    if site is None or not url:
        return False
    if url in site.by_url:
        return True
    if url in externals_by_url(site):
        return False
    try:
        from .seo.tabs.internal import is_internal_url
        return bool(is_internal_url(site, url))
    except Exception:
        return False


def background_links(page) -> List[dict]:
    """CSS background images of one page's HTML that its resources do not list already, the same entries
    as source_index (type "Background Image"): [{"url"}]."""
    p = getattr(page, "seo", None) or {}
    seen = {res.get("url") or "" for res in p.get("resources") or ()}
    out = []
    for bg in p.get("background_images") or ():
        if isinstance(bg, str) and bg and bg not in seen and not bg.startswith("data:"):
            seen.add(bg)
            out.append({"url": bg})
    return out


def redirect_links(site) -> List[dict]:
    """Every HTTP redirect of the crawl as a link, the same entries as source_index (type "HTTP Redirect"):
    [{"source" (the redirecting URL), "url" (where it points, normalised), "raw" (the Location header as
    sent, "" when not kept)}]. Screaming Frog lists these as inlinks and outlinks of type HTTP Redirect."""
    out = []
    if site is None:
        return out
    try:
        from .seo.tabs.internal import header
    except Exception:
        header = None
    for r in (getattr(site, "by_url", None) or {}).values():
        if row_category(r) != "3xx":
            continue
        dest = getattr(r, "redirect_destination", None)
        if not dest or dest == "-":
            continue
        raw = ""
        if header is not None:
            try:
                raw = header(r, "location").strip()
            except Exception:
                raw = ""
        out.append({"source": r.url, "url": _norm_url(dest), "raw": raw})
    return out


def inlink_rows(site) -> List[list]:
    """Every internal link and embed on every crawled page: one row per hyperlink, then one per image,
    script, stylesheet, frame or CSS background image a page loads (once per page), then one per HTTP
    redirect, then one per other link type (canonical, hreflang, rel next / prev, meta refresh, Refresh header,
    JavaScript redirect, mobile alternate, form action, other link elements), pointing at an internal URL (Bulk
    Export > All Inlinks); Type says which and Rel holds the rel values. The same entries as the URL detail pane's
    Inlinks (source_index + other_sources_of); the per-filter Inlinks exports list source_index only."""
    rows = []
    if site is None:
        return rows

    def add(source, dest, anchor, nofollow, region, line, kind, raw, rel=""):
        target = site.by_url.get(dest)
        code = row_status_code(target) if target is not None else None
        rows.append([source, dest, anchor, nofollow, region, line, code if code is not None else "",
                     row_status_text(target) if target is not None else "Not crawled", kind, path_type(raw), rel])

    for source, links in (site.outlinks or {}).items():
        for link in links or []:
            if link.get("internal"):
                add(source, link.get("url") or "", link.get("anchor") or "", bool(link.get("nofollow")),
                    link.get("region") or "", int(link.get("line") or 0), "Hyperlink", link.get("raw") or "",
                    link_rel(link))
    for page in site.pages:
        seen = set()
        for res in (getattr(page, "seo", None) or {}).get("resources") or ():
            url = res.get("url") or ""
            if url and res.get("internal") and url not in seen:
                seen.add(url)
                add(page.url, url, res.get("alt") or "", False, res.get("region") or "",
                    int(res.get("line") or 0), _RESOURCE_TYPES.get(res.get("type") or "", "Other"),
                    res.get("raw") or "", resource_rel(res))
        for bg in background_links(page):
            if is_internal_target(site, bg["url"]):
                add(page.url, bg["url"], "", False, "CSS background image", "", "Background Image", "")
    for rd in redirect_links(site):
        if is_internal_target(site, rd["url"]):
            add(rd["source"], rd["url"], "", False, "HTTP redirect", "", "HTTP Redirect", rd["raw"])
    for dest, entries in (getattr(site, "other_inlinks", None) or {}).items():
        if not is_internal_target(site, dest):
            continue
        for e in entries or ():
            o = _other_entry(e, dest)
            add(o["source"], dest, o["anchor"], False, o["region"], o["line"] or "", o["type"], o["raw"], o["rel"])
    return rows


PATH_TYPES = ("Absolute", "Protocol-Relative", "Root-Relative", "Path-Relative")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def path_type(raw: Any) -> str:
    """How an href / src is written (Screaming Frog "Path Type"): Absolute (https://host/a),
    Protocol-Relative (//host/a), Root-Relative (/a) or Path-Relative (a, ../a, ?q); "" when unknown."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return "Protocol-Relative"
    if _SCHEME.match(text):
        return "Absolute"
    if text.startswith("/") or text.startswith("\\"):
        return "Root-Relative"
    return "Path-Relative"


def filter_source_rows(site, index, tab_id: str, filter_id: str = "all") -> Optional[List[list]]:
    """SOURCE_HEADERS rows: for every URL of one tab filter, one row per page (or redirect) pointing at
    it, i.e. the pages to edit for broken links, redirects, images without alt text and so on.
    None when the tab or filter does not exist."""
    tr = index.tabs.get(tab_id) if index is not None else None
    if tr is None:
        return None
    items = tr.rows if filter_id in ("", "all", None) else tr.filter_rows.get(filter_id)
    if items is None:
        return None
    rows = []
    for r in items:
        url = getattr(r, "url", "") or ""
        code = row_status_code(r)
        code = code if code is not None else ""
        status = row_status_text(r)
        entries = sources_of(site, url)
        if not entries:
            rows.append([url, "(no crawled page links here)", "", "", "", "", "", "", code, status])
            continue
        for e in entries:
            alt = e.get("alt")
            alt_state = ("" if e.get("type") != "Image" else
                         "Missing" if alt is None else "Empty" if not str(alt).strip() else "Present")
            rows.append([url, e.get("source") or "", e.get("type") or "", e.get("anchor") or "", alt_state,
                         bool(e.get("nofollow")), e.get("region") or "", int(e.get("line") or 0) or "", code, status])
    return rows


def issue_url_rows(index, issues: Optional[List[dict]] = None,
                   dismissed_urls: Optional[Dict[str, List[str]]] = None) -> List[list]:
    """ISSUE_URL_HEADERS rows: one per (issue, URL) for every flagged filter, in Issues overview order.
    `issues` (default index.issues) and `dismissed_urls` {"tab:filter": [urls]} leave out what the team
    dismissed (dismiss.apply_dismissals), so the sheet matches the overview's counts."""
    rows = []
    if index is None:
        return rows
    for issue in (issues if issues is not None else index.issues) or []:
        tr = index.tabs.get(issue.get("tab"))
        if tr is None:
            continue
        filter_name = next((f.name for f in tr.tab.filters if f.id == issue.get("filter")), "")
        hidden = set((dismissed_urls or {}).get(f"{issue.get('tab')}:{issue.get('filter')}") or ())
        for r in tr.filter_rows.get(issue.get("filter"), []):
            if hidden and (getattr(r, "url", "") or "") in hidden:
                continue
            code = row_status_code(r)
            rows.append([issue.get("name", ""), issue.get("issue_type") or "", issue.get("priority") or "",
                         getattr(r, "url", "") or "", code if code is not None else "", tr.tab.name, filter_name])
    return rows


def _lines(items: Any, limit: int = 12) -> str:
    """A list as numbered lines for one cell ("" for nothing)."""
    items = [text_value(i).strip() for i in (items or []) if text_value(i).strip()]
    if len(items) == 1:
        return items[0]
    return "\n".join(f"{n}. {t}" for n, t in enumerate(items[:limit], start=1))


def check_rows(checks: Optional[List[dict]]) -> List[list]:
    """CHECKS_HEADERS rows: every check as Passed / Failed / Not checked (failed first). A check that
    did not run is "Not checked" with its reason, never a pass."""
    rows = []
    ordered = sorted(checks or [], key=lambda c: (CHECK_ORDER.get(c.get("status"), 3),
                                                  0 if not c.get("not_applicable") else 1))
    for c in ordered:
        label = "Not applicable" if c.get("not_applicable") else CHECK_LABELS.get(c.get("status"), "Not checked")
        reason = c.get("reason") or ""
        if c.get("dismissed"):
            if c.get("status") == "failed":
                label = "Dismissed"   # flagged, and the team decided it needs no fix: not counted as failed
            reason = (reason + " " if reason else "") + "(Dismissed by the team.)"
        rows.append([c.get("name") or "", label, reason, int(c.get("urls") or 0), c.get("issue_type") or "",
                     c.get("priority") or "", c.get("tab") or "", c.get("filter") or ""])
    return rows


def _attempts_text(attempts: Any) -> str:
    out = []
    for a in attempts or []:
        code = a.get("status") if isinstance(a, dict) else None
        via = " (browser)" if isinstance(a, dict) and a.get("via") == "browser" else ""
        out.append(f"{code if code is not None else 'no response'}{via}")
    return ", ".join(out)


def _reported_text(entry: dict) -> str:
    rep = entry.get("reported") or {}
    return text_value(rep.get("http_status") or rep.get("status") or "")


def _now_text(entry: dict) -> str:
    now = entry.get("now") or {}
    if now.get("http_status") or now.get("status"):
        return text_value(now.get("http_status") or now.get("status"))
    attempts = entry.get("attempts") or []
    if attempts:
        last = attempts[-1].get("status")
        return str(last) if last is not None else "No response"
    return ""


def validation_rows(validation: Optional[dict]) -> List[list]:
    """VALIDATION_HEADERS rows: one per re-checked URL (confirmed first), then the URLs that could not be
    re-checked as "Not checked" with the reason."""
    if not validation:
        return []
    results = validation.get("results") or {}
    rows = []
    for url, e in sorted(results.items(), key=lambda kv: (VERDICT_ORDER.get(kv[1].get("verdict"), 9), kv[0])):
        lop = e.get("link_on_parent")
        link = "Yes" if lop is True else ("No - link removed" if lop is False else "Not checked")
        rows.append([url, VERDICT_LABELS.get(e.get("verdict"), text_value(e.get("verdict"))), _reported_text(e),
                     _now_text(e), _attempts_text(e.get("attempts")), link, e.get("parent") or "",
                     e.get("note") or "", e.get("checked_at") or ""])
    for s in validation.get("skipped") or []:
        rows.append([s.get("url") or "", "Not checked", "", "", "", "Not checked", "", s.get("reason") or "", ""])
    return rows


def login_guard_rows(validation: Optional[dict]) -> List[list]:
    """LOGIN_GUARD_HEADERS rows: each member page requested again without the login."""
    rows = []
    for g in (validation or {}).get("login_guard") or []:
        if g.get("exposed"):
            result = "Exposed"
        elif g.get("protected"):
            result = "Protected"
        elif g.get("verified") is False:
            result = "Not verified"
        else:
            result = "Public"
        status = g.get("logged_out_status")
        rows.append([g.get("url") or "", result, status if status is not None else "No response",
                     g.get("note") or "", _lines(g.get("warnings"))])
    return rows


def fix_plan_rows(intel: Optional[dict]) -> List[list]:
    """FIX_PLAN_HEADERS rows: the root-cause groups of GET /api/intel/errors in fix-first order."""
    groups = sorted((intel or {}).get("groups") or [], key=lambda g: int(g.get("rank") or 10 ** 6))
    rows = []
    for g in groups:
        title = (g.get("title") or "") + (f" ({g['split_note']})" if g.get("split_note") else "")   # "1 more URL in #12"
        rows.append([int(g.get("rank") or 0), title, (g.get("category") or "").replace("_", " "),
                     int(g.get("urls_count") or 0), int(g.get("impact") or 0), g.get("effort") or "",
                     g.get("verified_label") or g.get("verified") or "", _lines(g.get("issues")),
                     g.get("root_cause") or "", g.get("why") or "",
                     _lines(g.get("fix_steps")), _lines(g.get("platform_steps")), g.get("prevent") or "",
                     _lines(g.get("sample_urls"), limit=5)])
    return rows


def _pct(value: Any) -> str:
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return ""


def content_rows(content: Optional[dict]) -> List[list]:
    """CONTENT_HEADERS rows: pages whose URL promises one thing and whose content delivers another, thin
    pages and pages off the topic of their section."""
    content = content or {}
    rows = []
    for m in content.get("mismatches") or []:
        rows.append(["Wrong page type", m.get("url") or "", m.get("url_intent") or "", m.get("content_intent") or "",
                     _pct(m.get("confidence")), "", m.get("evidence") or "", m.get("suggestion") or ""])
    for t in content.get("thin") or []:
        rows.append(["Thin content", t.get("url") or "", "", "", "", int(t.get("words") or 0), t.get("note") or "",
                     t.get("ai_suggestion") or ""])
    for o in content.get("off_topic") or []:
        rows.append(["Off topic", o.get("url") or "", o.get("section") or "", "", "", "", o.get("note") or "",
                     o.get("ai_suggestion") or ""])
    return rows


def dismissed_rows(dismissed: Optional[List[dict]]) -> List[list]:
    """DISMISSED_HEADERS rows: every dismissal and how many URLs it covered in this crawl."""
    rows = []
    for d in dismissed or []:
        rows.append([d.get("issue_name") or d.get("name") or f"{d.get('tab', '')}: {d.get('filter', '')}",
                     d.get("tab") or "", d.get("filter") or "", d.get("pattern") or "(whole issue)",
                     int(d.get("urls") or 0), d.get("note") or "", d.get("created_at") or ""])
    return rows


def _sheet_title(wb, name: str) -> str:
    """A unique, Excel-safe sheet title (31 characters, no []:*?/\\)."""
    base = _SHEET_BAD_CHARS.sub(" ", name).strip()[:31] or "Sheet"
    title, n = base, 2
    while title in wb.sheetnames:
        suffix = f" ({n})"
        title, n = base[:31 - len(suffix)] + suffix, n + 1
    return title


def _discard(wb) -> None:
    """Closes an unfinished write-only workbook whose build failed: each sheet's rows, then the sheet, then its
    temporary file. Left to the garbage collector, an abandoned workbook keeps its temporary files and its sheets
    are closed in any order, which makes lxml print "inconsistent exit action in context manager" tracebacks
    (often in another thread) that hide the real error."""
    for ws in list(getattr(wb, "_sheets", None) or []):
        writer = getattr(ws, "_writer", None)
        if writer is None or getattr(ws, "closed", False):
            continue
        rows = getattr(ws, "_rows", None)
        try:
            if rows is not None:
                rows.close()     # </sheetData> first ...
            writer.close()       # ... then </worksheet>
        except Exception:
            pass
        try:
            writer.cleanup()
        except Exception:
            pass


def _save(wb, filepath: str) -> str:
    """Saves the workbook and returns the path written. It is written next to the target and then swapped in, so
    a crash mid-write never leaves a broken .xlsx; every save has its own temporary name, so two builds of one
    report never write into the same file. When the target cannot be replaced (on Windows: it is still being
    downloaded), the report is saved under a new name instead."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    partial = f"{filepath}.{uuid.uuid4().hex[:12]}.partial"
    try:
        try:
            wb.save(partial)
        except BaseException:
            _discard(wb)
            raise
        try:
            os.replace(partial, filepath)
        except OSError:
            root, ext = os.path.splitext(filepath)
            filepath = f"{root}_{time.strftime('%H%M%S')}{uuid.uuid4().hex[:4]}{ext}"
            os.replace(partial, filepath)
    finally:
        if os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError:
                pass
    return filepath


HYPERLINK_ROW_LIMIT = 65_000   # Excel caps hyperlinks at 65,530 per sheet


def _link(value: Any) -> str:
    """The cell value when it can be a clickable link (Excel rejects hyperlinks over ~2000 characters)."""
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) and len(value) < 2000 else ""


def _xl(value: Any) -> Any:
    """xl_value that keeps empty cells empty."""
    return None if value is None else xl_value(value)


@lru_cache(maxsize=None)
def _align(horizontal: str = "left", vertical: str = "center", wrap: bool = False) -> Alignment:
    return Alignment(horizontal=horizontal, vertical=vertical, wrap_text=wrap)


class _Styler:
    """Styled cells for one write-only sheet. Each (font, fill, border, alignment) combination is looked up in the
    workbook's style tables once and copied afterwards (four look-ups per cell made big workbooks slow)."""

    def __init__(self, ws):
        self.ws = ws
        self._cache: Dict[tuple, tuple] = {}

    def cell(self, value: Any, font=None, fill=None, border=None, alignment=None, hyperlink: str = ""):
        c = WriteOnlyCell(self.ws, value=value)
        key = (id(font), id(fill), id(border), id(alignment))
        hit = self._cache.get(key)
        if hit is None:
            if font is not None:
                c.font = font
            if fill is not None:
                c.fill = fill
            if border is not None:
                c.border = border
            if alignment is not None:
                c.alignment = alignment
            self._cache[key] = (copy(c._style), (font, fill, border, alignment))   # keeps the ids alive
        else:
            c._style = copy(hit[0])
        if hyperlink:
            c.hyperlink = hyperlink
        return c

    def header(self, headers: List[str], fill, wrap: bool = False) -> list:
        align = _align("center", "center", wrap)
        return [self.cell(h, font=WHITE_BOLD_FONT, fill=fill, alignment=align) for h in headers]


def _new_sheet(wb, title: str, widths=(), freeze: str = "A2", header_height: Optional[float] = 28):
    """A write-only sheet with its column widths, frozen panes and header height set: they are written before
    the first row, so they must be known up front."""
    ws = wb.create_sheet(title=title)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    if freeze:
        ws.freeze_panes = freeze
    if header_height:
        ws.row_dimensions[1].height = header_height
    return ws


def _location(r: CrawlResult) -> str:
    if r.source_file:
        return f"JavaScript › {r.source_file}"
    parts = [p for p in (f"Line {r.link_line}" if r.link_line else "", r.link_region, r.link_selector) if p]
    return " › ".join(parts) or "-"


def _internal_row(idx: int, r: CrawlResult) -> list:
    return [
        idx, r.page_title, r.url, r.parent_url, r.anchor_text or "-", _location(r),
        r.http_status, r.status_code if r.status_code is not None else "N/A",
        CATEGORY_LABELS.get(classify_result(r), ""), r.redirect_destination or "-",
        r.depth, r.discovery_source, r.content_type or "-", r.response_time_ms, r.error_message or "-",
    ]


def _issue_row(idx: int, r: CrawlResult, site=None) -> list:
    d = diagnose(r) or {}
    first, pages = found_on(site, r) if site is not None else (r.parent_url, [])
    return [
        idx, r.http_status, d.get("title", ""), (d.get("severity") or "").upper(), r.url, first or r.parent_url,
        len(pages), pages_cell(pages),
        r.anchor_text or "-", _location(r), r.link_html or "-", d.get("summary", ""),
        "\n".join(f"• {c}" for c in d.get("causes", [])), d.get("fix", ""), r.response_time_ms,
    ]


def _external_row(idx: int, e: ExternalLink) -> list:
    return [idx, e.url, e.domain, e.parent_url, e.anchor_text,
            e.validation_status or "Recorded (Not Followed)", e.discovery_source]


class ExcelExporter:
    @staticmethod
    def export(
        results: List[CrawlResult],
        filepath: str,
        stats: dict = None,
        external_links: Optional[List[ExternalLink]] = None,
        category: str = "all",
        site=None,
        seo_index=None,
        issues: Optional[List[dict]] = None,
        checks: Optional[List[dict]] = None,
        validation: Optional[dict] = None,
        fix_plan: Optional[dict] = None,
        content: Optional[dict] = None,
        dismissed: Optional[List[dict]] = None,
        dismissed_urls: Optional[Dict[str, List[str]]] = None,
        fmt: str = "xlsx",
    ) -> str:
        """
        Exports crawl results into a styled Excel report (fmt="xlsx") or the same tables as CSV (fmt="csv":
        one .csv when the export has one table, else a .zip with one .csv per sheet; see export_csv).
        category="all" builds the full workbook:
          Crawl Summary, Issues Overview (SEO), then the follow-up sheets that have data (Fix Plan,
          Validation, Login Check, Checks, Content Intelligence, Dismissed Issues), Internal URLs,
          Broken Links & Errors, Redirects (3xx), External Links, then one sheet per non-empty SEO tab
          (when site and seo_index are given).
        Any other key of EXPORT_CATEGORIES builds a focused workbook: Crawl Summary + that category.
        URL and parent-page cells are clickable hyperlinks. The workbook is write-only (rows stream to a temporary
        file, so memory stays flat on big sites). Returns the path written (see _save).
        issues      Issues overview rows to show instead of seo_index.issues (e.g. without dismissed URLs)
        checks      GET /api/crawl/checks "checks" (Passed / Failed / Not checked per check)
        validation  GET /api/validate/status (live re-check verdicts and the login check)
        fix_plan    GET /api/intel/errors (root-cause groups in fix-first order)
        content     GET /api/intel/content (wrong page type, thin and off-topic pages)
        dismissed   GET /api/issues/dismissed "dismissed"
        dismissed_urls  {"tab:filter": [urls]} the team dismissed (left out of the Issue URLs sheet)
        """
        if category not in EXPORT_CATEGORIES:
            raise ValueError(f"Unknown export category: {category}")
        if fmt == "csv":
            return ExcelExporter.export_csv(results, filepath, stats, external_links, category, site, seo_index,
                                            issues, checks, validation, fix_plan, content, dismissed,
                                            dismissed_urls)
        check_format(fmt)
        overview = issues   # the Issues overview rows (the name `issues` is reused for broken URLs below)
        wb = Workbook(write_only=True)   # rows stream to disk: memory stays flat on big sites
        try:
            stats = stats or {}
            external_links = external_links or []

            ExcelExporter._summary_sheet(wb, results, stats, external_links, category, validation)

            if category == "all" and seo_index is not None:
                ExcelExporter._issues_overview_sheet(wb, list(overview if overview is not None else seo_index.issues or []))
            if category == "all":
                ExcelExporter._follow_up_sheets(wb, checks, validation, fix_plan, content, dismissed)

            if category == "all":
                ExcelExporter._sheet(wb, "Internal URLs", INTERNAL_HEADERS, NAVY_FILL, "internal",
                                     [(r, _internal_row(i, r)) for i, r in enumerate(results, start=1)])
                issues = sorted([r for r in results if classify_result(r) in ISSUE_CATEGORIES], key=issue_sort_key)
                ExcelExporter._sheet(wb, "Broken Links & Errors", ISSUE_HEADERS, RED_HEADER_FILL, "issue",
                                     [(r, _issue_row(i, r, site)) for i, r in enumerate(issues, start=1)])
                ExcelExporter._broken_external_sheet(wb, site, external_links)
                redirects = [r for r in results if classify_result(r) == "3xx"]
                ExcelExporter._sheet(wb, "Redirects (3xx)", INTERNAL_HEADERS, ORANGE_FILL, "internal",
                                     [(r, _internal_row(i, r)) for i, r in enumerate(redirects, start=1)])
                blocked = [r for r in results if classify_result(r) == "blocked"]
                if blocked:
                    ExcelExporter._sheet(wb, "Blocked by robots.txt", INTERNAL_HEADERS, SLATE_FILL, "internal",
                                         [(r, _internal_row(i, r)) for i, r in enumerate(blocked, start=1)])
                ExcelExporter._sheet(wb, "External Links", EXTERNAL_HEADERS, PURPLE_FILL, "external",
                                     [(None, _external_row(i, e)) for i, e in enumerate(external_links, start=1)])
            elif category == "external":
                ExcelExporter._sheet(wb, "External Links", EXTERNAL_HEADERS, PURPLE_FILL, "external",
                                     [(None, _external_row(i, e)) for i, e in enumerate(external_links, start=1)])
                ExcelExporter._broken_external_sheet(wb, site, external_links)
            else:
                title, keep, layout = EXPORT_CATEGORIES[category]
                subset = [r for r in results if keep(r)]
                if layout == "issue":
                    subset.sort(key=issue_sort_key)
                    ExcelExporter._sheet(wb, title, ISSUE_HEADERS, RED_HEADER_FILL, "issue",
                                         [(r, _issue_row(i, r, site)) for i, r in enumerate(subset, start=1)])
                    if category == "issues":
                        ExcelExporter._broken_external_sheet(wb, site, external_links)
                else:
                    fill = ORANGE_FILL if category == "3xx" else NAVY_FILL
                    ExcelExporter._sheet(wb, title, INTERNAL_HEADERS, fill, "internal",
                                         [(r, _internal_row(i, r)) for i, r in enumerate(subset, start=1)])

            if category == "all" and seo_index is not None:
                # Which URLs have each SEO issue (the overview only gives counts)
                ExcelExporter._rows_sheet(wb, "Issue URLs", ISSUE_URL_HEADERS,
                                          issue_url_rows(seo_index, overview, dismissed_urls),
                                          widths=(48, 13, 14, 70, 11, 18, 40), cap=ISSUE_URL_SHEET_CAP, url_col=4)
            if category == "all" and site is not None and seo_index is not None:
                for tab_id, tr in sorted(seo_index.tabs.items(), key=lambda kv: kv[1].tab.order):
                    if not tr.rows:
                        continue
                    why = _tab_not_checked(tr)
                    if why:   # not connected / did not run: say so, never a sheet of empty cells
                        write_table(wb, not_checked_table(tr.tab.name, why))
                        continue
                    columns, rows = tab_grid(site, seo_index, tab_id)
                    ExcelExporter._grid_sheet(wb, tr.tab.name, columns, rows or [], cap=TAB_SHEET_ROW_CAP,
                                              link_all_urls=False)
            return _save(wb, filepath)
        except BaseException:
            _discard(wb)
            raise

    @staticmethod
    def export_csv(results: List[CrawlResult], filepath: str, stats: dict = None,
                   external_links: Optional[List[ExternalLink]] = None, category: str = "all", site=None,
                   seo_index=None, issues: Optional[List[dict]] = None, checks: Optional[List[dict]] = None,
                   validation: Optional[dict] = None, fix_plan: Optional[dict] = None, content: Optional[dict] = None,
                   dismissed: Optional[List[dict]] = None,
                   dismissed_urls: Optional[Dict[str, List[str]]] = None) -> str:
        """The CSV twin of export(): the same tables with the same columns (tables()), written as one UTF-8 .csv
        when there is one table, else as a .zip with one .csv per sheet. Returns the path written (the
        extension of filepath is replaced by .csv / .zip)."""
        tables = ExcelExporter.tables(results, stats, external_links, category, site, seo_index, issues, checks,
                                      validation, fix_plan, content, dismissed, dismissed_urls)
        name, data, _ = tables_bytes(tables, "csv", os.path.splitext(os.path.basename(filepath))[0])
        return _save_bytes(data, _with_ext(filepath, os.path.splitext(name)[1]))

    @staticmethod
    def tables(results: List[CrawlResult], stats: dict = None, external_links: Optional[List[ExternalLink]] = None,
               category: str = "all", site=None, seo_index=None, issues: Optional[List[dict]] = None,
               checks: Optional[List[dict]] = None, validation: Optional[dict] = None,
               fix_plan: Optional[dict] = None, content: Optional[dict] = None,
               dismissed: Optional[List[dict]] = None,
               dismissed_urls: Optional[Dict[str, List[str]]] = None) -> List["Table"]:
        """The sheets of export() as plain tables, same order and columns. A category export (2xx, 4xx ...)
        gives its URL table only (plus Broken External Links for "issues"): the summary block is a sheet
        heading in Excel, not data. Tab tables are not capped (Excel caps them at TAB_SHEET_ROW_CAP)."""
        if category not in EXPORT_CATEGORIES:
            raise ValueError(f"Unknown export category: {category}")
        stats = stats or {}
        external_links = external_links or []
        out: List[Table] = []

        def add(title, headers, rows, url_cols=(), keep_empty=True):
            if rows or keep_empty:
                out.append(Table(title, list(headers), [list(r) for r in rows], url_cols=tuple(url_cols)))

        broken_ext = broken_external_rows(site, external_links)
        if category == "all":
            add("Crawl Summary", ["Item", "Value"],
                [["Report", "ARACHNIX Audit Report"]]
                + [[k, str(v)] for k, v in ExcelExporter._summary_rows(results, stats, external_links, validation)])
            if seo_index is not None:
                add("Issues Overview", ISSUES_OVERVIEW_HEADERS,
                    issues_overview_rows(list(issues if issues is not None else seo_index.issues or [])))
            for title, headers, rows in (("Fix Plan", FIX_PLAN_HEADERS, fix_plan_rows(fix_plan)),
                                         ("Validation", VALIDATION_HEADERS, validation_rows(validation)),
                                         ("Login Check", LOGIN_GUARD_HEADERS, login_guard_rows(validation)),
                                         ("Checks", CHECKS_HEADERS, check_rows(checks)),
                                         ("Content Intelligence", CONTENT_HEADERS, content_rows(content)),
                                         ("Dismissed Issues", DISMISSED_HEADERS, dismissed_rows(dismissed))):
                add(title, headers, rows, keep_empty=False)
            add("Internal URLs", INTERNAL_HEADERS, [_internal_row(i, r) for i, r in enumerate(results, start=1)],
                url_cols=(3, 4))
            broken = sorted([r for r in results if classify_result(r) in ISSUE_CATEGORIES], key=issue_sort_key)
            add("Broken Links & Errors", ISSUE_HEADERS, [_issue_row(i, r, site) for i, r in enumerate(broken, 1)],
                url_cols=(5, 6))
            add("Broken External Links", BROKEN_EXTERNAL_HEADERS, broken_ext, url_cols=(3, 5), keep_empty=False)
            redirects = [r for r in results if classify_result(r) == "3xx"]
            add("Redirects (3xx)", INTERNAL_HEADERS, [_internal_row(i, r) for i, r in enumerate(redirects, 1)],
                url_cols=(3, 4))
            blocked = [r for r in results if classify_result(r) == "blocked"]
            add("Blocked by robots.txt", INTERNAL_HEADERS, [_internal_row(i, r) for i, r in enumerate(blocked, 1)],
                url_cols=(3, 4), keep_empty=False)
            add("External Links", EXTERNAL_HEADERS, [_external_row(i, e) for i, e in enumerate(external_links, 1)],
                url_cols=(2, 4))
            if seo_index is not None:
                add("Issue URLs", ISSUE_URL_HEADERS, issue_url_rows(seo_index, issues, dismissed_urls), url_cols=(4,))
            if site is not None and seo_index is not None:
                for tab_id, tr in sorted(seo_index.tabs.items(), key=lambda kv: kv[1].tab.order):
                    if tr.rows:
                        why = _tab_not_checked(tr)
                        if why:
                            out.append(not_checked_table(tr.tab.name, why))
                            continue
                        columns, rows = tab_grid(site, seo_index, tab_id)
                        out.append(grid_table(tr.tab.name, columns, rows or []))
        elif category == "external":
            add("External Links", EXTERNAL_HEADERS, [_external_row(i, e) for i, e in enumerate(external_links, 1)],
                url_cols=(2, 4))
            add("Broken External Links", BROKEN_EXTERNAL_HEADERS, broken_ext, url_cols=(3, 5), keep_empty=False)
        else:
            title, keep, layout = EXPORT_CATEGORIES[category]
            subset = [r for r in results if keep(r)]
            if layout == "issue":
                subset.sort(key=issue_sort_key)
                add(title, ISSUE_HEADERS, [_issue_row(i, r, site) for i, r in enumerate(subset, 1)], url_cols=(5, 6))
                if category == "issues":
                    add("Broken External Links", BROKEN_EXTERNAL_HEADERS, broken_ext, url_cols=(3, 5),
                        keep_empty=False)
            else:
                add(title, INTERNAL_HEADERS, [_internal_row(i, r) for i, r in enumerate(subset, 1)], url_cols=(3, 4))
        return out

    # -----------------------------------------------------------------------------------------
    # SEO tab exports
    # -----------------------------------------------------------------------------------------
    @staticmethod
    def export_tab(filepath: str, title: str, columns: List[dict], rows: List[dict], fmt: str = "xlsx") -> str:
        """One tab grid (all rows of the chosen filter, the tab's columns) as a single-sheet workbook, or as a
        CSV with the same columns and rows (fmt="csv"; written next to filepath with a .csv extension)."""
        if fmt == "csv":
            t = grid_table(title, columns, rows)
            return _save_bytes(csv_bytes(t.headers, t.rows), _with_ext(filepath, ".csv"))
        check_format(fmt)
        wb = Workbook(write_only=True)   # rows stream to disk: memory stays flat on big sites
        try:
            ExcelExporter._grid_sheet(wb, title, columns, rows, cap=None, link_all_urls=True)
            return _save(wb, filepath)
        except BaseException:
            _discard(wb)
            raise

    @staticmethod
    def export_issues_overview(filepath: str, issues: List[dict], fmt: str = "xlsx") -> str:
        """Issues overview (Screaming Frog's Issues tab): name, type, priority, URLs, %, description, fix."""
        if fmt == "csv":
            return _save_bytes(csv_bytes(ISSUES_OVERVIEW_HEADERS, issues_overview_rows(issues)),
                               _with_ext(filepath, ".csv"))
        check_format(fmt)
        wb = Workbook(write_only=True)   # rows stream to disk: memory stays flat on big sites
        try:
            ExcelExporter._issues_overview_sheet(wb, issues)
            return _save(wb, filepath)
        except BaseException:
            _discard(wb)
            raise

    @staticmethod
    def export_inlinks(filepath: str, rows: List[list], fmt: str = "xlsx") -> str:
        """Bulk export of every internal link and embed. Streams rows (write-only) so large sites stay fast."""
        return ExcelExporter.export_rows(filepath, "All Inlinks", INLINK_HEADERS, rows,
                                         widths=(60, 60, 36, 10, 20, 12, 12, 24, 12, 16), fmt=fmt)

    @staticmethod
    def export_rows(filepath: str, title: str, headers: List[str], rows: List[list],
                    widths: Optional[tuple] = None, fmt: str = "xlsx") -> str:
        """A single-sheet bulk export (header + rows), streamed write-only so large sites stay fast; fmt="csv"
        writes the same header and rows as a UTF-8 CSV (with BOM, so Excel reads accents) next to filepath."""
        if fmt == "csv":
            return _save_bytes(csv_bytes(headers, rows), _with_ext(filepath, ".csv"))
        check_format(fmt)
        wb = Workbook(write_only=True)
        try:
            ws = wb.create_sheet(title=_SHEET_BAD_CHARS.sub(" ", title).strip()[:31] or "Export")
            for i, w in enumerate(widths or (), start=1):
                ws.column_dimensions[get_column_letter(i)].width = w
            ws.freeze_panes = "A2"
            header = []
            for h in headers:
                c = WriteOnlyCell(ws, value=h)
                c.fill, c.font = TEAL_FILL, WHITE_BOLD_FONT
                header.append(c)
            ws.append(header)
            for row in rows[:INLINKS_ROW_CAP]:
                ws.append([xl_value(v) for v in row])
            if len(rows) > INLINKS_ROW_CAP:
                ws.append([f"Showing the first {INLINKS_ROW_CAP:,} of {len(rows):,} rows (Excel row limit)."])
            return _save(wb, filepath)
        except BaseException:
            _discard(wb)
            raise

    @staticmethod
    def _rows_sheet(wb, title: str, headers: List[str], rows: List[list], widths: tuple = (),
                    cap: Optional[int] = None, url_col: Optional[int] = None):
        """A plain table sheet (header row, rows, optional hyperlinked URL column)."""
        ws = _new_sheet(wb, _sheet_title(wb, title), widths)
        st = _Styler(ws)
        ws.append(st.header(headers, TEAL_FILL))
        shown = rows if cap is None else rows[:cap]
        for row_num, row in enumerate(shown, start=2):
            values = [xl_value(v) for v in row]
            if url_col and len(values) >= url_col and row_num < HYPERLINK_ROW_LIMIT:
                link = _link(values[url_col - 1])
                if link:
                    values[url_col - 1] = st.cell(link, font=LINK_FONT, hyperlink=link)
            ws.append(values)
        if cap is not None and len(rows) > cap:
            ws.append([f"Showing the first {cap:,} of {len(rows):,} rows. Use Bulk export for every row."])
        ws.auto_filter.ref = f"A1:{get_column_letter(max(1, len(headers)))}{len(shown) + 1}"
        return ws

    @staticmethod
    def _follow_up_sheets(wb, checks, validation, fix_plan, content, dismissed):
        """Fix Plan, Validation, Login Check, Checks, Content Intelligence and Dismissed Issues sheets,
        each only when it has rows."""
        rows = fix_plan_rows(fix_plan)
        if rows:
            ExcelExporter._table_sheet(wb, "Fix Plan", FIX_PLAN_HEADERS, rows,
                                       widths=(9, 44, 12, 8, 10, 9, 11, 40, 50, 50, 60, 50, 44, 60),
                                       wrap_cols=(2, 8, 9, 10, 11, 12, 13, 14), center_cols=(1, 4, 5, 6, 7),
                                       header_fill=NAVY_FILL)
        rows = validation_rows(validation)
        if rows:
            ExcelExporter._table_sheet(wb, "Validation", VALIDATION_HEADERS, rows,
                                       widths=(60, 15, 18, 18, 24, 18, 50, 60, 21), wrap_cols=(8,),
                                       url_cols=(1, 7), result_col=2, center_cols=(2, 3, 4, 6))
        rows = login_guard_rows(validation)
        if rows:
            ExcelExporter._table_sheet(wb, "Login Check", LOGIN_GUARD_HEADERS, rows, widths=(60, 14, 16, 70, 70),
                                       wrap_cols=(4, 5), url_cols=(1,), result_col=2, center_cols=(2, 3))
        rows = check_rows(checks)
        if rows:
            ExcelExporter._table_sheet(wb, "Checks", CHECKS_HEADERS, rows, widths=(52, 15, 70, 9, 13, 11, 16, 26),
                                       wrap_cols=(3,), result_col=2, center_cols=(2, 4, 5, 6))
        rows = content_rows(content)
        if rows:
            ExcelExporter._table_sheet(wb, "Content Intelligence", CONTENT_HEADERS, rows,
                                       widths=(17, 60, 14, 14, 11, 8, 70, 60), wrap_cols=(7, 8), url_cols=(2,),
                                       result_col=1, center_cols=(3, 4, 5, 6))
        rows = dismissed_rows(dismissed)
        if rows:
            ExcelExporter._table_sheet(wb, "Dismissed Issues", DISMISSED_HEADERS, rows,
                                       widths=(48, 16, 26, 30, 12, 60, 21), wrap_cols=(6,), center_cols=(5,),
                                       header_fill=SLATE_FILL)

    @staticmethod
    def _table_sheet(wb, title: str, headers: List[str], rows: List[list], widths: tuple = (),
                     wrap_cols: tuple = (), url_cols: tuple = (), result_col: Optional[int] = None,
                     center_cols: tuple = (), header_fill=TEAL_FILL):
        """A styled table: header row, bordered rows, wrapped long-text columns, hyperlinked URL columns and
        the result column tinted (green = fine, red = problem, amber = check, grey = not checked)."""
        ws = _new_sheet(wb, _sheet_title(wb, title), widths, freeze="B2", header_height=30)
        st = _Styler(ws)
        ws.append(st.header(headers, header_fill, wrap=True))
        aligns = [_align("center" if ci in center_cols else "left", "top", ci in wrap_cols)
                  for ci in range(1, len(headers) + 1)]
        for row_num, row in enumerate(rows, start=2):
            values = [xl_value(v) for v in row]
            result_fill = RESULT_FILLS.get(str(row[result_col - 1])) if result_col else None
            cells = []
            for ci in range(1, len(headers) + 1):
                value = values[ci - 1] if ci <= len(values) else None
                link = _link(value) if ci in url_cols and row_num < HYPERLINK_ROW_LIMIT else ""
                font, fill = (LINK_FONT if link else REGULAR_FONT), None
                if ci == result_col and result_fill:
                    font, fill = BOLD_FONT, result_fill
                cells.append(st.cell(value, font=font, fill=fill, border=THIN_BORDER, alignment=aligns[ci - 1],
                                     hyperlink=link))
            ws.append(cells)
        ws.auto_filter.ref = f"A1:{get_column_letter(max(1, len(headers)))}{len(rows) + 1}"
        return ws

    @staticmethod
    def _broken_external_sheet(wb, site, external_links):
        """Links to other websites that the checker found broken, with every page that links to each."""
        rows = broken_external_rows(site, external_links)
        if not rows:
            return None
        return ExcelExporter._sheet(wb, "Broken External Links", BROKEN_EXTERNAL_HEADERS, RED_HEADER_FILL,
                                    "broken_external", [(None, row) for row in rows])

    @staticmethod
    def _grid_sheet(wb, title: str, columns: List[dict], rows: List[dict], cap: Optional[int] = None,
                    link_all_urls: bool = True):
        """A registry grid as a sheet: header row, one row per URL, URL cells as hyperlinks
        (every url column, or only the first one when link_all_urls is False), status cells tinted."""
        headers = [c["label"] for c in columns]
        shown = rows if cap is None else rows[:cap]
        widths = [len(h) for h in headers]
        for row in shown[:201]:            # column widths from the header and the first 200 rows
            for i, c in enumerate(columns):
                widths[i] = max(widths[i], len(str(xl_value(row["cells"].get(c["id"], "")))))
        ws = _new_sheet(wb, _sheet_title(wb, title), [min(max(w + 3, 10), 65) for w in widths])
        st = _Styler(ws)
        ws.append(st.header(headers, TEAL_FILL))

        url_cols = [i for i, c in enumerate(columns, start=1) if c.get("kind") == "url"]
        if not link_all_urls:
            url_cols = url_cols[:1]
        status_cols = [i for i, c in enumerate(columns, start=1) if c.get("kind") == "status"]
        for row_num, row in enumerate(shown, start=2):
            values = [xl_value(row["cells"].get(c["id"], "")) for c in columns]
            fill = CATEGORY_FILLS.get(row.get("category") or "")
            if fill:
                for ci in status_cols:
                    values[ci - 1] = st.cell(values[ci - 1], fill=fill)
            if row_num < HYPERLINK_ROW_LIMIT:
                for ci in url_cols:
                    value = values[ci - 1]
                    link = _link(value)
                    if link:
                        values[ci - 1] = st.cell(link, font=LINK_FONT, fill=fill if ci in status_cols else None,
                                                 hyperlink=link)
            ws.append(values)
        if cap is not None and len(rows) > cap:
            ws.append([f"Showing the first {cap:,} of {len(rows):,} rows. Export this tab on its own for every row."])
        ws.auto_filter.ref = f"A1:{get_column_letter(max(1, len(headers)))}{len(shown) + 1}"
        return ws

    @staticmethod
    def _issues_overview_sheet(wb, issues: List[dict]):
        ws = _new_sheet(wb, _sheet_title(wb, "Issues Overview"), (48, 13, 14, 9, 11, 70, 70), freeze="B2")
        st = _Styler(ws)
        ws.append(st.header(ISSUES_OVERVIEW_HEADERS, RED_HEADER_FILL))
        aligns = [_align("center" if ci in (2, 3, 4, 5) else "left", "top", ci in (6, 7))
                  for ci in range(1, len(ISSUES_OVERVIEW_HEADERS) + 1)]
        warning = checks_warning_row()
        if warning:       # an SEO check module failed to load: say so above the issues, in red
            ws.append([st.cell(xl_value(v), font=BOLD_FONT, fill=PatternFill(start_color="FCA5A5", end_color="FCA5A5",
                                                                              fill_type="solid"),
                               border=THIN_BORDER, alignment=aligns[ci - 1])
                       for ci, v in enumerate(warning, start=1)])
        for i, row in zip(issues, issues_overview_rows(issues, warn=False)):
            values = [xl_value(v) for v in row]
            fills = {2: ISSUE_TYPE_FILLS.get(i.get("issue_type") or ""), 3: PRIORITY_FILLS.get(i.get("priority") or "")}
            ws.append([st.cell(v, font=BOLD_FONT if ci == 1 else REGULAR_FONT, fill=fills.get(ci),
                               border=THIN_BORDER, alignment=aligns[ci - 1])
                       for ci, v in enumerate(values, start=1)])
        ws.auto_filter.ref = f"A1:G{len(issues) + 1 + (1 if warning else 0)}"
        return ws

    @staticmethod
    def _sheet(wb, title: str, headers: list, header_fill, kind: str, rows: list):
        link_cols = {headers.index(h) + 1 for h in LINK_COLUMNS[kind] if h in headers}
        status_cols = {headers.index(h) + 1 for h in ("HTTP Status", "Status Code", "Category", "Issue") if h in headers}
        centered = {headers.index(h) + 1 for h in CENTERED if h in headers}
        wrap_cols = {headers.index(h) + 1 for h in ("Likely Causes", "Why It Happens", "How To Fix",
                                                    "All Linking Pages") if h in headers}
        # Column widths from the longest first line in each column (header included)
        widths = [len(str(h or "").split("\n")[0]) for h in headers]
        for _, values in rows:
            for i, v in enumerate(values[:len(headers)]):
                n = len(str(v or "").split("\n")[0])
                if n > widths[i]:
                    widths[i] = n
        ws = _new_sheet(wb, title, [min(max(w + 3, 10), 65) for w in widths])
        st = _Styler(ws)
        ws.append(st.header(headers, header_fill))
        vertical = "top" if wrap_cols else "center"
        aligns = [_align("center" if ci in centered else "left", vertical, ci in wrap_cols)
                  for ci in range(1, len(headers) + 1)]

        for row_num, (result, values) in enumerate(rows, start=2):
            fill = CATEGORY_FILLS.get(classify_result(result)) if result is not None else None
            cells = []
            for ci in range(1, len(headers) + 1):
                value = _xl(values[ci - 1]) if ci <= len(values) else None
                link = _link(value) if ci in link_cols and row_num < HYPERLINK_ROW_LIMIT else ""
                cells.append(st.cell(value, font=LINK_FONT if link else REGULAR_FONT,
                                     fill=fill if ci in status_cols else None, border=THIN_BORDER,
                                     alignment=aligns[ci - 1], hyperlink=link))
            ws.append(cells)
        ws.auto_filter.ref = f"A1:{get_column_letter(max(1, len(headers)))}{len(rows) + 1}"
        return ws

    @staticmethod
    def _summary_sheet(wb, results: List[CrawlResult], stats: dict, external_links: list, category: str,
                       validation: Optional[dict] = None):
        summary_rows = ExcelExporter._summary_rows(results, stats, external_links, validation)
        ws = _new_sheet(wb, "Crawl Summary", (36, 60), freeze="", header_height=36)
        for row_i in range(3, 3 + len(summary_rows)):
            ws.row_dimensions[row_i].height = 22
        st = _Styler(ws)
        scope = "" if category == "all" else f" \u2014 {EXPORT_CATEGORIES[category][0]}"
        ws.append([st.cell(f"ARACHNIX Audit Report{scope}", font=SUMMARY_TITLE_FONT, fill=NAVY_FILL,
                           alignment=_align("center", "center"))])
        ws.merged_cells.add("A1:B1")
        ws.append([])
        for k, v in summary_rows:
            ws.append([st.cell(k, font=BOLD_FONT, fill=LABEL_FILL, border=THIN_BORDER),
                       st.cell(xl_value(str(v)), font=REGULAR_FONT, border=THIN_BORDER)])
        return ws

    @staticmethod
    def _summary_rows(results: List[CrawlResult], stats: dict, external_links: list,
                      validation: Optional[dict] = None) -> List[Tuple[str, Any]]:
        """(label, value) rows of the Crawl Summary sheet; every status class count comes from the HTTP
        status (classify_result), so they add up to the internal URLs. "Total URLs Crawled" is Screaming
        Frog's (and the dashboard Overview's): internal + external URLs minus the ones never requested
        (blocked by robots.txt, external links not checked)."""
        total_internal = len(results)
        totals = crawl_totals(results, external_links)
        counts = {k: 0 for k in CATEGORY_LABELS}
        for r in results:
            counts[classify_result(r)] += 1
        health = health_score(results)

        def pct(n: int) -> str:
            return f"{n} ({round(n / max(1, total_internal) * 100, 1)}%)"

        limitations = stats.get("limitations", [])
        not_crawled = int(stats.get("not_crawled_max_urls") or 0) + int(stats.get("not_crawled_max_depth") or 0)
        status = stats.get("status", "completed").capitalize()
        if stats.get("limit_reached"):
            status += " (stopped at the Max URLs limit: part of the site only)"
        discovered = stats.get("total_discovered", total_internal)
        if not_crawled:
            discovered = (f"{int(discovered) + not_crawled:,} ({total_internal:,} in this report, {not_crawled:,} "
                          "found but not crawled because of the Max URLs / Max depth limits)")
        checked = totals["external"] - totals["external_blocked"] - totals["external_not_checked"]
        broken_ext = sum(1 for e in external_links if is_broken_external(e))
        if checked or totals["external_blocked"]:
            blocked_ext = (f", {totals['external_blocked']:,} blocked by their robots.txt"
                           if totals["external_blocked"] else "")
            ext_text = (f"{len(external_links):,} ({checked:,} checked for their status, {broken_ext:,} broken "
                        f"or unreachable{external_recheck_note(external_links, validation)}{blocked_ext}; "
                        "the other sites' pages are not crawled)")
        elif not external_links:
            ext_text = "0 (no links to other websites were found)"
        else:
            ext_text = f"{len(external_links):,} (recorded only; their status was not checked)"
        summary_rows = [
            ("Target Start URL", stats.get("start_url", results[0].url if results else "N/A")),
            ("Crawl Date & Time", stats.get("started_at") or time.strftime("%Y-%m-%d %H:%M:%S")),
            ("Completion Status", status),
            ("Authentication Used", "Yes - Authenticated" if stats.get("auth_used") else "No (Public Crawl)"),
            ("Authentication Details", stats.get("auth_status", "Not required")),
            ("Total Unique Internal URLs Discovered", discovered),
            ("Total URLs Crawled", f"{totals['crawled']:,} ({crawled_note(totals)})"),
            ("Total Internal URLs", f"{total_internal:,} (every status class below adds up to it)"),
            ("Health Score", f"{health['score']}% ({health['pages_ok']} of {health['pages']} HTML pages load)"),
            ("Internal URLs by Type", content_type_split(results)),
        ]
        why = pages_vs_html_note(results, health)
        if why:   # the health score counts page links; the Internal tab files URLs by the Content-Type answered
            summary_rows.append(("Pages vs HTML", why))
        summary_rows += [
            (f"{CATEGORY_LABELS['2xx']} (URLs)", pct(counts["2xx"])),
            (f"{CATEGORY_LABELS['3xx']} (URLs)", pct(counts["3xx"])),
            (f"{CATEGORY_LABELS['4xx']} (URLs)", pct(counts["4xx"])),
            (f"{CATEGORY_LABELS['5xx']} (URLs)", pct(counts["5xx"])),
            (f"{CATEGORY_LABELS['error']} (URLs)", pct(counts["error"])),
            ("4xx Found Only in JavaScript",
             f"{sum(1 for r in results if classify_result(r) == '4xx' and found_in_code(r))} "
             "(of the 4xx above; the link is in the site's code, not in the page HTML)"),
            (f"{CATEGORY_LABELS['blocked']} (URLs)", pct(counts["blocked"])),
            ("External Links Discovered", ext_text),
            ("Unique External Domains", len(set(e.domain for e in external_links if e.domain))),
            ("Crawl Duration", f"{stats.get('elapsed_seconds', 'N/A')}s"),
            ("Identified Limitations & Notes", "; ".join(limitations) if limitations else "None encountered"),
        ]
        return summary_rows


# ---------------------------------------------------------------------------------------------
# CSV, report tables and in-memory files (Reports, Bulk Export, Multi-Export, detail-pane exports)
# ---------------------------------------------------------------------------------------------
FORMATS = ("xlsx", "csv")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv; charset=utf-8"
ZIP_MIME = "application/zip"
UTF8_BOM = b"\xef\xbb\xbf"           # Excel reads a CSV as UTF-8 only with it (accents, non-Latin text)
NOT_CHECKED_HEADERS = ["Status", "Reason"]
_FORMULA_START = ("=", "+", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)*%?$")
_FILE_BAD = re.compile(r"[^\w.-]+")


def check_format(fmt: str) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format: {fmt}. Use one of: {', '.join(FORMATS)}")
    return fmt


def csv_value(v: Any) -> str:
    """A cell for CSV: the same text as the Excel cell (Yes/No, lists joined with " | ") without Excel's
    length cap. Text that Excel would run as a formula (= + - @ at the start) gets a leading apostrophe;
    plain numbers such as -12 or a lone "-" are left alone."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v) if math.isfinite(v) else ""
    s = text_value(v).replace("\x00", "")
    if s[:1] in _FORMULA_START or (s[:1] == "-" and len(s) > 1 and not _PLAIN_NUMBER.match(s)):
        return "'" + s
    return s


def csv_bytes(headers: Sequence[Any], rows: Iterable[Sequence[Any]]) -> bytes:
    """UTF-8 CSV with a byte-order mark and CRLF line ends (what Excel expects)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([csv_value(h) for h in headers])
    for row in rows:
        w.writerow([csv_value(v) for v in row])
    return UTF8_BOM + buf.getvalue().encode("utf-8", "replace")


def safe_name(text: Any, limit: int = 80) -> str:
    """A file-name-safe version of a title ("Response Codes - Client Error (4xx)" -> "Response_Codes-...")."""
    out = _FILE_BAD.sub("_", str(text or "")).strip("._")
    return out[:limit].rstrip("._") or "export"


def _with_ext(path: str, ext: str) -> str:
    return os.path.splitext(path)[0] + ext


def _save_bytes(data: bytes, filepath: str) -> str:
    """Writes bytes atomically (same rules as _save: a temporary name, then a swap; a new name when the
    target cannot be replaced). Returns the path written."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    partial = f"{filepath}.{uuid.uuid4().hex[:12]}.partial"
    try:
        with open(partial, "wb") as fh:
            fh.write(data)
        try:
            os.replace(partial, filepath)
        except OSError:
            root, ext = os.path.splitext(filepath)
            filepath = f"{root}_{time.strftime('%H%M%S')}{uuid.uuid4().hex[:4]}{ext}"
            os.replace(partial, filepath)
    finally:
        if os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError:
                pass
    return filepath


@dataclass
class Table:
    """One sheet of a report or bulk export: the same headers and rows go to Excel and to CSV.
    not_checked: why the data could not be produced ("" = it ran); such a table holds one
    ["Not checked", reason] row under NOT_CHECKED_HEADERS, never a row of zeros."""
    title: str
    headers: List[str]
    rows: List[list] = field(default_factory=list)
    url_cols: Tuple[int, ...] = ()          # 1-based columns holding URLs (clickable in Excel)
    widths: Tuple[int, ...] = ()            # Excel column widths (default: from the header and first rows)
    not_checked: str = ""
    name: str = ""                          # file name stem inside a zip (default: from the title)

    @property
    def has_data(self) -> bool:
        return bool(self.rows) and not self.not_checked


def _tab_not_checked(tr) -> str:
    """Why a whole tab has no data ("" = it ran): its "All" filter is not checked (an integration that is not
    connected, custom snippets that are not set). Such a tab is written as a Not checked table."""
    return str((getattr(tr, "not_checked", None) or {}).get("all", "") or "")


def not_checked_table(title: str, reason: str) -> Table:
    return Table(title, list(NOT_CHECKED_HEADERS), [["Not checked", reason]], not_checked=reason or "Not checked")


def grid_table(title: str, columns: List[dict], rows: List[dict]) -> Table:
    """A registry grid ([{"id","label","kind"}] + tab_grid rows) as a Table with the tab's columns."""
    headers = [user_label(c["label"]) for c in columns]
    body = [[(row.get("cells") or {}).get(c["id"], "") for c in columns] for row in rows]
    url_cols = tuple(i for i, c in enumerate(columns, start=1) if c.get("kind") == "url")
    return Table(title, headers, body, url_cols=url_cols)


def filter_name(tab, filter_id: str) -> str:
    if filter_id in ("", "all", None):
        return getattr(tab, "total_label", "All") or "All"
    return next((f.name for f in tab.filters if f.id == filter_id), filter_id)


def tab_table(site, index, tab_id: str, filter_id: str = "all") -> Optional[Table]:
    """One tab filter as a Table (the grid's columns and rows, so the row count is the filter's count).
    A filter that could not run gives a not-checked table; None when the tab or filter does not exist
    (hidden tabs such as AMP are not in the index)."""
    tr = index.tabs.get(tab_id) if index is not None else None
    if tr is None:
        return None
    title = f"{tr.tab.name} - {filter_name(tr.tab, filter_id)}"
    why = (getattr(tr, "not_checked", None) or {}).get(filter_id or "all", "")
    if why:
        return not_checked_table(title, why)
    columns, rows = tab_grid(site, index, tab_id, filter_id)
    if rows is None:
        return None
    return grid_table(title, columns, rows)


def checks_warning_row() -> Optional[list]:
    """An Issues overview row that says checks are missing (an SEO check module failed to load), else None."""
    try:
        from .seo.registry import checks_warning
        text = checks_warning()
    except Exception as e:
        text = f"WARNING: checks incomplete - the SEO checks could not be loaded ({type(e).__name__})."
    if not text:
        return None
    return [text, "Warning", "High", "", "", "Some SEO checks did not run, so their issues are missing from this "
            "list. This row is not an issue of the site.", "Fix the module error, restart ARACHNIX and crawl again."]


def issues_overview_rows(issues: List[dict], warn: bool = True) -> List[list]:
    """ISSUES_OVERVIEW_HEADERS rows (name, type, priority, URLs, % of total, description, fix). When an SEO check
    module failed to load, a "WARNING: checks incomplete" row comes first (warn=False leaves it out)."""
    head = checks_warning_row() if warn else None
    return ([head] if head else []) + [[i.get("name", "") or "", i.get("issue_type") or "", i.get("priority") or "", int(i.get("urls") or 0),
             float(i.get("pct") or 0.0), i.get("description", "") or "", i.get("fix", "") or ""]
            for i in issues or []]


def write_table(wb, t: Table, cap: int = INLINKS_ROW_CAP):
    """A Table as a styled sheet of a write-only workbook (teal header, the first URL column clickable: a
    hyperlinked cell costs openpyxl twice the time of a plain one, and Excel keeps at most ~65,000 links per
    sheet)."""
    widths = list(t.widths)
    if not widths:
        widths = [len(str(h)) for h in t.headers]
        for row in t.rows[:200]:
            for i, v in enumerate(row[:len(widths)]):
                widths[i] = max(widths[i], len(str(xl_value(v)).split("\n")[0]))
        widths = [min(max(w + 3, 10), 65) for w in widths]
    ws = _new_sheet(wb, _sheet_title(wb, t.title), widths)
    st = _Styler(ws)
    ws.append(st.header(t.headers, SLATE_FILL if t.not_checked else TEAL_FILL))
    shown = t.rows[:cap]
    link_cols = tuple(t.url_cols[:1])
    for row_num, row in enumerate(shown, start=2):
        values = [xl_value(v) for v in row]
        if row_num < HYPERLINK_ROW_LIMIT:
            for ci in link_cols:
                if ci <= len(values):
                    link = _link(values[ci - 1])
                    if link:
                        values[ci - 1] = st.cell(link, font=LINK_FONT, hyperlink=link)
        ws.append(values)
    if len(t.rows) > cap:
        ws.append([f"Showing the first {cap:,} of {len(t.rows):,} rows (Excel row limit). Export as CSV for "
                   "every row."])
    ws.auto_filter.ref = f"A1:{get_column_letter(max(1, len(t.headers)))}{len(shown) + 1}"
    return ws


def xlsx_bytes(tables: List[Table]) -> bytes:
    """A workbook with one sheet per table (a workbook needs one sheet: an empty list gives an empty sheet)."""
    wb = Workbook(write_only=True)
    try:
        for t in tables or [Table("Export", ["Note"], [["Nothing to export."]])]:
            write_table(wb, t)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except BaseException:
        _discard(wb)
        raise


def zip_bytes(files: List[Tuple[str, bytes]]) -> bytes:
    """A deflated zip of (name, bytes); a repeated name gets " (2)", " (3)"... before its extension."""
    buf = io.BytesIO()
    used: set = set()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            root, ext = os.path.splitext(name)
            unique, n = name, 2
            while unique.lower() in used:
                unique, n = f"{root} ({n}){ext}", n + 1
            used.add(unique.lower())
            zf.writestr(unique, data)
    return buf.getvalue()


def tables_bytes(tables: List[Table], fmt: str, name: str) -> Tuple[str, bytes, str]:
    """(file name, bytes, media type) of tables: one workbook (xlsx), one .csv (csv, one table) or a .zip
    with one .csv per table (csv, several tables)."""
    check_format(fmt)
    stem = safe_name(name)
    if fmt == "xlsx":
        return f"{stem}.xlsx", xlsx_bytes(tables), XLSX_MIME
    tables = tables or [Table("Export", ["Note"], [["Nothing to export."]])]
    if len(tables) == 1:
        t = tables[0]
        return f"{stem}.csv", csv_bytes(t.headers, t.rows), CSV_MIME
    return f"{stem}.zip", zip_bytes([(f"{safe_name(t.name or t.title)}.csv", csv_bytes(t.headers, t.rows))
                                     for t in tables]), ZIP_MIME


# ---------------------------------------------------------------------------------------------
# Response headers without secrets (exports never carry cookie values, tokens or the crawl's secrets)
# ---------------------------------------------------------------------------------------------
_SECRET_HEADER = re.compile(r"cookie|authorization|token|secret|session|api-?key|signature|csrf|xsrf|passw", re.I)
_COOKIE_PAIR = re.compile(r"(^|[;,]\s*)([^=;,\s]+)=([^;,]*)")
_COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "samesite", "priority", "partitioned", "secure", "httponly"}


def _masked_set_cookie(value: str) -> str:
    """A Set-Cookie header with every cookie value replaced by *** (names and attributes stay readable)."""
    found = []

    def sub(m):
        if m.group(2).lower() in _COOKIE_ATTRS:
            return m.group(0)
        found.append(1)
        return f"{m.group(1)}{m.group(2)}=***"
    masked = _COOKIE_PAIR.sub(sub, value)
    return masked if found else "***"


def safe_header_value(name: str, value: Any, secrets: Optional[Sequence[str]] = None) -> str:
    """A response header value fit for an export: Set-Cookie values and any header that can carry a login
    (cookie, token, session, key...) are masked as ***, and so is every secret of the crawl."""
    v = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value if value is not None else "")
    low = str(name or "").lower()
    if low == "set-cookie":
        v = _masked_set_cookie(v)
    elif _SECRET_HEADER.search(low):
        v = "***"
    for secret in secrets or ():
        if secret and len(str(secret)) >= 4 and str(secret) in v:
            v = v.replace(str(secret), "***")
    return v


# ---------------------------------------------------------------------------------------------
# Detail-pane exports: inlinks / outlinks / images / resources / headers / cookies of one URL
# ---------------------------------------------------------------------------------------------
DETAIL_PARTS = ("inlinks", "outlinks", "images", "resources", "headers", "cookies")
DETAIL_INLINK_HEADERS = ["Type", "From", "To", "Anchor Text", "Alt Text", "Follow", "Path Type", "Link Position",
                         "Source Line", "Rel"]
DETAIL_OUTLINK_HEADERS = ["Type", "From", "To", "Anchor Text", "Follow", "Target", "Internal", "Path Type",
                          "Link Position", "Source Line", "Status Code", "Status", "Rel"]
DETAIL_IMAGE_HEADERS = ["Image URL", "Alt Text", "Alt Attribute", "Width Attribute", "Height Attribute",
                        "Inside Link", "Size (Bytes)", "Status Code", "Status", "From"]
# PageData images[] "from" -> where the file is named on the page (P11)
IMAGE_FROM = {"src": "src", "srcset": "srcset", "picture": "<picture> source", "rendered": "rendered page"}
DETAIL_RESOURCE_HEADERS = ["Resource URL", "Type", "Internal", "Path Type", "Link Position", "Source Line",
                           "Status Code", "Status", "Size (Bytes)"]
DETAIL_HEADER_HEADERS = ["Header", "Value"]
DETAIL_COOKIE_HEADERS = ["Name", "Domain", "Path", "Expires", "Max-Age", "Secure", "HttpOnly", "SameSite", "Set By"]


def cookie_set_by(c: dict) -> str:
    """Where a page's cookie came from: its server answer, or (rendered in Chrome) a request / a script."""
    src = str(c.get("source") or "")
    if src == "request":
        return "Request while rendering" + (f": {c.get('set_by')}" if c.get("set_by") else "")
    if src == "script":
        return "Script while rendering"
    return "Server answer (Set-Cookie)"
DETAIL_TITLES = {"inlinks": "Inlinks", "outlinks": "Outlinks", "images": "Image Details", "resources": "Resources",
                 "headers": "HTTP Headers", "cookies": "Cookies"}


def externals_by_url(site) -> Dict[str, Any]:
    """url -> ExternalLink (checked status of links to other sites), built once per Site."""
    if site is None:
        return {}
    return _memo(site, "export_ext_by_url", lambda: {getattr(e, "url", ""): e for e in site.externals or ()})


def find_url(site, url: str):
    """(result, is_internal) for a URL of the crawl (exact, normalised, with / without the trailing slash);
    (None, False) when the crawl never saw it."""
    if site is None or not url:
        return None, False
    url = url.strip()
    cands = [url, _norm_url(url)]
    cands += [c.rstrip("/") for c in cands if c.endswith("/")] + [c + "/" for c in cands if not c.endswith("/")]
    for c in cands:
        if c in site.by_url:
            return site.by_url[c], True
    ext = externals_by_url(site)
    for c in cands:
        if c in ext:
            return ext[c], False
    return None, False


def target_status(site, url: str) -> Tuple[Any, str]:
    """(status code or "", status text) of a linked URL as the crawl saw it ("Not crawled" when never requested)."""
    target = (site.by_url.get(url) if site is not None else None) or externals_by_url(site).get(url)
    if target is None:
        return "", "Not crawled"
    code = row_status_code(target)
    return ("" if code is None else code), row_status_text(target)


def _size_of(target) -> Any:
    if target is None:
        return ""
    seo = getattr(target, "seo", None) or {}
    if seo.get("size_bytes"):
        return int(seo["size_bytes"])
    if getattr(target, "size_bytes", None):
        return int(target.size_bytes)
    length = str((getattr(target, "response_headers", None) or {}).get("content-length", "") or "")
    return int(length) if length.isdigit() else ""


def _alt_state(alt: Any) -> str:
    return "Missing" if alt is None else ("Empty" if not str(alt).strip() else "Present")


def detail_table(site, url: str, part: str, secrets: Optional[Sequence[str]] = None) -> Table:
    """One detail-pane table of a URL (DETAIL_PARTS). Raises LookupError when the crawl never saw the URL and
    ValueError for an unknown part. Header values are masked (safe_header_value); cookies are names and
    attributes only (the crawler never keeps their values)."""
    if part not in DETAIL_PARTS:
        from .seo import registry
        sections = registry.detail_sections()
        if part in sections:
            return section_table(site, url, part)
        raise ValueError(f"Unknown detail part: {part}. Use one of: {', '.join(DETAIL_PARTS + tuple(sections))}")
    r, internal = find_url(site, url)
    if r is None:
        raise LookupError(f"URL not in this crawl: {url}")
    url = getattr(r, "url", url)
    seo = (getattr(r, "seo", None) or {}) if internal else {}
    title = f"{DETAIL_TITLES[part]} - {url}"
    rows: List[list] = []
    if part == "inlinks":
        for e in sources_of(site, url) + other_sources_of(site, url):
            kind = e.get("type") or ""
            rows.append([kind, e.get("source") or "", url, e.get("anchor") if kind != "Image" else "",
                         (e.get("alt") or "") if kind == "Image" else "", not e.get("nofollow"),
                         path_type(e.get("raw")), e.get("region") or "", int(e.get("line") or 0) or "",
                         e.get("rel") or ""])
        return Table(title, DETAIL_INLINK_HEADERS, rows, url_cols=(2, 3))
    if part == "outlinks":
        for link in seo.get("links") or ():
            dest = link.get("url") or ""
            code, status = target_status(site, dest)
            rows.append(["Hyperlink", url, dest, link.get("anchor") or "", not link.get("nofollow"),
                         "_blank" if link.get("target_blank") else "", bool(link.get("internal")),
                         path_type(link.get("raw")), link.get("region") or "", int(link.get("line") or 0) or "",
                         code, status, link_rel(link)])
        for e in (other_outlinks_of(site, r) if internal else ()):
            dest = e["target"]
            code, status = target_status(site, dest)
            rows.append([e["type"], url, dest, e["anchor"], True, "", is_internal_target(site, dest),
                         path_type(e.get("raw")), e["region"], int(e.get("line") or 0) or "", code, status,
                         e["rel"]])
        return Table(title, DETAIL_OUTLINK_HEADERS, rows, url_cols=(2, 3))
    if part == "images":
        for img in seo.get("images") or ():
            src = img.get("src") or ""
            target = site.by_url.get(src) or externals_by_url(site).get(src)
            code, status = target_status(site, src)
            rows.append([src, img.get("alt") or "", _alt_state(img.get("alt")), bool(img.get("has_width")),
                         bool(img.get("has_height")), bool(img.get("in_link")), _size_of(target), code, status,
                         IMAGE_FROM.get(str(img.get("from") or "src"), str(img.get("from") or ""))])
        return Table(title, DETAIL_IMAGE_HEADERS, rows, url_cols=(1,))
    if part == "resources":
        for res in seo.get("resources") or ():
            dest = res.get("url") or ""
            target = site.by_url.get(dest) or externals_by_url(site).get(dest)
            code, status = target_status(site, dest)
            rows.append([dest, _RESOURCE_TYPES.get(res.get("type") or "", "Other"), bool(res.get("internal")),
                         path_type(res.get("raw")), res.get("region") or "", int(res.get("line") or 0) or "",
                         code, status, _size_of(target)])
        return Table(title, DETAIL_RESOURCE_HEADERS, rows, url_cols=(1,))
    if part == "headers":
        headers = getattr(r, "response_headers", None) or {}
        rows = [[str(k), safe_header_value(k, v, secrets)] for k, v in headers.items()]
        return Table(title, DETAIL_HEADER_HEADERS, rows)
    for c in seo.get("cookies") or ():
        if isinstance(c, dict):
            rows.append([c.get("name") or "", c.get("domain") or "", c.get("path") or "", c.get("expires") or "",
                         c.get("max_age") if c.get("max_age") not in (None, "") else "", bool(c.get("secure")),
                         bool(c.get("httponly")), c.get("samesite") or "", cookie_set_by(c)])
    return Table(title, DETAIL_COOKIE_HEADERS, rows)


def export_detail(site, url: str, part: str, fmt: str = "csv",
                  secrets: Optional[Sequence[str]] = None) -> Tuple[str, bytes, str]:
    """(file name, bytes, media type) of one detail-pane table of a URL, as CSV or a one-sheet workbook."""
    t = detail_table(site, url, part, secrets)
    try:
        u = urlparse(getattr(find_url(site, url)[0], "url", url))
        where = (u.netloc + u.path).strip("/") or u.netloc
    except Exception:
        where = url
    return tables_bytes([t], fmt, f"{part}_{safe_name(where, 60)}")


SECTION_LISTS = ("violations", "items", "errors", "matches", "words", "rows", "results")


def _cell(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return "; ".join(" = ".join(map(str, x)) if isinstance(x, (list, tuple)) else str(x) for x in v)[:32000]
    if isinstance(v, dict):
        return "; ".join(f"{k}: {x}" for k, x in v.items())[:32000]
    return v


def section_table(site, url: str, section: str) -> "Table":
    """A registered details section of one URL as a table (W5): the section's list of records one row each
    (structured data: one row per property value), else one row per key; Not checked -> a one-row table with the
    reason."""
    from .seo import registry
    r, _internal = find_url(site, url)
    if r is None:
        raise LookupError(f"URL not in this crawl: {url}")
    url = getattr(r, "url", url)
    data = registry.details(site, url, [section]).get(section)
    title = f"{section.replace('_', ' ').title()} - {url}"
    if not isinstance(data, dict):
        return Table(title, ["Note"], [["This section does not apply to this URL."]])
    if "not_checked" in data:
        return Table(title, ["Not Checked"], [[str(data["not_checked"])]])
    if section == "structured_data":
        rows = []
        for n, item in enumerate(data.get("items") or [], 1):
            errs, warns = "; ".join(item.get("errors") or []), "; ".join(item.get("warnings") or [])
            for path, value in item.get("values") or [("", "")]:
                rows.append([n, item.get("format"), item.get("type"), path, value, errs, warns])
        return Table(title, ["Item", "Format", "Type", "Property Path", "Value", "Errors", "Warnings"], rows)
    records = next((data[k] for k in SECTION_LISTS if isinstance(data.get(k), list)
                    and all(isinstance(x, dict) for x in data[k])), None)
    if records:
        headers: List[str] = []
        for rec in records:
            for k in rec:
                if k not in headers:
                    headers.append(k)
        return Table(title, [h.replace("_", " ").title() for h in headers],
                     [[_cell(rec.get(h)) for h in headers] for rec in records])
    return Table(title, ["Field", "Value"], [[k.replace("_", " ").title(), _cell(v)] for k, v in data.items()])
