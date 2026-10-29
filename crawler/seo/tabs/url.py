"""
URL tab (Screaming Frog "URL"): problems in how internal URLs are written. "All" lists every
internal URL (the same total as Internal, so percentages match Screaming Frog). As in Screaming
Frog, Non ASCII Characters is checked on every URL (images and files too), while the other checks
run on HTML documents only: an image named "Logo_Final.PNG" is not flagged as Uppercase.

The crawler normalizes URLs (collapses '//' and strips utm_/_ga/_gl parameters), so Multiple
Slashes and GA Tracking Parameters also look at the links as written on the linking pages.
Broken Bookmark lists the internal HTML pages that other pages (or the page itself) link to with a
#fragment that no element on the page carries as its id (or <a name>), so the link opens the page at the top
instead of the section (analysis.compute_broken_bookmarks; the Broken Bookmarks column says which fragment
and from where). Script hooks (#, #top), app routes (#/..., #!...) and state fragments (#page=2) are not
bookmarks and are left out.
"""
import re
from typing import Iterable, List
from urllib.parse import parse_qsl, unquote, urlparse

from ..registry import Column, Filter, Tab, pref_number, register, seo, threshold_filter
from .internal import (ADDRESS, CONTENT_TYPE, INDEXABILITY, INDEXABILITY_STATUS, STATUS, STATUS_CODE,
                       canonical_of, internal_rows, is_html_doc, memo)

MAX_URL_LENGTH = 115          # default "Over 115 Characters" (Preferences url_max_chars, P14)

SEARCH_PARAMS = {"s", "q", "query", "search", "search_query", "searchterm", "keyword", "keywords", "k"}
SEARCH_PATH = re.compile(r"/(search|suche|recherche|buscar|zoeken)(/|$|\?)", re.I)
GA_PARAMS = re.compile(r"(^|[?&;])(utm_[a-z_]*|_ga|_gl)=", re.I)


def _raw_hrefs(site, url: str) -> List[str]:
    """Every way a crawled page wrote a link to this URL (raw href values)."""
    def build():
        out = {}
        for page in site.pages:
            for link in seo(page).get("links") or ():
                try:
                    u, raw = link.get("url") or "", link.get("raw") or ""
                    if u and raw:
                        out.setdefault(u, []).append(raw)
                except Exception:
                    continue
        return out
    raws = list(memo(site, "url_raw_hrefs", build).get(url, []))
    origin = site.by_url.get(url)
    if origin is not None and getattr(origin, "raw_href", ""):
        raws.append(origin.raw_href)
    return raws


def _forms(r, site) -> Iterable[str]:
    yield r.url
    yield from _raw_hrefs(site, r.url)


def _path(url: str) -> str:
    try:
        return urlparse(url).path or ""
    except Exception:
        return ""


def _html(test):
    return lambda r, site: is_html_doc(r) and test(r, site)


def non_ascii(r, site) -> bool:
    try:
        decoded = unquote(r.url, errors="strict")
    except Exception:
        decoded = r.url
    return any(ord(c) > 127 for c in decoded)


def underscores(r, site) -> bool:
    return "_" in _path(r.url)


_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def uppercase(r, site) -> bool:
    """A capital letter in the path or query as a visitor reads it: percent-escapes are decoded first, so the
    hex digits of "/caf%C3%A9" (written in capitals by URL encoding) are not letters, while "/%C3%89cole"
    (a capital E with an accent) is flagged."""
    u = urlparse(r.url)
    written = u.path + ("?" + u.query if u.query else "")
    try:
        readable = unquote(written, errors="strict")
    except UnicodeDecodeError:                      # escapes that are not UTF-8: judge the rest of the URL
        readable = _PERCENT_ESCAPE.sub("", written)
    return any(c.isupper() for c in readable)


def multiple_slashes(r, site) -> bool:
    for form in _forms(r, site):
        if "://" in form:
            p = _path(form)
        elif form.startswith("//"):                 # protocol-relative "//host/path"
            p = _path("http:" + form)
        else:                                       # relative href as written
            p = form.split("?", 1)[0].split("#", 1)[0]
        if "//" in p:
            return True
    return False


def repetitive_path(r, site) -> bool:
    segs = [s.lower() for s in _path(r.url).split("/") if s]
    return len(segs) != len(set(segs))


def contains_space(r, site) -> bool:
    return " " in r.url or "%20" in r.url.lower()


def internal_search(r, site) -> bool:
    u = urlparse(r.url)
    if SEARCH_PATH.search(u.path or ""):
        return True
    return any(k.lower() in SEARCH_PARAMS for k, _ in parse_qsl(u.query, keep_blank_values=True))


def parameters(r, site) -> bool:
    return bool(urlparse(r.url).query)


def ga_tracking(r, site) -> bool:
    for form in _forms(r, site):
        q = form.split("?", 1)[1] if "?" in form else ""
        if q and GA_PARAMS.search("?" + q.split("#", 1)[0]):
            return True
    return False


def broken_bookmarks(site, r) -> list:
    return list((getattr(site, "broken_bookmarks", None) or {}).get(getattr(r, "url", ""), []))


def _bookmark_cell(r, site) -> str:
    out = []
    for b in broken_bookmarks(site, r)[:10]:
        out.append(f"#{b.get('fragment')} (from {b.get('source')})")
    more = len(broken_bookmarks(site, r)) - len(out)
    return "; ".join(out) + (f"; +{more} more" if more > 0 else "")


def over_length(r, site) -> bool:
    return len(r.url) > pref_number(site, "url_max_chars")


FILTERS = [
    Filter("non_ascii", "Non ASCII Characters", non_ascii, "Warning", "Low",
           description="The URL contains characters outside plain ASCII (accented letters, symbols, other "
                       "scripts). Browsers encode them into long %XX sequences, and some tools, emails and "
                       "platforms break or mangle them when the link is shared.",
           fix="Use plain a-z, 0-9 and hyphens in URLs. If the address must keep these characters, link to it "
               "with the characters percent-encoded, and redirect any old forms of the URL."),
    Filter("underscores", "Underscores", _html(underscores), "Opportunity", "Low",
           description="The URL uses underscores between words. Google treats hyphens as word separators but "
                       "may read words joined by underscores as one word.",
           fix="Use hyphens in new URLs. Changing existing URLs is only worth it with 301 redirects and when "
               "the pages matter; otherwise leave them."),
    Filter("uppercase", "Uppercase", _html(uppercase), "Warning", "Low",
           description="The URL contains capital letters. URLs are case-sensitive, so /Page and /page can be "
                       "two different addresses, which splits links and creates duplicates.",
           fix="Use lowercase URLs, 301-redirect the uppercase versions to them, and update internal links."),
    Filter("multiple_slashes", "Multiple Slashes", _html(multiple_slashes), "Issue", "Low",
           description="The URL path (or a link to it) contains '//' after the domain. This usually comes from "
                       "a template joining paths badly and can create duplicate URLs.",
           fix="Fix the link or template that builds the URL so there is a single slash, and redirect the "
               "double-slash version to the clean one."),
    Filter("repetitive_path", "Repetitive Path", _html(repetitive_path), "Warning", "Low",
           description="A folder name appears more than once in the path (e.g. /services/seo/services/). It can "
                       "be legitimate, but it often points to relative links stacking up into endless URLs.",
           fix="Check the links that lead here; make relative links absolute (start with '/') and redirect or "
               "remove the repeated-path URLs."),
    Filter("contains_space", "Contains Space", _html(contains_space), "Issue", "Low",
           description="The URL contains a space (or %20). Spaces are not valid in URLs, get encoded "
                       "inconsistently and break when links are copied or shared.",
           fix="Rename the page or file with hyphens instead of spaces, redirect the old URL and update links."),
    Filter("internal_search", "Internal Search", _html(internal_search), "Warning", "Low",
           description="The URL looks like a page of your site's own search results (a /search path or an "
                       "s=, q= or query= parameter). These pages are thin, endless and waste crawl budget.",
           fix="Add noindex to search result pages and avoid linking to them from crawlable pages; you can "
               "also disallow them in robots.txt once they are out of the index."),
    Filter("parameters", "Parameters", _html(parameters), "Warning", "Low",
           description="The URL has query parameters ('?' or '&'). Parameters can create many URLs with the same "
                       "content (sorting, filters, session ids) that compete with each other.",
           fix="Link to the clean URL where possible, and give parameter versions a canonical pointing to it."),
    Filter("broken_bookmark", "Broken Bookmark", lambda r, site: bool(broken_bookmarks(site, r)),
           "Issue", "Low",
           description="Links point to a section of this page (#name) that does not exist: no element on the "
                       "page has that id. Visitors land at the top of the page instead of the part they "
                       "wanted, and search engines cannot use the link as a jump link.",
           fix="Give the target section the id the links use, or correct the links (see Broken Bookmarks "
               "for the fragment and the page that links with it).",
           short_fix="Add the missing id to the page or fix the #link",
           requires=("pagedata:ids",)),
    Filter("ga_tracking", "GA Tracking Parameters", _html(ga_tracking), "Warning", "Low",
           description="Internal links carry Google Analytics tracking parameters (utm_, _ga, _gl). Each "
                       "tagged link is a separate URL, and clicking it restarts the visitor's session in "
                       "analytics.",
           fix="Remove utm_/_ga/_gl parameters from links between your own pages; keep them only on links "
               "from emails, ads and other sites."),
    threshold_filter("over_115", "url_max_chars", "Over {n} Characters", _html(over_length),
                     issue_type="Opportunity", priority="Low",
                     description="The URL is longer than {n} characters. Not an error, but people prefer short, "
                                 "readable addresses and long ones get cut off in search results and when shared.",
                     fix="Use shorter, descriptive slugs for new pages. Only shorten existing URLs with 301 "
                         "redirects, and only when it is worth it."),
]

register(Tab(
    id="url", name="URL", order=50,
    columns=[ADDRESS, CONTENT_TYPE, STATUS_CODE, STATUS, INDEXABILITY, INDEXABILITY_STATUS,
             Column("length", "Length", lambda r, site: len(r.url), "num"),
             Column("canonical", "Canonical Link Element 1", lambda r, site: canonical_of(r), "url"),
             Column("broken_bookmarks", "Broken Bookmarks", _bookmark_cell)],
    filters=FILTERS,
    rows=internal_rows,
))
