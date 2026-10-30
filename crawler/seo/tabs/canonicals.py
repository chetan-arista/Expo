"""
Canonicals tab (Screaming Frog "Canonicals"): the canonical URL each internal document declares,
through <link rel="canonical"> in the HTML or a Link: <...>; rel="canonical" response header.

How the counts match Screaming Frog (checked against a Screaming Frog 24.3 crawl in research/):
  - "All" lists internal documents that answered 2xx and can carry a canonical: HTML pages and
    PDFs (or any other file that sent a canonical header). Images, scripts and stylesheets are left
    out, as are redirects and error pages.
  - Contains Canonical = Self Referencing + Canonicalised; the canonical that counts is the first
    HTML one, else the HTTP header one (the same rule analysis.indexability_of uses).
  - Missing runs on indexable URLs only: a noindex page without a canonical is not reported.
  - Non-Indexable Canonical / Unlinked can only judge canonical targets the crawl requested (or
    the external links it checked); a target that was never fetched is not guessed at.

This module also holds small URL-lookup helpers that the Pagination, Hreflang and AMP tabs share.
"""
from typing import Any, List, Optional

from ...url_utils import normalize_url
from ..registry import Column, Filter, Tab, first, register, seo
from .internal import (ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, category_of, content_kind,
                       first_status_code, internal_rows, is_external, memo)

# Indexability statuses that make a canonical / pagination / hreflang target a bad one
NON_INDEXABLE_TARGET = ("Blocked by robots.txt", "No Response", "Redirected", "Client Error",
                        "Server Error", "Non-200", "noindex")


# ---------------------------------------------------------------------------------------------
# Shared helpers (Canonicals, Pagination, Hreflang, AMP)
# ---------------------------------------------------------------------------------------------
def norm(url: Any) -> str:
    """Normalized URL for comparisons ('' when empty or unparsable)."""
    if not url or not isinstance(url, str):
        return ""
    try:
        return normalize_url(url.strip()) or url.strip()
    except Exception:
        return url.strip()


def same_url(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a == b or norm(a) == norm(b))


def _externals_by_url(site) -> dict:
    out = {}
    for e in site.externals or ():
        u = getattr(e, "url", "")
        if u:
            out.setdefault(u, e)
            out.setdefault(norm(u), e)
    return out


def find_result(site, url: str) -> Optional[Any]:
    """The crawled internal result (or checked external link) for a URL, None if never requested."""
    if not url:
        return None
    r = site.by_url.get(url)
    if r is None:
        r = site.by_url.get(norm(url))
    if r is None:
        ext = memo(site, "externals_by_url", lambda: _externals_by_url(site))
        r = ext.get(url) or ext.get(norm(url))
    return r


def is_linked(site, url: str) -> bool:
    """Some crawled page links to the URL with an ordinary hyperlink (<a>/<area>)."""
    if not url:
        return False
    if url in site.inlinks:
        return True
    keys = memo(site, "inlink_norm_keys", lambda: {norm(u) for u in site.inlinks})
    return norm(url) in keys


def is_start_url(site, url: str) -> bool:
    return same_url(url, site.start_url)


def target_non_200(site, url: str) -> bool:
    """The URL was requested and did not answer 200 (blocked, no response, 3xx, 4xx, 5xx)."""
    t = find_result(site, url)
    if t is None:
        return False
    code = first_status_code(t)
    return code is not None and code != 200


def target_non_indexable(site, url: str) -> bool:
    """The URL was requested and cannot be indexed (blocked, no response, redirect, error, noindex)."""
    t = find_result(site, url)
    if t is None:
        return False
    if is_external(t):
        cat = category_of(t)
        return cat != "" and cat != "2xx"
    return site.indexability_status.get(t.url, "") in NON_INDEXABLE_TARGET


EXTERNAL_NOT_CHECKED = "on another host, and external links were not checked"


def _site_hosts(site) -> set:
    return memo(site, "canonical_site_hosts",
                lambda: {u.split("://", 1)[-1].split("/", 1)[0].lower() for u in site.by_url})


def target_unchecked(site, url: str) -> str:
    """Why a canonical target could not be judged ("" when it could): it is on another host and external links
    were not checked, it is on another host no page links to (the crawl checks other sites only through the
    links pages make to them), or the crawl did not request it."""
    t = find_result(site, url)
    if t is None:
        host = (url or "").split("://", 1)[-1].split("/", 1)[0].lower()
        if host and host not in _site_hosts(site):
            return "on another host that no crawled page links to, so it was never requested"
        return "not requested by the crawl (outside its scope or limits)"
    if is_external(t) and category_of(t) == "":
        return EXTERNAL_NOT_CHECKED
    return ""


def has_noindex(site, url: str) -> bool:
    t = find_result(site, url)
    if t is None or is_external(t):
        return False
    d = seo(t).get("directives") or []
    return "noindex" in d or "none" in d


# ---------------------------------------------------------------------------------------------
# Canonical values
# ---------------------------------------------------------------------------------------------
def html_canonicals(r) -> List[str]:
    return [c for c in (seo(r).get("canonicals") or []) if c]


def http_canonical(r) -> str:
    return seo(r).get("http_canonical") or ""


def all_canonicals(r) -> List[str]:
    """Every canonical the URL declares: HTML link elements first, then the HTTP header."""
    out = html_canonicals(r)
    h = http_canonical(r)
    return out + [h] if h else out


def canonical_target(r) -> str:
    """The canonical that counts: first HTML one, else the HTTP header one."""
    return first(html_canonicals(r)) or http_canonical(r)


def is_self_referencing(r) -> bool:
    t = canonical_target(r)
    return bool(t) and (same_url(t, r.url) or same_url(t, seo(r).get("final_url") or ""))


def is_canonicalised(r) -> bool:
    return bool(canonical_target(r)) and not is_self_referencing(r)


def canonical_rows(site) -> List[Any]:
    """Internal documents that answered 2xx and can declare a canonical (HTML, PDF, or any file
    that sent a canonical header)."""
    return [r for r in internal_rows(site)
            if category_of(r) == "2xx" and (content_kind(r) in ("html", "pdf") or http_canonical(r))]


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
def _multiple_conflicting(r, site) -> bool:
    return len({norm(c) for c in all_canonicals(r)}) > 1


def _non_indexable_canonical(r, site) -> bool:
    t = canonical_target(r)
    return bool(t) and not is_self_referencing(r) and target_non_indexable(site, t)


def _is_relative(raw: str) -> bool:
    return not raw.strip().lower().startswith(("http://", "https://"))


def _relative(r, site) -> bool:
    return any(_is_relative(v) for v in (seo(r).get("canonicals_raw") or []) if v and v.strip())


def _unlinked(r, site) -> bool:
    """A canonical points at a URL no crawled page links to (the canonical is its only way in)."""
    for c in {norm(c) for c in all_canonicals(r)}:
        if c and not is_start_url(site, c) and not is_linked(site, c):
            return True
    return False


def _fragment(r, site) -> bool:
    raws = [v for v in (seo(r).get("canonicals_raw") or []) if v]
    return any("#" in v for v in raws + [http_canonical(r)])


def unchecked_canonicals(site) -> List[Any]:
    """Canonicalised URLs whose canonical target could not be judged (not requested / external not checked)."""
    return memo(site, "canonical_unchecked", lambda: [
        r for r in canonical_rows(site) if is_canonicalised(r) and target_unchecked(site, canonical_target(r))])


def skip_reason(site, filter_id: str) -> str:
    """Non-Indexable Canonical is "Not checked" (not passed) when it found nothing while some canonical targets
    could not be judged: on another host with the external link check off, or never requested."""
    if filter_id != "non_indexable_canonical":
        return ""
    unchecked = unchecked_canonicals(site)
    if not unchecked:
        return ""
    if any(_non_indexable_canonical(r, site) for r in canonical_rows(site)):
        return ""          # it found some: the check ran (the unjudged ones are a caveat, see intel/checks.py)
    n = len(unchecked)
    whys = [target_unchecked(site, canonical_target(r)) for r in unchecked]
    why = max(sorted(set(whys)), key=whys.count)
    return (f"{n:,} page{'s' if n != 1 else ''} point{'s' if n == 1 else ''} the canonical at a URL that was not "
            f"checked ({why}), so whether that target can be indexed is unknown."
            + (" Turn on 'Check external links' to check them." if EXTERNAL_NOT_CHECKED in whys else ""))


FILTERS = [
    Filter("contains_canonical", "Contains Canonical", lambda r, site: bool(all_canonicals(r)),
           description="The URL declares a canonical, either pointing to itself or to another URL."),
    Filter("self_referencing", "Self Referencing", lambda r, site: is_self_referencing(r),
           description="The canonical points to the URL itself. This is the recommended setup for pages "
                       "you want indexed."),
    Filter("canonicalised", "Canonicalised", lambda r, site: is_canonicalised(r),
           issue_type="Warning", priority="High",
           description="The canonical points to a different URL, so this URL asks search engines to index "
                       "that other URL instead. It is fine for true duplicates (sorting, tracking "
                       "parameters), but a mistake on a page you want to rank.",
           fix="Check each one: if the page should rank on its own, make its canonical point to itself. "
               "If it is a duplicate, keep the canonical and link to the canonical URL instead."),
    Filter("missing", "Missing", lambda r, site: not all_canonicals(r),
           issue_type="Warning", priority="Medium", only_indexable=True,
           description="The URL declares no canonical, neither in the HTML nor in a response header, so "
                       "search engines pick the main version themselves. Checked on indexable URLs only.",
           fix="Add <link rel=\"canonical\" href=\"...\"> with the full URL in the <head> of every "
               "indexable page (for PDFs and other files, send a Link: <...>; rel=\"canonical\" header)."),
    Filter("multiple", "Multiple", lambda r, site: len(all_canonicals(r)) > 1,
           issue_type="Warning", priority="Low",
           description="More than one canonical is declared (several link elements, or the HTML and the "
                       "response header both set one). Even when they agree, it is easy for them to drift "
                       "apart later.",
           fix="Keep a single canonical per URL. Look for a theme and an SEO plugin both adding one."),
    Filter("multiple_conflicting", "Multiple Conflicting", _multiple_conflicting,
           issue_type="Issue", priority="High",
           description="The URL declares several canonicals that point to different URLs. Search engines "
                       "are likely to ignore all of them.",
           fix="Remove the extra canonicals so one remains, pointing to the URL you want indexed."),
    Filter("non_indexable_canonical", "Non-Indexable Canonical", _non_indexable_canonical,
           issue_type="Issue", priority="High",
           description="The canonical points to a URL that cannot be indexed: it is blocked by robots.txt, "
                       "gives no response, redirects, returns an error, or is noindex. Search engines will "
                       "usually ignore such a canonical. Only targets the crawl requested are checked.",
           fix="Point the canonical at the final, working, indexable URL (follow the redirect to its end, "
               "or fix the target page)."),
    Filter("canonical_is_relative", "Canonical Is Relative", _relative,
           issue_type="Warning", priority="Low",
           description="The canonical href is a relative path (like /page/) instead of a full URL. It works, "
                       "but is easy to get wrong, for example on staging copies or other protocols.",
           fix="Write the canonical as an absolute URL, including https:// and the host name."),
    Filter("unlinked", "Unlinked", _unlinked,
           issue_type="Warning", priority="Medium",
           description="The canonical points to a URL that no crawled page links to, so the canonical is "
                       "the only way to find it. That usually means weak internal linking or a wrong "
                       "canonical URL.",
           fix="Link to the canonical URL from your pages (menus, listings, content), or correct the "
               "canonical if it points to the wrong address."),
    Filter("invalid_attribute", "Invalid Attribute In Annotation",
           lambda r, site: bool(seo(r).get("canonical_invalid_attrs")),
           issue_type="Issue", priority="High",
           description="A rel=\"canonical\" link also carries hreflang, lang, media or type. Google ignores "
                       "canonicals with these attributes, so the canonical may have no effect.",
           fix="Remove the extra attribute from the canonical link. Use separate rel=\"alternate\" links "
               "for language or mobile versions."),
    Filter("contains_fragment", "Contains Fragment URL", _fragment,
           issue_type="Issue", priority="High",
           description="The canonical URL contains a # fragment. Search engines drop the fragment, so the "
                       "canonical may not point where you meant.",
           fix="Remove the #... part from the canonical URL."),
    Filter("outside_head", "Outside <head>", lambda r, site: bool(seo(r).get("canonical_outside_head")),
           issue_type="Issue", priority="High",
           description="A canonical link element sits in the <body>. Search engines only honour canonicals "
                       "in the <head>; this often happens when a script or invalid tag closes the head early.",
           fix="Move the canonical into the <head>, and check that nothing before it (an <img>, <div> or "
               "script output) ends the head early."),
]


def _meta_robots_1(r, site) -> str:
    return first(seo(r).get("meta_robots") or [])


def _x_robots_1(r, site) -> str:
    return first(seo(r).get("x_robots_tag") or [])


META_ROBOTS_1 = Column("meta_robots_1", "Meta Robots 1", _meta_robots_1)
X_ROBOTS_TAG_1 = Column("x_robots_tag_1", "X-Robots-Tag 1", _x_robots_1)
CANONICAL_1 = Column("canonical_1", "Canonical Link Element 1", lambda r, site: first(html_canonicals(r)), "url")
HTTP_CANONICAL = Column("http_canonical", "HTTP Canonical", lambda r, site: http_canonical(r), "url")
REL_NEXT_1 = Column("rel_next_1", 'rel="next" 1', lambda r, site: seo(r).get("rel_next") or "", "url")
REL_PREV_1 = Column("rel_prev_1", 'rel="prev" 1', lambda r, site: seo(r).get("rel_prev") or "", "url")
HTTP_REL_NEXT_1 = Column("http_rel_next_1", 'HTTP rel="next" 1', lambda r, site: seo(r).get("http_rel_next") or "", "url")
HTTP_REL_PREV_1 = Column("http_rel_prev_1", 'HTTP rel="prev" 1', lambda r, site: seo(r).get("http_rel_prev") or "", "url")

COLUMNS = [
    ADDRESS,
    Column("occurrences", "Occurrences", lambda r, site: len(all_canonicals(r)), "num"),
    INDEXABILITY, INDEXABILITY_STATUS,
    CANONICAL_1,
    Column("canonical_2", "Canonical Link Element 2", lambda r, site: html_canonicals(r)[1]
           if len(html_canonicals(r)) > 1 else "", "url"),
    HTTP_CANONICAL, META_ROBOTS_1, X_ROBOTS_TAG_1,
    REL_NEXT_1, REL_PREV_1, HTTP_REL_NEXT_1, HTTP_REL_PREV_1,
]

register(Tab(id="canonicals", name="Canonicals", order=130, columns=COLUMNS, filters=FILTERS,
             rows=canonical_rows, skip_reason=skip_reason))
