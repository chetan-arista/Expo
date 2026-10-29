"""
Analytics tab (Screaming Frog "Analytics"): Google Analytics 4 page metrics joined to the crawl. Optional: it
needs the Google connection with a GA4 property and data stream (Configuration > API Access) and a fetch for
this crawl; until then every filter is "Not connected" / "not fetched yet", never 0.

Rows ("All"): every internal URL the crawl requested except page resources, then the URLs GA4 reports that the
crawl never found (ApiOnlyURL, "Not crawled"). Columns: one per fetched metric (default Sessions, Views,
Engaged Sessions, Engagement Rate, Key Events, Event Count, Total Revenue) plus Bounce Rate. GA4 paths are joined
onto the data stream's address and matched like Screaming Frog (trailing slash, http/https, optional case / www);
of several GA4 rows matching one crawled URL the one with the most sessions is kept (never summed).
Bounce Rate Above 70%: GA4's bounce rate = share of sessions that were not engaged (1 - engagement rate).
"""
from typing import Any, List, Optional, Tuple

from ...integrations import ga4
from ..registry import Column, Filter, Tab, pref_number, register, threshold_filter
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, STATUS_CODE, memo
from .search_console import ApiOnlyURL, _domain, page_rows

BOUNCE_LIMIT = 0.70


def _rows(site) -> List[Any]:
    def build():
        rows = list(page_rows(site))
        for url in ga4.joined(site)["orphans"]:
            rows.append(ApiOnlyURL(url=url, domain=_domain(url), source="Google Analytics 4"))
        return rows
    return memo(site, "ga4_rows", build)


def _crawled(r) -> bool:
    return not isinstance(r, ApiOnlyURL)


def _ga(r, site) -> Optional[dict]:
    j = ga4.joined(site)
    return j["orphans"].get(r.url) if isinstance(r, ApiOnlyURL) else j["rows"].get(r.url)


def _complete(site) -> bool:
    d = ga4.data_of(site)
    return d.get("status") == "done" and not d.get("truncated")


def _metric_col(metric: str) -> Column:
    rate = metric in ga4.RATE_METRICS

    def get(r, site):
        row = _ga(r, site)
        if row is None:
            return 0 if _crawled(r) and _complete(site) and not rate else ""
        v = row.get(metric)
        if v is None:
            return ""
        return round(float(v) * 100, 2) if rate else v
    label = ga4.label(metric) + (" (%)" if rate else "")
    return Column(f"ga_{metric}", label, get, "num")


def _ga_url(r, site) -> str:
    row = _ga(r, site)
    api = (row or {}).get("api_url") or ""
    return api if api and api != getattr(r, "url", "") else ""


BASE = [ADDRESS, STATUS_CODE, INDEXABILITY, INDEXABILITY_STATUS]
TAIL = [Column("ga_url", "GA4 URL", _ga_url, "url"),
        Column("ga_note", "Note", lambda r, site: "" if _crawled(r) else
               "Found in Google Analytics only: not linked from the crawled pages")]


def _metrics_for(site) -> List[str]:
    ms = list(ga4.data_of(site).get("metrics") or ga4.DEFAULT_METRICS)
    if "bounceRate" not in ms and ("engagementRate" in ms or not ga4.data_of(site)):
        ms.append("bounceRate")
    return ms


def _sessions(r, site) -> int:
    return int((_ga(r, site) or {}).get("sessions") or 0)


def _sessions_above_0(r, site) -> bool:
    return _crawled(r) and _sessions(r, site) > 0


def bounce_limit(site) -> float:
    """The bounce rate this crawl flags above, as a fraction (Preferences bounce_rate_pct, default 70%)."""
    try:
        return float(pref_number(site, "bounce_rate_pct")) / 100.0
    except (TypeError, ValueError):
        return BOUNCE_LIMIT


def _bounce_high(r, site) -> bool:
    row = _ga(r, site) or {}
    br = row.get("bounceRate")
    return _crawled(r) and _sessions(r, site) > 0 and br is not None and float(br) > bounce_limit(site)


def _no_data(r, site) -> bool:
    return _crawled(r) and _complete(site) and _ga(r, site) is None


def _non_indexable_with_data(r, site) -> bool:
    return _crawled(r) and _sessions(r, site) > 0 and not site.indexable.get(r.url, False)


def _orphan(r, site) -> bool:
    return not _crawled(r)


REQ = ("integration:ga4",)
FILTERS: List[Filter] = [
    Filter("sessions_above_0", "Sessions Above 0", _sessions_above_0, None, None,
           "Pages with at least one GA4 session in the date range.", ""),
    threshold_filter("bounce_rate_above_70", "bounce_rate_pct", "Bounce Rate Above {n}%", _bounce_high,
                     issue_type="Warning", priority="Low",
                     description="Pages where more than {n}% of GA4 sessions were not engaged (left within 10 "
                                 "seconds, with one page view and no key event). The page may not match what "
                                 "visitors expected.",
                     fix="Check the page answers the search or ad that brings visitors: clear heading, fast first "
                         "view, a visible next step; compare with the page's traffic source before changing it.",
                     short_fix="Make the page match the visit intent; add a clear next step"),
    Filter("no_ga_data", "No GA Data", _no_data, "Warning", "Low",
           "Indexable pages with no GA4 data in the date range: nobody visited them (with the current filter), or "
           "the GA4 tag is missing on them.",
           "Check the GA4 tag is on these pages; if it is, decide whether they need links, promotion or merging.",
           only_indexable=True, short_fix="Check the GA4 tag; promote or merge unvisited pages"),
    Filter("non_indexable_with_ga_data", "Non-Indexable with GA Data", _non_indexable_with_data, "Issue", "Medium",
           "Pages that get visitors but are not indexable (noindex, redirect, error, canonicalised elsewhere): "
           "possibly valuable pages hidden from search, or visitors landing on errors.",
           "If the page should be found in search, make it indexable; if it is retired, redirect it to the best "
           "live page.",
           short_fix="Fix or redirect non-indexable pages that get visits"),
    Filter("orphan_urls", "Orphan URLs", _orphan, "Warning", "Medium",
           "URLs with GA4 sessions that the crawl never found through internal links: orphan pages, old URLs, or "
           "pages outside the crawl's settings.",
           "Link to the pages that matter; redirect obsolete URLs. Turn on 'Crawl New URLs Discovered In Google "
           "Analytics 4' to check their status.",
           short_fix="Link to orphan pages or redirect obsolete ones"),
]


def _dynamic(site) -> Tuple[List[Column], List[Filter]]:
    return BASE + [_metric_col(m) for m in _metrics_for(site)] + TAIL, FILTERS


def _skip(site, filter_id: str) -> str:
    d = ga4.data_of(site)
    if not d:
        return ""
    if filter_id == "bounce_rate_above_70":
        rows = ga4.joined(site)["rows"]
        if rows and not any(v.get("bounceRate") is not None for v in rows.values()):
            return "Bounce rate needs the Engagement Rate or Bounce Rate metric (GA4 > Metrics)."
    if filter_id == "no_ga_data" and not _complete(site):
        return ("Not judged: the GA4 fetch was cut off (stopped or hit 'Limit Max Results'), so a page without data "
                "might just be beyond the limit.")
    return ""


register(Tab(id="analytics", name="Analytics", order=220,
             columns=BASE + [_metric_col(m) for m in ga4.DEFAULT_METRICS + ["bounceRate"]] + TAIL,
             filters=FILTERS, rows=_rows, requires=REQ, skip_reason=_skip, dynamic=_dynamic, static_filters=True,
             description="Google Analytics 4 sessions and engagement per page (optional connection)."))
