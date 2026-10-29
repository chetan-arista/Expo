"""
Hygiene tab: small technical faults that waste crawl time or send visitors nowhere, read from the
PageData of internal HTML pages that answered 2xx (crawler/seo/extract.py), plus the random-URL probes
of crawler/seo/trap_probe.py (run after the crawl) as rows of their own:

  links to staging copies / private networks, same-page #anchors with no target, internal links to
  http:// from https pages, malformed hrefs (bare emails, spaces, backslashes, broken http:// prefix),
  javascript: pseudo-links, large pages without gzip / brotli, HTML without Cache-Control, missing
  doctype, no or non-UTF-8 charset, DOM over 1,500 elements, canonical on another host, crawl-trap URL
  patterns, and servers that answer 200 (or redirect home) for addresses that do not exist.

Checks other tabs already report are not repeated here: localhost links (Links), internal nofollow
(Links), HTML vs HTTP canonical conflict (Canonicals: Multiple Conflicting), repetitive paths (URL).
Ideas from open-seo-crawler's page checks (MIT, see THIRD_PARTY_NOTICES.md).
"""
import ipaddress
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlparse

from ...url_utils import clean_hostname
from ..crawl_budget import ALWAYS_TRAP, classify_key, norm_key
from ..extract import new_page
from ..postcrawl import SiteRow, not_run_reason, report_ran
from ..registry import Column, Filter, Tab, first, register, seo
from .internal import ADDRESS, STATUS_CODE, indexability_label, memo
from .page_titles import content_rows

LARGE_PAGE_BYTES = 50 * 1024       # uncompressed HTML above this should travel gzip / brotli encoded
DOM_LIMIT = 1500                   # elements (Lighthouse warns from about this size)
MAX_PATH_DEPTH = 12
DEEP_PAGE_NUMBER = 100

_LOCAL_ONLY = {"localhost", "0.0.0.0", "::1", "[::1]"}          # the Links tab reports these already
_LOCAL_SUFFIXES = (".local", ".test", ".internal", ".localdomain", ".lan", ".intranet", ".corp", ".home.arpa")
# Staging labels count only on the site's own domain: dev.to and test.com are real websites
_STAGING_LABELS = {"staging", "stage", "stg", "dev", "develop", "development", "test", "testing", "uat", "qa",
                   "preprod", "pre-prod", "preview", "sandbox", "beta"}
# Platform staging / preview hosts (Shopify, WP Engine, Kinsta, Pantheon, SiteGround, Flywheel, Cloudways, tunnels)
_PLATFORM_STAGING = (".myshopify.com", ".wpengine.com", ".wpenginepowered.com", ".kinsta.cloud", ".pantheonsite.io",
                     ".mystagingwebsite.com", ".flywheelsites.com", ".cloudwaysapps.com", ".ngrok.io",
                     ".ngrok-free.app", ".ngrok.app", ".localtest.me")
_ENCODED = ("gzip", "br", "deflate", "zstd", "compress")
_DATE_VALUE = re.compile(r"^\d{4}-\d{1,2}(-\d{1,2})?$")
_CALENDAR_PATH = re.compile(r"/(?:events?|calendar|agenda|kalender)/(?:.*/)?(?:\d{4}-\d{1,2}(?:-\d{1,2})?|\d{4}/\d{1,2})(?:/|$)", re.I)


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------
def _probe_report(site) -> Optional[dict]:
    rep = getattr(site, "trap_probe", None)
    return rep if report_ran(rep) else None


def probe_rows(site) -> List[SiteRow]:
    """The random-URL probes that found a problem, as rows with their own HTTP status."""
    def build():
        rep = _probe_report(site)
        out: List[SiteRow] = []
        for p in (rep or {}).get("probes") or []:
            if p.get("verdict") in ("ok", "error"):
                continue
            page = new_page(p.get("url") or "")
            page.update({k: v for k, v in (p.get("page") or {}).items() if k in page and v is not None})
            page["is_html"] = bool(p.get("page"))
            code = p.get("status")
            out.append(SiteRow(url=p.get("url") or "", status_code=code, http_status=str(code or ""),
                               redirect_destination=p.get("location") or None,
                               content_type="text/html" if p.get("page") else "", seo=page,
                               kind=f"probe_{p.get('kind')}", data=p, page_title=p.get("title") or "",
                               parent_url=p.get("parent") or "", discovery_source="Random-URL probe"))
        return out
    return memo(site, "hygiene_probe_rows", build)


def hygiene_rows(site) -> List[Any]:
    return content_rows(site) + probe_rows(site)


def _page(test):
    """A page test: never true on a probe row; False for pages read by an older build (keys missing)."""
    def run(r, site):
        if isinstance(r, SiteRow):
            return False
        return bool(test(r, site, seo(r)))
    return run


def _probe(verdict: str):
    return lambda r, site: isinstance(r, SiteRow) and (r.data or {}).get("verdict") == verdict


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
@lru_cache(maxsize=50000)
def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


@lru_cache(maxsize=4096)
def private_host_kind(host: str, site_domain: str) -> str:
    """Why a linked host is a staging copy or a private address ("" when it is a normal public host).
    Plain localhost is left to the Links tab ("Outlinks To Localhost")."""
    bare = (host or "").split(":")[0].strip("[]").lower()
    if not bare or bare in _LOCAL_ONLY or bare.startswith("127.") or bare.endswith(".localhost"):
        return ""
    domain = clean_hostname(site_domain or "")
    if domain and clean_hostname(bare) == domain:
        return ""
    try:
        ip = ipaddress.ip_address(bare)
        if ip.is_private or ip.is_link_local or ip.is_reserved:
            return "Private network address"
        return ""
    except ValueError:
        pass
    if bare.endswith(_LOCAL_SUFFIXES):
        return "Local network name"
    if bare.endswith(_PLATFORM_STAGING) and not (domain and domain.endswith(_PLATFORM_STAGING)):
        return "Platform staging / preview host"
    if domain:
        label, _, rest = clean_hostname(bare).partition(".")
        rest = rest[4:] if rest.startswith("www.") else rest
        if label in _STAGING_LABELS and (rest == domain or rest.endswith("." + domain)):
            return "Staging copy of this site"
    return ""


def staging_links(site, p: dict) -> List[str]:
    """Distinct hosts of links to staging copies or private networks."""
    out: List[str] = []
    domain = getattr(site, "base_domain", "") or ""
    seen = set()
    for link in p.get("links") or ():
        host = _host(link.get("url") or "")
        if not host or host in seen:
            continue
        seen.add(host)
        if private_host_kind(host, domain):
            out.append(host)
    return out


def http_internal_links(r, p: dict) -> List[str]:
    if not (p.get("final_url") or r.url).lower().startswith("https:"):
        return []
    return list(dict.fromkeys(l.get("url") for l in p.get("links") or ()
                              if l.get("internal") and (l.get("url") or "").lower().startswith("http:")))


def _malformed(p: dict, js: bool) -> List[dict]:
    return [m for m in p.get("malformed_hrefs") or [] if (m.get("code") == "javascript") == js]


def _headers(r) -> Dict[str, str]:
    return getattr(r, "response_headers", None) or {}


def _uncompressed_large(r, site, p) -> bool:
    h = _headers(r)
    if not h or not h.get("content-type"):
        return False                        # no header record for this page (browser-fetched): cannot judge
    enc = (h.get("content-encoding") or "").lower()
    return int(p.get("size_bytes") or 0) > LARGE_PAGE_BYTES and not any(e in enc for e in _ENCODED)


def _no_cache_control(r, site, p) -> bool:
    h = _headers(r)
    return bool(h) and bool(h.get("content-type")) and "cache-control" not in h and "expires" not in h


def canonical_host_change(r, p: dict) -> str:
    """The other host the canonical points at ("" when it stays on the page's host, www-insensitive)."""
    target = first(p.get("canonicals") or []) or p.get("http_canonical") or ""
    if not target:
        return ""
    th, ph = _host(target), _host(p.get("final_url") or r.url)
    if not th or not ph or clean_hostname(th) == clean_hostname(ph):
        return ""
    return th


def trap_reasons(url: str) -> List[str]:
    """Why a URL looks like part of a crawl trap (an endless URL space), in plain words."""
    try:
        u = urlparse(url)
    except Exception:
        return []
    out: List[str] = []
    segs = [s.lower() for s in (u.path or "").split("/") if s]
    if any(segs[i] == segs[i + 1] for i in range(len(segs) - 1)):
        out.append("same folder twice in a row")
    elif any(segs.count(s) >= 3 for s in set(segs)):
        out.append("a folder repeats 3+ times")
    else:
        for n in range(2, len(segs) // 2 + 1):
            if segs[-n:] == segs[-2 * n:-n]:
                out.append("the path repeats itself (relative-link loop)")
                break
    if len(segs) > MAX_PATH_DEPTH:
        out.append(f"{len(segs)} folders deep")
    if _CALENDAR_PATH.search(u.path or ""):
        out.append("calendar date page")
    pairs = parse_qsl(u.query, keep_blank_values=True) if u.query else []
    keys = {norm_key(k) for k, _ in pairs}
    if keys & ALWAYS_TRAP or any(k.startswith("e-filter-") for k in keys):
        out.append("page-builder AJAX parameter")
    for k, v in pairs:
        cls = classify_key(norm_key(k))
        if cls == "calendar" and _DATE_VALUE.match(v.strip()):
            out.append("calendar date parameter")
            break
        if cls == "pagination" and v.isdigit() and len(v) < 9 and int(v) > DEEP_PAGE_NUMBER:
            out.append(f"page number {v}")
            break
    if len(keys) >= 4:
        out.append(f"{len(keys)} parameters combined (faceted navigation)")
    return out


# ---------------------------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------------------------
FILTERS = [
    Filter("staging_links", "Links To Staging Or Private Hosts", _page(lambda r, site, p: staging_links(site, p)),
           issue_type="Issue", priority="High",
           description="The page links to a staging copy of the site (staging., dev., a myshopify.com or WP Engine "
                       "preview host) or to a private network address. Visitors and crawlers hit a copy they should "
                       "not see, or a dead end.",
           fix="Replace those links with the live address; search the theme, menus and content for the staging "
               "host (a search-and-replace after the last migration usually fixes it)."),
    Filter("dead_anchors", "Broken Same-Page Anchors", _page(lambda r, site, p: p.get("dead_fragments")),
           issue_type="Warning", priority="Low",
           description="A link such as #pricing points at a section of this page that does not exist (no element "
                       "with that id), so clicking it does nothing.",
           fix="Give the target section the matching id, or correct the link."),
    Filter("http_internal_links", "Internal Links To HTTP", _page(lambda r, site, p: http_internal_links(r, p)),
           issue_type="Warning", priority="Medium",
           description="This https page links to http:// addresses of your own site. Every click goes through a "
                       "redirect (or, without one, to the insecure page) and crawlers spend a request on it.",
           fix="Change the links to https:// (a search-and-replace in the content and menus)."),
    Filter("malformed_links", "Malformed Links", _page(lambda r, site, p: _malformed(p, False)),
           issue_type="Issue", priority="Medium",
           description="A link is written in a broken way: an email address without mailto:, spaces, backslashes, "
                       "a broken http:// prefix or a file path on someone's computer. Visitors land on a missing "
                       "page.",
           fix="Correct the href (for email use mailto:name@example.com)."),
    # A plain filter, not an issue: every page it lists is also in Links > "Pages With Uncrawlable Internal
    # Outlinks" (Screaming Frog's issue), so counting it again would double the same javascript: links.
    Filter("javascript_links", "JavaScript Pseudo-Links", _page(lambda r, site, p: _malformed(p, True)),
           description="Links with href=\"javascript:...\" are not addresses: crawlers cannot follow them, so "
                       "whatever they open is not found through them. The Issues overview counts these pages "
                       "once, under Links: Pages With Uncrawlable Internal Outlinks.",
           fix="Use a real href for navigation, or a <button> for actions."),
    Filter("no_compression", "Large Page Without Compression", _page(_uncompressed_large),
           issue_type="Warning", priority="Medium",
           description=f"The HTML is over {LARGE_PAGE_BYTES // 1024} KB and is sent without gzip or brotli, so it "
                       "downloads several times slower than it needs to, for visitors and crawlers.",
           fix="Turn on gzip / brotli compression for text/html on the server or CDN."),
    Filter("no_cache_control", "HTML Without Cache-Control", _page(_no_cache_control),
           issue_type="Opportunity", priority="Low",
           description="The page is sent without a Cache-Control (or Expires) header, so browsers and CDNs guess "
                       "how long they may keep it.",
           fix="Send a Cache-Control header for HTML (for example 'no-cache' or a short max-age)."),
    Filter("missing_doctype", "Missing Doctype", _page(lambda r, site, p: p.get("has_doctype") is False),
           issue_type="Warning", priority="Medium",
           description="The page does not start with <!DOCTYPE html>, so browsers render it in 'quirks mode' and "
                       "layout can break in odd ways.",
           fix="Put <!DOCTYPE html> as the very first line of the page template."),
    Filter("no_charset", "No Charset Declared", _page(lambda r, site, p: p.get("charset") == ""),
           issue_type="Warning", priority="Low",
           description="Neither the HTTP header nor a <meta charset> says which character set the page uses, so "
                       "accented letters and symbols can show as garbage.",
           fix="Add <meta charset=\"utf-8\"> at the top of <head>."),
    Filter("non_utf8_charset", "Non-UTF-8 Charset",
           _page(lambda r, site, p: bool(p.get("charset")) and p.get("charset") not in ("utf-8", "utf8")),
           issue_type="Warning", priority="Low",
           description="The page declares a character set other than UTF-8 (for example ISO-8859-1), which "
                       "breaks characters outside that set.",
           fix="Save the templates as UTF-8 and declare charset=utf-8."),
    Filter("large_dom", f"DOM Over {DOM_LIMIT:,} Elements", _page(lambda r, site, p: int(p.get("dom_nodes") or 0) > DOM_LIMIT),
           issue_type="Warning", priority="Low",
           description=f"The page has more than {DOM_LIMIT:,} HTML elements. Large pages render and respond more "
                       "slowly, especially on phones.",
           fix="Simplify the template: fewer nested wrappers, paginate long lists, load hidden menus on demand."),
    Filter("cross_host_canonical", "Canonical Points To Another Host",
           _page(lambda r, site, p: canonical_host_change(r, p)),
           issue_type="Warning", priority="High",
           description="The canonical tells search engines the main version of this page is on another host "
                       "(another domain or a staging copy), so this page is not indexed in its own right.",
           fix="Point the canonical at this page's own address unless the content really is a copy of the other "
               "site."),
    Filter("crawl_trap_url", "Crawl Trap URL Pattern", _page(lambda r, site, p: trap_reasons(r.url)),
           issue_type="Warning", priority="Medium",
           description="The URL looks like part of an endless URL space: repeated folders (a relative-link loop), "
                       "calendar dates, page-builder AJAX parameters, very deep pagination or many filters "
                       "combined. Crawlers can follow such links forever.",
           fix="Fix relative links (start them with /), noindex and block calendar dates far in the future, and "
               "block filter / AJAX parameters in robots.txt (see the Parameters tab)."),
    Filter("soft_404_server", "Unknown URLs Answer 200 (Soft 404)", _probe("soft_404"),
           issue_type="Issue", priority="High",
           description="A made-up address on the site answered 200 instead of 404, so every mistyped or invented "
                       "URL becomes another page search engines may index (soft 404s).",
           fix="Make the server (or the CMS 404 template) answer HTTP 404 for addresses that do not exist."),
    Filter("infinite_url_space", "Infinite URL Space", _probe("infinite_space"),
           issue_type="Issue", priority="High",
           description="A made-up address under a real page answered 200 with that same page. Its relative links "
                       "then resolve one folder deeper each time, so crawlers find endless copies.",
           fix="Answer 404 for unknown sub-paths (check the CMS route / rewrite rules) and make links absolute."),
    Filter("unknown_redirect_home", "Unknown URLs Redirect Home", _probe("redirect_home"),
           issue_type="Warning", priority="Medium",
           description="Addresses that do not exist are redirected to the home page. Google treats that as a soft "
                       "404, and visitors who followed a broken link land somewhere unexpected.",
           fix="Answer 404 (with a helpful not-found page) instead of redirecting to the home page."),
    Filter("unknown_server_error", "Unknown URLs Cause Server Error", _probe("server_error"),
           issue_type="Warning", priority="Low",
           description="An address that does not exist makes the server fail (5xx) instead of answering 404, which "
                       "looks like an outage to search engines.",
           fix="Make the 404 handling robust so unknown URLs answer 404."),
]


def _problems(r, site) -> str:
    if isinstance(r, SiteRow):
        return (r.data or {}).get("note", "")
    names = []
    for f in FILTERS:
        try:
            if f.test(r, site):
                names.append(f.name)
        except Exception:
            continue
    return "; ".join(names)


def _cell(fn):
    return lambda r, site: "" if isinstance(r, SiteRow) else fn(r, site, seo(r))


COLUMNS = [
    ADDRESS,
    STATUS_CODE,
    Column("problems", "Problems", _problems),
    Column("staging_hosts", "Staging / Private Hosts", _cell(lambda r, site, p: ", ".join(staging_links(site, p)))),
    Column("dead_anchors", "Broken Anchors", _cell(lambda r, site, p: ", ".join("#" + f for f in p.get("dead_fragments") or []))),
    Column("http_links", "Links To HTTP", _cell(lambda r, site, p: len(http_internal_links(r, p))), "num"),
    Column("malformed", "Malformed Links",
           _cell(lambda r, site, p: "; ".join(f"{m.get('raw')} ({m.get('reason')})" for m in (p.get("malformed_hrefs") or [])[:5]))),
    Column("size_kb", "Size (KB)", _cell(lambda r, site, p: round(int(p.get("size_bytes") or 0) / 1024, 1)), "num"),
    Column("content_encoding", "Content-Encoding", _cell(lambda r, site, p: _headers(r).get("content-encoding", ""))),
    Column("cache_control", "Cache-Control", _cell(lambda r, site, p: _headers(r).get("cache-control", ""))),
    Column("doctype", "Doctype", _cell(lambda r, site, p: "" if p.get("has_doctype") is None else
                                       ("Yes" if p.get("has_doctype") else "No"))),
    Column("charset", "Charset", _cell(lambda r, site, p: p.get("charset") or "")),
    Column("dom_nodes", "DOM Elements", _cell(lambda r, site, p: int(p.get("dom_nodes") or 0) or ""), "num"),
    Column("canonical_host", "Canonical Host", _cell(lambda r, site, p: canonical_host_change(r, p))),
    Column("trap_pattern", "Trap Pattern", _cell(lambda r, site, p: ", ".join(trap_reasons(r.url)))),
    Column("indexability", "Indexability", lambda r, site: "" if isinstance(r, SiteRow) else indexability_label(site, r)),
]

PROBE_FILTERS = {"soft_404_server", "infinite_url_space", "unknown_redirect_home", "unknown_server_error"}
EXTRACTOR_FILTERS = {"dead_anchors", "malformed_links", "javascript_links", "missing_doctype", "no_charset",
                     "non_utf8_charset", "large_dom"}


def skip_reason(site, filter_id: str) -> str:
    """Why a filter of this tab is "Not checked" ("" when it ran)."""
    if filter_id in PROBE_FILTERS:
        reason = not_run_reason(getattr(site, "trap_probe", None), "random-URL probe")
        if reason:
            return reason
        rep = _probe_report(site) or {}
        if filter_id == "infinite_url_space" and not any(p.get("kind") == "nested" for p in rep.get("probes") or []):
            return "No crawled page to probe under, so the nested probe did not run."
        return ""
    if filter_id in EXTRACTOR_FILTERS:
        pages = content_rows(site)
        if pages and all(seo(r).get("has_doctype") is None for r in pages):
            return "These pages were read by an older version of the crawler; run a new crawl to check this."
    if filter_id in ("no_compression", "no_cache_control"):
        pages = content_rows(site)
        if pages and not any(_headers(r).get("content-type") for r in pages):
            return "The response headers of the pages were not recorded (pages loaded through the browser)."
    return ""


TAB = register(Tab(id="hygiene", name="Hygiene", order=250, columns=COLUMNS, filters=FILTERS, rows=hygiene_rows))
TAB.skip_reason = skip_reason
