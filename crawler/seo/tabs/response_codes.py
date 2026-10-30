"""
Response Codes tab (Screaming Frog "Response Codes"): internal URLs and external links together,
grouped by the HTTP status of the request. Each URL falls in exactly one of Blocked by Robots.txt,
No Response, Success, Redirection, Client Error or Server Error (external links that were never
checked fall in "External Not Checked"), so those counts add up to "All". Meta refresh, HTTP
refresh, redirect chain and redirect loop are extra views on top of that split. "Redirect Kind" says why
a URL redirects (HTTP to HTTPS, www added, trailing slash...; crawl_budget.redirect_type).

Blocked Resource is a view on top of Blocked by Robots.txt: a blocked URL that crawled pages load as a
resource (image, script, stylesheet, frame, CSS background), found in the HTML or, when pages were rendered,
in the requests Chrome made. Redirection (JavaScript) lists pages that answered 2xx but whose script sent the
browser to another address (needs JavaScript rendering, Configuration > Rendering); they stay counted under
Success, like meta refresh redirects. Internal Redirect Chain and Loop follow every kind of hop (P4): HTTP
redirects, meta refresh, Refresh headers and JavaScript redirects, so a meta refresh to a 301 is a chain of two.
External pages are never rendered, so External Redirection (JavaScript)
is always "Not checked". External Redirection (Meta Refresh / HTTP Refresh) come from the external link check,
which keeps the Refresh header and reads the first 64 KB of an external page that answers 2xx with HTML;
those pages stay counted under External Success (a 200 is a 2xx). Crawls made before that check existed show
them "Not checked".

All Errors (4xx, 5xx & No Response), and its Internal / External versions, list Client Error + Server Error +
No Response of their scope in one view (the three never overlap, so its count is their sum). They are plain
filters, not issues: each of those URLs is already one issue row under its own status, and a 404 must not count
twice in the Issues overview.

Percentages: the Internal filters are a share of the internal URLs ("Internal All") and the External filters a
share of the external URLs ("External All") (Filter.of_rows); the unscoped filters a share of every row.
"""
import re
from typing import Optional, Set

from ...url_utils import normalize_url
from ..crawl_budget import REDIRECT_KIND
from ..registry import Column, Filter, Tab, register, rendered, seo
from .internal import (ADDRESS, CONTENT_TYPE, INDEXABILITY, INDEXABILITY_STATUS, INLINKS, REDIRECT_TYPE,
                       REDIRECT_URL, RESPONSE_TIME, STATUS, STATUS_CODE, category_of, http_refresh_target,
                       internal_rows, is_external, memo, meta_refresh_target, redirect_chain, redirect_hop,
                       status_codes_seen)


def response_rows(site):
    """Internal URLs first (crawl order), then external links."""
    return internal_rows(site) + list(site.externals or [])


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
def _cat(c: str):
    return lambda r, site: category_of(r) == c


def _internal(test):
    return lambda r, site: not is_external(r) and test(r, site)


def _external(test):
    return lambda r, site: is_external(r) and test(r, site)


def _meta_refresh(r, site) -> bool:
    return not is_external(r) and bool(meta_refresh_target(r))


def _http_refresh(r, site) -> bool:
    return not is_external(r) and bool(http_refresh_target(r))


def _ext_meta_refresh(r, site) -> bool:
    return is_external(r) and category_of(r) == "2xx" and bool(meta_refresh_target(r))


def _ext_http_refresh(r, site) -> bool:
    return is_external(r) and bool(category_of(r)) and bool(http_refresh_target(r))


ERROR_CATEGORIES = ("4xx", "5xx", "error")


def _all_error(r, site) -> bool:
    """4xx, 5xx or no response (blocked and never-checked external links are not errors)."""
    return category_of(r) in ERROR_CATEGORIES


EXTERNAL_REFRESH_FILTERS = ("external_meta_refresh", "external_http_refresh")


def external_refresh_reason(site) -> str:
    """Why the external refresh filters could not run ("" when they did): external links not checked, or a
    crawl made before the external check looked for refreshes."""
    externals = list(getattr(site, "externals", None) or [])
    if not externals:
        return ""          # nothing linked externally: the checks report "Not applicable" themselves
    checked = [e for e in externals if category_of(e) not in ("", "blocked")]
    if not checked:
        return "External links were not checked (turn on 'Check external links' in the crawl settings)."
    if not any(getattr(e, "refresh_checked", False) for e in checked):
        return ("This crawl did not look for refreshes on external pages (it was made by an older version); run a "
                "new crawl to check them.")
    return ""


def _norm(url: str) -> str:
    try:
        return normalize_url(url) or url
    except Exception:
        return url


def resource_urls(site) -> Set[str]:
    """Every URL a crawled page loads as a resource: <img>/<script>/<link>/<iframe>/<source>... in the HTML,
    CSS background images, and (rendered pages) every request Chrome made, blocked ones included."""
    def build():
        out: Set[str] = set()
        for page in site.pages:
            p = seo(page)
            for res in p.get("resources") or ():
                u = res.get("url") if isinstance(res, dict) else None
                if u:
                    out.add(u)
            for u in p.get("background_images") or ():
                if isinstance(u, str) and u:
                    out.add(u)
            rd = rendered(page)
            if rd is not None:
                for res in list(rd.get("resources") or []) + list(rd.get("blocked_resources") or []):
                    u = res.get("url") if isinstance(res, dict) else None
                    if u:
                        out.add(u)
                        out.add(_norm(u))
        return out
    return memo(site, "rc_resource_urls", build)


def _blocked_resource(r, site) -> bool:
    return category_of(r) == "blocked" and getattr(r, "url", "") in resource_urls(site)


def _js_redirect(r, site) -> bool:
    if is_external(r):
        return False
    rd = rendered(r)
    return rd is not None and bool(rd.get("js_redirect"))


def js_redirect_url(r) -> str:
    """Where a page's script sent the browser ("" when it did not, or the page was not rendered)."""
    rd = None if is_external(r) else rendered(r)
    return str(rd.get("js_redirect_url") or "") if rd and rd.get("js_redirect") else ""


def skip_reason(site, filter_id: str) -> str:
    """Why a Blocked Resource / JavaScript redirect filter is "Not checked" ("" when it ran)."""
    cfg = getattr(site, "config", None)
    if filter_id.endswith("blocked_resource"):
        if cfg is not None and not getattr(cfg, "respect_robots", True):
            return "robots.txt was ignored in this crawl ('Respect robots.txt' was off), so nothing was blocked."
        rendered_any = any(rendered(p) is not None for p in site.pages)
        if cfg is not None and not getattr(cfg, "crawl_resources", True) and not rendered_any:
            return ("Images, scripts and stylesheets were not requested (resource crawling off) and no page was "
                    "rendered, so blocked files could not be seen.")
    if filter_id == "external_js_redirect":
        return "External pages are only checked for their HTTP answer, never rendered in Chrome."
    if filter_id in EXTERNAL_REFRESH_FILTERS:
        return memo(site, "rc_external_refresh_reason", lambda: external_refresh_reason(site))
    if filter_id == "external_all_error":
        externals = list(getattr(site, "externals", None) or [])
        if externals and not any(category_of(e) not in ("", "blocked") for e in externals):
            return "External links were not checked (turn on 'Check external links' in the crawl settings)."
    return ""


def redirect_hops(r) -> int:
    """Number of redirect responses the request went through (from the recorded chain, or from an
    http_status like "301 -> 302 -> 200"; the engine's "301 -> 200" form gives 1)."""
    chain = redirect_chain(r)
    known = [c for _, c in chain if c]
    from_chain = sum(1 for c in known if 300 <= c < 400) if known else len(chain)
    from_codes = sum(1 for c in status_codes_seen(r) if 300 <= c < 400)
    return max(from_chain, from_codes)


def _target(r) -> Optional[str]:
    """Where the URL's hop leads (HTTP redirect, meta refresh, Refresh header, JavaScript), None without one."""
    return redirect_hop(r)[0] or None


def _redirect_chain(r, site) -> bool:
    """Internal URL that redirects (any kind of hop) to a crawled URL that redirects again (2+ hops)."""
    if is_external(r) or _redirect_loop(r, site):
        return False
    if redirect_hops(r) >= 2:
        return True
    target = _target(r)
    if not target:
        return False
    nxt = site.by_url.get(target)
    return nxt is not None and nxt is not r and bool(_target(nxt))


_LOOP_MSG = re.compile(r"redirect", re.I)
_LOOP_HINT = re.compile(r"exceed|loop|too many", re.I)


def _redirect_loop(r, site) -> bool:
    """Internal URL whose redirects come back to a URL already in the chain."""
    if is_external(r):
        return False
    msg = getattr(r, "error_message", "") or ""
    if _LOOP_MSG.search(msg) and _LOOP_HINT.search(msg):
        return True
    urls = [u for u, _ in redirect_chain(r) if u]
    if len(urls) != len(set(urls)):
        return True
    t = _target(r)
    if not t:
        return False
    if t == r.url:
        return True
    # Two crawled URLs sending the visitor to each other (or a longer cycle among crawled URLs), whatever the kind
    # of each hop
    seen = {r.url}
    cur = site.by_url.get(t)
    for _ in range(20):
        if cur is None or not _target(cur):
            return False
        if cur.url in seen:
            return True
        seen.add(cur.url)
        cur = site.by_url.get(_target(cur) or "")
    return False


# ---------------------------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------------------------
_BOTH = [
    Filter("blocked_robots", "Blocked by Robots.txt", _cat("blocked"),
           description="URLs (internal or external) that robots.txt does not allow the crawler to request, and "
                       "links a site refused with a non-standard bot-block code such as LinkedIn's 999 (Status: "
                       "\"Blocked by site (999)\"; never counted as a 5xx)."),
    Filter("blocked_resource", "Blocked Resource", _blocked_resource,
           description="Images, scripts, stylesheets and frames that pages load but robots.txt blocks. Search "
                       "engines cannot fetch them either, so they may not see the page as visitors do. They "
                       "are also counted under Blocked by Robots.txt."),
    Filter("no_response", "No Response", _cat("error"),
           description="URLs that gave no answer at all: timeout, refused connection, DNS, SSL or malformed URL."),
    Filter("success", "Success (2xx)", _cat("2xx"), description="URLs that loaded normally (200-299)."),
    Filter("redirection", "Redirection (3xx)", _cat("3xx"),
           description="URLs that answered with a server-side redirect (301, 302, 307, 308...)."),
    Filter("js_redirect", "Redirection (JavaScript)", _js_redirect, requires=("rendering",),
           description="Pages that load (2xx) but whose JavaScript then sends the browser to another address "
                       "(found by rendering the page in Chrome). They are also counted under Success."),
    Filter("meta_refresh", "Redirection (Meta Refresh)",
           lambda r, site: _meta_refresh(r, site) or _ext_meta_refresh(r, site),
           description="Pages that send visitors elsewhere with a <meta http-equiv=\"refresh\"> tag. "
                       "They return 200, so they are also counted under Success."),
    Filter("http_refresh", "Redirection (HTTP Refresh)",
           lambda r, site: _http_refresh(r, site) or _ext_http_refresh(r, site),
           description="URLs that send visitors elsewhere with a 'Refresh' response header."),
    Filter("client_error", "Client Error (4xx)", _cat("4xx"),
           description="URLs that are broken or refuse access (404, 403, 410, 429...)."),
    Filter("server_error", "Server Error (5xx)", _cat("5xx"),
           description="URLs where the server failed (500, 502, 503, 504...)."),
    Filter("all_error", "All Errors (4xx, 5xx & No Response)", _all_error,
           description="Every URL that is broken, failed on the server or gave no answer, internal and external, "
                       "in one list: Client Error (4xx) + Server Error (5xx) + No Response. Each of these URLs is "
                       "already counted under its own status, so this view is not a separate issue."),
]

_INTERNAL_LIST = [
    Filter("internal_all", "Internal All", lambda r, site: not is_external(r),
           description="Every URL on your own site that was requested."),
    Filter("internal_blocked_robots", "Internal Blocked by Robots.txt", _internal(_cat("blocked")),
           "Warning", "High",
           description="Your own robots.txt stops search engines from crawling these URLs, yet your pages link "
                       "to them. Search engines cannot read what is on them, and links pointing there are wasted.",
           fix="If the pages should appear in search, remove or narrow the robots.txt rule. If they really "
               "should stay private, stop linking to them, and use noindex rather than robots.txt to keep "
               "pages out of the index."),
    Filter("internal_no_response", "Internal No Response", _internal(_cat("error")), "Issue", "High",
           description="Your own URLs gave no answer at all (timeout, refused connection, DNS or SSL failure). "
                       "Visitors and search engines see nothing.",
           fix="Open the URL in a browser to confirm. Check the server, firewall or bot protection, and the "
               "SSL certificate; if the link itself is malformed, correct it on the linking pages."),
    Filter("internal_success", "Internal Success (2xx)", _internal(_cat("2xx")),
           description="Your own URLs that loaded normally."),
    Filter("internal_blocked_resource", "Internal Blocked Resource", _internal(_blocked_resource),
           "Warning", "High",
           description="Your own images, scripts, stylesheets or frames that your pages load but your robots.txt "
                       "blocks. Google renders pages before ranking them; without these files it may see a "
                       "broken or empty layout, miss content and judge the page not mobile-friendly.",
           fix="Allow the files in robots.txt (remove or narrow the Disallow rule for the folders that hold "
               "CSS, JavaScript and images), then test the page with Google's URL Inspection.",
           short_fix="Allow these CSS/JS/image files in robots.txt"),
    Filter("internal_redirection", "Internal Redirection (3xx)", _internal(_cat("3xx")), "Warning", "Low",
           description="Your pages link to URLs on your own site that redirect. Each redirect adds a delay for "
                       "visitors and makes search engines do extra work.",
           fix="Update the links (in menus, content and sitemaps) to point straight at the final URL shown in "
               "Redirect URL. Keep the redirect itself for old bookmarks and outside links."),
    Filter("internal_js_redirect", "Internal Redirection (JavaScript)", _js_redirect, "Warning", "Low",
           requires=("rendering",),
           description="Your own pages load normally (2xx) and then their JavaScript sends the visitor to another "
                       "address. Search engines only see that redirect if they run the script, and visitors see "
                       "the first page flash before it changes.",
           fix="Replace the script redirect with a 301 redirect on the server, and link straight to the "
               "final address shown in JS Redirect URL.",
           short_fix="Use a server 301 redirect instead of a JavaScript redirect"),
    Filter("internal_meta_refresh", "Internal Redirection (Meta Refresh)", _internal(_meta_refresh),
           "Warning", "Low",
           description="Your own pages redirect with a meta refresh tag instead of a server redirect. Search "
                       "engines may not treat it as a redirect and visitors can see a flash of the old page.",
           fix="Replace the meta refresh with a 301 redirect on the server, and link to the target directly."),
    Filter("internal_http_refresh", "Internal Redirection (HTTP Refresh)", _internal(_http_refresh),
           "Warning", "Low",
           description="Your own URLs redirect with a 'Refresh' response header instead of a proper redirect "
                       "status. Not every search engine follows it as a redirect.",
           fix="Replace the Refresh header with a 301 redirect, and link to the target directly."),
    Filter("internal_redirect_chain", "Internal Redirect Chain", _redirect_chain, "Warning", "Medium",
           description="Your own URLs that redirect to a URL that redirects again (two or more hops; HTTP redirects, "
                       "meta refresh, Refresh headers and JavaScript redirects all count). Every hop slows the "
                       "visitor down and search engines may stop following long chains.",
           fix="Point the first redirect straight at the final destination, and update links to use the "
               "final URL."),
    Filter("internal_redirect_loop", "Internal Redirect Loop", _redirect_loop, "Issue", "High",
           description="Your own URLs whose redirects lead back to a URL already in the chain, so the page never "
                       "loads. Browsers show a 'too many redirects' error.",
           fix="Check the redirect rules for these URLs (server config, CMS plugin, CDN) and remove the rule "
               "that sends the visitor back; each URL should end on a page that returns 200."),
    Filter("internal_client_error", "Internal Client Error (4xx)", _internal(_cat("4xx")), "Issue", "High",
           description="Your own URLs that are broken or refuse access (404 not found, 410 gone, 403 forbidden). "
                       "Visitors hit a dead end and the links pass no value.",
           fix="Fix or remove the links pointing here (see Inlinks), or bring the page back. If the content "
               "moved, add a 301 redirect to the new URL."),
    Filter("internal_server_error", "Internal Server Error (5xx)", _internal(_cat("5xx")), "Issue", "High",
           description="Your server failed while answering for these URLs. Visitors see an error and search "
                       "engines may drop the pages if it keeps happening.",
           fix="Check the server error logs for the time of the crawl, then re-test. Fix the failing code, "
               "plugin or database, or raise server capacity if it is overloaded."),
    Filter("internal_all_error", "Internal All Errors (4xx, 5xx & No Response)", _internal(_all_error),
           description="Every URL of your own site that is broken, failed on the server or gave no answer, in one "
                       "list: Internal Client Error (4xx) + Internal Server Error (5xx) + Internal No Response. "
                       "Each one is an issue under its own status already; this list gathers them for one fix pass."),
]

_EXTERNAL_LIST = [
    Filter("external_all", "External All", lambda r, site: is_external(r),
           description="Every URL on another website that your pages link to or load (scripts, styles, "
                       "images, frames)."),
    Filter("external_blocked_robots", "External Blocked by Robots.txt", _external(_cat("blocked")),
           description="Other sites whose robots.txt does not allow the crawler to check these links, or that "
                       "refused the check with a bot-block code such as LinkedIn's 999 (Status: \"Blocked by "
                       "site (999)\"; the link usually works for visitors)."),
    Filter("external_no_response", "External No Response", _external(_cat("error")), "Warning", "Low",
           description="Links to other websites that gave no answer at all. The site may be down, gone, or "
                       "blocking crawlers.",
           fix="Open the link in a browser. If the site is gone, remove the link or point it to a working "
               "source; if it works in a browser, it is likely blocking crawlers and can be left alone."),
    Filter("external_blocked_resource", "External Blocked Resource", _external(_blocked_resource),
           description="Files on other websites that your pages load but whose robots.txt blocks the crawler "
                       "(fonts, scripts or images from a CDN or a widget provider)."),
    Filter("external_success", "External Success (2xx)", _external(_cat("2xx")),
           description="Links to other websites that load normally."),
    Filter("external_js_redirect", "External Redirection (JavaScript)", lambda r, site: False,
           description="External pages are only checked for their HTTP answer, never rendered, so a JavaScript "
                       "redirect on another website cannot be seen."),
    Filter("external_redirection", "External Redirection (3xx)", _external(_cat("3xx")),
           description="Links to other websites that redirect. Usually harmless; update them when the "
                       "destination is clearly the new home of the same page."),
    Filter("external_client_error", "External Client Error (4xx)", _external(_cat("4xx")), "Warning", "Low",
           description="Links to other websites that are broken or refuse access (404, 410, 403). Visitors who "
                       "click them hit a dead end.",
           fix="Replace the link with a working page that covers the same thing, or remove it. A 403 can be the "
               "other site blocking crawlers, so check it in a browser first."),
    Filter("external_server_error", "External Server Error (5xx)", _external(_cat("5xx")), "Warning", "Low",
           description="Links to other websites whose server failed when checked. It may be temporary.",
           fix="Re-check later. If it keeps failing, replace the link with a working source or remove it."),
    Filter("external_all_error", "External All Errors (4xx, 5xx & No Response)", _external(_all_error),
           description="Every link to another website that is broken, failed on the server or gave no answer, in "
                       "one list: External Client Error (4xx) + External Server Error (5xx) + External No "
                       "Response. External links that were not checked are not in it."),
    Filter("external_meta_refresh", "External Redirection (Meta Refresh)", _ext_meta_refresh, "Warning", "Low",
           description="Links to pages on other websites that load (2xx) and then send the visitor elsewhere with "
                       "a <meta http-equiv=\"refresh\"> tag. Visitors see the first page flash before it changes, "
                       "and the link may not pass its value on. They stay counted under External Success.",
           fix="Open the link and update it to the address the page sends you to (shown in Redirect URL).",
           short_fix="Link straight to the page the meta refresh sends visitors to"),
    Filter("external_http_refresh", "External Redirection (HTTP Refresh)", _ext_http_refresh, "Warning", "Low",
           description="Links to pages on other websites that send the visitor elsewhere with a 'Refresh' "
                       "response header instead of a redirect status. Not every search engine treats it as a "
                       "redirect.",
           fix="Update the link to the address the Refresh header points to (shown in Redirect URL).",
           short_fix="Link straight to the page the Refresh header points to"),
    Filter("external_not_checked", "External Not Checked", _external(_cat("")),
           description="External links that were recorded but never requested (external link checking is "
                       "turned off or still running)."),
]

# Screaming Frog order within each scope (Blocked Resource after Blocked by Robots.txt, the JavaScript
# redirect after Redirection (3xx)), our extras (chain, loop, not checked) where they fit
_INTERNAL_ORDER = ["internal_all", "internal_blocked_robots", "internal_blocked_resource", "internal_no_response",
                   "internal_success", "internal_redirection", "internal_js_redirect", "internal_meta_refresh",
                   "internal_http_refresh", "internal_redirect_chain", "internal_redirect_loop",
                   "internal_client_error", "internal_server_error", "internal_all_error"]
_EXTERNAL_ORDER = ["external_all", "external_blocked_robots", "external_blocked_resource", "external_no_response",
                   "external_success", "external_redirection", "external_js_redirect", "external_meta_refresh",
                   "external_http_refresh", "external_client_error", "external_server_error", "external_all_error",
                   "external_not_checked"]
_INTERNAL = sorted(_INTERNAL_LIST, key=lambda f: _INTERNAL_ORDER.index(f.id))
_EXTERNAL = sorted(_EXTERNAL_LIST, key=lambda f: _EXTERNAL_ORDER.index(f.id))


def _internal_row(r) -> bool:
    return not is_external(r)


# Each scope has its own "All" (like Screaming Frog): an Internal filter's "% of total" is a share of the internal
# URLs and an External one's of the external URLs, so "Internal Client Error (4xx)" shows the same % as the
# Crawl Overview's 4xx share; the unscoped filters above stay a share of every row
for _f in _INTERNAL:
    _f.of_rows = _internal_row
for _f in _EXTERNAL:
    _f.of_rows = is_external

register(Tab(
    id="response_codes", name="Response Codes", order=40,
    columns=[ADDRESS, CONTENT_TYPE, STATUS_CODE, STATUS, INDEXABILITY, INDEXABILITY_STATUS, INLINKS,
             RESPONSE_TIME, REDIRECT_URL, REDIRECT_TYPE, REDIRECT_KIND,
             Column("js_redirect_url", "JS Redirect URL", lambda r, site: js_redirect_url(r), "url")],
    filters=_BOTH + _INTERNAL + _EXTERNAL,
    rows=response_rows,
    skip_reason=skip_reason,
))
