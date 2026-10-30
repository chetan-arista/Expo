"""
Crawl snapshots and crawl-to-crawl comparison (GET /api/crawls, POST /api/crawl/compare, Mode > Compare).

  save_snapshot(crawler, index, reports_dir) -> id     called when a crawl finishes (background thread)
  snapshot_from(crawler, index, segments=None) -> dict the current crawl as a snapshot, NOT saved (compare "current")
  list_snapshots(reports_dir, host=None) -> [meta]     newest first
  load_snapshot(id, reports_dir=None) -> dict | None
  compare(a, b, reports_dir=None, current=None, url_map=None, change_detection=None, thresholds=None)
      -> {"summary", "issues", "changed", "a", "b", "overview", "totals", "changes", "url_map"}
  compare_urls(a, b, key, kind, ...) -> {"total", "urls"}   the URLs behind one number (paged)
  parse_url_map(rules) -> (compiled rules, errors)     URL Mapping rules {"regex", "replace"} ("$1" = group 1)

A snapshot is one gzip JSON file, reports_dir/snapshots/<host>/<ts>.json.gz, id "<host>/<ts>" (ts is the
crawl's start time, so saving the same crawl again replaces its file instead of adding one), with its meta
also in <ts>.meta.json so listing crawls stays fast. The crawl's own snapshot keeps what the crawl found:
once a re-check has changed its URL rows, saving again writes the re-checked state as its own entry
"<host>/<ts>-r" (meta rechecked_at, base_id) and the crawl's entry is left as it was. It holds:
  meta     id, host, site, started_at, finished_at, pages / pages_ok (HTML pages and how many load: the same
           count as the health score, the dashboard, the PDF and the Excel), urls, issues (issue types
           flagged), issue_urls, health_score, auth_used, limit_reached (+ rechecked_at, base_id)
  rows     one compact row per internal URL: url, status, category, title, meta_desc, h1, indexability,
           canonical, word_count; version 3 adds crawl_depth, inlinks, unique_inlinks, outlinks, sd_types
           (structured data types), content_hash, simhash (content fingerprint) and segment; version 4 adds
           internal_outlinks, unique_internal_outlinks, external_outlinks, unique_external_outlinks (hyperlinks,
           counted like the Links tab)
  counts   {"tab:filter": n} for every filter of every tab (the dropdown counts; "tab:all" = the tab's rows)
  filters  {"tab:filter": {"tab", "filter", "name", "issue_type", "priority"}} for every Issue / Warning /
           Opportunity filter that flagged something (version 2 also has its "urls" here)
  checks   {"tab:filter": "passed" | "failed" | "not_checked"} (Passed / Failed / Not checked of that crawl)
  version 3 only:
  url_table / members   every URL listed by any filter of any tab: members {"tab:filter": delta-coded indexes
           into the sorted url_table}, for EVERY filter (and "tab:all"), so each filter can be compared
  names    {"tab:filter": [tab name, filter name, issue type, priority]} for every filter
  version 4 only:
  settings the crawl settings that decide which URLs some filters hold (crawl_settings: preferences thresholds,
           near_dup_threshold, content_area, ignore_non_indexable_issues). A version 3 snapshot was made with the
           defaults. When the two crawls differ, the filters that depend on that setting are "Not comparable" (with
           the change, e.g. "60 -> 50 characters") instead of listing added / removed URLs (not_comparable()).
  version 2 only:
  scope    {tab: [urls]} URLs a tab listed that are not internal rows (images, external links), so the
           comparison knows they were looked at again (version 3 reads this from members)
Version 2 snapshots stay readable: their non-issue filters have counts only, so their Added / New /
Removed / Missing columns are None ("-") and the change detection of fields they lack says "Not checked".
The last MAX_SNAPSHOTS (env CRAWLER_MAX_SNAPSHOTS, default 20) snapshots per host are kept.

Comparison, per issue filter (keyed on tab + filter ids), counted in (URL, filter) pairs:
  new           flagged in b, not in a
  regressed     new in b, but it had been flagged in an earlier snapshot and later fixed: it came back
                (a subset of "new"; summary["new"] counts only the brand-new ones)
  fixed         flagged in a, crawled again in b and no longer flagged
  not_rechecked flagged in a, but b did not look at it again (URL not crawled in b, or the check did not
                run in b): never counted as fixed (a check that did not run is "Not checked", never "0")
  unchanged     flagged in both
Screaming Frog's columns, per filter of every tab ("overview"; a = previous crawl, b = current crawl):
  current / previous   URLs in the filter (unique URLs), change = current - previous, change_pct
  added     URL in both crawls, in b's filter, not in a's       new      URL only in b, in b's filter
  removed   URL in both crawls, in a's filter, not in b's       missing  URL not in b, was in a's filter
  added + new - removed - missing == change, always (each row carries "reconciled"). In the issue terms:
  removed ~ fixed (when b ran the check), missing ~ not rechecked.
Change detection ("changes") compares URLs found in both crawls: title, meta description, H1, word count,
crawl depth, inlinks, unique inlinks, internal / unique internal / external / unique external outlinks (version 4),
indexability, status code, canonical, structured data types and content (similarity of the simhash fingerprints,
changed below the threshold). Text fields and outlinks compare pages that answered 2xx in both crawls; status
code, indexability, depth and inlinks compare every URL. An element a crawl did not record (an older snapshot) is
"Not checked: the older crawl did not record it", never 0 changes. URL Mapping rules rewrite the PREVIOUS crawl's
URLs (a) before matching (staging -> live, http -> https, a folder that moved).
"""
import gzip
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

SNAPSHOT_VERSION = 4            # 3: members of every filter + depth / inlinks / fingerprints per row;
                               # 4: internal / external outlink counts per row + the crawl settings
MAX_SNAPSHOTS = int(os.environ.get("CRAWLER_MAX_SNAPSHOTS", "20") or 20)
MAX_LIST_URLS = 200            # URLs listed per issue and per change list in a comparison (counts stay exact)
MAX_CHANGES = 500              # title / status changes listed
TEXT_LIMIT = 300               # characters kept of a title / description / h1 in a snapshot row
RECHECK_SUFFIX = "-r"          # id suffix of a crawl's re-checked state (saved beside what the crawl found)
MAX_URL_MAP_RULES = 20
OBSERVED_KEYS = ("url", "status", "category", "title", "meta_desc", "h1", "indexability", "canonical", "word_count")
CHANGE_ELEMENTS: "OrderedDict[str, dict]" = OrderedDict([
    ("title", {"label": "Page title", "field": "title", "kind": "text", "pages": True}),
    ("meta_description", {"label": "Meta description", "field": "meta_desc", "kind": "text", "pages": True}),
    ("h1", {"label": "H1", "field": "h1", "kind": "text", "pages": True}),
    ("word_count", {"label": "Word count", "field": "word_count", "kind": "number", "pages": True}),
    ("crawl_depth", {"label": "Crawl depth", "field": "crawl_depth", "kind": "number", "pages": False}),
    ("inlinks", {"label": "Internal inlinks", "field": "inlinks", "kind": "number", "pages": False}),
    ("unique_inlinks", {"label": "Unique inlinks", "field": "unique_inlinks", "kind": "number", "pages": False}),
    ("internal_outlinks", {"label": "Internal outlinks", "field": "internal_outlinks", "kind": "number",
                           "pages": True}),
    ("unique_internal_outlinks", {"label": "Unique internal outlinks", "field": "unique_internal_outlinks",
                                  "kind": "number", "pages": True}),
    ("external_outlinks", {"label": "External outlinks", "field": "external_outlinks", "kind": "number",
                           "pages": True}),
    ("unique_external_outlinks", {"label": "Unique external outlinks", "field": "unique_external_outlinks",
                                  "kind": "number", "pages": True}),
    ("indexability", {"label": "Indexability", "field": "indexability", "kind": "text", "pages": False}),
    ("status", {"label": "Status code", "field": "status", "kind": "value", "pages": False}),
    ("canonical", {"label": "Canonical", "field": "canonical", "kind": "text", "pages": True}),
    ("structured_data", {"label": "Structured data types", "field": "sd_types", "kind": "set", "pages": True}),
    ("content", {"label": "Content", "field": "simhash", "kind": "simhash", "pages": True}),
])
DEFAULT_THRESHOLDS = {"word_count": 1, "crawl_depth": 1, "inlinks": 1, "unique_inlinks": 1, "internal_outlinks": 1,
                      "unique_internal_outlinks": 1, "external_outlinks": 1, "unique_external_outlinks": 1,
                      "content_similarity": 90.0}
OLDER_NOT_RECORDED = "Not checked: the older crawl did not record it."
# Settings that decide which URLs a filter holds (P10): two crawls made with different values are "Not comparable"
# for those filters. preference -> (filter keys, unit shown in "60 -> 50 characters")
PREFERENCE_FILTERS: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "title_max_chars": (("page_titles:over_60_characters",), "characters"),
    "title_min_chars": (("page_titles:below_30_characters",), "characters"),
    "title_max_px": (("page_titles:over_561_pixels",), "pixels"),
    "title_min_px": (("page_titles:below_200_pixels",), "pixels"),
    "desc_max_chars": (("meta_description:over_155_characters",), "characters"),
    "desc_min_chars": (("meta_description:below_70_characters",), "characters"),
    "desc_max_px": (("meta_description:over_985_pixels",), "pixels"),
    "desc_min_px": (("meta_description:below_400_pixels",), "pixels"),
    "url_max_chars": (("url:over_115",), "characters"),
    "h1_max_chars": (("h1:over_70_characters",), "characters"),
    "h2_max_chars": (("h2:over_70_characters",), "characters"),
    "alt_max_chars": (("images:alt_text_over_100_characters",), "characters"),
    "image_max_kb": (("images:over_100_kb",), "kB"),
    "low_content_words": (("content:low_content_pages",), "words"),
    "high_internal_outlinks": (("links:high_internal_outlinks",), "links"),
    "high_external_outlinks": (("links:high_external_outlinks",), "links"),
    "high_crawl_depth": (("links:high_crawl_depth",), "clicks"),
    "html_max_mb": (("validation:html_over_2mb",), "MB"),
    "resource_max_mb": (("validation:resource_over_2mb",), "MB"),
    "bounce_rate_pct": (("analytics:bounce_rate_above_70",), "%"),
    "carbon_rating_flag": (("validation:high_carbon_rating",), ""),
}
NEAR_DUP_FILTERS = ("content:near_duplicates",)
# filters read from the content area (word count, text fingerprints, top terms, spelling text)
CONTENT_AREA_FILTERS = ("content:exact_duplicates", "content:near_duplicates", "content:low_content_pages",
                        "content:readability_difficult", "content:readability_very_difficult",
                        "content:soft_404_pages", "content:semantically_similar", "content:low_relevance",
                        "content:spelling_errors", "content:grammar_errors")
# tabs whose filters are not on-page issues (ignore_non_indexable_issues leaves them as they are)
NON_PAGE_TABS = frozenset({"internal", "external", "response_codes", "security", "sitemaps", "robots_txt",
                           "ai_crawlers", "pagespeed", "search_console", "analytics", "link_metrics", "log_files",
                           "crawl_budget"})
BUCKETS = ("added", "new", "removed", "missing")
_ID_RE = re.compile(r"^([A-Za-z0-9._-]{1,200})/(\d{8}T\d{6}Z(?:-\d{1,3})?(?:-r)?)$")
_lock = threading.RLock()
_cache: "OrderedDict[str, Tuple[float, dict]]" = OrderedDict()   # path -> (mtime, snapshot)
_CACHE_SIZE = 8
_views: "OrderedDict[tuple, Any]" = OrderedDict()                # (id, saved_at, map) -> _View
_VIEW_CACHE = 4


# ---------------------------------------------------------------------------------------------
# Paths and ids
# ---------------------------------------------------------------------------------------------
def default_reports_dir() -> str:
    return os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()


def host_key(url_or_host: str) -> str:
    """Folder-safe site key: lower-case host (and port), without a leading "www."."""
    text = str(url_or_host or "").strip()
    netloc = urlparse(text).netloc if "://" in text else text.split("/")[0]
    netloc = netloc.lower().rsplit("@", 1)[-1]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    key = re.sub(r"[^a-z0-9.-]+", "_", netloc).strip("._")
    return key[:200] or "site"


_URL_USERINFO = re.compile(r"(://[^/?#@:]*:)([^/?#@]*)(@)")


def _mask_userinfo(url: Any) -> str:
    """A URL with any embedded password ("user:pass@host") replaced by "user:***@host". A login password must
    never be persisted (it still reaches crawled URLs because normalize_url keeps the userinfo), and a snapshot
    is written to disk and shared through the crawl history. Positional (keeps the user name, host, port and
    path), so comparing two snapshots still matches URLs, and a no-op on a URL without an embedded password."""
    s = str(url or "")
    if "@" not in s:
        return s
    return _URL_USERINFO.sub(lambda m: m.group(1) + "***" + m.group(3) if m.group(2) else m.group(0), s)


def _scrub_userinfo(obj: Any) -> Any:
    """`obj` (a snapshot or part of one) with the password masked in every URL string it holds (row urls, the
    url table, canonicals...), so no login password is persisted in a snapshot. Only URL-shaped strings change."""
    if isinstance(obj, str):
        return _mask_userinfo(obj)
    if isinstance(obj, list):
        return [_scrub_userinfo(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub_userinfo(v) for k, v in obj.items()}
    return obj


def _root(reports_dir: Optional[str]) -> str:
    return os.path.join(reports_dir or default_reports_dir(), "snapshots")


def _path_of(snapshot_id: str, reports_dir: Optional[str]) -> Optional[str]:
    m = _ID_RE.match(str(snapshot_id or ""))
    if not m:
        return None
    return os.path.join(_root(reports_dir), m.group(1), m.group(2) + ".json.gz")


def _iso(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------------------------
# Building a snapshot
# ---------------------------------------------------------------------------------------------
def _clip(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:TEXT_LIMIT]


def _first(values: Any) -> str:
    try:
        for v in values or ():
            if str(v or "").strip():
                return str(v)
    except Exception:
        pass
    return ""


def _split_index(crawler, index) -> Tuple[Any, Any]:
    """(site, index): accepts a TabIndex or the (site, index) pair of crawler.site_index()."""
    site = None
    if isinstance(index, tuple) and len(index) == 2:
        site, index = index
    if site is None and crawler is not None:
        cached = getattr(crawler, "_index_cache", None)
        if isinstance(cached, tuple) and len(cached) == 2 and (index is None or cached[1] is index):
            site, index = cached
    return site, index


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _outlink_counts(links: Any) -> Tuple[int, int, int, int]:
    """(internal, unique internal, external, unique external) hyperlinks of a page, counted as the Links tab counts
    them (tabs.internal.outlink_counts, tabs.links.external_hyperlinks)."""
    internal = external = 0
    uniq_in: Set[str] = set()
    uniq_ext: Set[str] = set()
    for link in links or ():
        u = link.get("url") if isinstance(link, dict) else None
        if not u:
            continue
        if link.get("internal"):
            internal += 1
            uniq_in.add(u)
        else:
            external += 1
            uniq_ext.add(u)
    return internal, len(uniq_in), external, len(uniq_ext)


def crawl_settings(config: Any = None) -> Dict[str, Any]:
    """The settings of a crawl that decide which URLs some filters hold (stored in a version 4 snapshot); the
    defaults for a crawl without them (a version 3 snapshot, an older saved crawl)."""
    try:
        from ..config import DEFAULT_PREFERENCES
    except Exception:
        DEFAULT_PREFERENCES = {}
    prefs = dict(DEFAULT_PREFERENCES)
    given = getattr(config, "preferences", None) if config is not None else None
    if isinstance(given, dict):
        prefs.update({k: v for k, v in given.items() if v not in (None, "")})
    try:
        from .analysis import near_threshold, NEAR_DUP_THRESHOLD
        near = near_threshold(config) if config is not None else NEAR_DUP_THRESHOLD
    except Exception:
        near = 90.0
    area = getattr(config, "content_area", None) if config is not None else None
    area = area if isinstance(area, dict) else {}
    return {"preferences": prefs, "near_dup_threshold": float(near),
            "content_area": {"include": [str(s) for s in area.get("include") or []],
                             "exclude": [str(s) for s in area.get("exclude") or []]},
            "ignore_non_indexable_issues": bool(getattr(config, "ignore_non_indexable_issues", False))
            if config is not None else False}


def _num_text(value: Any) -> str:
    try:
        f = float(value)
        return f"{int(f):,}" if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(value)


def not_comparable(settings_a: Dict[str, Any], settings_b: Dict[str, Any],
                   keys: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """{"tab:filter": "Not comparable: ..."} for the filters whose deciding setting differs between two crawls
    (a = previous, b = current); `keys` (every tab:filter of the comparison) is needed for the settings that touch
    many filters (ignore issues on non-indexable pages)."""
    out: Dict[str, str] = {}
    pa, pb = settings_a.get("preferences") or {}, settings_b.get("preferences") or {}
    for pref, (filters, unit) in PREFERENCE_FILTERS.items():
        va, vb = pa.get(pref), pb.get(pref)
        if va is None or vb is None or str(va) == str(vb):
            continue
        try:
            if float(va) == float(vb):
                continue
        except (TypeError, ValueError):
            pass
        what = "rating" if pref == "carbon_rating_flag" else "threshold"
        change = f"{_num_text(va)} -> {_num_text(vb)}" + (f" {unit}" if unit and unit != "%" else unit)
        for key in filters:
            out.setdefault(key, f"Not comparable: the {what} changed ({change}).")
    na, nb = settings_a.get("near_dup_threshold"), settings_b.get("near_dup_threshold")
    if na is not None and nb is not None and float(na) != float(nb):
        for key in NEAR_DUP_FILTERS:
            out.setdefault(key, f"Not comparable: the near-duplicate threshold changed ({_num_text(na)}% -> "
                                f"{_num_text(nb)}%).")
    if (settings_a.get("content_area") or {}) != (settings_b.get("content_area") or {}):
        for key in CONTENT_AREA_FILTERS:
            out.setdefault(key, "Not comparable: the content area changed (Configuration > Content), so the text "
                                "these checks read is not the same.")
    ia, ib = bool(settings_a.get("ignore_non_indexable_issues")), bool(settings_b.get("ignore_non_indexable_issues"))
    if ia != ib:
        on = "the current" if ib else "the previous"
        for key in keys or ():
            tab, _, fid = key.partition(":")
            if fid != "all" and tab not in NON_PAGE_TABS:
                out.setdefault(key, f"Not comparable: only {on} crawl left issues on non-indexable pages out.")
    return out


def _row(r, site, segment: str = "") -> dict:
    from .analysis import indexability_of
    p = getattr(r, "seo", None) or {}
    url = str(getattr(r, "url", "") or "")
    try:
        from ..issues import classify_result
        category = classify_result(r)
    except Exception:
        category = "error"
    if site is not None and url in getattr(site, "indexability_status", {}):
        ok = bool(site.indexable.get(url))
        why = site.indexability_status.get(url) or ""
    else:
        try:
            ok, why = indexability_of(r)
        except Exception:
            ok, why = False, "No Response"
    canonical = _first(p.get("canonicals")) or str(p.get("http_canonical") or "")
    depth = (getattr(site, "crawl_depth", None) or {}).get(url) if site is not None else None
    sd = p.get("structured_data") if isinstance(p.get("structured_data"), dict) else {}
    try:
        simhash = int(p.get("simhash") or 0)
    except (TypeError, ValueError):
        simhash = 0
    out_in, out_in_u, out_ext, out_ext_u = _outlink_counts(p.get("links"))
    return {
        "url": url, "status": getattr(r, "status_code", None), "category": category,
        "title": _clip(_first(p.get("titles")) or getattr(r, "page_title", "")),
        "meta_desc": _clip(_first(p.get("meta_descriptions"))), "h1": _clip(_first(p.get("h1"))),
        "indexability": "Indexable" if ok else (why or "Non-Indexable"), "canonical": canonical,
        "word_count": _int(p.get("word_count")),
        "crawl_depth": _int(depth if depth is not None else getattr(r, "depth", 0)),
        "inlinks": len((getattr(site, "inlinks", None) or {}).get(url) or ()) if site is not None else 0,
        "unique_inlinks": _int((getattr(site, "unique_inlinks", None) or {}).get(url)) if site is not None else 0,
        "outlinks": len(p.get("links") or ()),
        "internal_outlinks": out_in, "unique_internal_outlinks": out_in_u,
        "external_outlinks": out_ext, "unique_external_outlinks": out_ext_u,
        "sd_types": sorted({str(t) for t in (sd.get("types") or []) if t})[:30],
        "content_hash": str(p.get("content_hash") or ""), "simhash": simhash,
        "segment": str(segment or ""),
    }


def _issue_total(index) -> Tuple[int, int]:
    issues = list(getattr(index, "issues", None) or [])
    return len(issues), sum(int(i.get("urls") or 0) for i in issues)


def _page_counts(results: List[Any], stats: Dict[str, Any]) -> Tuple[int, int, Any]:
    """(pages, pages_ok, health_score) by the health score's definition (issues.health_score: every HTML
    page, redirects and error pages included, files and robots-blocked URLs left out), the same numbers the
    dashboard (stats pages_total / pages_ok), the PDF and the Excel show. Falls back to the crawler's
    running counts when the results cannot be read."""
    try:
        from ..issues import health_score
        if results:
            h = health_score(results)
            return int(h["pages"]), int(h["pages_ok"]), h["score"]
    except Exception:
        pass
    return int(stats.get("pages_total") or 0), int(stats.get("pages_ok") or 0), stats.get("health_score")


def _observed(rows: List[dict], crawler) -> str:
    """Fingerprint of what the crawl observed: every URL row (its OBSERVED_KEYS, so editing segments or a newer
    snapshot layout never looks like a re-check) and the answer of every checked external link. A re-check
    changes it; a post-crawl check finishing (bot check, intel) does not."""
    h = hashlib.sha1()
    for r in sorted(rows, key=lambda x: str(x.get("url") or "")):
        core = {k: r.get(k) for k in OBSERVED_KEYS if k in r}
        h.update(json.dumps(core, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
    try:
        ext = sorted((getattr(crawler, "external_links", None) or {}).items())
    except Exception:
        ext = []
    for url, e in ext:
        h.update(json.dumps([url, getattr(e, "status_code", None), getattr(e, "http_status", ""),
                             getattr(e, "redirect_destination", None)], default=str).encode("utf-8"))
    return h.hexdigest()


def _delta_encode(indexes: Iterable[int]) -> List[int]:
    out, prev = [], 0
    for i in sorted(set(indexes)):
        out.append(i - prev)
        prev = i
    return out


def _delta_decode(deltas: Iterable[Any]) -> List[int]:
    out, cur = [], 0
    for d in deltas or ():
        try:
            cur += int(d)
        except (TypeError, ValueError):
            continue
        out.append(cur)
    return out


def _urls_of(rows: Iterable[Any]) -> List[str]:
    seen, out = set(), []
    for h in rows or ():
        u = str(getattr(h, "url", "") or "")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def snapshot_from(crawler, index, site=None, segments: Optional[Dict[str, str]] = None,
                  snapshot_id: Optional[str] = None) -> dict:
    """The crawl as a snapshot dict (not written to disk). segments: {url: segment name} stored per row.
    snapshot_id overrides the id derived from the crawl's start time: an imported or duplicated crawl keeps
    its own id ("<ts>-1"...), not the original crawl's, so its snapshot never lands on the original's entry.
    Never raises; a crawl without results gives a snapshot with empty rows."""
    site2, index = _split_index(crawler, index)
    site = site or site2
    start_url = str(getattr(crawler, "start_url", "") or getattr(site, "start_url", "") or "")
    host = host_key(start_url)
    started = float(getattr(crawler, "start_time", 0) or 0) or time.time()
    finished = float(getattr(crawler, "end_time", 0) or 0)
    if site is not None:
        results = list(getattr(site, "results", None) or [])
    else:
        results = list(getattr(crawler, "results_list", None) or [])
    seg = segments or {}
    rows, seen, unique = [], set(), []
    for r in results:
        url = getattr(r, "url", None)
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(r)
        try:
            rows.append(_row(r, site, seg.get(url, "")))
        except Exception:
            continue

    counts: Dict[str, int] = {}
    filters: Dict[str, dict] = {}
    names: Dict[str, list] = {}
    member_urls: Dict[str, List[str]] = {}
    for tab_id, tr in (getattr(index, "tabs", None) or {}).items():
        tab = tr.tab
        counts[f"{tab_id}:all"] = len(tr.rows or [])
        member_urls[f"{tab_id}:all"] = _urls_of(tr.rows)
        names[f"{tab_id}:all"] = [tab.name, getattr(tab, "total_label", "All") or "All", None, None]
        for f in tab.filters:
            key = f"{tab_id}:{f.id}"
            hits = tr.filter_rows.get(f.id, []) or []
            counts[key] = len(hits)
            names[key] = [tab.name, f.name, f.issue_type, f.priority]
            member_urls[key] = _urls_of(hits)
            if f.issue_type and hits:
                filters[key] = {"tab": tab_id, "filter": f.id, "name": f"{tab.name}: {f.name}",
                                "issue_type": f.issue_type, "priority": f.priority or "Low"}

    table = sorted({u for urls in member_urls.values() for u in urls})
    position = {u: i for i, u in enumerate(table)}
    members = {k: _delta_encode(position[u] for u in urls) for k, urls in member_urls.items() if urls}

    checks: Dict[str, str] = {}
    try:
        from ..intel.checks import check_statuses
        for c in check_statuses(index, crawler, site):
            if not c.get("virtual"):
                checks[f"{c.get('tab')}:{c.get('filter')}"] = str(c.get("status") or "")
    except Exception:
        checks = {}

    stats: Dict[str, Any] = {}
    try:
        stats = crawler.get_stats() if crawler is not None and hasattr(crawler, "get_stats") else {}
    except Exception:
        stats = {}
    n_issues, n_issue_urls = _issue_total(index)
    ts = _stamp(started)
    pages, pages_ok, health = _page_counts(unique, stats)
    meta = {
        "id": snapshot_id or f"{host}/{ts}", "host": host, "site": _mask_userinfo(start_url), "started_at": _iso(started),
        "finished_at": _iso(finished), "saved_at": _iso(time.time()), "pages": pages, "pages_ok": pages_ok,
        "urls": len(rows), "issues": n_issues, "issue_urls": n_issue_urls, "health_score": health,
        "auth_used": bool(stats.get("auth_used") or getattr(crawler, "auth_used", False)),
        "limit_reached": bool(stats.get("limit_reached")), "status": str(stats.get("status") or ""),
        "mode": str(stats.get("mode") or getattr(crawler, "mode", "") or "spider"),
        "version": SNAPSHOT_VERSION, "observed": _observed(rows, crawler),
        "checks_loaded": list(getattr(index, "checks_loaded", None) or []),
        "checks_failed": dict(getattr(index, "checks_failed", None) or {}),
        "checks_incomplete": bool(getattr(index, "checks_failed", None)),
    }
    config = getattr(crawler, "config", None) if crawler is not None else None
    if config is None and site is not None:
        config = getattr(site, "config", None)
    snap = {"version": SNAPSHOT_VERSION, "meta": meta, "rows": rows, "counts": counts, "filters": filters,
            "checks": checks, "names": names, "url_table": table, "members": members,
            "settings": crawl_settings(config)}
    return _scrub_userinfo(snap) if "@" in start_url else snap


def _rows_by_url(snap: dict) -> Dict[str, dict]:
    return {r.get("url"): r for r in snap.get("rows") or [] if isinstance(r, dict) and r.get("url")}


def _observed_rows(snap: dict) -> Dict[str, dict]:
    return {u: {k: r.get(k) for k in OBSERVED_KEYS if k in r} for u, r in _rows_by_url(snap).items()}


def _rechecked_since(path: str, snap: dict) -> bool:
    """The crawl saved at `path` observed something else than `snap` (a re-check changed a URL's answer)."""
    meta = _read_meta(path) or {}
    if meta.get("observed"):
        return meta["observed"] != snap["meta"].get("observed")
    saved = _read(path)                       # saved before fingerprints existed: compare the URL rows
    return bool(saved) and _observed_rows(saved) != _observed_rows(snap)


def save_snapshot(crawler, index, reports_dir: Optional[str] = None, site=None,
                  segments: Optional[Dict[str, str]] = None, snapshot_id: Optional[str] = None) -> str:
    """Write the crawl's snapshot and keep the newest MAX_SNAPSHOTS for its host. Returns the id ("" when
    there is nothing to save or the write failed). Never raises. snapshot_id forces the id (import / duplicate
    build a snapshot for their own id so a loaded copy can be compared, instead of reusing the original's).

    Saving the same crawl again (a post-crawl check finished) replaces its entry while what the crawl
    observed is unchanged (meta "observed": URL rows + external link answers). Once a re-check has changed
    it, the crawl's entry keeps what the crawl found and the re-checked state goes to "<id>-r" (replaced by
    later saves; removed when the answers are back to what the crawl found), so the history never shows
    re-check results under the crawl's own entry."""
    try:
        snap = snapshot_from(crawler, index, site=site, segments=segments, snapshot_id=snapshot_id)
        if not snap["rows"]:
            return ""
        base_id = snap["meta"]["id"]
        base = _path_of(base_id, reports_dir)
        if not base:
            return ""
        os.makedirs(os.path.dirname(base), exist_ok=True)
        with _lock:
            rev_id = base_id + RECHECK_SUFFIX
            rev = _path_of(rev_id, reports_dir)
            path = base
            if rev and os.path.isfile(base) and _rechecked_since(base, snap):
                snap["meta"].update(id=rev_id, base_id=base_id, rechecked_at=snap["meta"]["saved_at"])
                path = rev
            elif rev:
                _remove(rev)          # back to what the crawl found: no separate re-checked entry
            data = gzip.compress(json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
            _write_meta(path, snap["meta"])
            _cache.pop(path, None)
            _prune(os.path.dirname(path), reports_dir)
        return snap["meta"]["id"]
    except Exception:
        return ""


def _remove(path: str) -> None:
    """Deletes one snapshot file and its meta sidecar (missing files are fine)."""
    for victim in (path, _meta_path(path)):
        try:
            os.remove(victim)
        except OSError:
            pass
    _cache.pop(path, None)


def _meta_path(path: str) -> str:
    return path[:-len(".json.gz")] + ".meta.json"


def _write_meta(path: str, meta: dict) -> None:
    """Small sidecar with the snapshot's meta, so listing crawls never decompresses whole snapshots."""
    try:
        tmp = f"{_meta_path(path)}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        os.replace(tmp, _meta_path(path))
    except OSError:
        pass


def _read_meta(path: str) -> Optional[dict]:
    try:
        with open(_meta_path(path), "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        if isinstance(meta, dict):
            return meta
    except (OSError, ValueError):
        pass
    snap = _read(path)               # older snapshot without a sidecar: read it once and add one
    if not snap:
        return None
    _write_meta(path, snap["meta"])
    return snap["meta"]


def _prune(folder: str, reports_dir: Optional[str] = None) -> None:
    """Keep the newest MAX_SNAPSHOTS snapshots per host. A snapshot that belongs to a locked saved crawl (its
    baseline and its re-check entry) is never pruned: a locked crawl is a baseline the team keeps to compare
    against, retention promises never to drop it, so its snapshot must outlive MAX_SNAPSHOTS newer crawls."""
    try:
        files = sorted(f for f in os.listdir(folder) if f.endswith(".json.gz"))
    except OSError:
        return
    extra = files[:-MAX_SNAPSHOTS] if len(files) > MAX_SNAPSHOTS else []
    if not extra:
        return
    protected = _protected_snapshot_files(folder, reports_dir)
    for name in extra:
        if name in protected:
            continue
        for victim in (name, name[:-len(".json.gz")] + ".meta.json"):
            try:
                os.remove(os.path.join(folder, victim))
            except OSError:
                pass


def _protected_snapshot_files(folder: str, reports_dir: Optional[str]) -> Set[str]:
    """Snapshot file names in `folder` whose saved crawl is locked (or in a locked project): never pruned.
    Best-effort - any error means nothing is treated as protected (pruning falls back to newest-wins)."""
    try:
        from .. import store
        host = os.path.basename(os.path.normpath(folder))
        rd = reports_dir if reports_dir is not None else os.path.dirname(os.path.dirname(os.path.normpath(folder)))
        protected: Set[str] = set()
        for m in store.list_crawls(rd, site=host):
            if not m.get("protected"):
                continue
            ts = str(m.get("id") or "").split("/", 1)[-1]
            if ts:
                protected.add(f"{ts}.json.gz")
                protected.add(f"{ts}{RECHECK_SUFFIX}.json.gz")
        return protected
    except Exception:
        return set()


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------
def _read(path: str) -> Optional[dict]:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            _cache.move_to_end(path)
            return hit[1]
    try:
        with gzip.open(path, "rb") as fh:
            snap = json.loads(fh.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(snap, dict) or not isinstance(snap.get("meta"), dict):
        return None
    with _lock:
        _cache[path] = (mtime, snap)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return snap


def load_snapshot(snapshot_id: str, reports_dir: Optional[str] = None) -> Optional[dict]:
    """The snapshot dict, or None for an unknown / malformed id (ids never reach outside the folder)."""
    path = _path_of(snapshot_id, reports_dir)
    return _read(path) if path and os.path.isfile(path) else None


def _list_meta(folder: str, host: str) -> List[dict]:
    out = []
    try:
        names = sorted((f for f in os.listdir(folder) if f.endswith(".json.gz")), reverse=True)
    except OSError:
        return out
    for name in names:
        m = _read_meta(os.path.join(folder, name))
        if not m:
            continue
        out.append({"id": f"{host}/{name[:-len('.json.gz')]}", "site": m.get("site", ""),
                    "host": host, "started_at": m.get("started_at", ""), "finished_at": m.get("finished_at", ""),
                    "pages": int(m.get("pages") or 0), "urls": int(m.get("urls") or 0),
                    "issues": int(m.get("issues") or 0), "issue_urls": int(m.get("issue_urls") or 0),
                    "pages_ok": m.get("pages_ok"), "health_score": m.get("health_score"),
                    "auth_used": bool(m.get("auth_used")), "limit_reached": bool(m.get("limit_reached")),
                    # "stopped": the crawl was stopped early, so it covers part of the site only
                    "status": str(m.get("status") or ""), "partial": str(m.get("status") or "") == "stopped"
                    or bool(m.get("limit_reached")),
                    "rechecked_at": m.get("rechecked_at") or "", "base_id": m.get("base_id") or "",
                    "mode": m.get("mode") or "spider", "version": int(m.get("version") or 1)})
    out.sort(key=_when, reverse=True)
    return out


def _when(meta: dict) -> Tuple[str, str]:
    """Time order of snapshots: by crawl start, a crawl's re-checked state right after the crawl itself."""
    return str(meta.get("started_at") or ""), str(meta.get("rechecked_at") or "")


def list_snapshots(reports_dir: Optional[str] = None, host: Optional[str] = None) -> List[dict]:
    """[{"id", "site", "started_at", "pages", "issues", ...}] newest first; host (a URL or host name) limits
    the list to one site."""
    root = _root(reports_dir)
    if host:
        key = host_key(host)
        return _list_meta(os.path.join(root, key), key)
    out: List[dict] = []
    try:
        hosts = [h for h in os.listdir(root) if os.path.isdir(os.path.join(root, h))]
    except OSError:
        return out
    for h in hosts:
        out.extend(_list_meta(os.path.join(root, h), h))
    out.sort(key=_when, reverse=True)
    return out


# ---------------------------------------------------------------------------------------------
# URL Mapping
# ---------------------------------------------------------------------------------------------
def parse_url_map(rules: Any) -> Tuple[List[Tuple[Any, str]], List[str]]:
    """([(compiled regex, replacement)], errors) from [{"regex", "replace"}] (or [[regex, replace]]); Java-style
    "$1" in a replacement means group 1. Invalid rules are left out and named in errors."""
    out: List[Tuple[Any, str]] = []
    errors: List[str] = []
    for i, rule in enumerate(rules or []):
        if isinstance(rule, dict):
            rx, rep = rule.get("regex"), rule.get("replace")
        elif isinstance(rule, (list, tuple)) and len(rule) == 2:
            rx, rep = rule
        else:
            errors.append(f"Rule {i + 1}: expected a regex and a replacement.")
            continue
        rx, rep = str(rx or ""), str(rep or "")
        if not rx:
            continue
        if len(out) >= MAX_URL_MAP_RULES:
            errors.append(f"At most {MAX_URL_MAP_RULES} URL mapping rules are used.")
            break
        try:
            compiled = re.compile(rx)
        except re.error as e:
            errors.append(f"Rule {i + 1}: invalid regex ({e}).")
            continue
        rep = re.sub(r"\$(\d{1,2})", r"\\g<\1>", rep)
        try:
            compiled.sub(rep, "https://example.com/")
        except (re.error, IndexError) as e:
            errors.append(f"Rule {i + 1}: invalid replacement ({e}).")
            continue
        out.append((compiled, rep))
    return out, errors


def _mapper(rules: List[Tuple[Any, str]]) -> Optional[Callable[[str], str]]:
    if not rules:
        return None
    memo: Dict[str, str] = {}

    def apply(url: str) -> str:
        hit = memo.get(url)
        if hit is None:
            hit = url
            for rx, rep in rules:
                try:
                    hit = rx.sub(rep, hit)
                except (re.error, IndexError):
                    continue
            memo[url] = hit
        return hit
    return apply


# ---------------------------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------------------------
def _resolve(x: Any, reports_dir: Optional[str], current: Optional[dict]) -> Optional[dict]:
    if isinstance(x, dict):
        return x
    if str(x or "").strip().lower() == "current":
        return current
    return load_snapshot(str(x or ""), reports_dir)


def _decode_members(snap: dict) -> Dict[str, List[str]]:
    table = snap.get("url_table") or []
    out: Dict[str, List[str]] = {}
    for key, deltas in (snap.get("members") or {}).items():
        out[key] = [table[i] for i in _delta_decode(deltas) if 0 <= i < len(table)]
    return out


def filter_urls(snap: dict, key: str) -> Optional[List[str]]:
    """The URLs of one "tab:filter" in a snapshot of any version (sorted), or None when the snapshot kept only
    the filter's count (version 2, non-issue filters). Use this instead of reading the layout directly."""
    if not isinstance(snap, dict):
        return None
    if "members" in snap:
        table = snap.get("url_table") or []
        deltas = (snap.get("members") or {}).get(key)
        if deltas is None:
            return [] if key in (snap.get("counts") or {}) else None
        return sorted(table[i] for i in _delta_decode(deltas) if 0 <= i < len(table))
    f = (snap.get("filters") or {}).get(key)
    if f is not None and f.get("urls") is not None:
        return sorted(f["urls"])
    return [] if int((snap.get("counts") or {}).get(key, -1)) == 0 else None


class _View:
    """Fast lookups over one snapshot; `mapper` rewrites every URL of it (URL Mapping). Filter memberships are
    decoded per filter when first asked, so the older snapshots a regression check reads stay cheap."""

    def __init__(self, snap: dict, mapper: Optional[Callable[[str], str]] = None):
        self._m = mapper or (lambda u: u)
        self.snap = snap
        self.meta = snap.get("meta") or {}
        self.version = int(snap.get("version") or self.meta.get("version") or 1)
        self.rows: Dict[str, dict] = {}
        self.mapped = 0
        for r in snap.get("rows") or []:
            u = r.get("url") if isinstance(r, dict) else None
            if not u:
                continue
            nu = self._m(u)
            if nu != u:
                self.mapped += 1
            self.rows.setdefault(nu, r)
        self.has_members = "members" in snap
        self._table = snap.get("url_table") or []
        self._raw: Dict[str, Any] = snap.get("members") or {}
        self._decoded: Dict[str, Set[str]] = {}
        self._filters = snap.get("filters") or {}
        self._flags: Dict[str, Set[str]] = {}
        self._scope: Dict[str, Set[str]] = {}
        if not self.has_members:
            self._scope = {t: {self._m(u) for u in v or []} for t, v in (snap.get("scope") or {}).items()}
        self._universe: Optional[Set[str]] = None
        self.checks = snap.get("checks") or {}
        self.counts = snap.get("counts") or {}
        self.names = snap.get("names") or {}
        # crawl settings (version 4); an older snapshot was made with the defaults
        settings = snap.get("settings")
        self.settings: Dict[str, Any] = settings if isinstance(settings, dict) else crawl_settings(None)

    @property
    def member_keys(self) -> List[str]:
        return list(self._raw)

    def member(self, key: str) -> Optional[Set[str]]:
        """URLs of a filter kept as members (version 3), None when not kept."""
        if key not in self._raw:
            return None
        hit = self._decoded.get(key)
        if hit is None:
            t = self._table
            hit = {self._m(t[i]) for i in _delta_decode(self._raw[key]) if 0 <= i < len(t)}
            self._decoded[key] = hit
        return hit

    def flagged(self, key: str) -> Set[str]:
        hit = self._flags.get(key)
        if hit is None:
            f = self._filters.get(key)
            if f is None:
                hit = set()
            elif f.get("urls") is not None:
                hit = {self._m(u) for u in f["urls"]}
            else:
                hit = set(self.member(key) or ())
            self._flags[key] = hit
        return hit

    def scope(self, tab: str) -> Set[str]:
        """URLs the tab listed that are not internal rows (images, external links)."""
        if self.has_members and tab not in self._scope:
            self._scope[tab] = (self.member(f"{tab}:all") or set()) - set(self.rows)
        return self._scope.get(tab, set())

    @property
    def universe(self) -> Set[str]:
        """Every URL this crawl looked at (rows and every URL any filter listed)."""
        if self._universe is None:
            u = set(self.rows)
            for k in self._raw:
                u |= self.member(k) or set()
            for v in self._scope.values():
                u |= v
            for k in self._filters:
                u |= self.flagged(k)
            self._universe = u
        return self._universe

    def ran(self, key: str) -> bool:
        """The check ran in this crawl (unknown = ran, for snapshots saved without check statuses)."""
        return self.checks.get(key, "") != "not_checked"

    def looked_at(self, key: str, url: str) -> bool:
        """This crawl looked at `url` again for the check `key` (so not being flagged means fixed)."""
        if not self.ran(key):
            return False
        tab = key.split(":", 1)[0]
        return url in self.rows or url in self.scope(tab) or url in self.flagged(key)

    def in_filter(self, key: str) -> Optional[Set[str]]:
        """URLs in a filter (None when this snapshot kept only its count)."""
        hit = self.member(key)
        if hit is not None:
            return hit
        if key in self._filters:
            return self.flagged(key)
        if self.has_members and key in self.counts:
            return set()          # version 3 keeps every non-empty filter; absent = empty
        if not self.has_members and key in self.counts and int(self.counts.get(key, 0) or 0) == 0:
            return set()
        return None


def _view(snap: dict, mapper_sig: str = "", mapper: Optional[Callable[[str], str]] = None) -> _View:
    meta = snap.get("meta") or {}
    key = (meta.get("id"), meta.get("saved_at"), meta.get("observed"), mapper_sig, id(snap))
    with _lock:
        hit = _views.get(key)
        if hit is not None and hit.snap is snap:
            _views.move_to_end(key)
            return hit
    v = _View(snap, mapper)
    with _lock:
        _views[key] = v
        while len(_views) > _VIEW_CACHE:
            _views.popitem(last=False)
    return v


def _history(b: dict, reports_dir: Optional[str], skip: Iterable[str],
             mapper: Optional[Callable[[str], str]] = None, sig: str = "") -> List[_View]:
    """Saved snapshots of b's site from before b, oldest first (b's own crawl counts as before b when b
    is that crawl's re-checked state)."""
    meta = b.get("meta") or {}
    when = _when(meta)
    skip = set(skip)
    out = []
    for m in list_snapshots(reports_dir, meta.get("host") or meta.get("site") or ""):
        if m["id"] in skip or not when[0] or not m.get("started_at") or _when(m) >= when:
            continue
        snap = load_snapshot(m["id"], reports_dir)
        if snap:
            out.append(_View(snap, mapper))
    out.sort(key=lambda v: _when(v.meta))
    return out


def _regressed(url: str, key: str, timeline: List[_View]) -> bool:
    """Flagged at some point, then looked at again and clean: a later flag means it came back."""
    flagged_before = False
    for v in timeline:
        if url in v.flagged(key):
            flagged_before = True
        elif flagged_before and v.looked_at(key, url):
            return True
    return False


def _cap(urls: Iterable[str]) -> List[str]:
    return sorted(urls)[:MAX_LIST_URLS]


def _empty(error: str = "") -> dict:
    out = {"summary": {"new": 0, "fixed": 0, "regressed": 0, "unchanged": 0, "not_rechecked": 0, "not_comparable": 0},
           "issues": [], "changed": {"titles": [], "status": []}, "a": None, "b": None,
           "overview": [], "totals": [], "changes": [], "url_map": {"rules": 0, "mapped": 0, "errors": []}}
    if error:
        out["error"] = error
    return out


def _meta_out(v: _View) -> dict:
    m = v.meta
    out = {k: m.get(k) for k in ("id", "site", "started_at", "pages", "pages_ok", "urls", "issues", "issue_urls",
                                 "health_score", "auth_used", "limit_reached", "rechecked_at", "base_id")}
    out["version"] = v.version
    return out


def _map_sig(rules: Any) -> str:
    try:
        return hashlib.md5(json.dumps(rules or [], sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    except Exception:
        return ""


class _NotFound(Exception):
    """A compared snapshot does not exist."""


def _prepare(a: Any, b: Any, reports_dir: Optional[str], current: Optional[dict], url_map: Any):
    """(sa, sb, va, vb, map info, mapper, sig) or raises _NotFound naming the missing snapshot."""
    sa, sb = _resolve(a, reports_dir, current), _resolve(b, reports_dir, current)
    if not sa or not sb:
        missing = [str(x) for x, s in ((a, sa), (b, sb)) if not s]
        raise _NotFound("Snapshot not found: " + ", ".join(missing))
    rules, errors = parse_url_map(url_map)
    mapper = _mapper(rules)
    sig = _map_sig([(r.pattern, rep) for r, rep in rules]) if rules else ""
    va, vb = _view(sa, sig, mapper), _view(sb)
    info = {"rules": len(rules), "mapped": va.mapped if rules else 0, "errors": errors}
    return sa, sb, va, vb, info, mapper, sig


def compare(a: Any, b: Any, reports_dir: Optional[str] = None, current: Optional[dict] = None,
            url_map: Any = None, change_detection: Optional[Iterable[str]] = None,
            thresholds: Optional[Dict[str, Any]] = None) -> dict:
    """Compare snapshot a (before / previous) with b (after / current). a / b: a snapshot id, a snapshot dict,
    or "current" (resolved to `current`, e.g. snapshot_from(crawler, index)). url_map: URL Mapping rules
    applied to a's URLs. change_detection: elements of CHANGE_ELEMENTS to compare (None = all). thresholds:
    DEFAULT_THRESHOLDS overrides. Returns the /api/crawl/compare shape; an unknown snapshot gives the empty
    shape with "error". Never raises."""
    try:
        return _compare(a, b, reports_dir, current, url_map, change_detection, thresholds)
    except _NotFound as e:
        return _empty(str(e))
    except Exception as e:
        return _empty(f"Comparison failed: {e}")


def _compare(a: Any, b: Any, reports_dir: Optional[str], current: Optional[dict], url_map: Any,
             change_detection: Optional[Iterable[str]], thresholds: Optional[Dict[str, Any]]) -> dict:
    sa, sb, va, vb, map_info, mapper, sig = _prepare(a, b, reports_dir, current, url_map)
    same_site = (va.meta.get("host") or "") == (vb.meta.get("host") or "")
    timeline: Optional[List[_View]] = None

    summary = {"new": 0, "fixed": 0, "regressed": 0, "unchanged": 0, "not_rechecked": 0, "not_comparable": 0}
    issues = []
    hidden = _hidden_tabs()
    keys = [k for k in OrderedDict.fromkeys(list((sb.get("filters") or {}).keys()) + list((sa.get("filters") or {}).keys()))
            if k.partition(":")[0] not in hidden]
    differs = _differs(va, vb)
    for key in keys:
        info = (sb.get("filters") or {}).get(key) or (sa.get("filters") or {}).get(key) or {}
        fa, fb = va.flagged(key), vb.flagged(key)
        if key in differs:
            # the setting that decides this filter changed: its URLs would show fake new / fixed issues
            summary["not_comparable"] += 1
            count_a = len(fa) if key in (sa.get("filters") or {}) else int(va.counts.get(key, 0) or 0)
            count_b = len(fb) if key in (sb.get("filters") or {}) else int(vb.counts.get(key, 0) or 0)
            issues.append({
                "name": info.get("name") or key, "tab": info.get("tab") or key.split(":", 1)[0],
                "filter": info.get("filter") or key.split(":", 1)[-1], "issue_type": info.get("issue_type"),
                "priority": info.get("priority"), "a": count_a, "b": count_b, "delta": None,
                "new_urls": [], "fixed_urls": [], "regressed_urls": [], "not_rechecked_urls": [],
                "new_count": 0, "fixed_count": 0, "regressed_count": 0, "unchanged_count": 0,
                "not_rechecked_count": 0, "b_check": vb.checks.get(key, ""), "not_comparable": differs[key],
            })
            continue
        new = fb - fa
        regressed: Set[str] = set()
        if new and same_site:
            if timeline is None:
                ids = [va.meta.get("id"), vb.meta.get("id")]
                timeline = _history(sb, reports_dir, [i for i in ids if i], mapper, sig)
                if _when(va.meta) < _when(vb.meta):
                    timeline = sorted(timeline + [va], key=lambda v: _when(v.meta))
            regressed = {u for u in new if _regressed(u, key, timeline)}
        gone = fa - fb
        fixed = {u for u in gone if vb.looked_at(key, u)}
        not_rechecked = gone - fixed
        unchanged = fa & fb
        brand_new = new - regressed
        summary["new"] += len(brand_new)
        summary["regressed"] += len(regressed)
        summary["fixed"] += len(fixed)
        summary["unchanged"] += len(unchanged)
        summary["not_rechecked"] += len(not_rechecked)
        count_a = len(fa) if key in (sa.get("filters") or {}) else int(va.counts.get(key, 0) or 0)
        count_b = len(fb) if key in (sb.get("filters") or {}) else int(vb.counts.get(key, 0) or 0)
        issues.append({
            "name": info.get("name") or key, "tab": info.get("tab") or key.split(":", 1)[0],
            "filter": info.get("filter") or key.split(":", 1)[-1], "issue_type": info.get("issue_type"),
            "priority": info.get("priority"), "a": count_a, "b": count_b, "delta": count_b - count_a,
            "new_urls": _cap(brand_new), "fixed_urls": _cap(fixed), "regressed_urls": _cap(regressed),
            "not_rechecked_urls": _cap(not_rechecked),
            "new_count": len(brand_new), "fixed_count": len(fixed), "regressed_count": len(regressed),
            "unchanged_count": len(unchanged), "not_rechecked_count": len(not_rechecked),
            "b_check": vb.checks.get(key, ""),
        })
    issues.sort(key=lambda i: (-(i["regressed_count"] + i["new_count"]), -abs(i["delta"] or 0), i["name"]))

    titles, status = [], []
    n_titles = n_status = 0
    for url, rb in vb.rows.items():
        ra = va.rows.get(url)
        if ra is None:
            continue
        if (ra.get("title") or "") != (rb.get("title") or "") and (rb.get("category") == "2xx" or ra.get("title")):
            n_titles += 1
            if len(titles) < MAX_CHANGES:
                titles.append({"url": url, "a": ra.get("title") or "", "b": rb.get("title") or ""})
        if ra.get("status") != rb.get("status"):
            n_status += 1
            if len(status) < MAX_CHANGES:
                status.append({"url": url, "a": ra.get("status"), "b": rb.get("status"),
                               "a_category": ra.get("category"), "b_category": rb.get("category")})
    added = [u for u in vb.rows if u not in va.rows]
    removed = [u for u in va.rows if u not in vb.rows]
    out = {
        "summary": summary, "issues": issues,
        "changed": {"titles": titles, "status": status, "titles_count": n_titles, "status_count": n_status,
                    "urls_added": len(added), "urls_removed": len(removed),
                    "added_sample": sorted(added)[:MAX_LIST_URLS], "removed_sample": sorted(removed)[:MAX_LIST_URLS]},
        "a": _meta_out(va), "b": _meta_out(vb),
        "overview": _overview(va, vb),
        "totals": _totals(va, vb),
        "changes": _changes(va, vb, change_detection, thresholds),
        "url_map": map_info,
        "thresholds": _thresholds(thresholds),
        "settings_changed": _settings_changes(va, vb),
    }
    if not same_site:
        out["note"] = "The two crawls are of different sites: nothing is marked as regressed."
    return out


def _pct(change: int, previous: int) -> Optional[float]:
    return round(change / previous * 100, 2) if previous else None


def _differs(va: "_View", vb: "_View") -> Dict[str, str]:
    """not_comparable() for two views, over every tab:filter either crawl has."""
    keys = list(OrderedDict.fromkeys(list(vb.counts) + list(va.counts) + vb.member_keys + va.member_keys
                                     + list(vb._filters) + list(va._filters)))
    return not_comparable(va.settings, vb.settings, keys)


def _settings_changes(va: "_View", vb: "_View") -> List[dict]:
    """[{"setting", "previous", "current"}] for each comparison-relevant setting that differs (for the report and
    the panel)."""
    out: List[dict] = []
    sa, sb = va.settings, vb.settings
    pa, pb = sa.get("preferences") or {}, sb.get("preferences") or {}
    for k in sorted(set(pa) | set(pb)):
        if str(pa.get(k)) != str(pb.get(k)):
            out.append({"setting": f"preferences.{k}", "previous": pa.get(k), "current": pb.get(k)})
    for k in ("near_dup_threshold", "content_area", "ignore_non_indexable_issues"):
        if sa.get(k) != sb.get(k):
            out.append({"setting": k, "previous": sa.get(k), "current": sb.get(k)})
    return out


def _buckets(va: _View, vb: _View, key: str) -> Optional[Dict[str, Set[str]]]:
    """Screaming Frog's four sets for one filter, or None when either crawl kept only the filter's count."""
    fa, fb = va.in_filter(key), vb.in_filter(key)
    if fa is None or fb is None:
        return None
    only_b, only_a = fb - fa, fa - fb
    return {"added": {u for u in only_b if u in va.universe}, "new": {u for u in only_b if u not in va.universe},
            "removed": {u for u in only_a if u in vb.universe}, "missing": {u for u in only_a if u not in vb.universe},
            "_a": fa, "_b": fb}


def _hidden_tabs() -> Set[str]:
    """Tabs the registry hides (AMP is deprecated): left out of comparisons, also of older snapshots."""
    try:
        from . import registry
        return {t.id for t in registry.all_tabs() if t.hidden}
    except Exception:
        return {"amp"}


def _name_of(key: str, va: _View, vb: _View) -> Tuple[str, str, Optional[str], Optional[str]]:
    for v in (vb, va):
        n = v.names.get(key)
        if isinstance(n, list) and len(n) >= 2:
            return str(n[0]), str(n[1]), (n[2] if len(n) > 2 else None), (n[3] if len(n) > 3 else None)
    tab_id, _, fid = key.partition(":")
    try:
        from . import registry
        tab = registry.get_tab(tab_id)
        if tab is not None:
            if fid == "all":
                return tab.name, tab.total_label or "All", None, None
            f = next((f for f in tab.filters if f.id == fid), None)
            if f is not None:
                return tab.name, f.name, f.issue_type, f.priority
            return tab.name, fid, None, None
    except Exception:
        pass
    return tab_id, fid, None, None


def _overview(va: _View, vb: _View) -> List[dict]:
    """One row per tab:filter of either crawl: current / previous / change / change % / added / new / removed /
    missing. current and previous count unique URLs when both crawls kept the filter's URLs (so the four
    buckets reconcile with the change exactly); otherwise the filter counts, with the buckets None."""
    keys = list(OrderedDict.fromkeys(list(vb.counts) + list(va.counts) + vb.member_keys + va.member_keys))
    hidden = _hidden_tabs()
    differs = not_comparable(va.settings, vb.settings, keys)
    # a snapshot records every filter of every tab (0 included): a filter one crawl lacks did not exist when it was
    # saved (an older version, a tab that was off), so that side is "Not checked", never 0
    has_a, has_b = set(va.counts) | set(va.member_keys), set(vb.counts) | set(vb.member_keys)
    out = []
    for key in keys:
        if key.partition(":")[0] in hidden:
            continue
        tab_name, filter_name, issue_type, priority = _name_of(key, va, vb)
        rows_b, rows_a = int(vb.counts.get(key, 0) or 0), int(va.counts.get(key, 0) or 0)
        why = differs.get(key, "")
        bk = None if why else _buckets(va, vb, key)
        if why:
            # the setting behind this filter changed: its URLs moved because of the setting, not the site
            fa, fb = va.in_filter(key), vb.in_filter(key)
            cur = len(fb) if fb is not None else rows_b
            prev = len(fa) if fa is not None else rows_a
            nums = {k: None for k in BUCKETS}
            reconciled = None
            change: Optional[int] = None
        elif bk is not None:
            cur, prev = len(bk["_b"]), len(bk["_a"])
            nums = {k: len(bk[k]) for k in BUCKETS}
            reconciled = nums["added"] + nums["new"] - nums["removed"] - nums["missing"] == cur - prev
            change = cur - prev
        else:
            cur, prev = rows_b, rows_a
            nums = {k: None for k in BUCKETS}
            reconciled = None
            change = cur - prev
        check_a, check_b = va.checks.get(key, ""), vb.checks.get(key, "")
        if key not in has_a or key not in has_b:
            nums = {k: None for k in BUCKETS}
            reconciled, change, bk = None, None, None
            if key not in has_a:
                prev, rows_a, check_a = None, None, "not_checked"
            if key not in has_b:
                cur, rows_b, check_b = None, None, "not_checked"
        tab_id, _, fid = key.partition(":")
        out.append(dict({"key": key, "tab": tab_id, "filter": fid, "tab_name": tab_name, "filter_name": filter_name,
                         "issue_type": issue_type, "priority": priority, "current": cur, "previous": prev,
                         "change": change, "change_pct": _pct(change, prev) if change is not None else None,
                         "rows_current": rows_b, "rows_previous": rows_a, "exact": bk is not None,
                         "reconciled": reconciled, "comparable": not why, "not_comparable": why,
                         "check_current": check_b, "check_previous": check_a},
                        **nums))
    return out


def _totals(va: _View, vb: _View) -> List[dict]:
    """Summary rows (Screaming Frog's Overview > Summary): internal URLs crawled and indexable ones."""
    def row(label: str, key: str, sa: Set[str], sb: Set[str]) -> dict:
        added = {u for u in sb - sa if u in va.universe}
        new = (sb - sa) - added
        removed = {u for u in sa - sb if u in vb.universe}
        missing = (sa - sb) - removed
        change = len(sb) - len(sa)
        return {"key": key, "label": label, "current": len(sb), "previous": len(sa), "change": change,
                "change_pct": _pct(change, len(sa)), "added": len(added), "new": len(new), "removed": len(removed),
                "missing": len(missing)}
    ra, rb = set(va.rows), set(vb.rows)
    ia = {u for u, r in va.rows.items() if r.get("indexability") == "Indexable"}
    ib = {u for u, r in vb.rows.items() if r.get("indexability") == "Indexable"}
    out = [row("Total internal URLs crawled", "urls", ra, rb), row("Internal indexable URLs", "indexable", ia, ib)]
    for cat in ("2xx", "3xx", "4xx", "5xx", "blocked", "error"):
        ca = {u for u, r in va.rows.items() if r.get("category") == cat}
        cb = {u for u, r in vb.rows.items() if r.get("category") == cat}
        if ca or cb:
            out.append(row(f"Internal URLs {cat}", f"category:{cat}", ca, cb))
    return out


def _thresholds(given: Optional[Dict[str, Any]]) -> Dict[str, float]:
    out = dict(DEFAULT_THRESHOLDS)
    for k, v in (given or {}).items():
        if k in out:
            try:
                out[k] = max(0.0, float(v))
            except (TypeError, ValueError):
                pass
    return out


def _simhash_similarity(a: int, b: int) -> float:
    return round((64 - bin((int(a) ^ int(b)) & ((1 << 64) - 1)).count("1")) / 64 * 100, 1)


def _element_diff(name: str, spec: dict, ra: dict, rb: dict, th: Dict[str, float]) -> Optional[dict]:
    """None when the URL is not compared for this element, else {"changed": bool, ...detail}."""
    if spec["pages"] and not (ra.get("category") == "2xx" and rb.get("category") == "2xx"):
        return None
    f = spec["field"]
    if f not in ra or f not in rb:
        return None
    va_, vb_ = ra.get(f), rb.get(f)
    kind = spec["kind"]
    if kind == "number":
        delta = _int(vb_) - _int(va_)
        return {"changed": abs(delta) >= max(1.0, th.get(name, 1)), "a": _int(va_), "b": _int(vb_), "delta": delta}
    if kind == "set":
        sa, sb = set(va_ or []), set(vb_ or [])
        return {"changed": sa != sb, "a": sorted(sa), "b": sorted(sb), "added": sorted(sb - sa),
                "removed": sorted(sa - sb)}
    if kind == "simhash":
        if not va_ or not vb_:
            return None
        sim = _simhash_similarity(va_, vb_)
        return {"changed": sim < th.get("content_similarity", 90.0), "similarity": sim}
    if kind == "text":
        return {"changed": (va_ or "") != (vb_ or ""), "a": va_ or "", "b": vb_ or ""}
    return {"changed": va_ != vb_, "a": va_, "b": vb_}


def _changes(va: _View, vb: _View, elements: Optional[Iterable[str]], thresholds: Optional[Dict[str, Any]],
             collect: Optional[str] = None) -> Any:
    """Change detection rows per element; collect="changed:title" returns that URL list instead."""
    th = _thresholds(thresholds)
    wanted = [e for e in (elements if elements is not None else CHANGE_ELEMENTS) if e in CHANGE_ELEMENTS]
    common = [u for u in vb.rows if u in va.rows]
    out = []
    picked: List[str] = []
    want_kind, _, want_el = (collect or "").partition(":")
    for name in wanted:
        spec = CHANGE_ELEMENTS[name]
        field = spec["field"]
        reason = ""
        if va.rows and not any(field in r for r in va.rows.values()):
            reason = OLDER_NOT_RECORDED if va.version < SNAPSHOT_VERSION else \
                "Not checked: the previous crawl did not record it."
        elif vb.rows and not any(field in r for r in vb.rows.values()):
            reason = OLDER_NOT_RECORDED if vb.version < SNAPSHOT_VERSION else \
                "Not checked: the current crawl did not record it."
        row = {"element": name, "label": spec["label"], "compared": 0, "changed": 0, "not_changed": 0,
               "not_checked": reason, "sample": []}
        if not reason:
            for u in common:
                d = _element_diff(name, spec, va.rows[u], vb.rows[u], th)
                if d is None:
                    continue
                row["compared"] += 1
                if d["changed"]:
                    row["changed"] += 1
                    if len(row["sample"]) < MAX_CHANGES:
                        row["sample"].append(dict(d, url=u))
                else:
                    row["not_changed"] += 1
                if collect and want_el == name and (want_kind == "changed") == bool(d["changed"]):
                    picked.append(u)
        if spec["kind"] == "number":
            row["sample"].sort(key=lambda s: -abs(s.get("delta") or 0))
        out.append(row)
    return picked if collect else out


def compare_urls(a: Any, b: Any, key: str, kind: str, reports_dir: Optional[str] = None,
                 current: Optional[dict] = None, url_map: Any = None, offset: int = 0, limit: int = 100,
                 thresholds: Optional[Dict[str, Any]] = None) -> dict:
    """The URLs behind one number of a comparison, sorted and paged. kind: added | new | removed | missing |
    current | previous (with key "tab:filter"), fixed | not_rechecked | unchanged (issue filters), or
    "changed:<element>" / "not_changed:<element>" (change detection; key is ignored). Never raises."""
    try:
        _sa, _sb, va, vb, _info, _m, _sig = _prepare(a, b, reports_dir, current, url_map)
    except _NotFound as e:
        return {"key": key, "kind": kind, "total": 0, "urls": [], "error": str(e)}
    except Exception as e:
        return {"key": key, "kind": kind, "total": 0, "urls": [], "error": f"Comparison failed: {e}"}
    kind = str(kind or "").strip()
    urls: Optional[Iterable[str]] = None
    if kind.startswith(("changed:", "not_changed:")):
        urls = _changes(va, vb, [kind.split(":", 1)[1]], thresholds, collect=kind)
    elif kind in BUCKETS and key in _differs(va, vb):
        return {"key": key, "kind": kind, "total": 0, "urls": [], "error": _differs(va, vb)[key]}
    elif kind in ("current", "previous") and key in _differs(va, vb):
        fs = (vb if kind == "current" else va).in_filter(key)
        if fs is None:
            return {"key": key, "kind": kind, "total": 0, "urls": [],
                    "error": "The older crawl kept only the count of this filter, not its URLs."}
        urls = fs
    elif kind in BUCKETS or kind in ("current", "previous"):
        bk = _buckets(va, vb, key)
        if bk is None:
            return {"key": key, "kind": kind, "total": 0, "urls": [],
                    "error": "The older crawl kept only the count of this filter, not its URLs."}
        urls = bk["_b"] if kind == "current" else bk["_a"] if kind == "previous" else bk[kind]
    elif kind in ("fixed", "not_rechecked", "unchanged") and key in _differs(va, vb):
        return {"key": key, "kind": kind, "total": 0, "urls": [], "error": _differs(va, vb)[key]}
    elif kind in ("fixed", "not_rechecked", "unchanged"):
        fa, fb = va.flagged(key), vb.flagged(key)
        gone = fa - fb
        urls = {"fixed": {u for u in gone if vb.looked_at(key, u)},
                "not_rechecked": {u for u in gone if not vb.looked_at(key, u)}, "unchanged": fa & fb}[kind]
    else:
        return {"key": key, "kind": kind, "total": 0, "urls": [], "error": f"Unknown list: {kind}"}
    ordered = sorted(set(urls))
    offset, limit = max(0, int(offset or 0)), max(1, min(5000, int(limit or 100)))
    return {"key": key, "kind": kind, "total": len(ordered), "offset": offset, "urls": ordered[offset:offset + limit]}
