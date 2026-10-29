"""
Pagination tab (Screaming Frog "Pagination"): rel="next" / rel="prev" annotations of every internal
HTML page that loaded (2xx), from <link> elements and from the Link response header.

Google no longer uses rel="next"/"prev" as an indexing signal, but the links are still a useful map
of paginated series, and the checks below catch series that crawlers cannot follow.

The checks that look at the other end of a link (Non-200, Sequence Error, Pagination Loop) only
judge URLs the crawl requested; a pagination URL that was never fetched is not guessed at.
"""
import dataclasses
from typing import Any, List

from ..registry import Column, Filter, Tab, register, seo
from .canonicals import (CANONICAL_1, HTTP_CANONICAL, HTTP_REL_NEXT_1, HTTP_REL_PREV_1, META_ROBOTS_1,
                         REL_NEXT_1, REL_PREV_1, X_ROBOTS_TAG_1, find_result, is_linked, norm, same_url,
                         target_non_200)
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, is_external, memo
from .page_titles import content_rows

MAX_SERIES = 1000          # stop walking a rel="next" chain after this many pages


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------
def _unique(urls: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for u in urls:
        k = norm(u)
        if k and k not in seen:
            seen.add(k)
            out.append(u)
    return out


def next_urls(r) -> List[str]:
    """Every rel="next" URL of the page (HTML link elements, then the HTTP header), deduplicated."""
    p = seo(r)
    html = p.get("rel_next_all") or ([p["rel_next"]] if p.get("rel_next") else [])
    return _unique(list(html) + ([p["http_rel_next"]] if p.get("http_rel_next") else []))


def prev_urls(r) -> List[str]:
    p = seo(r)
    html = p.get("rel_prev_all") or ([p["rel_prev"]] if p.get("rel_prev") else [])
    return _unique(list(html) + ([p["http_rel_prev"]] if p.get("http_rel_prev") else []))


def pagination_urls(r) -> List[str]:
    return _unique(next_urls(r) + prev_urls(r))


def has_pagination(r) -> bool:
    return bool(next_urls(r) or prev_urls(r))


def _page_of(site, url: str):
    """Crawled internal page for a pagination URL, None when it was not crawled."""
    t = find_result(site, url)
    return None if t is None or is_external(t) else t


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
def _not_in_anchor(r, site) -> bool:
    """A pagination URL that the page itself does not also link to with an <a href>."""
    urls = pagination_urls(r)
    if not urls:
        return False   # most pages: no rel=next/prev, so no need to normalize every link
    links = {norm(l.get("url") or "") for l in seo(r).get("links") or () if l.get("url")}
    return any(norm(u) not in links for u in urls)


def _non_200(r, site) -> bool:
    return any(target_non_200(site, u) for u in pagination_urls(r))


def _unlinked(r, site) -> bool:
    return any(not is_linked(site, u) for u in pagination_urls(r))


def _multiple(r, site) -> bool:
    return len(next_urls(r)) > 1 or len(prev_urls(r)) > 1


def _walk_has_loop(site, start, step) -> bool:
    """Follow one direction (next or prev) from `start`; True if the chain comes back to a page it
    has already visited."""
    seen = {norm(start.url)}
    cur = start
    for _ in range(MAX_SERIES):
        urls = step(cur)
        if not urls:
            return False
        nxt = norm(urls[0])
        if nxt in seen:
            return True
        seen.add(nxt)
        cur = _page_of(site, urls[0])
        if cur is None:
            return False
    return False


def _loops(site) -> set:
    out = set()
    for r in content_rows(site):
        if has_pagination(r) and (_walk_has_loop(site, r, next_urls) or _walk_has_loop(site, r, prev_urls)):
            out.add(r.url)
    return out


def _loop(r, site) -> bool:
    return r.url in memo(site, "pagination_loops", lambda: _loops(site))


def _mirror_target(site, u: str):
    """The crawled page a rel=next / prev URL points to, when it answered 200 with HTML (a target that does not
    answer 200 is reported once, under Non-200 Pagination URLs, not again as a sequence error)."""
    t = _page_of(site, u)
    if t is None or not seo(t).get("is_html") or target_non_200(site, u):
        return None
    return t


def _sequence_error(r, site) -> bool:
    """rel="next" and rel="prev" do not mirror each other: the next page does not point back with
    rel="prev", or the previous page does not point forward with rel="next"."""
    for u in next_urls(r):
        t = _mirror_target(site, u)
        if t is not None and not any(same_url(p, r.url) for p in prev_urls(t)):
            return True
    for u in prev_urls(r):
        t = _mirror_target(site, u)
        if t is not None and not any(same_url(n, r.url) for n in next_urls(t)):
            return True
    return False


FILTERS = [
    Filter("contains_pagination", "Contains Pagination", lambda r, site: has_pagination(r),
           description="The page declares a rel=\"next\" and/or rel=\"prev\" URL, so it is part of a "
                       "paginated series (like page 1, 2, 3 of a blog listing)."),
    Filter("first_page", "First Page", lambda r, site: bool(next_urls(r)) and not prev_urls(r),
           description="The page has a rel=\"next\" but no rel=\"prev\": it is the first page of a series."),
    Filter("paginated_2_plus", "Paginated 2+ Pages", lambda r, site: bool(prev_urls(r)),
           description="The page has a rel=\"prev\": it is page 2 or later in a series."),
    Filter("not_in_anchor", "Pagination URL Not in Anchor Tag", _not_in_anchor,
           issue_type="Issue", priority="High",
           description="A rel=\"next\"/\"prev\" URL is declared in the <head> but the page does not link to "
                       "it with a normal <a href> link. Google only follows real links, so the later pages "
                       "of the series may never be found.",
           fix="Add plain <a href> links to the next and previous pages (numbered page links or "
               "Next/Previous buttons that are real links, not script-only buttons)."),
    Filter("non_200", "Non-200 Pagination URLs", _non_200,
           issue_type="Issue", priority="High",
           description="A rel=\"next\"/\"prev\" URL does not answer 200: it is blocked, gives no response, "
                       "redirects or returns an error.",
           fix="Point the pagination links at the working, final URLs of the series, or fix the pages "
               "that fail."),
    Filter("unlinked", "Unlinked Pagination URLs", _unlinked,
           issue_type="Issue", priority="Medium",
           description="A rel=\"next\"/\"prev\" URL is not linked from any crawled page, so search engines "
                       "can only reach it through the pagination annotation.",
           fix="Link to every page of the series with ordinary <a href> links."),
    Filter("non_indexable", "Non-Indexable", lambda r, site: has_pagination(r) and not site.indexable.get(r.url),
           issue_type="Warning", priority="High",
           description="A page in a paginated series cannot be indexed (noindex, canonicalised to another "
                       "page, and so on). Pages 2+ usually list different items, so hiding them can stop "
                       "those items from being found.",
           fix="Let each page of the series be indexable with a self-referencing canonical, unless you "
               "really want it out of search results."),
    Filter("multiple", "Multiple Pagination URLs", _multiple,
           issue_type="Issue", priority="Low",
           description="The page declares more than one rel=\"next\" or more than one rel=\"prev\" URL, "
                       "which makes the order of the series unclear.",
           fix="Keep one rel=\"next\" and one rel=\"prev\" per page (check for a theme and a plugin both "
               "adding them)."),
    Filter("loop", "Pagination Loop", _loop,
           issue_type="Issue", priority="Low",
           description="Following rel=\"next\" (or rel=\"prev\") from this page leads back to a page already "
                       "seen in the series, so the series never ends.",
           fix="Make the last page have no rel=\"next\" and the first page have no rel=\"prev\", and check "
               "that each page points to the right neighbour."),
    Filter("sequence_error", "Sequence Error", _sequence_error,
           issue_type="Issue", priority="Low",
           description="The links do not mirror each other: the next page does not point back to this page "
                       "with rel=\"prev\", or the previous page does not point forward to it with rel=\"next\".",
           fix="Make every page's rel=\"next\" target point back with rel=\"prev\" (and the other way "
               "around), using exactly the same URLs."),
]

COLUMNS = [
    ADDRESS, INDEXABILITY, INDEXABILITY_STATUS,
    REL_NEXT_1, HTTP_REL_NEXT_1, REL_PREV_1, HTTP_REL_PREV_1,
    CANONICAL_1, HTTP_CANONICAL, META_ROBOTS_1, X_ROBOTS_TAG_1,
]

# Checks that compare pages with each other run in Crawl Analysis (step "Pagination")
ANALYSIS_FILTERS = {"unlinked", "loop", "sequence_error"}
FILTERS = [dataclasses.replace(f, requires=("analysis:pagination",)) if f.id in ANALYSIS_FILTERS else f
           for f in FILTERS]

register(Tab(id="pagination", name="Pagination", order=140, columns=COLUMNS, filters=FILTERS,
             rows=content_rows))
