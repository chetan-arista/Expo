"""
Parameters tab: every crawled internal URL with a query string, with what its parameters do (tracking,
sort / view, filter, pagination, session, search, action, calendar, language / currency). Each class is a
filter, so the class counts show where crawl time goes; the issues are the URLs search engines should
not spend time on: session IDs, cart / wishlist actions, indexable sort / filter / search variants and
page-builder pagination traps.

The site-level summary (wasted URL share, the suggested robots.txt Disallow block, canonical advice,
redirect kinds) is crawl_budget.get(site), the same data this tab reads.
"""
from functools import lru_cache
from typing import Any, List, Tuple
from urllib.parse import parse_qsl, urlparse

from ..crawl_budget import ALWAYS_TRAP, CLASSES, WASTE_CLASSES, get, is_asset, norm_key, robots_rules, url_classes
from ..registry import Column, Filter, Tab, first, register, seo
from .internal import (ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, STATUS_CODE, internal_rows, is_external, memo)


def parameter_rows(site) -> List[Any]:
    """Internal URLs with a query string, in crawl order."""
    return memo(site, "parameter_rows",
                lambda: [r for r in internal_rows(site) if not is_external(r) and "?" in (r.url or "")])


def _classes(r) -> Tuple[str, ...]:
    """What the URL's parameters do; none for a file pages need to render (its ?ver= / ?w= / ?q=75 are file
    versions or sizes, not page variants, and must stay crawlable)."""
    return () if is_asset(r) else url_classes(r.url)


def _has_class(cls: str):
    return lambda r, site: cls in _classes(r)


def _names(r) -> Tuple[str, ...]:
    return _param_names(r.url)


@lru_cache(maxsize=50000)
def _param_names(url: str) -> Tuple[str, ...]:
    """Parameter names of a URL as written, first occurrence order."""
    try:
        return tuple(dict.fromkeys(k for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)))
    except Exception:
        return ()


def _indexable_variant(r, site) -> bool:
    return bool(site.indexable.get(r.url)) and any(c in ("sort", "filter", "search", "tracking", "calendar")
                                                   for c in _classes(r))


def _builder_trap(r, site) -> bool:
    return any(norm_key(k) in ALWAYS_TRAP for k in _names(r))


def _without_query(r, site) -> str:
    return r.url.split("?", 1)[0]


def _base_crawled(r, site) -> str:
    base = r.url.split("?", 1)[0]
    if base in site.by_url:
        return "Yes"
    return "Yes" if (base.rstrip("/") in site.by_url or base + "/" in site.by_url) else "No"


def _suggested_rules(r, site) -> str:
    """The Disallow lines (from the crawl-budget suggestion) that would cover this URL."""
    if is_asset(r):
        return ""
    report = get(site)
    keyed = {p["key"]: p for p in report.get("params") or []}
    out: List[str] = []
    for k in _names(r):
        p = keyed.get(norm_key(k))
        if p and p.get("trap") and not p.get("review"):
            for rule in p.get("rules") or robots_rules(p["key"]):
                if rule not in out:
                    out.append(rule)
    return "; ".join(out[:2])


def _already_blocked(r, site) -> str:
    report = get(site)
    blocked = set(report.get("already_blocked") or [])
    return "Yes" if any(norm_key(k) in blocked for k in _names(r)) else ""


FILTERS = [
    Filter("session", "Session IDs In URLs", _has_class("session"),
           issue_type="Issue", priority="High",
           description="The URL carries a session ID. Every visit makes new addresses for the same page, so search "
                       "engines crawl and index endless duplicates.",
           fix=CLASSES["session"][1]),
    Filter("action", "Cart And Action URLs", _has_class("action"),
           issue_type="Issue", priority="Medium",
           description="Add-to-cart, wishlist, compare or comment-reply links were crawled. They are actions, not "
                       "pages: each one costs crawl time and some change the cart when a crawler follows them.",
           fix=CLASSES["action"][1]),
    Filter("indexable_variants", "Indexable Sort, Filter Or Search Variants", _indexable_variant,
           issue_type="Warning", priority="Medium",
           description="A sorted, filtered, search or tracking version of a page is indexable (no canonical to the "
                       "plain page, no noindex), so search engines see many near-copies of the same listing.",
           fix="Canonicalise these URLs to the page without the parameter (or noindex search results); block the "
               "parameter in robots.txt once they are out of the index."),
    Filter("builder_pagination", "Page-Builder Pagination Traps", _builder_trap,
           issue_type="Warning", priority="Medium",
           description="Page-builder AJAX pagination (Elementor ?e-page-...) or infinite-scroll URLs: every page "
                       "number of every widget makes another canonicalised copy, and crawlers can follow them for ever.",
           fix="Block the parameter in robots.txt (Disallow: /*?e-page-) or switch the widget to normal "
               "pagination links."),
] + [
    Filter(f"class_{c}", label, _has_class(c), description=f"{label}: {advice}")
    for c, (label, advice) in CLASSES.items()
]

COLUMNS = [
    ADDRESS,
    STATUS_CODE,
    Column("parameters", "Parameters", lambda r, site: ", ".join(_names(r))),
    Column("param_type", "Type", lambda r, site: "File the page needs (not judged)" if is_asset(r)
           else ", ".join(CLASSES[c][0] for c in _classes(r))),
    Column("wasted", "Crawl Waste",
           lambda r, site: "Yes" if any(c in WASTE_CLASSES for c in _classes(r))
           or (site.indexability_status.get(r.url) == "Canonicalised" and not is_asset(r)) else ""),
    Column("without_parameters", "Without Parameters", _without_query, "url"),
    Column("base_crawled", "Plain URL Crawled", _base_crawled),
    Column("canonical", "Canonical", lambda r, site: first(seo(r).get("canonicals") or []) or seo(r).get("http_canonical") or "", "url"),
    Column("suggested_rule", "Suggested robots.txt Rule", _suggested_rules),
    Column("already_blocked", "Already Blocked", _already_blocked),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="parameters", name="Parameters", order=260, columns=COLUMNS, filters=FILTERS, rows=parameter_rows,
             total_label="All Parameter URLs"))
