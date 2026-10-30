"""
Search Console tab (Screaming Frog "Search Console"): Google Search Console data joined to the crawl. Optional:
it needs the Google connection with a Search Console property (Configuration > API Access) and a fetch for this
crawl; until then every filter is "Not connected" / "not fetched yet", never 0.

Rows ("All"): every internal URL the crawl requested except page resources (images, scripts, styles, fonts),
then the URLs Search Console knows that the crawl never found (ApiOnlyURL, status blank: "Not crawled").
Search Analytics (clicks, impressions, CTR, position for the date range) is matched to crawled URLs with the
Screaming Frog rules (trailing slash, http/https, optional upper/lower case and www); when several API URLs
match one crawled URL the one with the most clicks is kept (never summed).
URL Inspection (optional, 2,000 URLs per property a day) gives Google's index status per URL.
AMP Results / AMP Issues and the AMP filter are left out (AMP is deprecated; user decision).
Mobile usability: Google retired it in December 2023; the filter stays "Not checked" unless Google still sends
a verdict.
"""
from dataclasses import dataclass
from typing import Any, List, Optional

from ...integrations import gsc
from ..registry import Column, Filter, Tab, register
from .internal import (ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, STATUS_CODE, content_kind, internal_rows, memo)

RESOURCE_KINDS = ("images", "javascript", "css", "fonts")


@dataclass
class ApiOnlyURL:
    """A URL an API (Search Console, GA4) reports that the crawl never requested. Shaped like an unchecked
    ExternalLink (url + domain, no status), so the shared helpers give it category "" ("Not crawled")."""
    url: str
    domain: str = ""
    source: str = ""
    status_code: Optional[int] = None
    http_status: str = ""
    content_type: str = ""
    not_crawled: bool = True


def _domain(url: str) -> str:
    try:
        from ...url_utils import get_domain
        return get_domain(url) or ""
    except Exception:
        return ""


def page_rows(site) -> List[Any]:
    """Every crawled internal URL except page resources, in crawl order (shared with the Analytics tab)."""
    return memo(site, "api_page_rows",
                lambda: [r for r in internal_rows(site) if content_kind(r) not in RESOURCE_KINDS])


def _rows(site) -> List[Any]:
    def build():
        rows = list(page_rows(site))
        for url in gsc.joined(site)["orphans"]:
            rows.append(ApiOnlyURL(url=url, domain=_domain(url), source="Search Console"))
        return rows
    return memo(site, "gsc_rows", build)


def _crawled(r) -> bool:
    return not isinstance(r, ApiOnlyURL)


def _sa(r, site) -> Optional[dict]:
    j = gsc.joined(site)
    if isinstance(r, ApiOnlyURL):
        return j["orphans"].get(r.url)
    return j["rows"].get(r.url)


def _sa_fetched(site) -> bool:
    return (gsc.data_of(site).get("analytics") or {}).get("status") in ("done", "stopped")


def _sa_complete(site) -> bool:
    sa = gsc.data_of(site).get("analytics") or {}
    return sa.get("status") == "done" and not sa.get("truncated")


def _metric(key: str, scale: float = 1.0, digits: Optional[int] = None):
    def get(r, site):
        row = _sa(r, site)
        if row is None:
            return 0 if _sa_fetched(site) and _sa_complete(site) and _crawled(r) else ""
        v = row.get(key)
        if v is None:
            return ""
        v = v * scale
        return round(v, digits) if digits is not None else v
    return get


def _insp(r, site) -> Optional[dict]:
    return gsc.joined(site)["inspection"].get(getattr(r, "url", "")) if _crawled(r) else None


def _i(fn):
    def get(r, site):
        c = _insp(r, site)
        return fn(c) if c else ""
    return get


_YES_NO = {"ALLOWED": "Yes", "DISALLOWED": "No", "INDEXING_ALLOWED": "Yes",
           "BLOCKED_BY_META_TAG": "No: 'noindex' meta tag",
           "BLOCKED_BY_HTTP_HEADER": "No: 'noindex' X-Robots-Tag header", "BLOCKED_BY_ROBOTS_TXT": "No: robots.txt"}


def _pretty(value: str) -> str:
    if not value or value.endswith("_UNSPECIFIED"):
        return ""
    return value.replace("_", " ").capitalize()


def _note(r, site) -> str:
    if not _crawled(r):
        return "Found in Search Console only: not linked from the crawled pages"
    return gsc.joined(site)["skipped"].get(r.url, "")


def _api_url(r, site) -> str:
    row = _sa(r, site)
    api = (row or {}).get("api_url") or (row or {}).get("url") or ""
    return api if api and api != getattr(r, "url", "") else ""


COLUMNS: List[Column] = [
    ADDRESS, STATUS_CODE, INDEXABILITY, INDEXABILITY_STATUS,
    Column("clicks", "Clicks", _metric("clicks"), "num"),
    Column("impressions", "Impressions", _metric("impressions"), "num"),
    Column("ctr", "CTR (%)", _metric("ctr", 100.0, 2), "num"),
    Column("position", "Position", _metric("position"), "num"),
    Column("gsc_url", "Search Console URL", _api_url, "url"),
    Column("summary", "Summary", _i(gsc.summary_label)),
    Column("coverage", "Coverage", _i(lambda c: c.get("coverage") or "")),
    Column("last_crawl", "Last Crawl", _i(lambda c: (c.get("last_crawl") or "").replace("T", " ").rstrip("Z"))),
    Column("crawled_as", "Crawled As", _i(lambda c: _pretty(c.get("crawled_as") or ""))),
    Column("crawl_allowed", "Crawl Allowed", _i(lambda c: _YES_NO.get(c.get("robots") or "", ""))),
    Column("page_fetch", "Page Fetch", _i(lambda c: _pretty(c.get("fetch") or ""))),
    Column("indexing_allowed", "Indexing Allowed", _i(lambda c: _YES_NO.get(c.get("indexing") or "", ""))),
    Column("user_canonical", "User-Declared Canonical", _i(lambda c: c.get("user_canonical") or ""), "url"),
    Column("google_canonical", "Google-Selected Canonical", _i(lambda c: c.get("google_canonical") or ""), "url"),
    Column("mobile_usability", "Mobile Usability", _i(lambda c: _pretty((c.get("mobile") or {}).get("verdict") or ""))),
    Column("mobile_issues", "Mobile Usability Issues", _i(lambda c: list((c.get("mobile") or {}).get("issues") or [])),
           "list"),
    Column("rich_results", "Rich Results", _i(lambda c: _pretty((c.get("rich") or {}).get("verdict") or ""))),
    Column("rich_types", "Rich Results Types", _i(lambda c: list((c.get("rich") or {}).get("types") or [])), "list"),
    Column("rich_type_errors", "Rich Results Types Errors",
           _i(lambda c: list((c.get("rich") or {}).get("error_types") or [])), "list"),
    Column("rich_warnings", "Rich Results Warnings", _i(lambda c: (c.get("rich") or {}).get("warnings", "")), "num"),
    Column("inspection_note", "Note", _note),
]


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
def _clicks_above_0(r, site) -> bool:
    row = _sa(r, site)
    return _crawled(r) and bool(row) and (row.get("clicks") or 0) > 0


def _no_data(r, site) -> bool:
    # a cut-off fetch (stopped, Limit Max Results) cannot prove "no data": nothing is flagged, _skip says why
    return _crawled(r) and _sa_complete(site) and _sa(r, site) is None


def _non_indexable_with_data(r, site) -> bool:
    row = _sa(r, site)
    return (_crawled(r) and bool(row) and (row.get("impressions") or 0) > 0
            and not site.indexable.get(r.url, False))


def _orphan(r, site) -> bool:
    return not _crawled(r)


def _not_on_google(r, site) -> bool:
    return gsc.summary_label(_insp(r, site)) == gsc.NOT_ON_GOOGLE


def _indexable_not_indexed(r, site) -> bool:
    return _not_on_google(r, site) and bool(site.indexable.get(r.url, False))


def _has_issues(r, site) -> bool:
    return gsc.summary_label(_insp(r, site)) == gsc.ON_GOOGLE_ISSUES


def _canonical_not_selected(r, site) -> bool:
    c = _insp(r, site) or {}
    user, google = c.get("user_canonical") or "", c.get("google_canonical") or ""
    if not user or not google:
        return False
    from ...integrations.common import normalize
    return normalize(user) != normalize(google)


def _not_mobile_friendly(r, site) -> bool:
    return ((_insp(r, site) or {}).get("mobile") or {}).get("verdict") == "FAIL"


def _rich_invalid(r, site) -> bool:
    rich = (_insp(r, site) or {}).get("rich") or {}
    return rich.get("verdict") == "FAIL" or int(rich.get("errors") or 0) > 0


SA = ("integration:gsc_analytics",)
INSPECT = ("integration:gsc_inspection",)

FILTERS: List[Filter] = [
    Filter("clicks_above_0", "Clicks Above 0", _clicks_above_0, None, None,
           "Pages that got at least one click from Google Search in the date range.", "", requires=SA),
    Filter("no_search_analytics_data", "No Search Analytics Data", _no_data, "Warning", "Low",
           "Indexable pages that got no impressions at all in Google Search during the date range: Google did not "
           "show them for any search (new pages, thin pages, or pages Google has not indexed).",
           "Check the page is indexed (URL Inspection), linked from important pages and in the XML sitemap, and that "
           "it targets something people search for; merge or improve thin pages.",
           only_indexable=True, short_fix="Get these pages indexed and linked, or improve them", requires=SA),
    Filter("non_indexable_with_data", "Non-Indexable with Search Analytics Data", _non_indexable_with_data,
           "Issue", "High",
           "Pages that are not indexable now (noindex, redirect, 4xx/5xx error, canonicalised elsewhere) but still "
           "got impressions in Google Search: traffic that is about to be lost, or a page made non-indexable by "
           "mistake.",
           "If the page should rank, remove the noindex / fix the error / point the canonical at itself; if it was "
           "retired on purpose, 301-redirect it to the closest live page.",
           short_fix="Fix pages with search traffic that are not indexable", requires=SA),
    Filter("orphan_urls", "Orphan URLs", _orphan, "Warning", "Medium",
           "URLs that get impressions in Google Search but that the crawl never found through internal links: "
           "orphan pages, old URLs Google still knows, or pages outside the crawl's settings.",
           "Link to the pages that should rank from relevant pages; redirect or remove the ones that are obsolete. "
           "Turn on 'Crawl New URLs Discovered In Google Search Console' to check their status.",
           short_fix="Link to orphan pages or redirect obsolete ones", requires=SA),
    Filter("url_not_on_google", "URL Is Not on Google", _not_on_google, "Issue", "High",
           "Google's URL Inspection says the URL is not in Google's index, so it cannot appear in search results "
           "(this includes pages that are non-indexable on purpose).",
           "Check the Coverage column for Google's reason; for pages that should rank, fix the cause and request "
           "indexing in Search Console.",
           short_fix="Fix the Coverage reason; request indexing", requires=INSPECT),
    Filter("indexable_not_indexed", "Indexable URL Not Indexed", _indexable_not_indexed, "Issue", "High",
           "Pages the crawl found indexable that Google has not indexed (for example 'Crawled - currently not "
           "indexed' or 'Discovered - currently not indexed').",
           "Improve the page's content and internal links, make sure it is in the XML sitemap, and request indexing "
           "in Search Console.",
           short_fix="Improve and link these pages; request indexing", requires=INSPECT),
    Filter("on_google_has_issues", "URL is on Google But Has Issues", _has_issues, "Warning", "Medium",
           "Indexed pages for which Google reports problems with enhancements such as rich results, so they may "
           "not show as well as they could.",
           "Open the Rich Results columns (or the inspection link in Search Console) and fix the reported errors.",
           short_fix="Fix the rich result problems Google reports", requires=INSPECT),
    Filter("canonical_not_selected", "User-Declared Canonical Not Selected", _canonical_not_selected, "Warning",
           "Medium",
           "Google chose a different canonical URL than the one the page declares, so it may index and rank "
           "another URL instead.",
           "Make the duplicates point to one URL consistently (canonical, internal links, sitemap, redirects) or make "
           "the page's content clearly different.",
           short_fix="Make canonical signals consistent", requires=INSPECT),
    Filter("not_mobile_friendly", "Page Is Not Mobile Friendly", _not_mobile_friendly, "Warning", "Medium",
           "Google's URL Inspection reports mobile usability problems for the page.",
           "Fix the issues in the Mobile Usability Issues column (viewport, text size, tap targets, content width).",
           short_fix="Fix the mobile usability problems Google reports", requires=INSPECT),
    Filter("rich_result_invalid", "Rich Result Invalid", _rich_invalid, "Issue", "Medium",
           "Google found structured data errors that stop the page from getting a rich result.",
           "Fix the errors listed in the Rich Results Types Errors column and validate with Google's Rich Results "
           "Test.",
           short_fix="Fix the structured data errors Google reports", requires=INSPECT),
]


def _skip(site, filter_id: str) -> str:
    if filter_id == "not_mobile_friendly":
        insp = gsc.joined(site)["inspection"]
        if insp and not any((c or {}).get("mobile") for c in insp.values()):
            return ("Google retired mobile usability reports in December 2023, so URL Inspection gave no "
                    "mobile-friendly verdict. The Mobile tab checks this instead.")
    if filter_id == "no_search_analytics_data" and _sa_fetched(site) and not _sa_complete(site):
        return ("Not judged: the Search Analytics fetch was cut off (stopped or hit 'Limit Max Results'), so a page "
                "without data might just be beyond the limit.")
    return ""


register(Tab(id="search_console", name="Search Console", order=222, columns=COLUMNS, filters=FILTERS, rows=_rows,
             requires=("integration:gsc",), skip_reason=_skip,
             description="Google Search Console clicks, impressions and index status (optional connection)."))
