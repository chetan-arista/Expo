"""
Sitemaps tab (Screaming Frog "Sitemaps"): crawled pages compared with the URLs listed in the site's
XML sitemaps.

Rows ("All"): every internal URL the crawl requested except page resources (images, JavaScript,
CSS, fonts), so HTML pages, PDFs and other documents, as Screaming Frog lists them; then any page
resource a sitemap lists; then sitemap URLs the crawl never requested (shown as SitemapURL, status
"Not crawled"); then sitemap files that break the size limits (SitemapFile).

Filters follow Screaming Frog:
  - URLs Not In Sitemap only looks at indexable pages (non-indexable URLs are expected to be left out
    of a sitemap) and only when at least one sitemap was found; with no sitemap data it stays empty.
  - Orphan URLs are sitemap URLs that no crawled page links to (analysis.Site.orphans).
  - XML Sitemap With Over 50k URLs / Over 50MB use per-sitemap stats when the crawler provides them
    (Site.sitemap_stats: {sitemap_url: {"urls": int, "size_bytes": int}}, optional); otherwise the URL
    count is taken from the internal URLs each sitemap listed and the size check is skipped.
  - <lastmod> checks (Site.sitemap_lastmod: {url: lastmod as written}, first sitemap that gives one wins):
    Invalid Lastmod (not a W3C date such as 2026-10-26 or 2026-10-26T09:30:00+05:30), Lastmod In The Future
    (more than a day ahead of the crawl), Missing Lastmod (listed without one) and Lastmod Older Than
    Last-Modified (the page's Last-Modified header is more than 30 days newer than the sitemap says; a
    Last-Modified within a minute of the response Date is the server stamping "now" and is not trusted).
    Google only uses lastmod when it is consistently accurate.
  - The comparisons run in Crawl Analysis (step "Sitemaps"); with the step off every filter is Not checked.
  - Partial sitemaps (P9): when sitemap files were listed but not read (Max sitemap files) or the sitemap URL index
    stopped at Max sitemap index URLs, a page missing from what was read may be listed in what was not. URLs Not
    In Sitemap then flags nothing: its pages are "Not checked" (with their count); the other URL checks keep what
    they found, with a caveat in the checks, and are "Not checked" (never passed) when they found nothing.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from ...url_utils import get_domain, normalize_url
from ..registry import Column, Filter, Tab, register, register_requirement, seo
from .internal import (ADDRESS, CONTENT_TYPE, INDEXABILITY, INDEXABILITY_STATUS, STATUS, STATUS_CODE,
                       category_of, content_kind, header, internal_rows, is_html_doc, last_modified, memo)

SITEMAP_MAX_URLS = 50000                 # sitemaps.org limit per file
SITEMAP_MAX_BYTES = 50 * 1024 * 1024     # 50MB uncompressed
RESOURCE_KINDS = ("images", "javascript", "css", "fonts")   # page resources, left out of "All"
LASTMOD_FUTURE_GRACE = timedelta(days=1)       # time zones and clock skew
LASTMOD_STALE_DAYS = 30                        # Last-Modified newer than lastmod by more than this = stale
_W3C_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?)?)?$")


@dataclass
class SitemapURL:
    """A URL listed in an XML sitemap that the crawl never requested. Shaped like an unchecked
    ExternalLink (url + domain, no status) so the shared helpers give it category ""."""
    url: str
    domain: str = ""
    sitemaps: List[str] = field(default_factory=list)
    status_code: Optional[int] = None
    http_status: str = ""
    content_type: str = ""
    not_crawled: bool = True


@dataclass
class SitemapFile:
    """An XML sitemap file itself (listed only when it breaks a size limit)."""
    url: str
    domain: str = ""
    url_count: int = 0
    size_bytes: Optional[int] = None
    status_code: Optional[int] = None
    http_status: str = ""
    content_type: str = "application/xml"
    is_sitemap_file: bool = True


def _domain(url: str) -> str:
    try:
        return get_domain(url) or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------------------------
# Sitemap lookups
# ---------------------------------------------------------------------------------------------
def sitemaps_of(site, url: str) -> List[str]:
    """Sitemaps that list this URL ([] if none)."""
    m = site.sitemap_urls or {}
    if url in m:
        return m[url] or []
    try:
        return m.get(normalize_url(url) or "", []) or []
    except Exception:
        return []


def in_sitemap(site, url: str) -> bool:
    m = site.sitemap_urls or {}
    if url in m:
        return True
    try:
        return (normalize_url(url) or "") in m
    except Exception:
        return False


def sitemap_stats(site) -> Dict[str, Dict[str, Any]]:
    """sitemap_url -> {"urls": int, "size_bytes": int|None}: the crawler's stats when present, else URL
    counts rebuilt from the sitemap index (internal URLs only, so a lower bound)."""
    def build():
        given = getattr(site, "sitemap_stats", None)
        if isinstance(given, dict) and given:
            out = {}
            for sm, st in given.items():
                st = st if isinstance(st, dict) else {}
                out[str(sm)] = {"urls": int(st.get("urls") or 0), "size_bytes": st.get("size_bytes")}
            return out
        counts: Dict[str, Dict[str, Any]] = {}
        for _, sms in (site.sitemap_urls or {}).items():
            for sm in sms or []:
                counts.setdefault(sm, {"urls": 0, "size_bytes": None})["urls"] += 1
        return counts
    return memo(site, "sitemap_stats", build)


def _oversized(st: Dict[str, Any]) -> bool:
    size = st.get("size_bytes")
    return st.get("urls", 0) > SITEMAP_MAX_URLS or (isinstance(size, int) and size > SITEMAP_MAX_BYTES)


# ---------------------------------------------------------------------------------------------
# <lastmod>
# ---------------------------------------------------------------------------------------------
def parse_lastmod(text: str) -> Optional[datetime]:
    """A W3C datetime (the sitemap <lastmod> format) as an aware datetime, None when it is not one."""
    t = (text or "").strip()
    if not t or not _W3C_DATE.match(t):
        return None
    try:
        if len(t) == 4:
            d = datetime(int(t), 1, 1)
        elif len(t) == 7:
            d = datetime(int(t[:4]), int(t[5:7]), 1)
        else:
            d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _http_date(text: str) -> Optional[datetime]:
    try:
        d = parsedate_to_datetime((text or "").strip())
    except (TypeError, ValueError, IndexError):
        return None
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def lastmod_of(site, url: str) -> Optional[str]:
    """<lastmod> as written for a sitemap URL ("" when listed without one, None when lastmods were not
    recorded for this crawl)."""
    m = getattr(site, "sitemap_lastmod", None)
    if m is None:
        return None
    v = m.get(url)
    if v is None:
        try:
            v = m.get(normalize_url(url) or "")
        except Exception:
            v = None
    return v or ""


def _now(site) -> datetime:
    return datetime.now(timezone.utc)


def page_last_modified(r) -> Optional[datetime]:
    """The page's Last-Modified header as a date, None when absent or when it is the server stamping the
    response time (within a minute of the Date header)."""
    lm = _http_date(last_modified(r))
    if lm is None:
        return None
    date = _http_date(header(r, "date"))
    if date is not None and abs((date - lm).total_seconds()) <= 60:
        return None
    return lm


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------
def _sitemap_rows(site) -> List[Any]:
    rows: List[Any] = []
    seen = set()
    for r in internal_rows(site):
        if content_kind(r) not in RESOURCE_KINDS or in_sitemap(site, r.url):
            rows.append(r)
            seen.add(r.url)
    for url, sms in (site.sitemap_urls or {}).items():
        if url in seen:
            continue
        seen.add(url)
        r = site.by_url.get(url)
        rows.append(r if r is not None else SitemapURL(url=url, domain=_domain(url), sitemaps=list(sms or [])))
    for sm, st in sitemap_stats(site).items():
        if _oversized(st) and sm not in seen:
            seen.add(sm)
            rows.append(SitemapFile(url=sm, domain=_domain(sm), url_count=st.get("urls", 0),
                                    size_bytes=st.get("size_bytes")))
    return rows


def sitemap_rows(site) -> List[Any]:
    return memo(site, "sitemap_rows", lambda: _sitemap_rows(site))


def _crawled(r) -> bool:
    return not isinstance(r, (SitemapURL, SitemapFile))


# ---------------------------------------------------------------------------------------------
# What the comparison could not see (P9)
# ---------------------------------------------------------------------------------------------
OLD_FILES_LIMIT = 25           # sitemap files a crawl read before the limit became a setting


def sitemap_gap(site) -> str:
    """Why only part of the sitemaps was compared ("" when every listed file was read and the URL index held every
    URL): files listed but not read, the URL index full."""
    def build():
        parts = []
        unread = len(getattr(site, "sitemaps_not_read", None) or ())
        if unread:
            limit = getattr(site, "sitemap_files_limit", None) or OLD_FILES_LIMIT
            parts.append(f"{unread:,} sitemap file{'s were' if unread != 1 else ' was'} listed but not read (at most "
                         f"{limit:,} are read per crawl: raise Max sitemap files)")
        full = getattr(site, "sitemap_index_full", None)
        if isinstance(full, dict):
            more = int(full.get("left_out") or 0)
            parts.append(f"the sitemap URL index stopped at {int(full.get('limit') or 0):,} URLs"
                         + (f" and {more:,} more were listed" if more else "") + " (raise Max sitemap index URLs)")
        return "; ".join(parts)
    return memo(site, "sitemap_gap", build)


def _not_in_read_sitemaps(r, site) -> bool:
    """An indexable page missing from the sitemaps that were read (it may still be in what was not read)."""
    return (bool(site.sitemap_urls) and _crawled(r) and is_html_doc(r) and bool(site.indexable.get(r.url))
            and not in_sitemap(site, r.url))


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
def _in_sitemap(r, site) -> bool:
    return not isinstance(r, SitemapFile) and in_sitemap(site, r.url)


def _not_in_sitemap(r, site) -> bool:
    """Only when every sitemap was read: with files left unread (or the index full) the page is Not checked."""
    return _not_in_read_sitemaps(r, site) and not sitemap_gap(site)


def _orphan(r, site) -> bool:
    orphans = memo(site, "sitemap_orphans", lambda: set(site.orphans or []))
    return not isinstance(r, SitemapFile) and r.url in orphans


def _non_indexable_in_sitemap(r, site) -> bool:
    return _crawled(r) and in_sitemap(site, r.url) and not site.indexable.get(r.url)


def _multiple(r, site) -> bool:
    return not isinstance(r, SitemapFile) and len(sitemaps_of(site, r.url)) > 1


def _listed(r, site) -> bool:
    return not isinstance(r, SitemapFile) and in_sitemap(site, r.url)


def _lastmod_invalid(r, site) -> bool:
    v = lastmod_of(site, r.url) if _listed(r, site) else None
    return bool(v) and parse_lastmod(v) is None


def _lastmod_future(r, site) -> bool:
    v = lastmod_of(site, r.url) if _listed(r, site) else None
    d = parse_lastmod(v) if v else None
    return d is not None and d > _now(site) + LASTMOD_FUTURE_GRACE


def _lastmod_missing(r, site) -> bool:
    return _listed(r, site) and lastmod_of(site, r.url) == ""


def _lastmod_stale(r, site) -> bool:
    if not _listed(r, site) or not _crawled(r) or category_of(r) != "2xx":
        return False
    v = lastmod_of(site, r.url)
    d = parse_lastmod(v) if v else None
    lm = page_last_modified(r) if d is not None else None
    return lm is not None and lm - d > timedelta(days=LASTMOD_STALE_DAYS)


def _over_urls(r, site) -> bool:
    return isinstance(r, SitemapFile) and r.url_count > SITEMAP_MAX_URLS


def _over_size(r, site) -> bool:
    return isinstance(r, SitemapFile) and isinstance(r.size_bytes, int) and r.size_bytes > SITEMAP_MAX_BYTES


# ---------------------------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------------------------
def _status(r, site):
    if isinstance(r, SitemapURL):
        return "Not crawled"
    if isinstance(r, SitemapFile):
        return "XML Sitemap"
    return STATUS.get(r, site)


def _sitemap_list(r, site) -> List[str]:
    if isinstance(r, SitemapFile):
        return []
    return list(sitemaps_of(site, r.url))


def _file_urls(r, site):
    """A sitemap file row: the <url> entries it lists (<sitemap> entries for an index)."""
    return r.url_count if isinstance(r, SitemapFile) else ""


def _file_size(r, site):
    """A sitemap file row: its uncompressed size in bytes ("" when the file was not read to its end)."""
    return r.size_bytes if isinstance(r, SitemapFile) and isinstance(r.size_bytes, int) else ""


COLUMNS = [
    ADDRESS, CONTENT_TYPE, STATUS_CODE,
    Column("status", "Status", _status),
    INDEXABILITY, INDEXABILITY_STATUS,
    Column("in_sitemaps", "Sitemaps", lambda r, site: len(_sitemap_list(r, site)), "num"),
    Column("sitemaps", "XML Sitemap(s)", _sitemap_list, "list"),
    Column("unique_inlinks", "Unique Inlinks", lambda r, site: (site.unique_inlinks or {}).get(r.url, 0), "num"),
    Column("lastmod", "Sitemap Lastmod", lambda r, site: (lastmod_of(site, r.url) or "") if _listed(r, site) else ""),
    Column("last_modified", "Last-Modified Header",
           lambda r, site: last_modified(r) if _crawled(r) else ""),
    Column("sitemap_file_urls", "Sitemap File URLs", _file_urls, "num"),
    Column("sitemap_file_size", "Sitemap File Size (Bytes)", _file_size, "num"),
]

FILTERS = [
    Filter("in_sitemap", "URLs in Sitemap", _in_sitemap,
           description="URLs listed in at least one of the site's XML sitemaps (crawled or not)."),
    Filter("not_in_sitemap", "URLs not in Sitemap", _not_in_sitemap, "Issue", "Medium",
           "Indexable pages found by following links that are missing from every XML sitemap. That can be "
           "on purpose for unimportant pages, but often the sitemap is just out of date. Non-indexable "
           "pages are not counted, since they should stay out of a sitemap. Only checked when a sitemap "
           "was found.",
           "Add the pages you want in search results (their canonical URLs) to the XML sitemap, ideally "
           "generated automatically by your CMS so it stays current."),
    Filter("orphan", "Orphan URLs", _orphan, "Issue", "Medium",
           "URLs listed in an XML sitemap that no crawled page links to. Visitors cannot reach them by "
           "browsing and search engines see them as unimportant. They may be old pages left in the "
           "sitemap by mistake, or important pages that lost their links.",
           "If the page matters, link to it from relevant pages on the site. If it is outdated, remove it "
           "from the sitemap (and redirect or retire the page)."),
    Filter("non_indexable_in_sitemap", "Non-Indexable URLs in Sitemap", _non_indexable_in_sitemap,
           "Issue", "Medium",
           "URLs listed in an XML sitemap that cannot be indexed: they redirect, return an error, are "
           "noindex, are canonicalised to another URL or are blocked by robots.txt. A sitemap should only "
           "list pages you want in search results; mixed signals waste crawl time.",
           "Remove these URLs from the sitemap and list their final, indexable URL instead, or make the "
           "page indexable if it should be in search results."),
    Filter("multiple_sitemaps", "URLs in Multiple Sitemaps", _multiple, "Warning", "Low",
           "URLs listed in more than one XML sitemap. This is allowed, but usually means the sitemaps "
           "overlap and are harder to maintain and to read in search engine reports.",
           "List each URL in one sitemap only, grouped in a way that makes sense (by section or content type)."),
    Filter("over_50k_urls", "XML Sitemap with over 50k URLs", _over_urls, "Issue", "High",
           f"XML sitemap files that list more than {SITEMAP_MAX_URLS:,} URLs, the maximum one sitemap may "
           "hold. Search engines may ignore the URLs beyond the limit or reject the file.",
           "Split the sitemap into several files of up to 50,000 URLs each and list them in a sitemap index file."),
    Filter("lastmod_invalid", "Invalid Lastmod", _lastmod_invalid, "Warning", "Low",
           "The sitemap gives this URL a <lastmod> that is not a valid W3C date (for example 26/10/2026 or "
           "'yesterday'), so search engines ignore it.",
           "Write lastmod as YYYY-MM-DD or a full timestamp such as 2026-10-26T09:30:00+05:30.",
           short_fix="Write lastmod as YYYY-MM-DD (W3C date format)",
           requires=("sitemap_lastmod",)),
    Filter("lastmod_future", "Lastmod In The Future", _lastmod_future, "Warning", "Low",
           "The sitemap's <lastmod> for this URL is more than a day in the future. That cannot be true, so "
           "search engines learn not to trust the site's lastmod values at all.",
           "Fill lastmod from the page's real last change date (the CMS's modified date), never a future date.",
           short_fix="Set lastmod to the real last change date, not a future one",
           requires=("sitemap_lastmod",)),
    Filter("lastmod_missing", "Missing Lastmod", _lastmod_missing, "Opportunity", "Low",
           "The sitemap lists this URL without a <lastmod>. Search engines then cannot tell which pages "
           "changed since their last visit and may recrawl changed pages later.",
           "Add an accurate lastmod to each sitemap entry (most CMS sitemap plugins can do this).",
           short_fix="Add an accurate lastmod to each sitemap entry",
           requires=("sitemap_lastmod",)),
    Filter("lastmod_stale", "Lastmod Older Than Last-Modified", _lastmod_stale, "Warning", "Low",
           f"The page's Last-Modified header is more than {LASTMOD_STALE_DAYS} days newer than the sitemap's "
           "<lastmod> for it, so the sitemap says the page has not changed when it has. Search engines may "
           "skip recrawling it.",
           "Generate lastmod from the same modified date the server uses, and regenerate the sitemap when "
           "pages change.",
           short_fix="Update sitemap lastmod when the page changes",
           requires=("sitemap_lastmod",)),
    Filter("over_50mb", "XML Sitemap over 50MB", _over_size, "Issue", "High",
           "XML sitemap files larger than 50MB (uncompressed), the maximum size allowed. Search engines may "
           "not read the whole file. Only checked when the crawler recorded the sitemap's size.",
           "Split the sitemap into smaller files and list them in a sitemap index file. Gzip only reduces the "
           "download; the 50MB limit applies to the uncompressed file."),
]

FILE_FILTERS = ("over_50k_urls", "over_50mb")      # judge the sitemap files themselves, not the URLs


def _found_any(site, filter_id: str) -> bool:
    f = next((x for x in FILTERS if x.id == filter_id), None)
    if f is None:
        return False

    def build():
        for r in sitemap_rows(site):
            try:
                if f.test(r, site):
                    return True
            except Exception:
                continue
        return False
    return memo(site, "sitemap_found:" + filter_id, build)


def gap_reason(site, filter_id: str) -> str:
    """The "Not checked" reason of a URL check when only part of the sitemaps was read ("" when the check could
    judge: everything was read, or it found something, or no page could be in what was not read)."""
    gap = sitemap_gap(site)
    if not gap or filter_id in FILE_FILTERS:
        return ""
    if filter_id == "not_in_sitemap":
        n = memo(site, "sitemap_not_in_read", lambda: sum(1 for r in sitemap_rows(site)
                                                          if _not_in_read_sitemaps(r, site)))
        if not n:
            return ""      # every indexable page is in the sitemaps that were read: nothing to be unsure about
        return (f"{n:,} indexable page{'s are' if n != 1 else ' is'} not in the sitemap URLs that were read, but "
                f"{gap}, so they may be listed in what was not read.")
    if _found_any(site, filter_id):
        return ""
    return f"Only part of the sitemaps was read: {gap}."


def skip_reason(site, filter_id: str) -> str:
    """Why a Sitemaps filter is "Not checked" ("" when it ran)."""
    cfg = getattr(site, "config", None)
    if cfg is not None and getattr(cfg, "use_sitemaps", True) is False:
        return "Sitemap discovery was turned off for this crawl."
    if not site.sitemap_urls:
        return "No XML sitemap was found for this site, so it could not be compared with the crawl."
    return gap_reason(site, filter_id)


def _lastmod_recorded(site, arg, rows) -> str:
    if getattr(site, "sitemap_lastmod", None) is None and getattr(site, "sitemap_urls", None):
        return "The sitemap <lastmod> values were not recorded for this crawl (crawled with an older version)."
    return ""


register_requirement("sitemap_lastmod", _lastmod_recorded)
register(Tab(id="sitemaps", name="Sitemaps", order=200, columns=COLUMNS, filters=FILTERS, rows=sitemap_rows,
             requires=("analysis:sitemaps",), skip_reason=skip_reason))
