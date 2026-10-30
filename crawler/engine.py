import codecs
import gc
import heapq
import http
import io
import os
import time
import re
import threading
from collections import Counter, deque
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_futures
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Optional, Callable, Pattern, Tuple
from urllib.parse import urljoin, urlparse
import requests
from requests.adapters import HTTPAdapter
import urllib3
from bs4 import BeautifulSoup
from urllib3.util.retry import Retry

from .config import (CrawlConfig, DEFAULT_USER_AGENT, MAX_CONCURRENCY, ROBOTS_TOKENS, effective_user_agent,
                     normalize_options, sync_scope)
from .url_utils import (
    DEFAULT_RULES,
    move_url_credentials,
    normalize_url,
    get_domain,
    clean_hostname,
    is_internal_url,
    is_parsable_html_url,
    is_excluded_path,
    is_login_url,
    is_unsafe_url,
    join_url,
    redirected_to_login,
    registrable_domain,
    rules_from_config,
    set_active_rules,
    LOGOUT_EXCLUDE_PATTERNS,
)
from .source_store import SourceStore
from .page_parser import PageParser, release_soup
from .sitemap_parser import SitemapParser
from .subdomains import check_extra_hosts, extra_host_urls
from .dynamic_extractor import DynamicExtractor, UNSAFE_LABEL
from .auth import AuthHandler, shows_login_form
from .browser import SharedBrowser, jar_to_cookies, memory_headroom_mb
from .robots import RobotsPolicy
from .issues import classify_result, diagnose, found_in_code, CODE_LINK_TAGS
from .net_guard import (BURST_LABEL, TRAP_LABELS, WAF_SCAN_CHARS, WAF_STATUSES, HostThrottle, SiteSession,
                        challenge_header, classify_error, classify_error_text, consistent_error_type, detect_waf,
                        error_message,
                        retry_after_seconds, root_cause_label, trap_example_key, trap_reason)
from .seo.extract import ExtractOptions, extract_page, extract_headers_only, merge_cookies, on_cdn, parse_html
from .seo.pdf_meta import TEXT_KEY_SUFFIX, pdf_info, pdf_title

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# seo["resources"] type -> link_tag recorded for a resource URL ("script" is taken by inline-script routes)
RESOURCE_LINK_TAGS = {"img": "img", "script": "script-src", "link": "stylesheet", "iframe": "iframe",
                      "frame": "iframe", "source": "source", "video": "video", "audio": "audio", "embed": "embed"}
FRAME_TAGS = ("iframe",)
_CHARSET_META = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_.:-]+)""", re.I)
# Client-side app mount points, with any attribute quoting: React/Vite "root", Next.js "__next", Vue/Vite "app",
# Nuxt "__nuxt" (and its state script), Quasar "q-app", Svelte "svelte", Angular <app-root> / ng-version
# Written "i(?<=\bi)d" rather than "\bid" (same matches): an alternative that starts with a letter instead of \b
# lets the regex engine skip ahead to the few characters that can start a match, about twice as fast on big pages
_SPA_MOUNT = re.compile(r"""i(?<=\bi)d\s*=\s*["']?(?:root|__next|app|__nuxt|q-app|svelte)["'\s>]|<app-root[\s>]|"""
                        r"""n(?<=\bn)g-version\s*=|d(?<=\bd)ata-v-app\b|window\.__NUXT__|__NUXT_DATA__""", re.I)


def looks_like_spa(html_text: str) -> bool:
    """The page is a JavaScript app shell (React, Next.js, Vue, Nuxt, Angular, Svelte mount points)."""
    return (bool(_SPA_MOUNT.search(html_text)) or "_next/static" in html_text
            or "window.__INITIAL_STATE__" in html_text)


# Menu openers in server-built pages (dropdowns, toggles, account menus): on a logged-in crawl, a page whose
# menus were not seen on an earlier page is opened in Chrome once, so its click-only menus are explored
_MENU_OPENERS = ("[aria-haspopup], [aria-expanded], [data-toggle], [data-bs-toggle], [class*=toggle], "
                 "[class*=dropdown], [class*=menu-trigger], [class*=menu-button]")
_NAV_CLICKABLES = "button, [role=button], summary, [onclick], [style*=cursor]"
_UNSAFE_LABEL_RE = re.compile(UNSAFE_LABEL, re.I)
MENU_RENDER_LIMIT = 40    # logged-in, server-built pages opened in Chrome to explore their menus, per crawl
SESSION_CHECK_SECONDS = 10.0   # a "has the login ended?" probe answer is reused this long
UNSAFE_LOG_LIMIT = 5      # skipped sign-out / data-changing links logged one by one
LOGGED_OUT_NOTE = ("Logged out here: the crawler's login session had ended, so the site answered as it does for "
                   "a visitor who is not logged in.")
FINISHED_STATES = ("completed", "stopped", "auth_failed")
INDEX_MIN_INTERVAL = 3.0  # seconds between site-index rebuilds while a crawl is running
# While crawling, wait at least this many times the last rebuild's duration before the next one, so
# rebuilding (which holds the GIL) never takes more than about a fifth of the crawl's time
INDEX_COST_FACTOR = 4.0
MAX_LIMITATIONS = 200     # distinct limitation notes kept per crawl
PDF_TITLE_MAX_BYTES = 20 * 1024 * 1024  # PDFs larger than this are not downloaded to read their title
# Accessibility audit (audit_hook): why a page has no axe result
AUDIT_PAGE_LIMIT = "Over the accessibility audit page limit"
AUDIT_TIME_LIMIT = "The accessibility audit time limit was reached"
AUDIT_NOT_ATTACHED = "The accessibility audit could not run: Chrome was not available to this crawl."
# P4: a redirect chain (HTTP, meta refresh, Refresh header, JavaScript) longer than CrawlConfig.max_redirects stops;
# the last URL keeps its own status (a 301 stays a 301) and carries this reason, its target is not requested
REDIRECT_LIMIT = "Redirect limit reached"
# P4 / P8: follow-ups of a page that loaded besides its links: (seo key or kind, discovery source, link tag, region)
REFRESH_SOURCES = {"meta_refresh": "Meta Refresh", "http_refresh": "HTTP Refresh", "js_redirect": "JavaScript Redirect"}
# P17: Configuration > Spider options. The follow key of each refresh source, the crawl_types key of each resource type,
# and the plain label of every reason a URL was not crawled because of an option or a limit
REFRESH_FOLLOW = {"Meta Refresh": "meta_refresh", "HTTP Refresh": "http_refresh", "JavaScript Redirect": "js_redirects"}
RESOURCE_CRAWL_TYPE = {"image": "images", "img": "images", "css": "css", "script": "js", "js": "js", "media": "media",
                       "video": "media", "audio": "media", "font": "fonts", "iframe": "iframes", "frame": "iframes"}
OPTION_LABELS = {
    "type_images": "Not followed: crawling of images is off", "type_css": "Not followed: crawling of CSS is off",
    "type_js": "Not followed: crawling of JavaScript is off", "type_media": "Not followed: crawling of media is off",
    "type_fonts": "Not followed: crawling of fonts is off", "type_iframes": "Not followed: crawling of iframes is off",
    "follow_canonicals": "Not followed: following canonicals is off",
    "follow_pagination": "Not followed: following rel next / prev is off",
    "follow_hreflang": "Not followed: following hreflang is off",
    "follow_meta_refresh": "Not followed: following meta refresh is off",
    "follow_http_refresh": "Not followed: following the Refresh header is off",
    "follow_js_redirects": "Not followed: following JavaScript redirects is off",
    "follow_iframes": "Not followed: following iframes is off",
    "follow_mobile_alternate": "Not followed: following mobile alternate links is off",
    "nofollow": "Not followed: the link is nofollow and 'Follow internal nofollow' is off",
    "limit_per_depth": "Left out: the limit of URLs per crawl depth was reached",
    "limit_folder_depth": "Left out: deeper than the folder depth limit",
    "limit_query_strings": "Left out: more query string parameters than the limit",
    "limit_per_host": "Left out: the limit of URLs per host was reached",
}
MAX_NOT_FOLLOWED_LISTED = 5000
# Server memory guard: once the container's free memory falls below this (MB), no more URLs are queued, the
# same way the Max URLs limit works, so the reports can still be built (a PC has no container limit: no effect)
MIN_FREE_MB = int(os.environ.get("CRAWLER_MIN_FREE_MB", "400") or 0)


def free_memory_mb() -> Optional[int]:
    """Free memory for this crawl in MB: the container's headroom (cgroup limit, Render / Docker), else the
    machine's: on Windows the commit headroom (GlobalMemoryStatusEx ullAvailPageFile: what can still be allocated
    before allocations fail; free physical memory alone is often low on a busy PC that has plenty of page file),
    on Linux /proc/meminfo MemAvailable; else None (P24)."""
    free = memory_headroom_mb()
    if free is not None:
        return free
    if os.name == "nt":
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullAvailPageFile // 2**20)
        except Exception:
            return None
        return None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None
MEMORY_CHECK_EVERY = 50   # queued URLs between two free-memory readings
MAX_RELOGINS = 2          # times per crawl the crawler logs in again after the site ended its session
STOP_GRACE_SECONDS = 60   # after Stop, how long the crawl waits for pages still being fetched or rendered; a
                          # worker stuck longer (a hung browser call) is left behind so the crawl can finish
BROWSER_IDLE_CLOSE = 60   # seconds the crawl's browser stays open after the crawl (screenshots, re-checks)
MAX_THROTTLE_REQUEUES = 3  # times a URL answering 429/503 goes back in the queue before its answer is kept
THROTTLE_LOG_LIMIT = 20   # 429/503 log lines per crawl (the rest are counted in the end summary)
TRAP_LOG_PER_REASON = 5   # skipped crawl-trap URLs logged per reason (all are counted)
MAX_TRAPS_LISTED = 5000   # skipped crawl-trap URLs returned by skipped_urls() (the count is always exact)
# crawler.phase while each post-crawl check runs (crawler/seo/postcrawl.py step names)
POST_CRAWL_PHASES = {"robots_audit": "checking robots.txt", "trap_probe": "probing URL traps",
                     "bot_check": "checking AI crawlers"}
POST_CRAWL_ATTRS = {"robots_audit": "robots_audit", "trap_probe": "trap_probe", "bot_check": "bot_report"}
SNAPSHOT_PHASE = "saving snapshot"
# siteone-crawler's loop guard: URLs linked from an error page and ending in a path segment that already gave this
# many 4xx/5xx answers are skipped (links on pages that loaded never are: a 404 is always reported). Login /
# firewall / rate-limit answers (401, 403, 407, 429, 503) do not count: they are not loops.
LOOP_GUARD_LIMIT = 5
LOOP_GUARD_IGNORED = frozenset({401, 403, 407, 429, 503})
LOOP_GUARD_SKIP_NAMES = frozenset({"index.html", "index.htm", "index"})
# A script name answers for many different pages (product.php?id=1, ?id=2): its key keeps the query
_SCRIPT_NAME = re.compile(r"\.(?:php\d?|aspx?|jsp|cfm|cgi|pl|do|action)$", re.I)
MAX_OUT_OF_SCOPE = 100000   # internal URLs remembered as "outside the crawl scope" (the count says "at least")
SITEMAP_WAIT_SECONDS = 300  # once every linked page is crawled, how long the crawl waits for the sitemap probe


# Garbage collection while crawling: Python's default (a young-generation pass every 2,000 new objects) promoted each
# page's parse tree into the old generation while the page was being processed, which then triggered ~50 full
# collections over a 3,000-page crawl (measured 14 s of a 132 s crawl). A larger young generation lets those objects
# die young: GC time 14 s -> 3.5 s, same peak memory. Process-wide, so it is set while at least one crawl (or index
# build) runs and restored when the last one ends.
GC_YOUNG_THRESHOLD = 20000
_gc_lock = threading.Lock()
_gc_users = 0
_gc_saved: Optional[Tuple[int, ...]] = None


@contextmanager
def crawl_gc():
    """Raises the young-generation GC threshold to GC_YOUNG_THRESHOLD for the duration of the block (nestable,
    thread-safe); the previous thresholds come back when the last block ends, unless someone changed them."""
    global _gc_users, _gc_saved
    with _gc_lock:
        if _gc_users == 0:
            current = gc.get_threshold()
            _gc_saved = current if current[0] < GC_YOUNG_THRESHOLD else None
            if _gc_saved is not None:
                gc.set_threshold(GC_YOUNG_THRESHOLD, *current[1:])
        _gc_users += 1
    try:
        yield
    finally:
        with _gc_lock:
            _gc_users -= 1
            if _gc_users == 0 and _gc_saved is not None:
                if gc.get_threshold()[0] == GC_YOUNG_THRESHOLD:
                    gc.set_threshold(*_gc_saved)
                _gc_saved = None


def _cpu_slots() -> int:
    """Workers that may parse and analyse a page at the same time (CRAWLER_CPU_SLOTS, default 1). Python runs one
    thread at a time anyway; 10-50 workers fighting over it made every page cost 2-3.5 times the CPU (measured: a
    67 KB page parsed in 14.5 ms alone, 52 ms with 4+ workers parsing at once), so the others wait on the gate
    while their requests are under way."""
    try:
        return max(1, int(os.environ.get("CRAWLER_CPU_SLOTS", "1") or 1))
    except ValueError:
        return 1


# Request headers a user may not set (the HTTP library manages them)
BLOCKED_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}$")
# Response headers kept for external links (cache lifetime and size checks of other sites' resources; the
# Refresh header for External Redirection (HTTP Refresh))
EXTERNAL_KEPT_HEADERS = ("cache-control", "expires", "content-type", "content-length", "last-modified", "etag",
                         "age", "content-encoding", "refresh")
# An external page that answers 2xx with HTML is read up to this many bytes (streamed, then closed) to find a
# <meta http-equiv="refresh"> (External Redirection (Meta Refresh)); the rest of the body is never downloaded
EXTERNAL_BODY_BYTES = 64 * 1024
_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_HTTP_EQUIV_REFRESH = re.compile(r"""http-equiv\s*=\s*["']?\s*refresh\b""", re.I)
_CONTENT_ATTR = re.compile(r"""\bcontent\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_REFRESH_TARGET = re.compile(r"""url\s*=\s*['"]?([^'"]+)""", re.I)


def meta_refresh_in(html_text: str) -> str:
    """The content of the first <meta http-equiv="refresh"> in a piece of HTML ("" when there is none)."""
    import html as html_lib
    for m in _META_TAG.finditer(html_text or ""):
        tag = m.group(0)
        if not _HTTP_EQUIV_REFRESH.search(tag):
            continue
        c = _CONTENT_ATTR.search(tag)
        if c:
            value = next((g for g in c.groups() if g is not None), "")
            return " ".join(html_lib.unescape(value).split())
    return ""


def refresh_target(content: str, base_url: str) -> str:
    """Absolute, normalized target of a refresh value such as "0; url=/new" ("" when it names none)."""
    m = _REFRESH_TARGET.search(content or "")
    if not m:
        return ""
    try:
        return normalize_url(join_url(base_url, m.group(1).strip())) or ""
    except Exception:
        return ""
LIST_META = {"anchor_text": "", "tag": "list", "region": "URL list", "selector": "", "html": ""}


def loop_guard_key(url: str) -> str:
    """siteone-crawler's "basename": the URL's last path segment (trailing slash ignored), with the query when
    the query itself holds a path or the segment is a script (product.php?id=4: each id is its own page);
    '' for the root and index pages (never guarded)."""
    try:
        p = urlparse(url)
    except Exception:
        return ""
    path = (p.path or "").rstrip("/")
    name = path.rsplit("/", 1)[-1] if path else ""
    if not name or name.lower() in LOOP_GUARD_SKIP_NAMES:
        return ""
    query = p.query or ""
    if query and ("/" in query or "%2f" in query.lower() or _SCRIPT_NAME.search(name)):
        return f"{name}?{query}"
    return name


def _plausible_host(url: str) -> bool:
    """The URL names a real-looking host: a dotted name, localhost or an IP address."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return bool(host) and (host == "localhost" or ":" in host or
                           bool(re.match(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$", host, re.I)))


def scope_folder(url: str) -> str:
    """The folder a Subfolder crawl stays in: the start URL's path up to its last "/", or the path itself plus
    "/" when its last segment has no file extension (/blog -> /blog/, /blog/post.html -> /blog/)."""
    path = urlparse(url).path or "/"
    if path.endswith("/"):
        return path
    last = path.rsplit("/", 1)[-1]
    return path + "/" if "." not in last else path[:len(path) - len(last)]


def proxy_url(proxy: Optional[dict]) -> str:
    """http://user:pass@host:port for requests ('' when no proxy is enabled). Never log the result."""
    if not isinstance(proxy, dict) or not proxy.get("enabled") or not str(proxy.get("host") or "").strip():
        return ""
    from urllib.parse import quote
    host = str(proxy.get("host")).strip()
    host = re.sub(r"^[a-z]+://", "", host, flags=re.I).rstrip("/")
    try:
        port = int(proxy.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    login = ""
    if str(proxy.get("username") or ""):
        user = quote(str(proxy.get("username")), safe="")
        login = user + ":" + quote(str(proxy.get("password") or ""), safe="") + "@"
    return f"http://{login}{host}" + (f":{port}" if port and ":" not in host.rsplit("]", 1)[-1] else "")


def clean_request_headers(headers: Any) -> Tuple[Dict[str, str], List[str]]:
    """({name: value} safe to send, problems). Names must be HTTP tokens; values may not contain line breaks;
    Host / Content-Length / Transfer-Encoding / Connection are left to the HTTP library. Problems name the
    header, never its value."""
    out: Dict[str, str] = {}
    problems: List[str] = []
    items = headers.items() if isinstance(headers, dict) else (
        ((h.get("name"), h.get("value")) for h in headers or () if isinstance(h, dict)))
    for name, value in items:
        name = str(name or "").strip()
        value = "" if value is None else str(value).strip()
        if not name:
            continue
        if not _HEADER_NAME.match(name):
            problems.append(f"HTTP header name {name[:40]!r} is not valid and was not sent.")
        elif name.lower() in BLOCKED_HEADERS:
            problems.append(f"The {name} header is set by the crawler itself and was not changed.")
        elif "\r" in value or "\n" in value:
            problems.append(f"The {name} header's value has a line break and was not sent.")
        else:
            out[name] = value
    return out, problems


def _serverless() -> bool:
    """Running in a serverless function (Vercel / AWS Lambda) with a hard time limit."""
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

# User-Agents for checking links to other sites. Bot walls judge the User-Agent: a browser one sent by a
# non-browser HTTP client is refused by some sites (Facebook answers 400, Shopify a 302 to a challenge),
# a crawler one by others (Cloudflare-protected sites answer 403). The checker asks as a link checker
# first, as Screaming Frog does with its own agent, and only calls a link broken (4xx/5xx/no answer) when
# a browser User-Agent fails too.
EXTERNAL_CHECK_USER_AGENT = "Mozilla/5.0 (compatible; SEO-Crawler-LinkCheck/1.0)"
_BOT_UA = re.compile(r"bot|crawl|spider|compatible;", re.I)


def external_check_user_agent(user_agent: str) -> str:
    """The crawl's User-Agent when it already names a crawler (Googlebot, bingbot, a custom bot),
    else EXTERNAL_CHECK_USER_AGENT."""
    return user_agent if user_agent and _BOT_UA.search(user_agent) else EXTERNAL_CHECK_USER_AGENT


def external_fallback_user_agent(user_agent: str) -> str:
    """Browser User-Agent for the second try: the crawl's own when it is a browser one, else Chrome's."""
    return user_agent if user_agent and not _BOT_UA.search(user_agent) else DEFAULT_USER_AGENT


def _menu_trigger_labels(soup) -> Set[str]:
    """Labels of the menu openers on a server-built page that are not links: dropdown/toggle elements anywhere
    and buttons, clickable divs and <summary> in navigation areas (nav, header, aside, menus). Labels that
    sign out or change data are left out (they are never clicked)."""
    if soup is None:
        return set()
    try:
        found = list(soup.select(_MENU_OPENERS))
        found += [el for el in soup.select(_NAV_CLICKABLES)
                  if el.find_parent(["nav", "header", "aside"]) is not None
                  or el.find_parent(attrs={"role": re.compile(r"navigation|menu", re.I)}) is not None]
    except Exception:
        return set()
    labels: Set[str] = set()
    for el in found[:300]:
        if el.name in ("input", "select", "option", "textarea", "form", "html", "body") or \
                el.find_parent("form") is not None:
            continue
        href = str(el.get("href") or "").strip().lower() if el.name == "a" else ""
        if href and href != "#" and not href.startswith(("#!", "javascript:")):
            continue   # a real link: crawled anyway
        label = str(el.get("aria-label") or el.get("title") or "").strip() or \
            re.sub(r"\s+", " ", el.get_text(" ") or "").strip()
        label = label[:60] or "class:" + " ".join(el.get("class") or [])[:60]
        if label != "class:" and not _UNSAFE_LABEL_RE.search(label):
            labels.add(label)
    return labels


def _shown_title(seo: dict) -> str:
    """The title PageParser.extract_title gives, from a page's PageData: <title>, og:title, twitter:title, first
    <h1>. Used for pages whose main values were read from the rendered HTML (render every page)."""
    h1 = next((h for h in seo.get("h1") or [] if str(h or "").strip()), "")
    for value in ((seo.get("titles") or [""])[0], (seo.get("og") or {}).get("title"),
                  (seo.get("twitter") or {}).get("title"), h1):
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""


def _post_form_actions(soup, base_url: str) -> Set[str]:
    """Normalized action URLs of the page's forms that do not use GET (login, contact, comment and cart forms
    post their data). A visitor never opens them as a page, so the crawler does not either: requested with GET
    they answer 404/405 and would be reported as broken links."""
    out: Set[str] = set()
    if soup is None:
        return out
    try:
        forms = soup.find_all("form")
    except Exception:
        return out
    for form in forms:
        method = str(form.get("method") or "get").strip().lower() or "get"
        if method != "get" and form.get("action") is not None:
            norm = normalize_url(str(form.get("action")), base_url=base_url)
            if norm:
                out.add(norm)
    return out


def _followed_targets(result) -> List[str]:
    """Every URL a crawled result points the crawler at: its links, resources, link elements
    (canonical, next/prev, hreflang, AMP, mobile), meta refresh, Refresh header, JavaScript redirect, hreflang in
    the Link header, form action and redirect target."""
    seo = result.seo or {}
    out: List[str] = [link.get("url") or "" for link in seo.get("links") or ()]
    out += [res.get("url") or "" for res in seo.get("resources") or ()]
    out += list(seo.get("canonicals") or ()) + list(seo.get("rel_next_all") or ()) + list(seo.get("rel_prev_all") or ())
    out += [h.get("url") or "" for h in seo.get("hreflang") or ()]
    out += [h.get("url") or "" for h in seo.get("http_hreflang") or ()]
    out += [seo.get("amphtml") or "", seo.get("mobile_alternate") or "", seo.get("meta_refresh_url") or "",
            seo.get("http_refresh_url") or "", js_redirect_target(seo)]
    out += [f.get("action") or "" for f in seo.get("forms") or ()]
    if result.redirect_destination:
        out.append(normalize_url(result.redirect_destination) or "")
    return out


def js_redirect_target(seo: Optional[Dict[str, Any]]) -> str:
    """Normalized address a rendered page's script sent the browser to ("" when it did not, or it was not rendered)."""
    rendered = (seo or {}).get("rendered")
    if not isinstance(rendered, dict) or not rendered.get("js_redirect"):
        return ""
    target = str(rendered.get("js_redirect_url") or "")
    return (normalize_url(target) or "") if target else ""


def crawl_targets(result) -> List[str]:
    """The URLs the crawler itself follows from a result: what a page that loaded (2xx) points at (_followed_targets
    without the forms that post their data and the AMP link, which the crawler never opens), only the target of a
    redirect (3xx), nothing from an error page or a URL that gave no answer."""
    cat = result.category
    if cat == "3xx" and result.redirect_destination:
        return [normalize_url(result.redirect_destination) or ""]
    if cat != "2xx":
        return []
    seo = result.seo or {}
    linked = {link.get("url") for link in seo.get("links") or ()}
    skip = {f.get("action") for f in seo.get("forms") or ()
            if str(f.get("method") or "get").lower() != "get" and f.get("action")} | {seo.get("amphtml") or ""}
    skip -= linked                     # also an ordinary link on the page: followed as a link
    return [t for t in _followed_targets(result) if t and t not in skip]


def link_depths(results: List["CrawlResult"]) -> Dict[str, int]:
    """Crawl depth as Screaming Frog counts it: the fewest clicks from the start URL (depth 0), found by a
    breadth-first walk over what each crawled URL links to, whatever order the pages were fetched in.
    URLs no link reaches (listed only in an XML sitemap) keep the depth they were queued at, and pass
    it on to anything only they link to."""
    known = {r.url: r for r in results if getattr(r, "url", None)}
    adj: Dict[str, Set[str]] = {
        url: {t for t in _followed_targets(r) if t in known and t != url} for url, r in known.items()}
    depth: Dict[str, int] = {url: 0 for url, r in known.items() if not (r.depth or 0)}
    frontier = deque(depth)
    while frontier:
        url = frontier.popleft()
        for target in adj[url]:
            if target not in depth:
                depth[target] = depth[url] + 1
                frontier.append(target)
    heap = [(int(r.depth or 0), url) for url, r in known.items() if url not in depth]
    heapq.heapify(heap)
    while heap:
        d, url = heapq.heappop(heap)
        if url in depth:
            continue
        depth[url] = d
        for target in adj[url]:
            if target not in depth:
                heapq.heappush(heap, (d + 1, target))
    return depth


def apply_link_depths(results: List["CrawlResult"]) -> int:
    """Sets every result's depth to its link_depths() value; returns how many changed."""
    by_url = link_depths(results)
    changed = 0
    for r in results:
        d = by_url.get(getattr(r, "url", None))
        if d is not None and d != r.depth:
            r.depth = d
            changed += 1
    return changed


class RateLimiter:
    """Thread-safe token bucket: at most `rate` acquisitions per second across all workers."""

    def __init__(self, rate: float):
        self.rate = float(rate)
        self.capacity = 1.0
        self._tokens = 1.0
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, stop_event: Optional[threading.Event] = None) -> bool:
        """Blocks until a request may start; False when the crawl was stopped while waiting."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self.rate
            if stop_event is not None:
                if stop_event.wait(min(wait, 0.5)):
                    return False
            else:
                time.sleep(min(wait, 0.5))


def _compile_patterns(patterns: List[str], label: str, log: Callable[[str], None]) -> List[Pattern]:
    out = []
    for p in patterns or []:
        p = (p or "").strip()
        if not p:
            continue
        try:
            out.append(re.compile(p, re.I))
        except re.error as e:
            log(f"Ignoring invalid {label} regex {p!r}: {e}")
    return out


def _fix_encoding(resp) -> None:
    """Picks the body's character set the way browsers do (requests would assume ISO-8859-1 for text/*
    without a charset): the Content-Type charset, else a byte-order mark, else <meta charset> in the
    first 4 KB, else UTF-8 when the bytes are valid UTF-8 and windows-1252 when they are not.
    A meta tag declaring UTF-16 means UTF-8: a tag readable as ASCII cannot really be UTF-16."""
    ctype = (resp.headers.get("content-type") or "").lower()
    if "charset" in ctype:
        return
    body = resp.content or b""
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        resp.encoding = "utf-16"
        return
    m = _CHARSET_META.search(body[:4096])
    if m:
        enc = m.group(1).decode("ascii", "ignore").strip().lower()
        if enc.replace("_", "-").replace("utf16", "utf-16").startswith("utf-16"):
            enc = "utf-8"
        try:
            codecs.lookup(enc)
            resp.encoding = enc
            return
        except LookupError:
            pass
    try:
        body.decode("utf-8")
        resp.encoding = "utf-8"
    except UnicodeDecodeError as e:
        # A body cut at the page-size limit can end inside a multi-byte character: still UTF-8
        resp.encoding = "utf-8" if e.start >= len(body) - 3 else "cp1252"


def redirect_location(resp) -> Optional[str]:
    """The Location header of a redirect answer: any 3xx that carries one except 304 Not Modified. requests'
    is_redirect knows only 301/302/303/307/308, but a 300, 305 or 306 with a Location still names where it
    sends the visitor, and Screaming Frog reports (and follows) it as the Redirect URL."""
    code = getattr(resp, "status_code", 0) or 0
    if not 300 <= code < 400 or code == 304:
        return None
    location = (resp.headers.get("location") or "").strip()
    return location or None


def _read_body(resp, limit: int) -> bool:
    """Downloads a streamed response body (decompressed) into resp.content, at most `limit` bytes
    (0 = no limit). Returns True when the body was cut at the limit."""
    data = bytearray()
    cut = False
    try:
        for chunk in resp.iter_content(65536):
            data += chunk
            if limit and len(data) > limit:
                del data[limit:]
                cut = True
                break
    finally:
        resp.close()
    resp._content = bytes(data)
    resp._content_consumed = True
    return cut


def _lower_headers(headers) -> Dict[str, str]:
    try:
        return {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    except Exception:
        return {}

# Client-side route patterns found in JS bundles: router configs / nav arrays and programmatic navigation
BUNDLE_ROUTE_PATTERNS = (
    re.compile(r"""(?:path|to|href|pathname|route|link)["']?\s*:\s*["'`](/[^"'`\s#?]*)["'`]"""),
    re.compile(r"""\.(?:push|replace|prefetch|navigate)\(\s*["'`](/[^"'`\s#?]*)["'`]"""),
)
NEXT_APP_CHUNK = re.compile(r"/_next/static/chunks/app/(.*)/page-[0-9a-f]+\.js$")
_JS_OBJECT = re.compile(r"\{(?:[^{}]|\$\{[^{}]{0,80}\}){0,600}\}")
_JS_HREF_KEY = re.compile(r"""\bhref["']?\s*:\s*["'`](/[^"'`\s#?]*)["'`]""")


def bundle_route_strings(text: str) -> List[str]:
    """Route strings in a JavaScript bundle. In an object with an href and another route key (a menu item
    such as {path: "/planner", href: `/workspace/${id}/planner`}) the href is where the menu really goes, so
    the other key's value is dropped unless the same path also appears on its own elsewhere."""
    found = [(m.start(1), m.group(1)) for pat in BUNDLE_ROUTE_PATTERNS for m in pat.finditer(text)]
    shadowed: Set[int] = set()
    for obj in _JS_OBJECT.finditer(text):
        body = obj.group(0)
        hrefs = set(_JS_HREF_KEY.findall(body))
        if hrefs:
            for m in BUNDLE_ROUTE_PATTERNS[0].finditer(body):
                if m.group(1) not in hrefs:
                    shadowed.add(obj.start() + m.start(1))
    return [route for pos, route in found if pos not in shadowed]


_JS_LABEL_KEY = re.compile(r"""\b(?:label|title|name|text)["']?\s*:\s*["'`]([^"'`\\]{1,40})["'`]""")


def bundle_route_label(text: str, route: str) -> str:
    """The menu label written next to a route in a bundle ({label: "Planner", path: "/planner"}), or ''."""
    key = re.compile(r"""(?:path|to|href|pathname|route|link)["']?\s*:\s*["'`]""" + re.escape(route) + r"""["'`]""")
    for m in key.finditer(text):
        start = text.rfind("{", max(0, m.start() - 400), m.start())
        end = text.find("}", m.end(), m.end() + 400)
        if start < 0:
            continue
        label = _JS_LABEL_KEY.search(text, start, end if end > 0 else m.end())
        if label:
            return label.group(1).strip()
    return ""


def _label_key(text: str) -> str:
    """Comparable form of a menu label or path segment: "Team tasks" and "team-tasks" -> "teamtasks"."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


BUNDLE_ASSET_EXTENSIONS = ('.js', '.css', '.png', '.jpg', '.jpeg', '.svg', '.json', '.ico', '.woff', '.woff2',
                           '.ttf', '.map', '.wasm', '.webp', '.gif', '.mp4', '.txt', '.xml')
BUNDLE_SKIP_SEGMENTS = ('/api/', '/_next/', '/static/', '/assets/', '/engine.io', '/socket.io', '/oauth/', '/node_modules/')


# Status lines for answers without a usable reason phrase (HTTP/2-style answers, LinkedIn's "999 <none>")
NONSTANDARD_REASONS = {999: "Request Denied", 520: "Unknown Error", 521: "Web Server Is Down", 522: "Connection Timed Out",
                       523: "Origin Is Unreachable", 524: "A Timeout Occurred", 525: "SSL Handshake Failed",
                       526: "Invalid SSL Certificate", 530: "Origin DNS Error"}


def status_line(code: int, reason: Optional[str]) -> str:
    """ "404 Not Found": the server's reason phrase, or the standard one when the server sent none (or "<none>")."""
    text = (reason or "").strip()
    if not text or text.strip("<>").lower() in ("none", "null", "unknown"):
        try:
            text = http.HTTPStatus(int(code)).phrase
        except (ValueError, TypeError):
            text = NONSTANDARD_REASONS.get(code, "")
    return f"{code} {text}".strip()


@dataclass
class CrawlResult:
    url: str
    page_title: str = ""
    parent_url: str = ""
    http_status: str = "Pending"
    status_code: Optional[int] = None
    redirect_destination: Optional[str] = None
    depth: int = 0
    discovery_source: str = "Page Link"
    content_type: str = ""
    response_time_ms: int = 0
    error_message: Optional[str] = None
    # The element on the parent page that links here (see PageParser.element_meta)
    anchor_text: str = ""
    link_tag: str = ""
    link_region: str = ""
    link_selector: str = ""
    link_html: str = ""
    raw_href: str = ""
    link_line: int = 0  # line of the linking element in the parent page's HTML source
    # JavaScript file a code-only route was read from (link_tag 'bundle')
    source_file: str = ""
    server: str = ""
    # 'page' for HTML documents, 'file' for images, PDFs, scripts and other non-HTML responses
    resource_type: str = "page"
    blocked: bool = False
    # Working pages read from the same JavaScript file (set after the crawl for code-found 4xx)
    code_siblings: List[str] = field(default_factory=list)
    # For a code-found 4xx: the menu item a page shows greyed out (disabled) for this path
    # {"label", "title", "selector", "html", "page"}; empty when none was seen
    code_greyed: Dict[str, str] = field(default_factory=dict)
    # SEO PageData (crawler/seo/model.py) and the URL's own response headers (lowercase keys).
    # Deliberately left out of to_dict(): the API serves them per URL.
    seo: Optional[Dict[str, Any]] = field(default=None, repr=False, compare=False)
    response_headers: Dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    # Root cause of a failed or blocked request (crawler/net_guard.py ERROR_TYPES): dns, ssl, timeout, refused,
    # reset, too_many_redirects, other (no answer), waf (firewall / bot challenge), rate_limited (429/503 kept
    # after MAX_THROTTLE_REQUEUES retries). Never changes the category, which comes from the HTTP status.
    error_type: str = ""
    waf: str = ""                    # firewall / bot protection that answered (e.g. "Cloudflare")
    retries: int = 0                 # times the URL was put back in the queue after a 429/503 answer
    error_status: Optional[int] = field(default=None, repr=False, compare=False)  # status error_type was set for
    # List mode: position of the URL in the uploaded list (0-based, CrawlConfig.seed_urls); None for URLs the
    # listed ones led to (redirect targets, resources)
    list_index: Optional[int] = None

    @property
    def category(self) -> str:
        return classify_result(self)

    def set_error_type(self, error_type: str, waf: str = ""):
        """Records the root cause for the current status (a later re-check that changes the status drops it)."""
        self.error_type = error_type
        self.error_status = self.status_code
        if waf:
            self.waf = waf

    @property
    def root_cause_type(self) -> str:
        """error_type when it still matches the result's status; a result without an answer and without a
        recorded type (e.g. re-checked) is classified from its error message."""
        if self.blocked:
            return ""
        if self.error_type and self.status_code == self.error_status:
            return self.error_type
        if self.status_code is None:
            return classify_error_text(self.error_message or self.http_status) or "other"
        return ""

    @property
    def root_cause(self) -> str:
        return root_cause_label(self.root_cause_type, self.waf, self.status_code, self.retries)

    def to_dict(self) -> dict:
        return {
            "page_title": self.page_title,
            "url": self.url,
            "parent_url": self.parent_url,
            "http_status": self.http_status,
            "status_code": self.status_code,
            "category": self.category,
            "redirect_destination": self.redirect_destination or "-",
            "depth": self.depth,
            "discovery_source": self.discovery_source,
            "content_type": self.content_type or "-",
            "response_time_ms": self.response_time_ms,
            "error_message": self.error_message if (self.error_message and self.error_message != "-") else None,
            "anchor_text": self.anchor_text,
            "link_tag": self.link_tag,
            "link_region": self.link_region,
            "link_selector": self.link_selector,
            "link_html": self.link_html,
            "link_line": self.link_line,
            "source_file": self.source_file,
            "resource_type": self.resource_type,
            "found_in_code": found_in_code(self),
            "code_greyed": dict(self.code_greyed) if self.code_greyed else None,
            "issue": diagnose(self),
            "error_type": self.root_cause_type,
            "root_cause": self.root_cause,
            "waf": self.waf if self.root_cause_type == "waf" else "",
            "retries": self.retries,
            "list_index": self.list_index,
        }

@dataclass
class ExternalLink:
    url: str
    domain: str
    parent_url: str
    anchor_text: str = "-"
    discovery_source: str = "Page Link"
    validation_status: str = "Recorded (Not Followed)"
    # Set when external links are checked (config.check_external_links); None = not checked / no response
    status_code: Optional[int] = None
    http_status: str = ""
    # From the check's response: Content-Type (files the URL under HTML/JS/CSS/Images... in the External
    # tab), how long it took, and the Location of a 3xx answer
    content_type: str = ""
    response_time_ms: Optional[int] = None
    redirect_destination: Optional[str] = None
    size_bytes: Optional[int] = None   # Content-Length of the check's response (Images tab "Over 100 KB")
    # Root cause of a failed check (see CrawlResult.error_type); "waf" when a bot-challenge header answered
    error_type: str = ""
    # EXTERNAL_KEPT_HEADERS of the check's response (cache lifetime / size checks), lowercase keys
    response_headers: Dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    # <meta http-equiv="refresh"> of a 2xx HTML answer: None = the body was not read (not a 2xx HTML answer, or a
    # crawl made before refreshes were looked for), "" = read, none found; meta_refresh_url = its absolute target.
    # refresh_checked: this check kept the Refresh header and looked for a meta refresh (False on older crawls,
    # whose External Redirection (Meta / HTTP Refresh) filters are therefore "Not checked")
    meta_refresh: Optional[str] = None
    meta_refresh_url: str = ""
    refresh_checked: bool = False

    def to_dict(self) -> dict:
        error_type = consistent_error_type(self.error_type, self.status_code)
        return {
            "url": self.url,
            "domain": self.domain,
            "parent_url": self.parent_url,
            "anchor_text": self.anchor_text,
            "discovery_source": self.discovery_source,
            "validation_status": self.validation_status,
            "status_code": self.status_code,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "error_type": error_type,
            "root_cause": root_cause_label(error_type, status=self.status_code),
        }

class _SettledUrls:
    """URLs whose link origin is final: queued already and not known only from JavaScript.
    PageParser skips building link metadata for these; code-only URLs still get it, so a
    real link found later can replace their origin."""
    def __init__(self, crawler: "WebsiteCrawler"):
        self._crawler = crawler

    def __contains__(self, url: str) -> bool:
        return url in self._crawler.enqueued_urls and url not in self._crawler._code_only


class WebsiteCrawler:
    def __init__(
        self,
        config: CrawlConfig,
        on_result: Optional[Callable[[CrawlResult], None]] = None,
        on_external_link: Optional[Callable[[ExternalLink], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[dict], None]] = None
    ):
        self.config = config
        self.on_result = on_result or (lambda r: None)
        self.on_external_link = on_external_link or (lambda e: None)
        self.on_log = on_log or (lambda msg: None)
        self.on_progress = on_progress or (lambda p: None)
        # get_stats() is only worth computing per result when someone listens for progress
        self._wants_progress = on_progress is not None

        # Effective User-Agent (preset or custom string); robots.txt rules follow the group of the crawler the
        # preset pretends to be (bingbot, GPTBot, ClaudeBot...)
        preset = (getattr(config, "user_agent_preset", "") or "chrome").lower()
        config.user_agent = effective_user_agent(preset, config.user_agent)
        if preset in ROBOTS_TOKENS and config.robots_user_agent == "Googlebot":
            config.robots_user_agent = ROBOTS_TOKENS[preset]
        sync_scope(config)   # crawl_scope <-> include_subdomains, mode
        normalize_options(config)   # Build 4 options set after the config was built are cleaned too
        self.mode: str = config.mode
        self.crawl_scope: str = config.crawl_scope
        self.concurrency = max(1, min(MAX_CONCURRENCY, int(config.concurrency or 1)))
        self._include_res = _compile_patterns(getattr(config, "include_patterns", None), "include", self.log)
        self._exclude_res = _compile_patterns(getattr(config, "exclude_regex", None), "exclude", self.log)
        rate = float(getattr(config, "max_urls_per_second", 0) or 0)
        self.rate_limiter: Optional[RateLimiter] = RateLimiter(rate) if rate > 0 else None
        # Problems with the configuration (invalid rules left out), logged when the crawl starts
        self.config_notes: List[str] = []
        # A user name and password written in the start URL go to the site as HTTP Basic authentication (a header
        # for the crawled site only) and leave every URL: no crawled URL, export or log line shows the password
        move_url_credentials(config)

        # URL Rewriting: the rules apply to every URL found during this crawl (url_utils.set_active_rules makes
        # every module comparing this crawl's URLs use them); the start URL and listed URLs get only the
        # built-in normalisation
        self.url_rules, notes = rules_from_config(getattr(config, "url_rewrite", None))
        self.config_notes += notes
        set_active_rules(self.url_rules)
        # Custom searches / extractors compiled once; a pool shares strings repeated across pages (ids, terms)
        self.extract_options = ExtractOptions.from_config(config)
        self.config_notes += self.extract_options.problems
        # P11: hosts whose images and files count as the site's own (CDNs): their resources are queued and reported
        # as internal URLs. They are not "on the site" for the custom HTTP headers (on_site), which stay on the
        # crawled site's own hosts.
        self.cdn_hosts: Tuple[str, ...] = tuple(self.extract_options.cdn_hosts)

        # List mode: the listed URLs (normalized, first position kept) and their positions in seed_urls
        self._list_index: Dict[str, int] = {}
        self._list_invalid: List[Tuple[int, str]] = []
        self._list_lines = 0
        if self.mode == "list":
            for i, raw in enumerate(getattr(config, "seed_urls", None) or ()):
                text = str(raw or "").strip()
                if not text:
                    continue
                self._list_lines += 1
                if not re.match(r"^[a-z][a-z0-9+.-]*:", text, re.I) or re.match(r"^[^:/]+:\d+(?:/|$)", text):
                    text = "https://" + text   # "example.com/page" or "example.com:8080/page"
                norm = normalize_url(text, rules=DEFAULT_RULES)
                if not norm or not _plausible_host(norm):
                    self._list_invalid.append((i, str(raw).strip()[:2000]))
                elif norm not in self._list_index:
                    self._list_index[norm] = i
            if self._list_index and not (config.start_url or "").strip():
                config.start_url = next(iter(self._list_index))

        self.start_url = normalize_url(config.start_url, rules=DEFAULT_RULES) or config.start_url
        if self.mode == "list" and self._list_index:
            self.start_url = next(iter(self._list_index))
        # URLs the include patterns never filter out
        self._always_allowed: Set[str] = {self.start_url} | set(self._list_index)
        self.base_domain = get_domain(self.start_url)
        self.include_subdomains: bool = bool(config.include_subdomains)
        self.allowed_domains = {clean_hostname(d) for d in config.allowed_domains if d}
        if self.base_domain:
            self.allowed_domains.add(self.base_domain)
            self.allowed_domains.add(clean_hostname(self.base_domain))
        for listed in self._list_index:   # every host in the list counts as the site
            host = get_domain(listed)
            if host:
                self.allowed_domains.add(host)
        # Subfolder scope: the host and folder the crawl stays in
        self._scope_host = clean_hostname(urlparse(self.start_url).netloc)
        self._scope_folder = scope_folder(self.start_url)
        # Find subdomains (crawler/subdomains.py): other hosts of the site's registrable domain chosen by the
        # person. Spider mode only: each one's home page is an extra start URL and the host is in scope whatever
        # the crawl scope (Exact URL: only its home page). Hosts without DNS or on a private network address are
        # dropped when the crawl starts (_check_extra_hosts); extra_hosts_skipped says why per host.
        self.extra_hosts: List[str] = []
        self.extra_hosts_skipped: Dict[str, str] = {}
        self._extra_keys: Set[str] = set()
        self._extra_added: Set[str] = set()     # names put in allowed_domains only for an extra host
        if self.mode != "list" and getattr(config, "extra_hosts", None):
            homes, skipped = extra_host_urls(config.extra_hosts, self.start_url)
            self.extra_hosts_skipped.update(skipped)
            for home in homes:
                self._add_extra_host(home)
            labels = {"other_domain": "is not part of this site's domain", "same_site": "is the crawled site itself",
                      "invalid": "is not a host name", "too_many": "is over the limit of extra hosts"}
            for value, why in list(skipped.items())[:10]:
                if why != "same_site":
                    self.config_notes.append(f"Extra host {value!r} {labels.get(why, why)}, so it is not crawled.")
        # Hosts seen in the XML sitemaps (page URLs per host, 0 = only sitemap files) and in robots.txt
        self.sitemap_hosts: Dict[str, int] = {}
        self.robots_hosts: Set[str] = set()
        # The latest Find subdomains result for this domain when the crawl started (subdomain report)
        self.subdomain_discovery: Optional[dict] = None
        # Internal URLs found but not crawled because they are outside the crawl scope (Subfolder / Exact URL /
        # List mode). By design, not a limit: reported as a count only.
        self._out_of_scope: Set[str] = set()
        self._out_of_scope_more = False
        # Loop guard: path-segment key -> 4xx/5xx answers so far
        self._loop_counts: Counter = Counter()
        self._link_capped_pages = 0      # pages whose links were cut at max_links_per_page
        self._error_stop = False         # the crawl stopped on an unexpected error
        # XML sitemap <lastmod> per listed URL (filled by the sitemap probe)
        self.sitemap_lastmod: Dict[str, str] = {}
        # Per sitemap file read: {"urls", "size_bytes" (uncompressed; None when not read to its end), "index"};
        # and the sitemap files listed but left unread (the parser reads at most max_sitemap_files per crawl)
        self.sitemap_stats: Dict[str, dict] = {}
        self.sitemaps_not_read: List[str] = []
        # P9: {"limit", "file", "left_out"} when the sitemap URL index stopped at max_sitemap_index_urls (URLs listed
        # after that are "Not checked" in Not in Sitemap / Orphan URLs), else None; and the most sitemap files the
        # probe could read (None = the probe did not run, or the crawl was made before the limit was a setting: 25)
        self.sitemap_index_full: Optional[dict] = None
        self.sitemap_files_limit: Optional[int] = None
        # P8: <xhtml:link rel="alternate" hreflang> of each sitemap <url> entry: page url -> [{"lang", "url"}], and
        # whether the parser stopped keeping them (MAX_SITEMAP_HREFLANG)
        self.sitemap_hreflang: Dict[str, List[dict]] = {}
        self.sitemap_hreflang_capped = False
        # Raw HTML of each internal HTML page, gzip on disk (View Source); None when disabled / serverless
        self.source_store: Optional[SourceStore] = None
        try:
            self.source_store = SourceStore.for_crawl(config, self.start_url)
        except Exception:
            self.source_store = None
        # Rendering seam for render_all (W1-render / integration): a callable (url, seo) -> rendered dict or None,
        # called in the worker for each internal 2xx HTML page; its result is stored as seo["rendered"]
        self.render_hook: Optional[Callable[[str, Dict[str, Any]], Optional[dict]]] = None
        # Accessibility audit seam (A2: RenderPool.audit): a callable (url, seo) -> PageData["axe"], called in the
        # worker for each internal 2xx HTML page while config.a11y_audit is on and render_all is off (with
        # render_all, axe runs inside the render). It sets only seo["axe"], within the page and time budgets.
        self.audit_hook: Optional[Callable[[str, Dict[str, Any]], Optional[dict]]] = None
        self._audit_pages = 0                    # pages handed to audit_hook
        self._audit_started: Optional[float] = None

        # Threading & Control
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set() # Unpaused initially

        # Internal URL Queues and Tracker Sets
        # Queue item: (url, parent_url, depth, source)
        self.queue: deque = deque()
        self.enqueued_urls: Set[str] = set()
        self.visited_urls: Set[str] = set()
        self.results: Dict[str, CrawlResult] = {}
        self.results_list: List[CrawlResult] = []
        # url -> element meta of the link that first led to it (anchor text, region, selector...)
        self.link_origin: Dict[str, dict] = {}
        # URLs known so far only from JavaScript code; a real link found later replaces the origin
        self._code_only: Set[str] = set()
        self._origin_parent: Dict[str, str] = {}
        self._tallies: Counter = Counter()
        # Internal URLs found but not queued because of the Max URLs / Max depth limits
        self._over_limit: Set[str] = set()
        self._over_depth: Set[str] = set()
        # P2: the smallest depth a still-queued URL was found at (a URL found again with fewer clicks while it waits
        # in the queue is crawled at that depth; popped with the URL)
        self._queued_depth: Dict[str, int] = {}
        # P4: redirects followed to reach a URL (HTTP, meta refresh, Refresh header, JavaScript; 0 for a linked URL)
        # and the targets not requested because a chain reached max_redirects: target -> the URL that pointed at it
        self._redirect_hops: Dict[str, int] = {}
        self._over_redirects: Dict[str, str] = {}
        # P9: URLs found only in XML sitemaps that were queued, and the ones left out by max_sitemap_only_urls
        self._sitemap_only_queued = 0
        self._over_sitemap_only: Set[str] = set()
        # Found but not queued because the server's memory was nearly full (see _memory_low)
        self._over_memory: Set[str] = set()
        # P17: URLs not requested because an option is off or a crawl limit was reached: url -> (code, parent), and
        # the number of distinct URLs per code (every one counted; the first MAX_NOT_FOLLOWED_LISTED listed)
        self._not_followed: Dict[str, Tuple[str, str]] = {}
        self._not_followed_counts: Counter = Counter()
        self._depth_counts: Counter = Counter()
        self._host_counts: Counter = Counter()
        self._memory_stop = False
        self._memory_free_mb: Optional[int] = None
        self._mem_checked_at = 0
        # The start URL and every URL it redirects to: their redirect targets count as the site itself
        self._start_chain: Set[str] = set()
        self._oversize_pages: int = 0

        # External Links Collection (NOT crawled, only recorded; status checked when enabled)
        self.external_links: Dict[str, ExternalLink] = {}
        self.external_links_list: List[ExternalLink] = []
        self.check_external = bool(getattr(config, "check_external_links", False) or config.validate_external_links)
        self._ext_executor: Optional[ThreadPoolExecutor] = None
        self._ext_futures: list = []
        self.external_checked: int = 0
        self._ext_domains: Set[str] = set()

        # Every page URL listed in the XML sitemaps -> the sitemaps listing it (filled by the sitemap probe,
        # including URLs that were not queued because of limits)
        self.sitemap_index: Dict[str, List[str]] = {}
        # (url, sitemap) pairs waiting until the linked pages are crawled (see _release_sitemap_backlog)
        self._sitemap_backlog: List[Tuple[str, str]] = []

        # "idle" -> "crawling" -> "checking external links" -> POST_CRAWL_PHASES -> SNAPSHOT_PHASE -> "done"
        self.phase: str = "idle"
        self.phase_detail: str = ""      # what the current post-crawl step is doing
        # Post-crawl reports (crawler/seo/postcrawl.py sets them; None = did not run) and the saved snapshot id
        self.robots_audit: Optional[dict] = None
        self.trap_probe: Optional[dict] = None
        self.bot_report: Optional[dict] = None
        self.post_crawl: Dict[str, str] = {}
        self.snapshot_id: str = ""
        self._crawl_done = threading.Event()   # the crawl itself finished; only post-crawl work is left
        self._post_stop = threading.Event()    # stops the post-crawl checks

        # Network guards (crawler/net_guard.py): per-host adaptive pacing, firewall detection, crawl traps
        self.throttle: Optional[HostThrottle] = HostThrottle() if getattr(config, "adaptive_throttle", True) else None
        self._throttle_requeues: Counter = Counter()   # url -> times put back after a 429/503 answer
        self._throttle_counts: Counter = Counter()     # answers / requeued / gave_up (429/503 answers)
        self._throttle_logged = 0
        self._waf_hits: Counter = Counter()            # firewall label -> answers it produced
        self._waf_hosts: Set[str] = set()
        self._waf_logged: Set[Tuple[str, str]] = set()   # (host, label) already logged
        self._recent_403: Dict[str, deque] = {}        # host -> recent 403 page results (burst marking)
        self._trap_skipped: Dict[str, Tuple[str, str]] = {}   # url -> (TRAP_LABELS code, linking page)
        self._trap_counts: Counter = Counter()         # TRAP_LABELS code -> URLs skipped
        self._trap_examples: Dict[str, str] = {}       # trap pattern key -> the one URL crawled as its example
        # Cached (site, index) for site_index()
        self._index_lock = threading.Lock()
        self._index_cache: Optional[Tuple[Any, Any]] = None
        self._index_key: Optional[tuple] = None
        self._index_time: float = 0.0
        self._index_cost: float = 0.0   # seconds the last rebuild took

        # Authentication State & Limitations. One shared Chrome carries the login for the login itself,
        # JavaScript rendering, screenshots and later features (see browser_lease)
        self.browser = SharedBrowser(self.config.user_agent, self.config.timeout, site_url=self.start_url,
                                     logger=self.log, proxy=getattr(self.config, "proxy", None))
        self.auth_handler = AuthHandler(
            auth_config=self.config.auth,
            user_agent=self.config.user_agent,
            timeout=self.config.timeout,
            verify_ssl=self.config.verify_ssl,
            logger=self.log,
            browser=self.browser,
        )
        # The login as data: full cookies + localStorage/sessionStorage (browser.export_session format)
        self.session_state: Optional[dict] = None
        self._relogin_lock = threading.Lock()
        self._relogins = 0
        self._session_gen = 0
        # url -> times it went back in the queue after a new login (at most MAX_RELOGINS)
        self._requeued_after_login: Counter = Counter()
        # The login ended and could not be renewed: the rest of the crawl runs logged out (and says so)
        self.session_dead = False
        self._session_check: Optional[Tuple[int, float, bool]] = None   # (session gen, time, ended)
        # Links a logged-in crawl never opens (sign-out, admin actions, token links): url -> linking page
        self._unsafe_skipped: Dict[str, str] = {}
        # Logged-in, server-built pages opened in Chrome to explore click-only menus (see _new_menu_labels)
        self._menu_labels_seen: Set[str] = set()
        self._menu_renders = 0
        # Set by stop(superseded=True): a new crawl replaced this one, so its reports are not wanted
        self.superseded = False
        self._logging_in = False
        self.auth_status: str = "Not required (Public)"
        self.auth_used: bool = False
        self.auth_cookies: dict = {}
        self.auth_error: Optional[str] = None
        self.auth_required_count: int = 0
        self.limitations: List[str] = []   # distinct notes in the order found (see _add_limitation)
        self._limitation_set: Set[str] = set()

        # Metrics
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self.status = "idle" # "idle", "running", "paused", "completed", "stopped"

        # Shared HTTP Session with retries & connection pooling.
        # Timeouts are retried once by _crawl_single_page itself, so the adapter only retries
        # 500/502/504 answers and one failed connect; read timeouts are not retried here
        # (stacking both used to hold a worker for ~2 minutes on one dead URL). 429/503 are never retried
        # blindly: HostThrottle pauses the host for Retry-After and the URL goes back in the queue; a 403 is
        # never retried (repeating a request a firewall refused only makes the block last longer).
        self.session = SiteSession()
        retries = Retry(
            total=self.config.max_retries,
            connect=1,
            read=0,
            status=self.config.max_retries,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 504],
            allowed_methods=frozenset(["GET", "HEAD"]),
            respect_retry_after_header=False,
            raise_on_status=False
        )
        # (connect, read): a host that never answers fails fast instead of eating the read timeout
        self.request_timeout = (min(self.config.timeout, 10), self.config.timeout)
        # One pool per host; each pool holds a connection per worker (plus headroom for bundle/robots fetches)
        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=max(10, self.concurrency),
            pool_maxsize=max(10, self.concurrency + 4)
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._guard(self.session)        # P24: refused addresses (block_addresses), a no-op by default
        self.http_session = self.session  # carries the login cookies (and a custom auth header)
        self.session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive"
        })
        # Configuration > HTTP Header: extra headers for the crawled site only (the external-link checker's
        # session never gets them). The session leaves them out of any request, or redirect hop, to another host
        # (robots.txt / sitemap / PDF / source-map redirects off the site). Names are logged, values never.
        self.request_headers, notes = clean_request_headers(getattr(config, "request_headers", None))
        self.config_notes += notes
        self._login_host = self._same_domain_host(getattr(getattr(config, "auth", None), "login_url", None) or "")
        self.session.set_site_headers(self.request_headers, self.on_site)
        # Proxy (server settings): every request of the crawl goes through it; the URL carries the proxy login,
        # so it is never logged
        self.proxies: Dict[str, str] = {}
        proxy = proxy_url(getattr(config, "proxy", None))
        if proxy:
            self.proxies = {"http": proxy, "https": proxy}
            self.session.proxies.update(self.proxies)

        self.robots: Optional[RobotsPolicy] = None
        if self.config.respect_robots:
            # A custom robots.txt (P3) replaces the live file of every host of the crawled site for this crawl
            custom = getattr(self.config, "custom_robots_txt", "") or ""
            self.robots = RobotsPolicy(
                self.session, agent=self.config.robots_user_agent, timeout=min(self.config.timeout, 10),
                verify_ssl=self.config.verify_ssl, logger=self.log,
                custom_text=custom if custom.strip() else None, custom_for=self.on_site
            )

        # Dynamic Browser (Headless Chrome) initialized lazily only when needed
        self.dynamic_extractor: Optional[DynamicExtractor] = None
        self._dynamic_init_lock = threading.Lock()
        # Parsing and analysing pages: a few workers at a time (see _cpu_slots); never held around network or
        # Chrome work, and never taken while holding self._lock
        self._cpu_gate = threading.BoundedSemaphore(_cpu_slots())
        # JS bundle URLs already scanned for client-side routes (each is fetched once per crawl)
        self._scanned_bundles: Set[str] = set()

    def log(self, message: str):
        self.on_log(f"[{time.strftime('%H:%M:%S')}] {message}")

    def stop(self, superseded: bool = False):
        """Signals the crawler to gracefully stop. Once the crawl itself has finished, only the post-crawl
        checks still running are stopped: the crawl stays "completed" and those checks say so.
        `superseded`: a new crawl replaces this one; callers then skip its reports (see `superseded`)."""
        if superseded:
            self.superseded = True
            self.log("A new crawl replaced this one: it stops now and its reports are skipped.")
        if self._crawl_done.is_set() and self.status != "stopped":
            self.log("Stop requested by user: the crawl had finished; skipping the remaining post-crawl checks.")
            self._post_stop.set()
            self._pause_event.set()
            if self.status == "paused":      # a pause that raced the end of the crawl
                self.status = "completed" if self.end_time else "running"
            return
        self.log("Stop requested by user.")
        self._stop_event.set()
        self._post_stop.set()
        self._pause_event.set()
        self.status = "stopped"

    def pause(self):
        """Pauses the crawler workers. Once the crawl itself has finished there is nothing left to pause."""
        if self._crawl_done.is_set():
            return
        self.log("Crawling paused.")
        self._pause_event.clear()
        self.status = "paused"

    def resume(self):
        """Resumes the crawler workers (after the crawl's end it only undoes a pause that raced that end)."""
        self._pause_event.set()
        if self._crawl_done.is_set():
            if self.status == "paused":
                self.status = "completed" if self.end_time else "running"
            return
        self.log("Crawling resumed.")
        self.status = "running"

    def _add_limitation(self, note: str):
        """Records a limitation note once (caller holds the lock); at most MAX_LIMITATIONS are kept."""
        if note not in self._limitation_set and len(self.limitations) < MAX_LIMITATIONS:
            self._limitation_set.add(note)
            self.limitations.append(note)

    def _report_progress(self):
        """Calls on_progress with fresh stats, only when a listener was given."""
        if not self._wants_progress:
            return
        try:
            self.on_progress(self.get_stats())
        except Exception:
            pass

    def limit_notes(self) -> List[str]:
        """Plain-language notes on URLs left out (Max URLs, Max depth, page size, crawl traps), followed by the
        crawl_warnings() (firewall, 429/503)."""
        notes = []
        if self._site_silent():
            first = self.results_list[0]
            why = str(getattr(first, "root_cause", "") or getattr(first, "http_status", "") or "no response")[:80]
            notes.append(f"The site did not answer: none of the {len(self.results_list):,} URL(s) requested got an "
                         f"HTTP answer ({why}). Every count is empty because nothing could be checked, not because "
                         "nothing is wrong. Check the address and that the site is up and allows this server, then "
                         "crawl again.")
        if self._over_limit:
            notes.append(f"Crawl stopped at the Max URLs limit of {self.config.max_urls:,}: "
                         f"{len(self._over_limit):,} more internal URLs were found but not crawled, so every "
                         "count in this report covers part of the site only.")
        if self._over_memory:
            notes.append(f"Crawl stopped early because the server's memory was nearly full: "
                         f"{len(self._over_memory):,} more internal URLs were found but not crawled, so every count "
                         "in this report covers part of the site only. Crawl one section at a time (Include "
                         "pattern) or use a bigger server for the rest.")
        if self._over_depth:
            notes.append(f"{len(self._over_depth):,} internal URLs deeper than the Max depth of "
                         f"{self.config.max_depth} clicks were found but not crawled.")
        if self._over_redirects:
            notes.append(f"{len(self._over_redirects):,} redirect chain(s) reached the Max redirects limit of "
                         f"{int(getattr(self.config, 'max_redirects', 10) or 10)}: the next URL of each was not "
                         f"requested, and the last URL reached keeps its own status with the reason "
                         f"'{REDIRECT_LIMIT}'.")
        sitemap_only = self._sitemap_only_left_out()
        if sitemap_only:
            notes.append(f"{sitemap_only:,} URL(s) found only in XML sitemaps were not crawled: at most "
                         f"{int(getattr(self.config, 'max_sitemap_only_urls', 0) or 0):,} are crawled (Max "
                         "sitemap-only URLs). The Sitemaps tab lists them as Not crawled.")
        notes += self.sitemap_notes()
        if self._oversize_pages:
            notes.append(f"{self._oversize_pages:,} page(s) were larger than "
                         f"{self._max_page_bytes() // (1024 * 1024)} MB; only the first part of each was read.")
        traps = sum(self._trap_counts.values())
        if traps:
            parts = ", ".join(f"{n:,} {TRAP_LABELS.get(code, code).split(' (')[0].lower()}"
                              for code, n in self._trap_counts.most_common())
            notes.append(f"{traps:,} internal URL(s) were skipped as crawl traps ({parts}). They are not "
                         "counted as errors or as crawled URLs; the first ones of each kind are in the Logs.")
        if self._unsafe_skipped:
            notes.append(f"{len(self._unsafe_skipped):,} link(s) were not opened because opening them while logged "
                         "in would sign out or change data on the site (sign-out links, admin screens such as "
                         "/wp-admin/, delete / deactivate / remove links, cart changes). They are not counted as "
                         "crawled URLs; the first ones are in the Logs.")
        if self._out_of_scope and self.mode != "list":
            where = {"subfolder": f"the start folder {self._scope_folder}",
                     "exact_url": "the start URL (Exact URL scope)"}.get(self.crawl_scope, "the crawl scope")
            more = "at least " if self._out_of_scope_more else ""
            notes.append(f"{more}{len(self._out_of_scope):,} internal URL(s) linked from crawled pages are outside "
                         f"{where}, so they were recorded as links but not crawled.")
        return notes + self.crawl_warnings()

    def _sitemap_only_left_out(self) -> int:
        """Sitemap-only URLs left out by max_sitemap_only_urls that no link reached either."""
        return len(self._over_sitemap_only - self.enqueued_urls) if self._over_sitemap_only else 0

    def sitemap_notes(self) -> List[str]:
        """Why the sitemap comparison covers part of the sitemaps only (P9): files listed but not read, the URL index
        full. Pages that could be in what was not read are "Not checked" in URLs Not In Sitemap, not flagged."""
        notes = []
        unread = len(self.sitemaps_not_read or ())
        if unread:
            limit = self.sitemap_files_limit or 25
            notes.append(f"{unread:,} XML sitemap file(s) were listed but not read (at most {limit:,} are read per "
                         "crawl, Max sitemap files): a page missing from the files that were read may be listed in "
                         "them, so URLs Not In Sitemap and Orphan URLs are Not checked for it.")
        full = self.sitemap_index_full
        if isinstance(full, dict):
            more = int(full.get("left_out") or 0)
            notes.append(f"The XML sitemap URL index stopped at {int(full.get('limit') or 0):,} URLs (Max sitemap "
                         f"index URLs){f', {more:,} more were listed' if more else ''}: URLs Not In Sitemap and "
                         "Orphan URLs are Not checked for URLs the index could not hold.")
        if getattr(self, "sitemap_hreflang_capped", False):
            from .sitemap_parser import MAX_SITEMAP_HREFLANG
            notes.append(f"The XML sitemaps list more than {MAX_SITEMAP_HREFLANG:,} hreflang alternates; the rest were "
                         "not kept, so a page whose sitemap hreflang was dropped can show as Missing in the Hreflang "
                         "tab.")
        return notes

    def _site_silent(self) -> bool:
        """The crawl finished and not one URL got an HTTP answer (wrong address, site down, server blocked)."""
        return bool(self._crawl_done.is_set() and self.results_list
                    and len(self.results_list) - self._tallies["error"] <= 0)

    def crawl_warnings(self) -> List[str]:
        """Crawl-level warnings for a banner: the site's firewall blocked or challenged the crawler, or the
        server asked it to slow down. Plain sentences with counts; empty when nothing happened."""
        out = []
        if self._waf_hits:
            labels = [k for k, _ in self._waf_hits.most_common() if k != BURST_LABEL]
            who = ", ".join(labels) if labels else "a firewall"
            n = sum(self._waf_hits.values())
            burst = (" Many URLs suddenly answered 403 Forbidden across different sections after earlier pages "
                     "loaded fine, which usually means a firewall started blocking the crawler."
                     if self._waf_hits.get(BURST_LABEL) else "")
            out.append(f"The site's bot protection ({who}) blocked or challenged {n:,} request(s) from the "
                       f"crawler, so those URLs show the block page's status, not the real page.{burst} The "
                       "crawler slowed down. Try a lower speed or another User-Agent, or ask the site owner to "
                       "allow this crawler, then re-check those URLs.")
        answers = self._throttle_counts["answers"]
        if answers:
            requeued, gave_up = self._throttle_counts["requeued"], self._throttle_counts["gave_up"]
            tail = (f"; {gave_up:,} answer(s) were still 429/503 after {MAX_THROTTLE_REQUEUES} retries and are "
                    "reported as they came" if gave_up else "")
            out.append(f"The server asked the crawler to slow down {answers:,} time(s) (HTTP 429/503). The "
                       f"crawler paused (honouring Retry-After), slowed down and retried {requeued:,} time(s)"
                       f"{tail}.")
        return out

    def get_stats(self) -> dict:
        with self._lock:
            total_discovered = len(self.enqueued_urls)
            total_crawled = len(self.visited_urls)
            counts = self._tallies
            status_2xx, status_3xx = counts["2xx"], counts["3xx"]
            status_4xx, status_5xx, status_err = counts["4xx"], counts["5xx"], counts["error"]
            failed_count = status_4xx + status_5xx + status_err
            pages, pages_ok = counts["pages"], counts["pages_ok"]

            elapsed = (time.time() - self.start_time) if self.start_time else 0.0
            if self.end_time and self.start_time:
                elapsed = self.end_time - self.start_time

            unique_ext_domains = len(self._ext_domains)
            notes = self.limit_notes()

            return {
                "status": self.status,
                "elapsed_seconds": round(elapsed, 1),
                # When the crawl ran (local time), so an opened saved crawl shows its own date
                "started_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.start_time))
                               if self.start_time else ""),
                "total_discovered": total_discovered,
                "total_crawled": total_crawled,
                "queue_size": len(self.queue),
                "status_2xx": status_2xx,
                "status_3xx": status_3xx,
                "status_4xx": status_4xx,
                "status_5xx": status_5xx,
                "status_err": status_err,
                "status_4xx_in_code": counts["4xx_code"],
                "status_blocked": counts["blocked"],
                "failed_count": failed_count,
                "health_score": round(pages_ok / pages * 100) if pages else 100,
                "pages_total": pages,
                "pages_ok": pages_ok,
                "files_total": counts["files"],
                "auth_required_count": self.auth_required_count,
                "total_external": len(self.external_links),
                "unique_external_domains": unique_ext_domains,
                "external_checked": self.external_checked,
                "phase": self.phase,
                "start_url": self.start_url,
                "auth_used": self.auth_used,
                "auth_status": self.auth_status,
                "auth_error": self.auth_error,
                "auth_prompt_needed": (self.auth_required_count > 0 and not self.auth_used),
                # The login ended and could not be renewed; pages after that were crawled logged out
                "session_lost": self.session_dead,
                "not_crawled_unsafe": len(self._unsafe_skipped),
                # Found but left out by the crawl limits (not part of total_discovered, which counts queued URLs)
                "not_crawled_max_urls": len(self._over_limit),
                "not_crawled_max_depth": len(self._over_depth),
                "not_crawled_memory": len(self._over_memory),
                "not_crawled_max_redirects": len(self._over_redirects),
                "not_crawled_sitemap_only": self._sitemap_only_left_out(),
                "sitemaps_not_read": len(self.sitemaps_not_read or ()),
                "sitemap_index_full": dict(self.sitemap_index_full) if isinstance(self.sitemap_index_full, dict)
                else None,
                # Finished without a single HTTP answer: every count is empty because nothing was checked
                "no_answer": self._site_silent(),
                "limit_reached": bool(self._over_limit or self._over_memory),
                "limitations": (notes + self.limitations[:10])[:10],
                # Network guards: crawl traps skipped (not crawled, not errors), firewall and 429/503 answers
                "not_crawled_traps": sum(self._trap_counts.values()),
                "trap_reasons": {TRAP_LABELS.get(k, k): n for k, n in self._trap_counts.most_common()},
                "waf_detected": bool(self._waf_hits),
                "waf": ({"labels": [k for k, _ in self._waf_hits.most_common()],
                         "answers": sum(self._waf_hits.values()), "hosts": sorted(self._waf_hosts)}
                        if self._waf_hits else None),
                "throttle": {"answers": self._throttle_counts["answers"],
                             "retried": self._throttle_counts["requeued"],
                             "gave_up": self._throttle_counts["gave_up"],
                             "hosts": self.throttle.summary() if self.throttle else {}},
                "warnings": self.crawl_warnings(),
                # Post-crawl checks ({check: status}, filled as they finish) and the saved snapshot id
                "phase_detail": self.phase_detail,
                "post_crawl": self._post_crawl_states(),
                "snapshot_id": self.snapshot_id,
                # Mode, scope and how complete the crawl is (coverage: see the property)
                "mode": self.mode,
                "crawl_scope": self.crawl_scope,
                "list": self.list_summary,
                "not_crawled_out_of_scope": len(self._out_of_scope),
                "not_crawled_loop_guard": self._trap_counts.get("loop_guard", 0),
                "coverage": self.coverage,
                # Find subdomains: hosts crawled besides the start host, and the ones left out (host: reason code)
                "extra_hosts": [urlparse(h).hostname or h for h in self.extra_hosts],
                "extra_hosts_skipped": dict(list(self.extra_hosts_skipped.items())[:50]),
                "config_notes": list(self.config_notes[:20]),
                "sources_stored": self.source_store.pages if self.source_store is not None else None,
            }

    def _add_extra_host(self, home: str):
        """Makes the host of `home` (an extra host's home page URL) part of the crawled site."""
        host = (urlparse(home).hostname or "").lower()
        key = clean_hostname(host)
        if not key:
            return
        self.extra_hosts.append(home)
        self._extra_keys.add(key)
        self._always_allowed.add(home)
        for name in {key, host}:
            if name not in self.allowed_domains:
                self.allowed_domains.add(name)
                self._extra_added.add(name)

    def _check_extra_hosts(self):
        """Before the extra hosts are queued: names without DNS, or whose addresses are all on a private / internal
        network (unless the crawled site is itself on one), are dropped and listed in extra_hosts_skipped."""
        if not self.extra_hosts:
            return
        kept, dropped = check_extra_hosts(self.extra_hosts, self.start_url)
        if not dropped:
            return
        with self._lock:
            for home in [h for h in self.extra_hosts if h not in kept]:
                host = (urlparse(home).hostname or "").lower()
                key = clean_hostname(host)
                self.extra_hosts.remove(home)
                self._always_allowed.discard(home)
                self._extra_keys.discard(key)
                for name in {key, host}:
                    if name in self._extra_added:
                        self.allowed_domains.discard(name)
                        self._extra_added.discard(name)
            self.extra_hosts_skipped.update(dropped)
        why = {"no_dns": "its name does not resolve (no DNS record)",
               "private_address": "it points to a private / internal network address"}
        for host, code in dropped.items():
            self.log(f"Extra host {host} is not crawled: {why.get(code, code)}.")

    def _enqueue(self, url: str, parent: str, depth: int, source: str, meta: Optional[dict] = None) -> bool:
        """Thread-safe enqueueing of a new URL candidate (Internal only).
        `meta` describes the element on `parent` that links to `url`."""
        notes: List[str] = []
        added = self._enqueue_locked(url, parent, depth, source, meta, notes)
        for note in notes:
            self.log(note)
        return added

    def _trap_check(self, url: str, parent: str, notes: List[str]) -> bool:
        """True when `url` is a crawl trap (see net_guard.trap_reason): it is remembered with its reason and
        counted, never queued, so it is neither crawled nor an error (caller holds the lock). The first URL
        of each pattern (net_guard.trap_example_key) is crawled as its example, so the Hygiene and Parameters
        tabs show the pattern with a real status; every further URL of it is skipped."""
        if not getattr(self.config, "skip_crawl_traps", True) or url in self._always_allowed:
            return False
        skipped = self._trap_skipped.get(url)
        if skipped is not None and skipped[0] != "loop_guard":   # the loop guard decides on its own URLs
            return True
        code = trap_reason(url, int(getattr(self.config, "max_url_length", 2000) or 0))
        if not code:
            return False
        example = trap_example_key(url, code)
        if example and self._trap_examples.setdefault(example, url) == url:
            return False
        # Every trap URL is remembered (each one is a link found on a crawled page, so the set stays bounded),
        # so a URL linked from many pages is counted once
        self._trap_counts[code] += 1
        self._trap_skipped[url] = (code, parent)
        n = self._trap_counts[code]
        if n <= TRAP_LOG_PER_REASON:
            shown = url if len(url) <= 160 else url[:157] + "..."
            crawled = self._trap_examples.get(example) if example else None
            tail = f"; {crawled} was crawled as the example of this pattern" if crawled else ""
            notes.append(f"Skipped crawl trap: {shown} - {TRAP_LABELS.get(code, code)} (linked from {parent}{tail})")
            if n == TRAP_LOG_PER_REASON:
                notes.append("More URLs skipped for the same reason are counted but not logged one by one.")
        return True

    def _in_scope(self, url: str, source: str, depth: int) -> bool:
        """True when `url` may be crawled under the crawl scope and mode (caller holds the lock). The start URL,
        listed URLs and the start URL's redirect targets are always in scope; redirect targets and resources
        (images, scripts, CSS, frames) of crawled pages are always checked, so chains resolve and pages are
        complete. Other URLs out of scope are counted (MAX_OUT_OF_SCOPE kept), never crawled."""
        if url in self._always_allowed or source in ("Redirect", "Resource", "List Canonical"):
            return True
        if self.mode == "list":
            ok = 0 < depth <= max(0, int(getattr(self.config, "list_max_depth", 0) or 0))
        elif self.crawl_scope == "exact_url":
            ok = False
        elif self.crawl_scope == "subfolder":
            try:
                p = urlparse(url)
            except Exception:
                return False
            path = p.path or "/"
            host_key = clean_hostname(p.netloc)
            ok = (host_key == self._scope_host and (
                path.startswith(self._scope_folder) or path == self._scope_folder.rstrip("/"))) \
                or host_key in self._extra_keys   # an extra host is crawled from its home page down
        else:
            return True
        if not ok and self.mode != "list":   # a listed page's links are not "outside" anything
            if len(self._out_of_scope) < MAX_OUT_OF_SCOPE:
                self._out_of_scope.add(url)
            else:
                self._out_of_scope_more = True
        return ok

    def _error_page(self, url: str) -> bool:
        """The crawled URL answered 4xx/5xx (caller holds the lock)."""
        result = self.results.get(url)
        code = getattr(result, "status_code", None) if result is not None else None
        return code is not None and code >= 400

    def _loop_guarded(self, url: str, parent: str, notes: List[str]) -> bool:
        """siteone-crawler's loop guard (caller holds the lock): a URL linked from a page that itself answered
        4xx/5xx, whose last path segment already answered 4xx/5xx LOOP_GUARD_LIMIT times, is skipped and recorded
        like a crawl trap (reason "loop_guard": counted and listed, not crawled). A link found on a page that
        loaded is never skipped: whatever it answers is a finding (a 404 is always a broken link, as Screaming
        Frog reports it), and since nothing is followed from an error page it cannot feed a loop. A URL skipped
        earlier is crawled after all once a page that loaded links to it."""
        if not getattr(self.config, "loop_guard", True) or url in self._always_allowed:
            return False
        skipped = self._trap_skipped.get(url)
        key = loop_guard_key(url)
        if not key or self._loop_counts.get(key, 0) < LOOP_GUARD_LIMIT or not self._error_page(parent):
            if skipped is not None and skipped[0] == "loop_guard":
                del self._trap_skipped[url]
                self._trap_counts["loop_guard"] -= 1
                if self._trap_counts["loop_guard"] <= 0:
                    del self._trap_counts["loop_guard"]
            return False
        if skipped is not None:
            return True
        self._trap_counts["loop_guard"] += 1
        self._trap_skipped[url] = ("loop_guard", parent)
        n = self._trap_counts["loop_guard"]
        if n <= TRAP_LOG_PER_REASON:
            shown = url if len(url) <= 160 else url[:157] + "..."
            notes.append(f"Skipped {shown}: it is linked only from the error page {parent}, and "
                         f"{self._loop_counts[key]} URLs ending in '{key[:60]}' already answered with an error, so "
                         "it is most likely a link loop.")
            if n == TRAP_LOG_PER_REASON:
                notes.append("More URLs skipped by the loop guard are counted but not logged one by one.")
        return True

    def _count_loop_answer(self, result: "CrawlResult"):
        """Counts a 4xx/5xx answer for the loop guard (caller holds the lock)."""
        code = result.status_code
        if code is None or code < 400 or code in LOOP_GUARD_IGNORED or result.url in self._always_allowed:
            return
        key = loop_guard_key(result.url)
        if key:
            self._loop_counts[key] += 1

    def _unsafe_check(self, url: str, parent: str, notes: List[str]) -> bool:
        """True when a logged-in crawl must not open `url` (url_utils.is_unsafe_url): it is remembered with the
        page linking to it and counted, never queued (caller holds the lock)."""
        if url in self._unsafe_skipped:
            return True
        if not is_unsafe_url(url):
            return False
        self._unsafe_skipped[url] = parent
        n = len(self._unsafe_skipped)
        if n <= UNSAFE_LOG_LIMIT:
            shown = url if len(url) <= 160 else url[:157] + "..."
            notes.append(f"Not opened while logged in: {shown} - it would sign out or change data on the site "
                         f"(linked from {parent}).")
            if n == UNSAFE_LOG_LIMIT:
                notes.append("More links like this are counted but not logged one by one.")
        return True

    def _no_new_cookies(self) -> None:
        """Configuration > Spider > Cookie storage "Do not store" (P17): the crawl's session stops keeping the cookies
        pages set (each request is like a first visit); cookies of the sign-in, set before, are still sent."""
        import http.cookiejar

        class _NoNew(http.cookiejar.DefaultCookiePolicy):
            def set_ok(self, cookie, request):
                return False
        try:
            self.session.cookies.set_policy(_NoNew())
        except Exception:
            pass

    def _skip_option(self, url: str, parent: str, code: str) -> None:
        """Remembers a URL that an option or a limit kept from being requested (caller holds the lock)."""
        if url in self._not_followed or url in self.enqueued_urls or url in self.visited_urls:
            return
        self._not_followed_counts[code] += 1
        if len(self._not_followed) < MAX_NOT_FOLLOWED_LISTED:
            self._not_followed[url] = (code, parent)
        else:
            self._not_followed.setdefault("", (code, parent))

    def _limit_reason(self, url: str, depth: int) -> str:
        """The crawl limit (CrawlConfig.limits, 0 = none) that leaves `url` out, '' when none does (caller holds
        the lock). per_host counts every host on its own (start host, subdomains, Find subdomains hosts)."""
        lim = getattr(self.config, "limits", None) or {}
        if not lim or not any(lim.values()):
            return ""
        p = urlparse(url)
        n = int(lim.get("per_depth") or 0)
        if n and self._depth_counts[depth] >= n:
            return "limit_per_depth"
        n = int(lim.get("folder_depth") or 0)
        if n:
            parts = [x for x in (p.path or "/").split("/") if x]
            folders = len(parts) if (p.path or "").endswith("/") else max(0, len(parts) - 1)
            if folders > n:
                return "limit_folder_depth"
        n = int(lim.get("query_strings") or 0)
        if n and p.query and len([x for x in p.query.split("&") if x]) > n:
            return "limit_query_strings"
        n = int(lim.get("per_host") or 0)
        if n and self._host_counts[clean_hostname(p.netloc)] >= n:
            return "limit_per_host"
        return ""

    def _follow_on(self, key: str) -> bool:
        follow = getattr(self.config, "follow", None)
        return not isinstance(follow, dict) or follow.get(key, True) is not False

    def _crawl_type_on(self, key: str) -> bool:
        types = getattr(self.config, "crawl_types", None)
        return not isinstance(types, dict) or types.get(key, True) is not False

    @staticmethod
    def _urls_in(value: Any) -> Set[str]:
        out: Set[str] = set()
        for v in (value if isinstance(value, (list, tuple, set)) else [value]):
            if isinstance(v, dict):
                v = v.get("url") or v.get("href") or ""
            if isinstance(v, str) and v:
                out.add(v)
        return out

    def _not_followed_links(self, result: "CrawlResult", links: Dict[str, dict]) -> Dict[str, str]:
        """{url: reason code} of the page's internal links that the Spider options say not to follow (P17): the
        <link> elements of canonicals, rel next / prev, hreflang and mobile alternates, and nofollow links when
        "Follow internal nofollow" is off. A URL that is also an ordinary link of the page is followed."""
        seo = result.seo or {}
        out: Dict[str, str] = {}
        kinds = (("canonicals", ("canonicals", "http_canonical")),
                 ("pagination", ("rel_next", "rel_prev", "rel_next_all", "rel_prev_all", "http_rel_next",
                                 "http_rel_prev")),
                 ("hreflang", ("hreflang",)), ("mobile_alternate", ("mobile_alternate",)))
        for key, fields in kinds:
            if self._follow_on(key):
                continue
            targets: Set[str] = set()
            for f in fields:
                targets |= self._urls_in(seo.get(f))
            for u in targets:
                meta = links.get(u)
                if meta is not None and (meta.get("tag") or "link") == "link":
                    out.setdefault(u, f"follow_{key}")
        if getattr(self.config, "follow_internal_nofollow", True) is False:
            robots = " ".join(str(x) for x in (seo.get("meta_robots") or [])).lower()
            page_nofollow = "nofollow" in robots or "none" in robots.split()
            followed = {l.get("url") for l in seo.get("links") or () if not l.get("nofollow")}
            for u, meta in links.items():
                if (meta or {}).get("tag", "a") != "a":
                    continue
                if page_nofollow or u not in followed:
                    out.setdefault(u, "nofollow")
        return out

    def _guard(self, session):
        """Mounts the refused-address guard of CrawlConfig.block_addresses ('' = none) on one of the crawl's
        sessions (pages, external checks, sitemaps; crawler/address_guard.py). The audited site's own loopback
        address stays allowed when the start URL is on this machine."""
        mode = str(getattr(self.config, "block_addresses", "") or "").strip().lower()
        if not mode:
            return session
        from .address_guard import guard_session
        host = (urlparse(self.start_url).hostname or "").lower()
        return guard_session(session, mode, loopback_ok=host in ("localhost", "127.0.0.1", "::1"))

    def _memory_low(self, queued: int, notes: List[str]) -> bool:
        """True once the server's free memory fell below MIN_FREE_MB (read every MEMORY_CHECK_EVERY queued URLs,
        from the container's cgroup, else the machine's available memory: free_memory_mb). Sticky, like the Max
        URLs limit: URLs already queued are still crawled, new ones are counted as not crawled (caller holds the
        lock)."""
        if self._memory_stop:
            return True
        if MIN_FREE_MB <= 0 or queued - self._mem_checked_at < MEMORY_CHECK_EVERY:
            return False
        self._mem_checked_at = queued
        free = free_memory_mb()
        if free is None or free >= MIN_FREE_MB:
            return False
        self._memory_stop, self._memory_free_mb = True, free
        notes.append(f"The server's memory is nearly full ({free} MB free, below {MIN_FREE_MB} MB): no more URLs "
                     f"are queued after {queued:,}, so the reports can still be built.")
        return True

    def skipped_urls(self) -> List[dict]:
        """URLs skipped as crawl traps: [{url, reason, label, parent}] (the first MAX_TRAPS_LISTED)."""
        with self._lock:
            items = list(self._trap_skipped.items())[:MAX_TRAPS_LISTED]
            options = [(u, v) for u, v in self._not_followed.items() if u][:MAX_TRAPS_LISTED]
        return [{"url": u, "reason": code, "label": TRAP_LABELS.get(code, code), "parent": parent}
                for u, (code, parent) in items] + \
               [{"url": u, "reason": code, "label": OPTION_LABELS.get(code, code), "parent": parent}
                for u, (code, parent) in options]

    def _enqueue_locked(self, url: str, parent: str, depth: int, source: str, meta: Optional[dict],
                        notes: List[str]) -> bool:
        with self._lock:
            if not url:
                return False
            if url in self.enqueued_urls or url in self.visited_urls:
                # Still waiting in the queue and found again with fewer clicks: it is crawled at the smaller depth
                # (P2; parallel workers find links in no fixed order)
                if url in self._queued_depth and depth < self._queued_depth[url]:
                    self._queued_depth[url] = depth
                # First seen only in JavaScript, now found as a real link on another page: that link is the
                # origin (the page's own canonical / self link does not count: it was found by opening the page)
                if meta and url in self._code_only and meta.get("tag") not in CODE_LINK_TAGS and parent != url:
                    self._code_only.discard(url)
                    self.link_origin[url] = meta
                    self._origin_parent[url] = parent
                    if url in self.results:
                        self._apply_origin(self.results[url])
                return False

            if not is_internal_url(
                url, self.base_domain,
                include_subdomains=self.config.include_subdomains,
                allowed_domains=self.allowed_domains
            ) and not (source == "Resource" and self.on_cdn(url)):
                return False

            # Crawl scope (Subfolder / Exact URL) and List mode: links outside it are recorded on their pages
            # (outlinks) but not crawled; redirect targets and resources are still checked
            if not self._in_scope(url, source, depth):
                return False

            # Logged in: never open a sign-out link (WooCommerce /customer-logout/, ?logout=1, route=account/logout)
            # or a link that changes data (WordPress admin row actions, token links, cart changes)
            if self.auth_used and url != self.start_url and self._unsafe_check(url, parent, notes):
                return False

            # Listed URLs are crawled as given: include / exclude settings never apply to them (as in Screaming Frog)
            if url not in self._list_index and is_excluded_path(url, self.config.exclude_patterns):
                return False

            if not self._passes_url_filters(url):
                return False

            if self._trap_check(url, parent, notes):
                return False

            if self._loop_guarded(url, parent, notes):
                return False

            # Limits last, so only URLs that would otherwise be crawled are counted as left out
            if self.config.max_depth and depth > self.config.max_depth:
                self._over_depth.add(url)
                return False
            # Within the depth limit after all (found again with fewer clicks): a URL left out from here on is left
            # out by another limit, never counted as too deep
            self._over_depth.discard(url)

            limit_code = self._limit_reason(url, depth) if url not in self._always_allowed else ""
            if limit_code:
                self._skip_option(url, parent, limit_code)
                return False

            if self.config.max_urls and len(self.enqueued_urls) >= self.config.max_urls:
                self._over_limit.add(url)
                return False

            if self._memory_low(len(self.enqueued_urls), notes):
                self._over_memory.add(url)
                return False

            self.enqueued_urls.add(url)
            self._depth_counts[depth] += 1
            self._host_counts[clean_hostname(urlparse(url).netloc)] += 1
            self._over_sitemap_only.discard(url)     # left out of the sitemap-only quota, then reached by a link
            if meta:
                self.link_origin[url] = meta
                if meta.get("tag") in CODE_LINK_TAGS:
                    self._code_only.add(url)
            self._queued_depth[url] = depth
            self.queue.append((url, parent, depth, source))
            return True

    def _next_item(self) -> Optional[tuple]:
        """The next queue item with the smallest depth its URL was found at while it waited (caller holds the
        lock; None when the queue is empty)."""
        if not self.queue:
            return None
        url, parent, depth, source = self.queue.popleft()
        best = self._queued_depth.pop(url, depth)
        return (url, parent, min(depth, best), source)

    def _seed_list(self):
        """List mode: queues every listed URL in list order (depth 0, source "List"). Lines that are not web
        addresses and repeated URLs are counted in list_summary, never crawled."""
        if self._list_invalid:
            shown = ", ".join(repr(v[:60]) for _, v in self._list_invalid[:5])
            self.log(f"{len(self._list_invalid)} line(s) of the URL list are not web addresses and were left out: "
                     f"{shown}")
        self.log(f"List mode: crawling {len(self._list_index):,} listed URL(s); their links are recorded, "
                 "not followed.")
        for url, i in self._list_index.items():
            meta = dict(LIST_META, html=f"Line {i + 1} of the URL list", raw_href=url)
            self._enqueue(url, parent="List Input", depth=0, source="List", meta=meta)

    @property
    def list_summary(self) -> Optional[dict]:
        """List mode: {"lines", "unique", "duplicates", "rejected": [{"index", "value", "reason"}]}; None otherwise."""
        if self.mode != "list":
            return None
        lines = self._list_lines
        return {"lines": lines, "unique": len(self._list_index),
                "duplicates": max(0, lines - len(self._list_index) - len(self._list_invalid)),
                "rejected": [{"index": i, "value": v, "reason": "not a web address"}
                             for i, v in self._list_invalid[:500]]}

    @property
    def coverage(self) -> dict:
        """How complete the crawl is: {"limit_hit": bool, "reasons": [codes], "counts": {code: n}, "notes": [..]}.
        limit_hit is True when something cut the crawl short (Max URLs / depth, memory, page size, links per page,
        the loop guard, a stop, a lost login or an error), so conclusions that need the whole site (orphans, a
        proposed sitemap) must say they cover part of it. Crawl traps and URLs outside the chosen scope are
        counted in "counts" but are not limits. Reads without the lock (get_stats holds it)."""
        counts: Dict[str, int] = {}
        if self._over_limit:
            counts["max_urls"] = len(self._over_limit)
        if self._over_depth:
            counts["max_depth"] = len(self._over_depth)
        if self._over_memory:
            counts["memory"] = len(self._over_memory)
        if self._over_redirects:
            counts["max_redirects"] = len(self._over_redirects)
        sitemap_only = self._sitemap_only_left_out()
        if sitemap_only:
            counts["sitemap_only"] = sitemap_only
        if self._oversize_pages:
            counts["page_size"] = self._oversize_pages
        if self._link_capped_pages:
            counts["links_per_page"] = self._link_capped_pages
        if self._trap_counts.get("loop_guard"):
            counts["loop_guard"] = self._trap_counts["loop_guard"]
        if self.status == "stopped" and not self.superseded:
            counts["stopped"] = 1
        if self.session_dead:
            counts["session_lost"] = 1
        if self._error_stop:
            counts["error"] = 1
        limited = sum(n for code, n in self._not_followed_counts.items() if code.startswith("limit_"))
        if limited:
            counts["crawl_limits"] = limited
        reasons = list(counts)
        off = sum(n for code, n in self._not_followed_counts.items() if not code.startswith("limit_"))
        if off:
            counts["not_followed"] = off
        traps = sum(n for code, n in list(self._trap_counts.items()) if code != "loop_guard")
        if traps:
            counts["traps"] = traps
        if self._out_of_scope and self.mode != "list":
            counts["out_of_scope"] = len(self._out_of_scope)
        if self.sitemaps_not_read:          # the sitemap comparison is partial; the crawl itself is not cut short
            counts["sitemap_files"] = len(self.sitemaps_not_read)
        if isinstance(self.sitemap_index_full, dict):
            counts["sitemap_index"] = int(self.sitemap_index_full.get("limit") or 0)
        notes = {
            "max_urls": f"{counts.get('max_urls', 0):,} more internal URLs were found after the Max URLs limit.",
            "max_depth": f"{counts.get('max_depth', 0):,} internal URLs were deeper than the Max depth.",
            "max_redirects": f"{counts.get('max_redirects', 0):,} redirect chain(s) stopped at the Max redirects "
                             "limit.",
            "sitemap_only": f"{counts.get('sitemap_only', 0):,} URLs found only in XML sitemaps were over the Max "
                            "sitemap-only URLs limit and were not crawled.",
            "memory": f"{counts.get('memory', 0):,} internal URLs were left out because the server's memory was low.",
            "page_size": f"{counts.get('page_size', 0):,} page(s) were larger than the page-size limit and were "
                         "read in part.",
            "links_per_page": f"{counts.get('links_per_page', 0):,} page(s) had more links than the per-page limit; "
                              "only the first ones were read.",
            "loop_guard": f"{counts.get('loop_guard', 0):,} URL(s) were skipped by the loop guard (their URL ending "
                          "kept answering with errors).",
            "crawl_limits": f"{counts.get('crawl_limits', 0):,} internal URL(s) were left out by the crawl limits "
                            "(Configuration > Spider > Limits): see the not-crawled list.",
            "not_followed": f"{counts.get('not_followed', 0):,} URL(s) were not followed because a Spider option is "
                            "off (Configuration > Spider): see the not-crawled list.",
            "stopped": "The crawl was stopped before it finished. A stopped crawl can't be resumed: start a new "
                       "crawl to check the rest.",
            "session_lost": "The login ended during the crawl; later member pages were crawled logged out.",
            "error": "The crawl stopped early because of an unexpected error.",
        }
        return {"limit_hit": bool(reasons), "reasons": reasons, "counts": counts,
                "notes": [notes[r] for r in reasons if r in notes],
                "out_of_scope_more": self._out_of_scope_more}

    def _requeue_over_depth(self) -> int:
        """P2: once the queue has drained, the true click depth of every crawled URL (link_depths) decides which URLs
        on the over-depth list are really within Max depth. Parallel workers can find a URL through a long path
        first; it is then crawled too deep and its links fall over the limit although a shorter path exists. Every
        over-depth URL that a crawled URL of true depth d < max_depth points at (the links and follow-ups the crawler
        follows from it, crawl_targets) is queued at d + 1, shallowest first. Called again at every drain until
        nothing new is queued; Max URLs and every other rule still apply. Returns how many were queued."""
        max_depth = int(getattr(self.config, "max_depth", 0) or 0)
        if max_depth <= 0 or self._stop_event.is_set():
            return 0
        with self._lock:
            if not self._over_depth:
                return 0
            results = list(self.results_list)
            over = set(self._over_depth)
        depths = link_depths(results)
        best: Dict[str, Tuple[int, Any]] = {}      # over-depth url -> (true depth of the linking URL, its result)
        for r in results:
            d = depths.get(r.url)
            if d is None or d >= max_depth:
                continue
            for target in crawl_targets(r):
                if target in over and target != r.url and (target not in best or d < best[target][0]):
                    best[target] = (d, r)
        added = 0
        for url in sorted(best, key=lambda u: (best[u][0], u)):
            if self._stop_event.is_set():
                break
            d, parent = best[url]
            source, meta = self._target_origin(parent, url)
            if self._enqueue(url, parent=parent.url, depth=d + 1, source=source, meta=meta):
                added += 1
        if added:
            self.log(f"Queued {added:,} URL(s) that were left over the Max depth of {max_depth}: a shorter path "
                     "through the site reaches them within the limit.")
            self._report_progress()
        return added

    @staticmethod
    def _target_origin(parent, url: str) -> Tuple[str, dict]:
        """(discovery source, link meta) of `url` as one of `parent`'s targets, for a URL queued after the fact."""
        seo = parent.seo or {}
        if parent.redirect_destination and (normalize_url(parent.redirect_destination) or "") == url:
            return "Redirect", {"anchor_text": "", "tag": "redirect", "region": "HTTP redirect", "selector": "",
                                "html": f"{parent.http_status}: {parent.url} -> {url}"[:240],
                                "raw_href": parent.redirect_destination}
        for link in seo.get("links") or ():
            if link.get("url") == url:
                raw = str(link.get("raw") or url)
                anchor = str(link.get("anchor") or "")
                return "Page Navigation", {"anchor_text": anchor, "tag": "a", "region": link.get("region") or "",
                                           "selector": "", "html": f'<a href="{raw}">{anchor}</a>'[:240],
                                           "raw_href": raw, "line": link.get("line") or 0}
        for res in seo.get("resources") or ():
            if res.get("url") == url:
                tag = RESOURCE_LINK_TAGS.get(res.get("tag", ""), res.get("tag", "") or res.get("type", ""))
                raw = res.get("raw", "") or url
                attr = "srcset" if res.get("candidate") else "src"
                return "Resource", {"anchor_text": res.get("alt", ""), "tag": tag, "region": res.get("region", ""),
                                    "selector": "", "html": f'<{res.get("tag", tag)} {attr}="{raw}">'[:240],
                                    "raw_href": raw, "line": res.get("line") or 0}
        for key, kind in (("meta_refresh_url", "meta_refresh"), ("http_refresh_url", "http_refresh")):
            if seo.get(key) == url:
                return REFRESH_SOURCES[kind], {"anchor_text": "", "tag": kind.replace("_", " "),
                                               "region": "Redirect", "selector": "", "html": "", "raw_href": url}
        if js_redirect_target(seo) == url:
            return REFRESH_SOURCES["js_redirect"], {"anchor_text": "", "tag": "js redirect", "region": "Redirect",
                                                    "selector": "", "html": "", "raw_href": url}
        return "Page Navigation", {"anchor_text": "", "tag": "link", "region": "Page <head> metadata",
                                   "selector": "", "html": "", "raw_href": url}

    def _release_sitemap_backlog(self) -> int:
        """Queues the XML sitemap URLs held back so far; returns how many were new. They wait until the
        linked pages are crawled, so a page's depth and discovery source come from its links (as in
        Screaming Frog) and only URLs no link reaches are credited to the sitemap. At most
        max_sitemap_only_urls of them are queued (0 = no limit, P9); the others are listed in the Sitemaps tab as
        "Not crawled" and counted (coverage "sitemap_only"). Once Max URLs is reached the rest are not tried."""
        with self._lock:
            pending, self._sitemap_backlog = self._sitemap_backlog, []
        cap = int(getattr(self.config, "max_sitemap_only_urls", 1000) or 0)
        added = left_out = 0
        for sm_url, sm_source in pending:
            if self._stop_event.is_set():
                break
            with self._lock:
                known = sm_url in self.enqueued_urls or sm_url in self.visited_urls
                full = bool(self.config.max_urls and len(self.enqueued_urls) >= self.config.max_urls)
                over = bool(cap and self._sitemap_only_queued >= cap)
                if not known and over and sm_url not in self._over_sitemap_only:
                    self._over_sitemap_only.add(sm_url)
                    left_out += 1
            if known or over:
                continue
            if full:
                # Max URLs reached: the sitemap-only URLs left (within the quota) are found but not crawled, counted
                # with the Max URLs limit as before; the Sitemaps tab lists every one of them as Not crawled
                room = (cap - self._sitemap_only_queued) if cap else len(pending)
                for left_url, left_source in pending[pending.index((sm_url, sm_source)):]:
                    if room <= 0 or self._stop_event.is_set():
                        break
                    if left_url in self.enqueued_urls or left_url in self.visited_urls:
                        continue
                    room -= 1
                    self._enqueue(left_url, parent=left_source, depth=1, source="XML Sitemap")   # -> _over_limit
                break
            sm_meta = {
                "anchor_text": "", "tag": "sitemap", "region": "XML sitemap",
                "selector": "", "html": f"<loc>{sm_url}</loc>"[:240], "raw_href": sm_url,
            }
            if self._enqueue(sm_url, parent=sm_source, depth=1, source="XML Sitemap", meta=sm_meta):
                added += 1
                with self._lock:
                    self._sitemap_only_queued += 1
        if added:
            self.log(f"Queued {added} URLs found only in XML sitemaps.")
            self._report_progress()
        if left_out:
            self.log(f"{left_out:,} more URL(s) found only in XML sitemaps were not crawled: at most {cap:,} "
                     "sitemap-only URLs are crawled (Max sitemap-only URLs). They are listed in the Sitemaps tab as "
                     "Not crawled.")
        return added

    def _passes_url_filters(self, url: str) -> bool:
        """Include/exclude regex configuration (re.search, case-insensitive). The start URL is always crawled."""
        if url in self._always_allowed:
            return True
        if self._exclude_res and any(rx.search(url) for rx in self._exclude_res):
            return False
        if self._include_res and not any(rx.search(url) for rx in self._include_res):
            return False
        return True

    def _index_state(self) -> Tuple[tuple, bool]:
        finished = self.status in FINISHED_STATES and self.phase in ("done", "idle", SNAPSHOT_PHASE)
        return (len(self.results_list), self.external_checked, finished), finished

    def _index_due(self, key: tuple, finished: bool, force: bool) -> bool:
        """The cached index no longer matches the data and may be rebuilt now. While crawling, rebuilds
        are spaced by INDEX_MIN_INTERVAL or INDEX_COST_FACTOR times the last rebuild, whichever is longer;
        once the crawl has finished (or when forced) a changed key rebuilds at once."""
        if self._index_cache is None:
            return True
        if key == self._index_key:
            return False
        if force or finished:
            return True
        interval = max(INDEX_MIN_INTERVAL, INDEX_COST_FACTOR * self._index_cost)
        return time.time() - self._index_time >= interval

    def site_index(self, force: bool = False):
        """(Site, TabIndex) for the SEO tabs, cached. Rebuilt when the results changed (see _index_due);
        `force` skips the waiting interval but never rebuilds data that has not changed. While one
        thread rebuilds, other callers get the previous index at once instead of waiting (callers
        with nothing built yet, or forced ones, wait for the rebuild)."""
        from .seo import analysis, registry
        key, finished = self._index_state()
        cached = self._index_cache
        if cached is not None and not self._index_due(key, finished, force):
            return cached
        if not self._index_lock.acquire(blocking=cached is None or force):
            return cached
        try:
            key, finished = self._index_state()
            if not self._index_due(key, finished, force):
                return self._index_cache
            started = time.time()
            with self._lock:
                results = list(self.results_list)
            with crawl_gc():
                apply_link_depths(results)
                self._apply_resource_weights(results)
                site = analysis.build_site(self)
                index = registry.run_all(site)
            self._index_cache = (site, index)
            self._index_key = key
            self._index_time = time.time()
            self._index_cost = self._index_time - started
            return self._index_cache
        finally:
            self._index_lock.release()

    def _apply_resource_weights(self, results: List["CrawlResult"]):
        """Page weight from the page's own resources: for each HTML page, the known sizes (Content-Length or body)
        of its internal resources (resource_bytes_total) and of those on other hosts (resource_bytes_external),
        and how many had a known size (resources_sized). Resources are checked after their page, so this is
        refreshed with every site index. The other sizes / formats of an image (srcset and <picture> candidates,
        "candidate": the browser loads one of them) are requested and checked but are not page weight."""
        sizes: Dict[str, int] = {}
        for r in results:
            seo = r.seo
            if seo and r.status_code is not None and 200 <= r.status_code < 300:
                size = int(seo.get("transferred_bytes") or seo.get("size_bytes") or 0)
                if size > 0:
                    sizes[r.url] = size
        with self._lock:
            external = {e.url: e.size_bytes for e in self.external_links_list if e.size_bytes}
        for r in results:
            seo = r.seo
            if not seo or not seo.get("is_html"):
                continue
            internal_total = external_total = sized = count = 0
            for res in seo.get("resources") or ():
                if res.get("candidate"):
                    continue
                count += 1
                url = res.get("url") or ""
                if res.get("internal"):
                    size = sizes.get(url)
                    if size:
                        internal_total += size
                        sized += 1
                else:
                    size = external.get(url)
                    if size:
                        external_total += size
                        sized += 1
            seo["resource_bytes_total"] = internal_total
            seo["resource_bytes_external"] = external_total
            seo["resources_sized"] = sized
            seo["resources_count"] = count

    def _apply_origin(self, result: "CrawlResult"):
        """Re-points a stored result at its current link origin (caller holds the lock)."""
        origin = self.link_origin.get(result.url) or {}
        self._tally(result, -1)
        result.parent_url = self._origin_parent.get(result.url, result.parent_url)
        result.anchor_text = origin.get("anchor_text", "")
        result.link_tag = origin.get("tag", "")
        result.link_region = origin.get("region", "")
        result.link_selector = origin.get("selector", "")
        result.link_html = origin.get("html", "")
        result.raw_href = origin.get("raw_href", "")
        result.link_line = origin.get("line") or 0
        result.source_file = origin.get("source_file", "")
        self._tally(result, +1)

    def _link_code_siblings(self):
        """For each broken URL found in JavaScript, note working pages read from the same file.
        A dead path listed next to /faq and /admin is most likely a menu item users can click."""
        with self._lock:
            working: Dict[str, List[str]] = {}
            for r in self.results_list:
                if r.source_file and r.category == "2xx":
                    working.setdefault(r.source_file, []).append(urlparse(r.url).path or "/")
            for r in self.results_list:
                if r.category == "4xx" and found_in_code(r) and r.source_file:
                    r.code_siblings = sorted(working.get(r.source_file, []))[:5]

    def _link_greyed_items(self):
        """A code-found path answering 4xx whose menu item a rendered page shows greyed out (disabled) gets that
        item recorded (code_greyed): reports then say "greyed out, not clickable" and screenshots box the item.
        Matched by the path's last segment or by the label written next to the path in the bundle."""
        ext = self.dynamic_extractor
        greyed = dict(getattr(ext, "disabled_items", None) or {}) if ext is not None else {}
        if not greyed:
            return
        with self._lock:
            todo = [r for r in self.results_list if r.category == "4xx" and found_in_code(r)]
        labels: Dict[str, str] = {}
        texts: Dict[str, str] = {}
        for r in todo:
            if r.source_file and r.source_file not in texts and len(texts) < 5 and not self._stop_event.is_set():
                try:   # source_file is the script's path on the site
                    resp = self.session.get(urljoin(r.url, r.source_file), timeout=min(self.config.timeout, 8))
                    texts[r.source_file] = resp.text if resp.status_code == 200 else ""
                except Exception:
                    texts[r.source_file] = ""
            text = texts.get(r.source_file or "", "")
            if text:
                labels[r.url] = bundle_route_label(text, urlparse(r.url).path or "/")
        for r in todo:
            keys = {_label_key(urlparse(r.url).path.rstrip("/").rsplit("/", 1)[-1]), _label_key(labels.get(r.url, ""))}
            keys.discard("")
            for page in sorted(greyed, key=lambda p: p != r.parent_url):   # the page it was found on first
                item = next((i for i in greyed[page] if _label_key(i.get("label", "")) in keys), None)
                if item:
                    r.code_greyed = dict(item, page=page)
                    break
        n = sum(1 for r in todo if r.code_greyed)
        if n:
            self.log(f"{n} path(s) found only in JavaScript show as greyed-out menu items (not clickable).")

    def _redirect_hops_of(self, result: "CrawlResult") -> List[Tuple[str, str, dict]]:
        """The redirect-like hops a fetched URL makes: [(normalized target, discovery source, link meta)] for an
        HTTP redirect, and for a page that loaded (2xx) its meta refresh, Refresh header and JavaScript redirect
        (seen in a render). A hop back to the URL itself is left out."""
        seo = result.seo or {}
        out: List[Tuple[str, str, dict]] = []
        dest = normalize_url(result.redirect_destination) if result.redirect_destination else None
        if dest and dest != result.url:
            out.append((dest, "Redirect", {
                "anchor_text": "", "tag": "redirect", "region": "HTTP redirect", "selector": "",
                "html": f"{result.http_status}: {result.url} -> {dest}"[:240], "raw_href": result.redirect_destination,
            }))
        if result.category != "2xx":
            return out   # like links, nothing else is followed from error pages
        refresh = seo.get("meta_refresh_url") or ""
        if refresh and refresh != result.url:
            out.append((refresh, REFRESH_SOURCES["meta_refresh"], {
                "anchor_text": "", "tag": "meta refresh", "region": "Page <head> metadata", "selector": "",
                "html": f'<meta http-equiv="refresh" content="{seo.get("meta_refresh", "")}">'[:240],
                "raw_href": refresh,
            }))
        header = seo.get("http_refresh_url") or ""
        if header and header != result.url:
            out.append((header, REFRESH_SOURCES["http_refresh"], {
                "anchor_text": "", "tag": "http refresh", "region": "HTTP response header", "selector": "",
                "html": f"Refresh: {(result.response_headers or {}).get('refresh', '')}"[:240], "raw_href": header,
            }))
        js = js_redirect_target(seo)
        if js and js != result.url:
            out.append((js, REFRESH_SOURCES["js_redirect"], {
                "anchor_text": "", "tag": "js redirect", "region": "JavaScript (rendered page)", "selector": "",
                "html": f"location -> {js}"[:240], "raw_href": js,
            }))
        return out

    def _redirect_limit_reached(self, result: "CrawlResult", target: str, hops: int):
        """A chain reached max_redirects at `result`: its target is not requested; the URL keeps its own status
        and says why (P4)."""
        limit = int(getattr(self.config, "max_redirects", 10) or 10)
        with self._lock:
            first = target not in self._over_redirects
            self._over_redirects.setdefault(target, result.url)
            note = (f"{REDIRECT_LIMIT}: {limit} redirects were followed to reach this URL, so the next one "
                    f"({target}) was not requested.")
            if not result.error_message or result.error_message == "-":
                result.error_message = note
            elif REDIRECT_LIMIT not in result.error_message:
                result.error_message = f"{result.error_message} {note}"
        if first and len(self._over_redirects) <= TRAP_LOG_PER_REASON:
            self.log(f"{REDIRECT_LIMIT} at {result.url} after {hops - 1} redirects: {target} was not requested "
                     f"(Max redirects {limit}).")

    def _enqueue_followups(self, result: "CrawlResult"):
        """URLs a fetched page points at besides its links: the redirect target, the meta refresh, Refresh header
        and JavaScript redirect targets (one chain follows at most max_redirects of these hops, P4), the hreflang
        URLs of its Link header (P8) and (when crawl_resources is on) its internal images, scripts, stylesheets and
        frames."""
        seo = result.seo or {}
        depth = result.depth + 1
        hops = self._redirect_hops.get(result.url, 0) + 1
        limit = int(getattr(self.config, "max_redirects", 10) or 10)
        for target, source, meta in self._redirect_hops_of(result):
            if source in REFRESH_FOLLOW and not self._follow_on(REFRESH_FOLLOW[source]):
                with self._lock:
                    self._skip_option(target, result.url, f"follow_{REFRESH_FOLLOW[source]}")
                continue
            if hops > limit:
                self._redirect_limit_reached(result, target, hops)
                continue
            if self._enqueue(target, parent=result.url, depth=depth, source=source, meta=meta):
                self._redirect_hops[target] = hops
        if result.category != "2xx":
            return  # like links, nothing else is followed from error pages
        # hreflang URLs sent in the Link response header (PDFs and other files have no <link> elements); HTML
        # hreflang link elements are followed as links (PageParser)
        for h in seo.get("http_hreflang") or ():
            alt = h.get("url") or ""
            if alt and alt != result.url and not self._follow_on("hreflang"):
                with self._lock:
                    self._skip_option(alt, result.url, "follow_hreflang")
                continue
            if alt and alt != result.url:
                self._enqueue(alt, parent=result.url, depth=depth, source="Hreflang", meta={
                    "anchor_text": "", "tag": "link", "region": "HTTP response header", "selector": "",
                    "html": f'<{alt}>; rel="alternate"; hreflang="{h.get("lang", "")}"'[:240], "raw_href": alt,
                })
        if not getattr(self.config, "crawl_resources", True):
            return
        for res in seo.get("resources") or []:
            if not res.get("internal"):
                continue
            kind = RESOURCE_CRAWL_TYPE.get(str(res.get("type") or res.get("tag") or "").lower(), "")
            off = ""
            if kind and not self._crawl_type_on(kind):
                off = f"type_{kind}"
            elif kind == "iframes" and not self._follow_on("iframes"):
                off = "follow_iframes"
            if off:
                with self._lock:
                    self._skip_option(res.get("url") or "", result.url, off)
                continue
            tag = RESOURCE_LINK_TAGS.get(res.get("tag", ""), res.get("tag", "") or res.get("type", ""))
            attr = "href" if res.get("tag") == "link" else ("srcset" if res.get("candidate") else "src")
            raw = res.get("raw", "") or res.get("url", "")
            self._enqueue(res["url"], parent=result.url, depth=depth, source="Resource", meta={
                "anchor_text": res.get("alt", ""), "tag": tag, "region": res.get("region", ""), "selector": "",
                "html": f'<{res.get("tag", tag)} {attr}="{raw}">'[:240], "raw_href": raw, "line": res.get("line") or 0,
            })
        # CSS background images (style attributes, <style> blocks and, on rendered pages, the computed styles):
        # requested like <img> so the Images tab shows their status and size
        for bg in self._background_urls(seo):
            if is_internal_url(bg, self.base_domain, include_subdomains=self.config.include_subdomains,
                               allowed_domains=self.allowed_domains) or self.on_cdn(bg):
                self._enqueue(bg, parent=result.url, depth=depth, source="Resource", meta={
                    "anchor_text": "", "tag": "img", "region": "CSS background image", "selector": "",
                    "html": f"background-image: url({bg})"[:240], "raw_href": bg,
                })

    @staticmethod
    def _background_urls(seo: Dict[str, Any]) -> List[str]:
        """The page's CSS background image URLs (HTML and rendered), normalized, without data: URIs."""
        urls: List[str] = []
        rendered = seo.get("rendered") if isinstance(seo.get("rendered"), dict) else {}
        for raw in list(seo.get("background_images") or []) + list(rendered.get("background_images") or []):
            if not isinstance(raw, str) or not raw.startswith(("http://", "https://")):
                continue
            url = normalize_url(raw)
            if url and url not in urls:
                urls.append(url)
        return urls[:200]

    # ------------------------------------------------------------------------------------------
    # External link checks (status only; external pages are never crawled for links)
    # ------------------------------------------------------------------------------------------
    def _make_external_session(self) -> requests.Session:
        """A plain session: no login cookies or auth headers leave the crawled site. It also builds
        the robots.txt policy for other hosts (Screaming Frog obeys theirs before checking a link)."""
        s = requests.Session()
        workers = max(2, min(self.concurrency, 16))
        adapter = HTTPAdapter(max_retries=0, pool_connections=max(10, workers * 2), pool_maxsize=workers)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        self._guard(s)
        s.headers.update({"User-Agent": external_check_user_agent(self.config.user_agent), "Accept": "*/*",
                          "Accept-Language": "en-US,en;q=0.9"})
        if self.proxies:
            s.proxies.update(self.proxies)
        self._ext_robots = RobotsPolicy(
            s, agent=self.config.robots_user_agent, timeout=min(self.config.timeout, 10),
            verify_ssl=self.config.verify_ssl) if self.config.respect_robots else None
        return s

    def _with_external_resources(self, result: "CrawlResult", new_external: Optional[Dict[str, dict]]) -> Dict[str, dict]:
        """Adds the page's resources on other hosts (scripts, stylesheets, images, iframes) to its external
        links, after the hyperlinks: Screaming Frog lists and checks them in the External tab too. Same rule
        as internal resources: only from pages that loaded (2xx) and only when crawl_resources is on."""
        out = dict(new_external or {})
        if not getattr(self.config, "crawl_resources", True) or result.category != "2xx":
            return out
        for res in (result.seo or {}).get("resources") or ():
            url = res.get("url") or ""
            if res.get("internal") or not url.startswith(("http://", "https://")) or url in out:
                continue
            out[url] = {"url": url, "domain": get_domain(url), "parent_url": result.url,
                        "anchor_text": res.get("alt") or "-", "discovery_source": "Resource"}
        for url in self._background_urls(result.seo or {}):
            if url in out or self.on_cdn(url) or is_internal_url(
                    url, self.base_domain, include_subdomains=self.config.include_subdomains,
                    allowed_domains=self.allowed_domains):
                continue
            out[url] = {"url": url, "domain": get_domain(url), "parent_url": result.url,
                        "anchor_text": "-", "discovery_source": "Resource"}
        return out

    def _with_external_hops(self, result: "CrawlResult", new_external: Dict[str, dict]) -> Dict[str, dict]:
        """Adds a page's meta refresh, Refresh header and JavaScript redirect targets on other websites to its
        external links (checked like any external link; an HTTP redirect to another site is added when it is
        fetched). Not past the Max redirects limit (P4)."""
        hops = self._redirect_hops.get(result.url, 0) + 1
        if hops > int(getattr(self.config, "max_redirects", 10) or 10):
            return new_external
        out = new_external
        for target, source, _ in self._redirect_hops_of(result):
            if source == "Redirect" or target in out or not target.startswith(("http://", "https://")):
                continue
            if is_internal_url(target, self.base_domain, include_subdomains=self.config.include_subdomains,
                               allowed_domains=self.allowed_domains):
                continue
            out = dict(out)
            out[target] = {"url": target, "domain": get_domain(target), "parent_url": result.url,
                           "anchor_text": "-", "discovery_source": source}
        return out

    def _submit_external_check(self, ext: ExternalLink):
        if self._ext_executor is None or self._stop_event.is_set():
            return
        try:
            self._ext_futures.append(self._ext_executor.submit(self._check_external, ext))
        except RuntimeError:
            pass  # executor already shut down

    def _check_external(self, ext: ExternalLink):
        """GETs the URL's headers (the body is not downloaded) the way Screaming Frog checks a link:
        its own status, Content-Type and Location, redirects not followed. A URL the other site's
        robots.txt disallows is not requested and is recorded as blocked. An error answer is
        confirmed with a browser User-Agent (see EXTERNAL_CHECK_USER_AGENT) before it is kept.
        Waits while the crawl is paused (stop() releases the wait)."""
        self._pause_event.wait()
        if self._stop_event.is_set():
            return
        robots = getattr(self, "_ext_robots", None)
        if robots is not None:
            allowed, rule = robots.check(ext.url)
            if not allowed:
                with self._lock:
                    ext.status_code, ext.http_status = None, "Blocked by robots.txt"
                    ext.validation_status = f"Blocked by robots.txt ({rule})"
                    self.external_checked += 1
                return
            self._pause_event.wait()   # the robots.txt fetch can take a while; re-check before the request
            if self._stop_event.is_set():
                return
        code, text, ctype, location, elapsed, size, etype, kept, meta = self._external_request(ext.url, None)
        self._pause_event.wait()
        if (code is None or code >= 400) and not self._stop_event.is_set():
            retry = self._external_request(ext.url, external_fallback_user_agent(self.config.user_agent))
            if retry[0] is not None and retry[0] < 400:
                code, text, ctype, location, elapsed, size, etype, kept, meta = retry
        with self._lock:
            ext.meta_refresh = meta
            ext.meta_refresh_url = refresh_target(meta, ext.url) if meta else ""
            ext.refresh_checked = True
            ext.status_code = code
            ext.http_status = text
            ext.content_type = ctype
            ext.response_time_ms = elapsed
            ext.redirect_destination = location
            ext.size_bytes = size
            ext.error_type = etype
            ext.response_headers = kept
            ext.validation_status = text or ext.validation_status
            self.external_checked += 1

    def _external_request(self, url: str, user_agent: Optional[str]) -> tuple:
        """One GET without following redirects: (status code or None, status text, Content-Type,
        absolute Location of a 3xx or None, response time in ms or None, Content-Length or None,
        error_type: the root cause of no answer, "waf" for a bot-challenge header, else "",
        the EXTERNAL_KEPT_HEADERS of the answer, the meta refresh of a 2xx HTML answer: its content, "" for
        none, None when the body was not read). Only a 2xx HTML body is read, at most EXTERNAL_BODY_BYTES."""
        timeout = (min(self.config.timeout, 5), min(self.config.timeout, 10))
        headers = {"User-Agent": user_agent} if user_agent else None
        start = time.time()
        try:
            resp = self._ext_session.get(url, timeout=timeout, verify=self.config.verify_ssl, headers=headers,
                                         allow_redirects=False, stream=True)
            meta: Optional[str] = None
            try:
                if 200 <= resp.status_code < 300 and "html" in (resp.headers.get("content-type") or "").lower():
                    meta = meta_refresh_in(self._external_head_text(resp))
            except Exception:
                meta = None           # the body could not be read: no verdict on a meta refresh
            finally:
                resp.close()
            location = redirect_location(resp)
            length = (resp.headers.get("content-length") or "").strip()
            lowered = _lower_headers(resp.headers)
            waf = resp.status_code >= 400 and challenge_header(lowered)
            kept = {k: lowered[k][:300] for k in EXTERNAL_KEPT_HEADERS if k in lowered}
            return (resp.status_code, status_line(resp.status_code, resp.reason),
                    (resp.headers.get("content-type") or "").strip().lower(),
                    join_url(url, location) if location else None,
                    int((time.time() - start) * 1000), int(length) if length.isdigit() else None,
                    "waf" if waf else "", kept, meta)
        except requests.exceptions.Timeout as e:
            text, kind = "Connection Timeout", classify_error(e)
        except requests.exceptions.SSLError as e:
            text, kind = "SSL Certificate Error", classify_error(e)
        except requests.exceptions.ConnectionError as e:
            text, kind = "Connection Failed", classify_error(e)
            if kind == "blocked_address":      # P24: the plain reason ("Not opened: ... points to ...")
                text = str(e)[:200] or "Not Opened (Blocked Address)"
        except Exception as e:
            text, kind = f"Request Exception: {e}"[:120], classify_error(e)
        return None, text, "", None, None, None, kind, {}, None

    @staticmethod
    def _external_head_text(resp) -> str:
        """The first EXTERNAL_BODY_BYTES of a streamed answer as text (decompressed, charset of the answer)."""
        data = bytearray()
        for chunk in resp.iter_content(16384):
            data += chunk
            if len(data) >= EXTERNAL_BODY_BYTES:
                break
        raw = bytes(data[:EXTERNAL_BODY_BYTES])
        try:
            return raw.decode(resp.encoding or "utf-8", errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    def _finish_external_checks(self):
        """Waits for outstanding external checks (cancels them when the crawl was stopped)."""
        if self._ext_executor is None:
            return
        pending = [f for f in self._ext_futures if not f.done()]
        if pending and not self._stop_event.is_set():
            self.phase = "checking external links"
            self.log(f"Checking the status of {len(pending)} remaining external link(s)...")
            while pending and not self._stop_event.is_set():
                _, not_done = wait_futures(pending, timeout=0.5)
                pending = list(not_done)
                self._report_progress()
        self._ext_executor.shutdown(wait=not self._stop_event.is_set(), cancel_futures=True)
        self._ext_executor = None
        checked = sum(1 for e in self.external_links_list if e.http_status)
        if checked:
            broken = sum(1 for e in self.external_links_list
                         if e.http_status and (e.status_code is None or e.status_code >= 400))
            self.log(f"External links checked: {checked}, broken or unreachable: {broken}")

    def _tally(self, result: "CrawlResult", sign: int):
        """Running counts behind get_stats (caller holds the lock); kept incremental so
        stats stay O(1) on large crawls. Mirrors issues.health_score."""
        cat = result.category
        self._tallies[cat] += sign
        if cat == "4xx" and found_in_code(result):
            self._tallies["4xx_code"] += sign
        if result.resource_type == "file":
            self._tallies["files"] += sign
        elif cat != "blocked":
            self._tallies["pages"] += sign
            if cat in ("2xx", "3xx"):
                self._tallies["pages_ok"] += sign

    def run(self) -> List[CrawlResult]:
        """Executes the full automatic crawl procedure. Whatever happens inside, the crawl ends in a finished
        state ("auth_failed" when the login itself broke, else "stopped") and its browser is released: an
        error used to leave the status "running" forever, with Chrome holding its slot. An error after the
        login is raised again once the crawl is marked as stopped."""
        try:
            with crawl_gc():
                return self._run()
        except Exception as e:
            if not self._end_after_error(e):
                raise
            return self.results_list
        finally:
            # The (logged-in) browser stays open briefly for screenshots and re-checks, then closes itself;
            # nothing was crawled after a failed login, so it closes now
            if self.status == "auth_failed":
                self.browser.close()
            else:
                self.browser.close_when_idle(BROWSER_IDLE_CLOSE)

    def _log_settings(self):
        """Logs the settings that change what is crawled (never a header value or the proxy login)."""
        if self.crawl_scope == "subfolder":
            self.log(f"Crawl scope: Subfolder - only URLs under {self._scope_folder} on this host are crawled.")
        elif self.crawl_scope == "exact_url":
            self.log("Crawl scope: Exact URL - only the start URL is crawled (its resources and redirects are "
                     "checked, its links recorded).")
        elif self.crawl_scope == "all_subdomains":
            self.log("Crawl scope: All Subdomains - every subdomain of the site counts as internal.")
        if self.extra_hosts:
            shown = ", ".join(urlparse(h).hostname or h for h in self.extra_hosts[:8])
            more = f" and {len(self.extra_hosts) - 8} more" if len(self.extra_hosts) > 8 else ""
            self.log(f"Also crawling {len(self.extra_hosts)} subdomain(s) chosen under Find subdomains, each from its "
                     f"home page: {shown}{more}.")
        if self.url_rules is not DEFAULT_RULES:
            self.log("URL Rewriting is on: URLs found during the crawl are rewritten before they are queued.")
        if (getattr(self.config, "custom_robots_txt", "") or "").strip():
            if self.config.respect_robots:
                self.log("Custom robots.txt used for this crawl: its rules replace the site's live robots.txt "
                         f"(obeyed as {self.config.robots_user_agent}).")
            else:
                self.log("A custom robots.txt was given, but 'Respect robots.txt' is off, so no robots.txt rules "
                         "are obeyed in this crawl.")
        if getattr(self.config, "sitemap_urls", None) and self.config.use_sitemaps:
            self.log(f"Also reading {len(self.config.sitemap_urls)} XML sitemap address(es) given in the settings.")
        if self.request_headers:
            self.log("Sending custom HTTP header(s) to the crawled site: " + ", ".join(sorted(self.request_headers)))
        if getattr(self.config, "_url_auth", False):
            self.log("The user name and password written in the start URL are sent to the crawled site as HTTP "
                     "Basic authentication; the URLs are listed without them.")
        if self.proxies:
            proxy = getattr(self.config, "proxy", None) or {}
            self.log(f"Requests go through the proxy {str(proxy.get('host') or '').strip()}"
                     + (f":{proxy.get('port')}" if proxy.get("port") else "")
                     + (" (with a login)" if proxy.get("username") else ""))
        opts = self.extract_options
        if opts.searches or opts.extractors:
            self.log(f"Custom search: {len(opts.searches)} rule(s); custom extraction: {len(opts.extractors)} rule(s)"
                     + (" (run on the rendered HTML of each page rendered in Chrome)." if self.config.render_all
                        else "."))
        for note in self.config_notes:
            self.log(f"Setting ignored: {note}")

    def _end_after_error(self, e: Exception) -> bool:
        """Marks the crawl as finished after an unexpected error: "auth_failed" (returns True: handled) when it
        broke during the login, else "stopped" with a limitation note (returns False: the caller re-raises)."""
        detail = f"{type(e).__name__}: {(str(e).splitlines() or [''])[0][:160]}".rstrip(": ")
        self._stop_event.set()
        self._post_stop.set()
        self._pause_event.set()
        self.end_time = self.end_time or time.time()
        self.phase, self.phase_detail = "done", ""
        if self._ext_executor is not None:
            try:
                self._ext_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        if self._logging_in:
            self._logging_in = False
            msg = (f"The login stopped with an unexpected error ({detail}). Try again, or log in through the "
                   "live login window.")
            self.auth_used, self.auth_error, self.auth_status = False, msg, msg
            with self._lock:
                self._add_limitation(f"Login failed: {msg}")
            self.status = "auth_failed"
            self.log(f"Login failed, nothing was crawled: {msg}")
            self._report_progress()
            return True
        with self._lock:
            self._add_limitation(f"The crawl stopped early because of an unexpected error ({detail}); the results "
                                 "cover only the URLs crawled before it.")
        self._error_stop = True
        self.status = "stopped"
        self.log(f"The crawl stopped early because of an unexpected error: {detail}")
        self._report_progress()
        return False

    def _run(self) -> List[CrawlResult]:
        self.status = "running"
        self.phase = "crawling"
        self.start_time = time.time()
        speed = f", max {self.rate_limiter.rate:g} URLs/s" if self.rate_limiter else ""
        self.log(f"Starting automatic crawl for: {self.start_url} (Workers: {self.concurrency}{speed})")
        # Find subdomains: what this server found for the domain so far (kept with the crawl for its subdomain
        # report); extra hosts without DNS or on a private network are dropped before anything is queued
        try:
            from .subdomains import last_discovery
            self.subdomain_discovery = last_discovery(self.start_url)
        except Exception:
            self.subdomain_discovery = None
        if self.extra_hosts:
            self._check_extra_hosts()
        self._log_settings()

        # 0. Execute Authentication if configured
        post_login_url = None
        auth_links = set()
        if self.config.auth and self.config.auth.enabled:
            self._logging_in = True
            login = self._login()
            self._logging_in = False
            if login is None:
                return self.results_list
            post_login_url, auth_links = login

        # 1. Enqueue Start URL & Authenticated Destinations (List mode: the listed URLs only)
        parsed_start = urlparse(self.start_url)
        root_url = f"{parsed_start.scheme}://{parsed_start.netloc}/"

        if post_login_url:
            self._always_allowed.add(post_login_url)
            if self.mode != "list":
                self._enqueue(post_login_url, parent="Authentication Portal", depth=0,
                              source="Authenticated Landing Page")

        if self.mode == "list":
            self._seed_list()
        else:
            # The domain root is crawled too unless the scope keeps the crawl to the start URL or its folder
            if self.start_url.rstrip('/') != root_url.rstrip('/') and \
                    self.crawl_scope in ("subdomain", "all_subdomains"):
                self._enqueue(root_url, parent="Domain Root", depth=0, source="Root Domain")
                self._start_chain.update({root_url, normalize_url(root_url) or root_url})
            self._start_chain.add(self.start_url)
            if getattr(self.config, "cookie_storage", "session") == "none":
                self._no_new_cookies()
            self._enqueue(self.start_url, parent="Direct Input", depth=0, source="Start URL")
            # Hosts chosen under Find subdomains: each home page is an extra start URL
            for home in list(self.extra_hosts):
                self._enqueue(home, parent="Find Subdomains", depth=0, source="Subdomain")

        if auth_links and self.mode != "list":
            self.log(f"Enqueuing {len(auth_links)} internal pages discovered from authenticated session...")
            for alink in auth_links:
                self._enqueue(alink, parent=post_login_url or self.start_url, depth=1, source="Authenticated Dashboard")

        # 2. Probe robots.txt and XML Sitemaps in background thread (non-blocking). Not in List mode or for an
        # Exact URL crawl, which crawl only what they were given.
        sitemap_thread = None
        if self.config.use_sitemaps and self.mode != "list" and self.crawl_scope != "exact_url":
            with self._lock:
                allowed = set(self.allowed_domains)   # a copy: the crawl may add to the set meanwhile

            # P9: the files read, the URL index and the sitemap addresses given in the settings; P3: a custom
            # robots.txt is read for its Sitemap lines instead of the live file
            custom_robots = getattr(self.config, "custom_robots_txt", "") or ""
            index_limit = int(getattr(self.config, "max_sitemap_index_urls", 100_000) or 100_000)
            self.sitemap_files_limit = int(getattr(self.config, "max_sitemap_files", 100) or 100)

            def probe_sitemaps():
                try:
                    self.log("Probing robots.txt and XML sitemaps in background...")
                    sitemap_parser = SitemapParser(
                        base_url=self.start_url,
                        base_domain=self.base_domain,
                        user_agent=self.config.user_agent,
                        timeout=min(self.config.timeout, 5),
                        verify_ssl=self.config.verify_ssl,
                        logger=self.log,
                        stop_event=self._stop_event,
                        max_sitemaps=self.sitemap_files_limit,
                        max_urls=index_limit,
                        index_limit=index_limit,
                        include_subdomains=self.config.include_subdomains,
                        headers=self.request_headers,
                        proxies=self.proxies,
                        allowed_domains=allowed,
                        on_site=self.on_site,
                        extra_sitemaps=list(getattr(self.config, "sitemap_urls", None) or ()),
                        robots_text=custom_robots if custom_robots.strip() else None,
                    )
                    self._guard(sitemap_parser.session)
                    sitemap_links = sitemap_parser.parse_all()
                    with self._lock:
                        self.sitemap_hosts = dict(sitemap_parser.hosts_seen)
                        self.robots_hosts = set(sitemap_parser.robots_hosts)
                        self.sitemap_index = {u: list(srcs) for u, srcs in sitemap_parser.url_sources.items()}
                        self.sitemap_lastmod = dict(sitemap_parser.url_lastmod)
                        self.sitemap_stats = {sm: dict(st) for sm, st in sitemap_parser.sitemap_stats.items()}
                        self.sitemaps_not_read = list(sitemap_parser.not_read)
                        self.sitemap_index_full = dict(sitemap_parser.index_full) if sitemap_parser.index_full \
                            else None
                        self.sitemap_hreflang = sitemap_parser.url_hreflang
                        self.sitemap_hreflang_capped = sitemap_parser.hreflang_capped
                        self._sitemap_backlog.extend(sitemap_links)
                    if sitemap_links:
                        self.log(f"Found {len(sitemap_links)} URLs in XML sitemaps; the ones no link reaches "
                                 "are crawled after the linked pages.")
                except Exception as e:
                    self.log(f"Sitemap background check completed: {e}")

            sitemap_thread = threading.Thread(target=probe_sitemaps, daemon=True)
            sitemap_thread.start()

        # External link status checks run alongside the crawl in their own small pool
        if self.check_external and self.config.collect_external_links:
            self._ext_session = self._make_external_session()
            self._ext_executor = ThreadPoolExecutor(max_workers=max(2, min(self.concurrency, 16)),
                                                    thread_name_prefix="extcheck")

        # 3. Multi-threaded worker pool for Breadth-First-Search crawl (Starts IMMEDIATELY). Not a `with` block:
        # leaving one waits for every worker without a limit, so a single hung worker would keep a stopped
        # crawl "finishing" for ever (see STOP_GRACE_SECONDS below)
        executor = ThreadPoolExecutor(max_workers=self.concurrency)
        futures = {}
        try:
            while not self._stop_event.is_set():
                self._pause_event.wait() # Check for paused state

                # Assign tasks while queue has items and workers available
                while len(self.queue) > 0 and len(futures) < self.concurrency:
                    with self._lock:
                        item = self._next_item()
                    if item is None:
                        break

                    future = executor.submit(self._crawl_single_page, item)
                    futures[future] = item

                # Linked pages all done: first the URLs left over the Max depth that the true click depth puts
                # within it (P2), then the sitemap-only URLs. The crawl waits for the sitemap probe (at
                # most SITEMAP_WAIT_SECONDS): a big sitemap can take longer than a small site, and without it the
                # sitemap-only URLs would go uncrawled and every Sitemaps filter would say no sitemap was found
                if not futures and len(self.queue) == 0:
                    if self._requeue_over_depth():
                        continue
                    if self._release_sitemap_backlog():
                        continue
                    if sitemap_thread and sitemap_thread.is_alive():
                        started, told = time.time(), False
                        while sitemap_thread.is_alive() and len(self.queue) == 0 and not self._stop_event.is_set() \
                                and time.time() - started < SITEMAP_WAIT_SECONDS:
                            if not told and time.time() - started > 3:
                                told = True
                                self.log("All linked pages are crawled; waiting for the XML sitemaps to finish "
                                         "loading...")
                            sitemap_thread.join(0.2)
                        if sitemap_thread.is_alive() and not self._stop_event.is_set() and len(self.queue) == 0:
                            self.log(f"The XML sitemaps were still loading after {SITEMAP_WAIT_SECONDS} seconds; "
                                     "the crawl ends without the URLs found only in them.")
                        if len(self.queue) > 0 or self._release_sitemap_backlog():
                            continue
                    break

                # Wake as soon as a worker finishes (a fixed sleep left finished workers idle for up to 50 ms
                # before they got their next URL); the timeout keeps pause / stop / sitemap checks responsive
                done_futures = [f for f in futures if f.done()]
                if not done_futures:
                    if futures:
                        wait_futures(futures, timeout=0.05, return_when=FIRST_COMPLETED)
                    else:
                        time.sleep(0.05)
                    continue

                for fut in done_futures:
                    self._collect(fut, futures.pop(fut))

                # Polite delay if set
                if self.config.delay > 0:
                    time.sleep(self.config.delay)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # Stopped: the requests already under way may finish (STOP_GRACE_SECONDS); keep what they fetched
        # (workers that had not sent their request yet put their URL back in the queue)
        if futures:
            wait_futures(list(futures), timeout=STOP_GRACE_SECONDS)
        stuck = [item for fut, item in futures.items() if not fut.done()]
        for fut, item in list(futures.items()):
            if fut.cancelled():        # never started: not crawled, so it goes back to the queue
                with self._lock:
                    self.visited_urls.discard(item[0])
                    self.queue.appendleft(item)
            elif fut.done():
                self._collect(fut, item)
        futures.clear()
        if stuck:
            with self._lock:
                for item in stuck:
                    self.visited_urls.discard(item[0])   # no result: not counted as crawled
            self.log(f"{len(stuck)} page(s) were still loading {STOP_GRACE_SECONDS} s after Stop and were left out: "
                     + ", ".join(item[0] for item in stuck[:3]))

        self._finish_external_checks()
        self._link_code_siblings()
        try:
            self._link_greyed_items()
        except Exception as e:
            self.log(f"Greyed menu item check skipped: {type(e).__name__}")
        with self._lock:
            apply_link_depths(self.results_list)
            finished = list(self.results_list)
        self._apply_resource_weights(finished)
        self._crawl_done.set()   # from here on, Stop only skips the remaining post-crawl checks

        total_time = round(time.time() - self.start_time, 2)
        ext_count = len(self.external_links)
        ext_note = ("status checked; their pages are not crawled"
                    if self.check_external and self.config.collect_external_links else "recorded only, not checked")
        self.log(
            f"Crawl finished in {total_time}s! Discovered: {len(self.enqueued_urls)} internal, "
            f"{ext_count} external links ({ext_note}), Crawled: {len(self.results_list)}"
        )

        # robots.txt audit, URL-trap probe, AI/search-bot check (status stays "running" meanwhile); a crawl that
        # a new one replaced skips them and is not saved to the crawl history
        if not self.superseded:
            self._run_post_crawl()
        self.end_time = time.time()
        if self.status != "stopped":
            self.status = "completed"
        if not self.superseded:
            self._save_snapshot()
        self.phase = "done"
        self.phase_detail = ""

        for note in self.limit_notes():
            self.log(note)
        self._report_progress()
        return self.results_list

    # ------------------------------------------------------------------------------------------
    # After the crawl: post-crawl checks and the crawl snapshot
    # ------------------------------------------------------------------------------------------
    def _run_post_crawl(self):
        """Runs crawler/seo/postcrawl.py (robots.txt audit, URL-trap probe, bot check) when
        config.post_crawl_checks is on; each report says why when a check did not run."""
        try:
            from .seo import postcrawl
        except Exception as e:
            self.log(f"Post-crawl checks unavailable: {e}")
            return
        analysis = getattr(self.config, "analysis", None) or {}
        manual = analysis.get("auto") is False
        if not getattr(self.config, "post_crawl_checks", True) or manual or analysis.get("post_crawl_checks") is False:
            # Off (or the caller runs them itself afterwards, as web/app.py and cli.py do; or Crawl Analysis is set
            # to run on request): the reports say why until then, so their tabs show "Not checked", not "passed"
            why = ("Crawl Analysis is set to run on request (Crawl Analysis > Start)" if manual
                   else "post-crawl checks are turned off for this crawl")
            for attr in POST_CRAWL_ATTRS.values():
                setattr(self, attr, postcrawl.status_report("skipped", why))
            self.post_crawl = self._post_crawl_states()
            return
        serverless = _serverless()
        options = {
            "stop_event": self._post_stop,
            "on_progress": self._post_crawl_progress,
            "bot_sample": max(1, int(getattr(self.config, "bot_check_sample", 10) or 10)),
            "render": bool(self.config.use_dynamic_js),
            "bot_check": not serverless and analysis.get("bot_check") is not False,
        }
        for step in ("robots_audit", "trap_probe"):   # optional per-step switches of the analysis settings
            if analysis.get(step) is False:
                options[step] = False
        started = time.time()
        try:
            postcrawl.run(self, options)
        except Exception as e:   # postcrawl.run never raises; a crawl must never fail here either
            self.log(f"Post-crawl checks failed: {e}")
        if serverless:
            self.bot_report = postcrawl.status_report(
                "skipped", "this server runs in serverless mode, whose time limit is too short for the bot check")
        elif self._post_stop.is_set() and not self._stop_event.is_set():
            for attr in POST_CRAWL_ATTRS.values():
                rep = getattr(self, attr, None)
                if isinstance(rep, dict) and rep.get("status") == "skipped" and "stopped" in (rep.get("reason") or ""):
                    rep["reason"] = "stopped by the user before it started"
        self.post_crawl = self._post_crawl_states()
        self._index_key = None   # the new reports change what the tabs show
        self.log(f"Post-crawl checks finished in {round(time.time() - started, 1)}s: "
                 + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in self.post_crawl.items()))

    def _post_crawl_progress(self, step: str, message: str):
        self.phase = POST_CRAWL_PHASES.get(step, self.phase)
        self.phase_detail = message or ""
        self._report_progress()

    def _post_crawl_states(self) -> Dict[str, str]:
        """{check: status} of the post-crawl reports that exist (see crawler/seo/postcrawl.py)."""
        out = {}
        for name, attr in POST_CRAWL_ATTRS.items():
            rep = getattr(self, attr, None)
            if isinstance(rep, dict):
                out[name] = str(rep.get("status") or "")
        return out

    def _save_snapshot(self):
        """Saves the finished crawl for crawl-to-crawl comparison (crawler/seo/compare.py) under
        config.reports_dir. Builds the site index once; the reports reuse it from the cache."""
        if not getattr(self.config, "save_snapshot", True) or not self.results_list:
            return
        self.phase = SNAPSHOT_PHASE
        self.phase_detail = ""
        self._report_progress()
        try:
            from .seo import compare
            index = self.site_index(force=True)
            self.snapshot_id = compare.save_snapshot(self, index, getattr(self.config, "reports_dir", "") or None)
            if self.snapshot_id:
                self.log(f"Crawl snapshot saved ({self.snapshot_id}) for comparison with later crawls.")
            else:
                self.log("The crawl snapshot could not be saved.")
        except Exception as e:
            self.log(f"The crawl snapshot could not be saved: {e}")

    def _collect(self, fut, item: tuple):
        """Stores one finished worker's result and queues what it found (links, follow-ups, external checks)."""
        try:
            result, new_links, new_external = fut.result()
        except Exception as e:
            self.log(f"Worker exception: {e}")
            with self._lock:
                self.visited_urls.discard(item[0])  # no result: not counted as crawled
            return
        if not result:
            return
        with self._lock:
            if self._list_index:
                result.list_index = self._list_index.get(result.url)
            self.results[result.url] = result
            self.results_list.append(result)
            self._tally(result, +1)
            self._count_loop_answer(result)
            # A real link to this URL turned up while it was being fetched
            if result.link_tag in CODE_LINK_TAGS and result.url not in self._code_only:
                self._apply_origin(result)

        # Record newly discovered external links (NEVER enqueued or crawled)
        newly_added_ext = []
        new_external = self._with_external_hops(result, self._with_external_resources(result, new_external))
        if new_external and self.config.collect_external_links:
            with self._lock:
                for ext_url, ext_data in new_external.items():
                    if ext_url not in self.external_links:
                        ext_obj = ExternalLink(
                            url=ext_data["url"],
                            domain=ext_data.get("domain", ""),
                            parent_url=ext_data.get("parent_url", result.url),
                            anchor_text=ext_data.get("anchor_text", "-"),
                            discovery_source=ext_data.get("discovery_source", "Page Link"),
                            validation_status=ext_data.get("validation_status", "Recorded (Not Followed)")
                        )
                        self.external_links[ext_url] = ext_obj
                        self.external_links_list.append(ext_obj)
                        if ext_obj.domain:
                            self._ext_domains.add(ext_obj.domain)
                        newly_added_ext.append(ext_obj)

        # Enqueue newly discovered internal links FIRST so metrics update immediately
        skip = self._not_followed_links(result, new_links) if new_links else {}
        for link, link_meta in new_links.items():
            if link in skip:
                with self._lock:
                    self._skip_option(link, result.url, skip[link])
                continue
            self._enqueue(url=link, parent=result.url, depth=result.depth + 1, source="Page Navigation",
                          meta=link_meta)
        if self.mode == "list" and result.depth == 0 and getattr(self.config, "list_follow_canonicals", False):
            for canon in self._urls_in((result.seo or {}).get("canonicals")):
                if canon != result.url:
                    self._enqueue(canon, parent=result.url, depth=1, source="List Canonical", meta={
                        "anchor_text": "canonical link", "tag": "link", "region": "Page <head> metadata",
                        "selector": "", "html": f'<link rel="canonical" href="{canon}">'[:240], "raw_href": canon})
        self._enqueue_followups(result)

        for ext_obj in newly_added_ext:
            self._submit_external_check(ext_obj)

        # Invoke callbacks safely outside the lock to prevent deadlocks
        for ext_obj in newly_added_ext:
            try:
                self.on_external_link(ext_obj)
            except Exception:
                pass
        try:
            self.on_result(result)
        except Exception:
            pass
        self._report_progress()

    def _crawl_single_page(self, item: tuple) -> tuple:
        """
        Processes a single internal URL: fetches the URL's own HTTP response (a redirect is not followed
        here; its target is queued and crawled as a URL of its own, as Screaming Frog does), extracts the
        page title, internal child links and external outbound links.
        Returns: (result, new_internal_links, new_external_links). The result is None when the crawl
        was stopped before the request went out; the URL then goes back to the queue, uncounted.
        """
        url, parent_url, depth, source = item

        notes: List[str] = []
        with self._lock:
            # The loop guard may have tripped while this URL waited in the queue: it is then skipped, uncounted
            guarded = self._loop_guarded(url, parent_url, notes)
            if guarded:
                self.enqueued_urls.discard(url)
            else:
                self.visited_urls.add(url)
                # A real link may have replaced a code-only origin while this URL sat in the queue
                parent_url = self._origin_parent.get(url, parent_url)
        for note in notes:
            self.log(note)
        if guarded:
            return None, {}, {}

        req_start = time.time()
        resp_time = 0
        title = ""
        pdf_props = None      # P21: a PDF's properties (seo["pdf"])
        status_code = None
        status_text = "Pending"
        redirect_destination = None
        content_type = ""
        error_msg = None
        server = ""
        new_internal: Dict[str, dict] = {}
        new_external: Dict[str, dict] = {}
        origin = self.link_origin.get(url) or {}
        origin_fields = dict(
            anchor_text=origin.get("anchor_text", ""),
            link_tag=origin.get("tag", ""),
            link_region=origin.get("region", ""),
            link_selector=origin.get("selector", ""),
            link_html=origin.get("html", ""),
            raw_href=origin.get("raw_href", ""),
            link_line=origin.get("line") or 0,
            source_file=origin.get("source_file", ""),
        )
        # Refined from the Content-Type once the response arrives
        resource_type = "page" if is_parsable_html_url(url) else "file"

        if self.robots:
            allowed, rule = self.robots.check(url)
            if not allowed:
                with self._lock:
                    self._add_limitation("Some URLs are blocked by robots.txt (turn off 'Respect robots.txt' to crawl them)")
                if depth == 0:
                    self.log(f"Start page {url} is blocked by robots.txt ({rule}). "
                             "Turn off 'Respect robots.txt' to crawl it anyway.")
                return CrawlResult(
                    url=url, page_title="[Blocked by robots.txt]", parent_url=parent_url,
                    http_status="Blocked by robots.txt", depth=depth, discovery_source=source,
                    error_message=f"{rule} (robots.txt, {self.config.robots_user_agent})",
                    resource_type=resource_type, blocked=True, seo=self._no_response_seo(url), **origin_fields
                ), new_internal, new_external

        max_attempts = 2
        attempt = 0
        note = ""          # LOGGED_OUT_NOTE once the crawl's login had ended for good
        seo: Optional[Dict[str, Any]] = None
        response_headers: Dict[str, str] = {}
        error_type = ""   # root cause (net_guard.ERROR_TYPES), "" for a normal answer
        waf = ""          # firewall / bot protection that answered
        # Images, scripts and stylesheets found as page resources are only checked (HEAD), never parsed;
        # frames are documents and are crawled like pages
        head_only = not is_parsable_html_url(url) or (
            source == "Resource" and origin.get("tag") not in FRAME_TAGS)

        while attempt < max_attempts:
            attempt += 1
            if self._stop_event.is_set():
                break
            # Per-host pacing: waits out a 429/503 pause or the slowed-down gap for this host
            if self.throttle is not None and not self.throttle.wait_turn(url, self._stop_event):
                break
            if self.rate_limiter and not self.rate_limiter.acquire(self._stop_event):
                break
            req_start = time.time()

            try:
                # Binary/media assets check (fast HEAD)
                if head_only:
                    head_timeout = (min(self.config.timeout, 5), min(self.config.timeout, 8))
                    head_resp = self.session.head(url, timeout=head_timeout, verify=self.config.verify_ssl,
                                                  allow_redirects=False)
                    code = head_resp.status_code
                    if code in (405, 501) or (code >= 400 and code not in (429, 503)):
                        # Server refuses HEAD, or answers it with an error: what a visitor gets comes from GET
                        # (some servers and CDNs answer HEAD 404/403 for files they serve). The body is not
                        # downloaded. 429/503 are paced below instead.
                        head_resp = self.session.get(url, timeout=head_timeout, verify=self.config.verify_ssl,
                                                     allow_redirects=False, stream=True)
                        head_resp.close()
                    resp_time = int((time.time() - req_start) * 1000)
                    status_code = head_resp.status_code
                    head_waf = detect_waf(status_code, _lower_headers(head_resp.headers))
                    requeued, head_error = self._pace_answer(item, head_resp, head_waf)
                    if requeued:
                        return None, {}, {}   # 429/503: back in the queue, retried after the host's pause
                    # No header means no Content-Type: never record a type the server did not send
                    content_type = head_resp.headers.get("content-type", "") or ""
                    redirect_destination = self._redirect_target(url, head_resp, depth)
                    seo = extract_headers_only(head_resp, url)
                    seo["final_url"] = redirect_destination or url
                    seo["response_time_ms"] = resp_time
                    if 200 <= status_code < 300 and "application/pdf" in content_type.lower():
                        title, seo["pdf"] = self._pdf_read(url)
                        if title:
                            seo["titles"] = [title]
                    if redirect_destination:
                        new_external.update(self._external_redirect(url, redirect_destination))

                    result = CrawlResult(
                        url=url,
                        page_title=f"[Asset] {url.split('/')[-1]}",
                        parent_url=parent_url,
                        http_status=status_line(status_code, head_resp.reason),
                        status_code=status_code,
                        redirect_destination=redirect_destination,
                        depth=depth,
                        discovery_source=source,
                        content_type=content_type,
                        response_time_ms=resp_time,
                        server=self._server_label(head_resp.headers),
                        resource_type="file",
                        seo=seo,
                        response_headers=_lower_headers(head_resp.headers),
                        **origin_fields
                    )
                    if head_error:
                        result.set_error_type(head_error, head_waf)
                    result.retries = self._throttle_requeues.get(url, 0)
                    return result, new_internal, new_external

                # HTML Page Crawling: headers first, the body only when it is needed (and only up to the
                # page-size limit)
                resp = self.session.get(
                    url,
                    timeout=self.request_timeout,
                    verify=self.config.verify_ssl,
                    allow_redirects=False,
                    stream=True,
                )
                resp = self._logged_in_response(item, resp)
                if resp is None:
                    return None, {}, {}   # the session had ended: queued again after logging in again
                note = getattr(resp, "crawler_note", "") or note
                resp_time = int((time.time() - req_start) * 1000)
                status_code = resp.status_code
                # Firewall / bot challenge, then 429/503 pacing (the URL may go back in the queue)
                waf = self._waf_answer(resp, url)
                requeued, error_type = self._pace_answer(item, resp, waf)
                if requeued:
                    return None, {}, {}
                status_text = status_line(status_code, resp.reason)
                content_type = resp.headers.get("content-type", "") or ""
                server = self._server_label(resp.headers)
                response_headers = _lower_headers(resp.headers)
                redirect_destination = self._redirect_target(url, resp, depth)

                # Check access limitations (401 Unauthorized, 403 Forbidden, or redirect to login); a firewall's
                # block page is not a login wall (it is reported as a crawl warning instead)
                if status_code in (401, 403) and not waf:
                    with self._lock:
                        self.auth_required_count += 1
                        self._add_limitation(f"HTTP {status_code} Access Restricted on: {url}")
                elif redirect_destination and redirected_to_login(url, redirect_destination) and not self.auth_used:
                    with self._lock:
                        self.auth_required_count += 1
                        self._add_limitation(f"Redirected to login portal on: {url} -> {redirect_destination}")

                # Only parse HTML pages (strictly skip JSON APIs, XML feeds, and non-HTML payloads)
                ctype = content_type.lower()
                is_html_content = ("html" in ctype) or (not content_type and is_parsable_html_url(url))
                resource_type = "page" if is_html_content else "file"
                if status_code == 205:
                    # 205 Reset Content never carries a body (RFC 9110); without a Content-Length the read
                    # would wait for one until the timeout and report a working URL as No Response
                    resp.close()
                elif 200 <= status_code < 300 and is_html_content and "json" not in ctype and "xml" not in ctype:
                    # One parse serves the SEO extraction, the title and link discovery
                    html_text = self._page_text(resp, url)
                    if not waf and len(html_text) <= WAF_SCAN_CHARS:
                        # A small 200 page can be a bot-challenge interstitial instead of the real page
                        waf = detect_waf(status_code, response_headers, html_text)
                        if waf:
                            error_type = "waf"
                            if self.throttle is not None:
                                self.throttle.blocked(url)
                            self._note_waf(url, waf, status_code)
                    with self._cpu_gate:
                        soup = parse_html(html_text)
                    # Logged in, but the site shows its login form in place of this page: the session ended
                    if self._login_form_instead(url, soup):
                        if not self.session_dead and self._requeued_after_login[url] < MAX_RELOGINS and \
                                self._recover_session(url, check_http=False):
                            resp.close()
                            self._requeue_after_login(item)
                            return None, {}, {}
                        if self.session_dead:
                            note = LOGGED_OUT_NOTE
                    with self._cpu_gate:
                        seo = self._extract_seo(html_text, resp, url, soup)
                    self._keep_source(url, html_text)
                    if self.config.render_all and self.render_hook is not None and not self._stop_event.is_set():
                        try:
                            rendered = self.render_hook(url, seo)
                            if rendered:
                                # Custom Search / Extraction results of the rendered HTML (the render hook
                                # adds them) replace the raw-HTML ones, as in JavaScript rendering mode
                                for key in ("custom_search", "custom_extraction"):
                                    if isinstance(rendered.get(key), dict):
                                        seo[key] = rendered.pop(key)
                                # Cookies tab: the ones Chrome received while rendering join the server's
                                # Set-Cookie ones (as in JavaScript rendering mode)
                                if rendered.get("cookies"):
                                    seo["cookies"] = merge_cookies(seo.get("cookies") or [], rendered["cookies"], url)
                                seo["rendered"] = rendered
                        except Exception as e:
                            self.log(f"Rendering {url} in Chrome failed: {type(e).__name__}")
                    elif getattr(self.config, "a11y_audit", False) and not self.config.render_all \
                            and not self._stop_event.is_set():
                        self._run_audit(url, seo)

                    with self._cpu_gate:
                        # 1. Extract title
                        title = PageParser.extract_title(html_text, soup=soup)
                        if isinstance(seo.get("original_html"), dict):
                            # its main values come from the rendered HTML (JavaScript rendering mode): the
                            # Title column shows the rendered title, as the Page Titles tab does
                            title = _shown_title(seo) or title

                        # 2. Extract internal links AND external links from the same parse
                        new_internal, new_external = PageParser.extract_links(
                            html_content=html_text,
                            current_url=str(resp.url),
                            base_domain=self.base_domain,
                            include_subdomains=self.config.include_subdomains,
                            allowed_domains=self.allowed_domains,
                            exclude_patterns=self.config.exclude_patterns,
                            collect_external=self.config.collect_external_links,
                            known_urls=_SettledUrls(self),
                            soup=soup,
                            max_links=self._max_links(),
                        )
                        # Forms that post their data (login, contact, comments, cart) are not pages to open
                        for action in _post_form_actions(soup, str(resp.url)):
                            if (new_internal.get(action) or {}).get("tag") == "form":
                                del new_internal[action]

                    # 2b. Render every page: links that only exist once the page ran its JavaScript are
                    # followed too (as Screaming Frog does in JavaScript rendering mode)
                    rendered_page = seo.get("rendered") if isinstance(seo, dict) else None
                    if rendered_page:
                        js_internal, js_external = self._rendered_links(rendered_page, str(resp.url))
                        for js_url, js_meta in js_internal.items():
                            # a real link in the rendered page beats the same path seen only in script code
                            if (new_internal.get(js_url) or {}).get("tag", "a") in CODE_LINK_TAGS:
                                new_internal[js_url] = js_meta
                            else:
                                new_internal.setdefault(js_url, js_meta)
                        for js_url, js_info in js_external.items():
                            new_external.setdefault(js_url, js_info)

                    # 3. Selective Headless Chrome fallback for client-side JavaScript / SPA rendering:
                    # Trigger when the HTML parser finds few links on an SPA, and on every SPA page of a
                    # logged-in crawl (app menus are usually rendered client-side after the session loads).
                    # A logged-in, server-built page whose menus were not seen before is opened once too, so
                    # menus that load their links only on a click ("My Account") are explored.
                    # looks_like_spa() scans the whole page, so it runs only when a rule below needs its answer
                    is_spa: Optional[bool] = None
                    html_link_count = len(new_internal)
                    wants_render = False
                    if (html_link_count == 0 or (depth == 0 and html_link_count < 3) or self.auth_used) \
                            and not rendered_page:   # a rendered page already ran its JavaScript
                        is_spa = wants_render = looks_like_spa(html_text)
                    menus = self._new_menu_labels(soup) if (
                        self.auth_used and not wants_render and self.config.use_dynamic_js) else []
                    if (wants_render or menus) and self.config.use_dynamic_js and not self._stop_event.is_set():
                        extractor = self._get_dynamic_extractor()
                        if extractor and extractor.available:
                            if wants_render:
                                self.log(f"Selectively rendering dynamic SPA with Headless Chrome: {url}")
                            else:
                                self.log(f"Opening the menus of {url} in Headless Chrome (new menu buttons: "
                                         + ", ".join(f"'{m}'" for m in menus[:8]) + ")")
                            dyn_title, _, dyn_internal, dyn_external = extractor.render_and_extract(
                                url=str(resp.url),
                                include_subdomains=self.config.include_subdomains,
                                exclude_patterns=self.config.exclude_patterns,
                                collect_external=self.config.collect_external_links,
                                stop_event=self._stop_event,
                            )
                            if not title and dyn_title:
                                title = dyn_title
                            for dyn_url, dyn_meta in dyn_internal.items():
                                new_internal.setdefault(dyn_url, dyn_meta)
                            for dyn_url, dyn_info in dyn_external.items():
                                # The renderer only knows the start host (and its subdomains); a link to another
                                # host of the crawled site (extra hosts, List mode hosts) is internal here too
                                if is_internal_url(dyn_url, self.base_domain,
                                                   include_subdomains=self.config.include_subdomains,
                                                   allowed_domains=self.allowed_domains):
                                    text = str((dyn_info or {}).get("anchor_text") or "")
                                    text = "" if text == "-" else text[:100]
                                    new_internal.setdefault(dyn_url, {
                                        "anchor_text": text, "tag": "a", "region": "Rendered by JavaScript",
                                        "selector": "", "html": f'<a href="{dyn_url[:80]}">{text[:60]}</a>',
                                        "raw_href": dyn_url})
                                else:
                                    new_external[dyn_url] = dyn_info

                    # 4. SPA Client-Side Route Discovery (React / Vite / Vue / Next.js router bundles).
                    # Always on in SPA mode; automatic for SPA pages that expose few HTML links or sit
                    # behind a login. Each bundle file is scanned once per crawl.
                    if (self.config.enable_spa_mode and len(new_internal) == 0) or \
                            ((self.config.enable_spa_mode or self.auth_used or html_link_count < 5)
                             and (is_spa if is_spa is not None else looks_like_spa(html_text))):
                        spa_routes = {
                            route: js for route, js in
                            self._extract_spa_bundle_routes(html_text, str(resp.url), soup=soup).items()
                            if route not in (url, str(resp.url))
                        }
                        if spa_routes:
                            self.log(f"Discovered {len(spa_routes)} client-side SPA routes from JavaScript bundle: {url}")
                            for route, js_url in spa_routes.items():
                                js_path = urlparse(js_url).path
                                new_internal.setdefault(route, {
                                    "anchor_text": "", "tag": "bundle", "region": "JavaScript bundle",
                                    "selector": "", "html": f"Referenced in {js_path}"[:240], "raw_href": route,
                                    "source_file": js_path,
                                })
                    release_soup(soup)   # nothing reads the parsed page after this point

                elif status_code >= 400 and "html" in ctype:
                    # Keep the error page's own title (e.g. "Page Not Found") for the issue report
                    html_text = self._page_text(resp, url)
                    with self._cpu_gate:
                        soup = parse_html(html_text)
                        title = PageParser.extract_title(html_text, soup=soup)
                        seo = self._extract_seo(html_text, resp, url, soup)
                    release_soup(soup)
                    self._keep_source(url, html_text)

                elif 200 <= status_code < 300:
                    # Other files reached as pages: read them for their size (and a PDF's title)
                    is_pdf = "application/pdf" in ctype
                    if _read_body(resp, PDF_TITLE_MAX_BYTES if is_pdf else self._max_page_bytes()):
                        resp._content = b""   # cut short: the size comes from Content-Length instead
                        if is_pdf:
                            pdf_props = self._pdf_too_big()
                    elif is_pdf:
                        title = pdf_title(resp.content)
                        pdf_props = self._pdf_props(url, resp.content)
                else:
                    resp.close()   # redirects and non-HTML errors: the body is not needed

                if seo is None:
                    seo = extract_headers_only(resp, url)
                    seo["final_url"] = redirect_destination or url
                    if title and "application/pdf" in ctype:
                        seo["titles"] = [title]
                    if pdf_props is not None:
                        seo["pdf"] = pdf_props
                seo["response_time_ms"] = resp_time
                if redirect_destination:
                    new_external.update(self._external_redirect(url, redirect_destination))

                # Successful fetch, break out of attempt loop
                break

            except requests.exceptions.Timeout:
                status_code, seo, error_type = None, None, "timeout"
                if self.throttle is not None:
                    self.throttle.error(url, "timeout")
                if attempt < max_attempts:
                    time.sleep(1.0)
                    continue
                resp_time = int((time.time() - req_start) * 1000)
                status_text = "Connection Timeout"
                error_msg = f"Timed out after {self.config.timeout}s ({max_attempts} attempts)"
            except requests.exceptions.SSLError as e:
                status_code, seo, error_type = None, None, "ssl"
                resp_time = int((time.time() - req_start) * 1000)
                status_text = "SSL Certificate Error"
                error_msg = error_message("ssl", e)
                break
            except requests.exceptions.ConnectionError as e:
                status_code, seo = None, None
                # With read retries off, urllib3 reports a read timeout wrapped in a ConnectionError
                cause = e.args[0] if e.args else None
                read_timeout = isinstance(cause, urllib3.exceptions.ReadTimeoutError) or \
                    isinstance(getattr(cause, "reason", None), urllib3.exceptions.ReadTimeoutError)
                error_type = "timeout" if read_timeout else classify_error(e)
                if error_type == "blocked_address":      # P24: refused before it went out; retrying changes nothing
                    resp_time = int((time.time() - req_start) * 1000)
                    status_text = "Not Opened (Blocked Address)"
                    error_msg = str(e) or error_message(error_type, e)
                    break
                if self.throttle is not None:
                    self.throttle.error(url, error_type)
                if attempt < max_attempts:
                    time.sleep(1.0)
                    continue
                resp_time = int((time.time() - req_start) * 1000)
                if error_type == "timeout":
                    status_text = "Connection Timeout"
                    error_msg = f"Timed out after {self.config.timeout}s ({max_attempts} attempts)"
                elif error_type == "ssl":
                    status_text = "SSL Certificate Error"
                    error_msg = error_message("ssl", e)
                else:
                    status_text = "Connection Failed"
                    error_msg = error_message(error_type, e)
            except requests.exceptions.TooManyRedirects as e:
                status_code, seo, error_type = None, None, "too_many_redirects"
                resp_time = int((time.time() - req_start) * 1000)
                status_text = "Too Many Redirects"
                error_msg = error_message("too_many_redirects", e)
                break
            except Exception as e:
                status_code, seo = None, None
                error_type = classify_error(e)
                resp_time = int((time.time() - req_start) * 1000)
                status_text = "Request Exception"
                error_msg = str(e)
                break

        if status_code is None and error_msg is None:
            # Stopped before the request went out: not crawled, so it goes back to the queue
            with self._lock:
                self.visited_urls.discard(url)
                self.queue.appendleft(item)
            return None, {}, {}

        if not title:
            if status_code and 200 <= status_code < 400:
                parsed_u = urlparse(url)
                title = f"{parsed_u.path or '/'}"
            elif error_msg:
                title = f"[Failed] {status_text}"
            else:
                title = f"[Status {status_code}]"

        if seo and not content_type and seo.get("is_html") and getattr(self.config, "assume_html", False):
            seo["content_type"] = "text/html"         # P17: no Content-Type, read as HTML -> HTML in every tab
            seo["content_type_assumed"] = True
        result = CrawlResult(
            url=url,
            page_title=title,
            parent_url=parent_url,
            http_status=status_text,
            status_code=status_code,
            redirect_destination=redirect_destination,
            depth=depth,
            discovery_source=source,
            content_type=content_type,
            response_time_ms=resp_time,
            error_message=error_msg,
            server=server,
            resource_type=resource_type,
            seo=seo if seo is not None else self._no_response_seo(url, resp_time),
            response_headers=response_headers if status_code is not None else {},
            **origin_fields
        )
        if error_type:
            result.set_error_type(error_type, waf)
        if note and status_code is not None and not result.error_message:
            result.error_message = note
        result.retries = self._throttle_requeues.get(url, 0)
        self._label_code_only(result)
        self._track_burst(result)

        return result, new_internal, new_external

    def _run_audit(self, url: str, seo: Optional[Dict[str, Any]]):
        """The accessibility audit (axe-core) of one internal 2xx HTML page that is not rendered anyway: calls
        audit_hook and stores its answer as seo["axe"] (nothing else changes). Pages past
        a11y_audit_max_pages, or once a11y_audit_max_minutes have passed since the first audit, get a "did not
        run" entry with the reason, so the Accessibility tab says "Not checked" for them, never "passed"."""
        if not isinstance(seo, dict):
            return
        hook = self.audit_hook
        if hook is None:
            seo["axe"] = {"ran": False, "reason": AUDIT_NOT_ATTACHED}
            return
        cfg = self.config
        now = time.time()
        with self._lock:
            if self._audit_started is None:
                self._audit_started = now
            if self._audit_pages >= max(1, int(getattr(cfg, "a11y_audit_max_pages", 500) or 500)):
                why = AUDIT_PAGE_LIMIT
            elif now - self._audit_started > max(1.0, float(getattr(cfg, "a11y_audit_max_minutes", 30) or 30)) * 60:
                why = AUDIT_TIME_LIMIT
            else:
                why = ""
                self._audit_pages += 1
        if why:
            seo["axe"] = {"ran": False, "reason": why}
            return
        try:
            axe = hook(url, seo)
        except Exception as e:
            axe = {"ran": False, "reason": f"The accessibility audit failed on this page ({type(e).__name__})."}
        seo["axe"] = axe if isinstance(axe, dict) else {
            "ran": False, "reason": "The accessibility audit returned no result for this page."}

    def _max_page_bytes(self) -> int:
        """Largest HTML body read per page (CrawlConfig.max_page_size_kb; 0 = no limit)."""
        try:
            return max(0, int(float(getattr(self.config, "max_page_size_kb", 0) or 0) * 1024))
        except (TypeError, ValueError):
            return 0

    def _max_links(self) -> int:
        """Most hyperlinks read per page (CrawlConfig.max_links_per_page; 0 = no limit)."""
        try:
            return max(0, int(getattr(self.config, "max_links_per_page", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _page_text(self, resp, url: str) -> str:
        """The decoded HTML of a streamed response, read up to the page-size limit."""
        limit = self._max_page_bytes()
        if _read_body(resp, limit):
            with self._lock:
                self._oversize_pages += 1
            self.log(f"{url} is larger than {limit // (1024 * 1024)} MB; only the first part was read.")
        _fix_encoding(resp)
        return resp.text

    def _redirect_target(self, url: str, resp, depth: int) -> Optional[str]:
        """Absolute Location of a redirect answer, None for any other answer. A redirect from the start
        URL (or from a URL the start URL redirects to) makes the target's host part of the crawled site,
        e.g. http://example.com -> https://www.example.com."""
        location = redirect_location(resp)
        if not location:
            return None
        target = join_url(str(resp.url or url), location)
        if depth == 0 or url in self._start_chain:
            norm = normalize_url(target) or target
            dest_domain = get_domain(target)
            with self._lock:
                self._start_chain.add(norm)
                self._always_allowed.add(norm)
                if dest_domain:
                    self.allowed_domains.add(clean_hostname(dest_domain))
                    self.allowed_domains.add(dest_domain)
        return target

    def _external_redirect(self, url: str, target: str) -> Dict[str, dict]:
        """A redirect to another website, recorded (and checked) as an external link of the redirecting URL."""
        norm = normalize_url(target)
        if not norm or is_internal_url(norm, self.base_domain, include_subdomains=self.config.include_subdomains,
                                       allowed_domains=self.allowed_domains):
            return {}
        return {norm: {"url": norm, "domain": get_domain(norm), "parent_url": url, "anchor_text": "-",
                       "discovery_source": "Redirect"}}

    def _same_domain_host(self, url: str) -> str:
        """The host of `url` when it shares the start URL's registrable domain (the login page), else ''."""
        host = clean_hostname(get_domain(url)) if url else ""
        start = clean_hostname(self.base_domain or "")
        if host and start and registrable_domain(host) == registrable_domain(start):
            return host
        return ""

    def on_cdn(self, url: str) -> bool:
        """`url` is on one of the crawl's CDN hosts (CrawlConfig.cdn_hosts): its images and files are the site's own
        (requested and reported as internal resources), but it is not the site itself (no custom headers)."""
        return bool(self.cdn_hosts) and on_cdn(url, self.cdn_hosts)

    def on_site(self, url: str) -> bool:
        """Whether `url` is on the crawled site: an internal host, or the configured login page's host. The
        custom HTTP headers (Configuration > HTTP Header) are sent there only."""
        host = clean_hostname(get_domain(url)) if url else ""
        if not host:
            return False
        if host == getattr(self, "_login_host", ""):
            return True
        for _ in range(3):
            try:
                return is_internal_url(url, self.base_domain, self.config.include_subdomains, self.allowed_domains)
            except RuntimeError:     # another worker added a host to allowed_domains meanwhile
                continue
        return False

    def _pdf_too_big(self) -> Dict[str, Any]:
        return {"read": False, "reason": f"Larger than {PDF_TITLE_MAX_BYTES // 2**20} MB: not downloaded to read it.",
                "title": "", "pages": None, "words": None, "encrypted": False}

    def _pdf_props(self, url: str, data: bytes) -> Dict[str, Any]:
        """P21: a PDF's properties (pdf_meta.pdf_info); its text goes to the source store (url + TEXT_KEY_SUFFIX)
        for the all_pdf_text export, never into PageData."""
        info, text = pdf_info(data)
        if text:
            self._keep_source(url + TEXT_KEY_SUFFIX, text)
        info["text_stored"] = bool(text) and self.source_store is not None
        return info

    def _pdf_read(self, url: str):
        """(title, properties) of a PDF found by a HEAD request: downloads it once (up to PDF_TITLE_MAX_BYTES)."""
        try:
            with self.session.get(url, timeout=self.request_timeout, verify=self.config.verify_ssl,
                                  allow_redirects=True, stream=True) as resp:
                if not 200 <= resp.status_code < 300:
                    return "", {"read": False, "reason": f"The download answered {resp.status_code}.",
                                "title": "", "pages": None, "words": None, "encrypted": False}
                data = bytearray()
                for chunk in resp.iter_content(65536):
                    data += chunk
                    if len(data) > PDF_TITLE_MAX_BYTES:
                        return "", self._pdf_too_big()
                    if self._stop_event.is_set():
                        return "", {"read": False, "reason": "The crawl was stopped first.", "title": "",
                                    "pages": None, "words": None, "encrypted": False}
            data = bytes(data)
            return pdf_title(data), self._pdf_props(url, data)
        except Exception as e:
            return "", {"read": False, "reason": f"The PDF could not be downloaded ({type(e).__name__}).",
                        "title": "", "pages": None, "words": None, "encrypted": False}

    def _pdf_title(self, url: str) -> str:
        """Document title stored in a PDF (Screaming Frog shows it as the PDF's Title 1). Downloads the
        file once; '' when it is larger than PDF_TITLE_MAX_BYTES, has no title or cannot be read."""
        try:
            with self.session.get(url, timeout=self.request_timeout, verify=self.config.verify_ssl,
                                  allow_redirects=True, stream=True) as resp:
                if not 200 <= resp.status_code < 300:
                    return ""
                data = bytearray()
                for chunk in resp.iter_content(65536):
                    data += chunk
                    if len(data) > PDF_TITLE_MAX_BYTES or self._stop_event.is_set():
                        return ""
            return pdf_title(bytes(data))
        except Exception:
            return ""

    def _extract_seo(self, html_text: str, resp, url: str, soup) -> Optional[Dict[str, Any]]:
        """PageData for an HTML response; a parser failure must never lose the crawl result."""
        try:
            with self._lock:
                allowed = frozenset(self.allowed_domains)  # another worker may add the start page's redirect host
            max_links = self._max_links()
            page = extract_page(html_text, str(resp.url), resp, self.base_domain, self.config.include_subdomains,
                                allowed, soup=soup, url=url, max_links=max_links, options=self.extract_options)
            if max_links and len(page.get("links") or ()) >= max_links:
                with self._lock:
                    self._link_capped_pages += 1
            return page
        except Exception as e:
            self.log(f"SEO extraction failed for {url}: {e}")
            page = extract_headers_only(resp, url)
            return page

    def _keep_source(self, url: str, html_text: str):
        """Stores the page's HTML for View Source (source_store; nothing when disabled)."""
        store = self.source_store
        if store is not None and html_text:
            store.put(url, html_text)

    @staticmethod
    def _no_response_seo(url: str, resp_time: int = 0) -> Dict[str, Any]:
        page = extract_headers_only(None, url)
        page["response_time_ms"] = resp_time
        return page

    @staticmethod
    def _server_label(headers) -> str:
        """Server/CDN identity used for diagnosis (e.g. 'cloudflare', 'nginx')."""
        server = str(headers.get("server", "") or "")
        if not server and headers.get("cf-ray"):
            server = "cloudflare"
        return server[:60]

    # ------------------------------------------------------------------
    # Network guards: firewall answers, 429/503 pacing, 403 bursts
    # ------------------------------------------------------------------
    def _waf_answer(self, resp, url: str) -> str:
        """Label of the firewall / bot protection behind an answer ('' for a normal one): a challenge header
        on any answer, or block-page markers in the body of a 403/429/503 (the body is read here as the
        error-page branch would read it; later reads reuse it)."""
        status = resp.status_code
        headers = _lower_headers(resp.headers)
        label = challenge_header(headers)
        if label or status not in WAF_STATUSES:
            return label
        ctype = headers.get("content-type", "").lower()
        try:
            if "html" in ctype or not ctype:
                body = self._page_text(resp, url)
            else:
                _read_body(resp, 65536)
                body = (resp.content or b"").decode("utf-8", "ignore")
        except Exception:
            body = ""
        return detect_waf(status, headers, body[:WAF_SCAN_CHARS])

    def _pace_answer(self, item: tuple, resp, waf: str) -> Tuple[bool, str]:
        """Adaptive backoff for one answer (net_guard.HostThrottle). A 429, or a 503 that is not a firewall
        page, pauses the host for Retry-After (or a growing backoff), slows it down and puts the URL back in
        the queue, at most MAX_THROTTLE_REQUEUES times; after that the answer is kept as it came. A firewall
        answer slows the host down hard and is never retried. Returns (requeued, error_type to record)."""
        url = item[0]
        status = resp.status_code
        throttle = self.throttle
        retry_after = retry_after_seconds(resp.headers.get("retry-after"))
        if status == 429 or (status == 503 and not waf):
            limited = "waf" if waf else ("rate_limited" if (status == 429 or retry_after is not None) else "")
            if throttle is None:
                return False, limited
            pause = throttle.throttled(url, retry_after)
            with self._lock:
                self._throttle_counts["answers"] += 1
                tries = self._throttle_requeues[url]
                requeue = (tries < MAX_THROTTLE_REQUEUES and not self._stop_event.is_set()
                           and not throttle.hopeless(url))
                if requeue:
                    self._throttle_requeues[url] = tries + 1
                    self._throttle_counts["requeued"] += 1
                    self.visited_urls.discard(url)   # not crawled yet: counted once, when it is kept
                    self.queue.append(item)
                else:
                    self._throttle_counts["gave_up"] += 1
                log_it = self._throttle_logged < THROTTLE_LOG_LIMIT
                if log_it:
                    self._throttle_logged += 1
                last_log = self._throttle_logged == THROTTLE_LOG_LIMIT
            if log_it:
                host = HostThrottle.host_of(url)
                asked = ""
                if retry_after is not None:
                    asked = f" (Retry-After {retry_after:g}s" + (f", capped at {pause:g}s)" if retry_after > pause else ")")
                if requeue:
                    self.log(f"HTTP {status} on {url}: pausing {host} for {pause:g}s{asked}, slowing down and "
                             f"retrying ({tries + 1}/{MAX_THROTTLE_REQUEUES}).")
                else:
                    self.log(f"HTTP {status} on {url} again after {tries} retries; keeping that answer.")
                if last_log:
                    self.log("Further 429/503 answers are counted but not logged one by one.")
            if requeue:
                resp.close()
                return True, ""
            return False, limited
        if waf:
            if throttle is not None:
                throttle.blocked(url, retry_after)
            self._note_waf(url, waf, status)
            return False, "waf"
        if throttle is not None and status != 403:   # a 403 neither speeds up nor slows down (see _track_burst)
            throttle.ok(url)
        return False, ""

    def _note_waf(self, url: str, label: str, status: Optional[int]):
        """Counts a firewall answer for the crawl warning; logs the first one per host and protection."""
        host = HostThrottle.host_of(url)
        with self._lock:
            self._waf_hits[label] += 1
            self._waf_hosts.add(host)
            first = (host, label) not in self._waf_logged
            self._waf_logged.add((host, label))
        if first:
            self.log(f"Bot protection ({label}) answered HTTP {status} on {url}. The crawler slows down for {host}; "
                     "URLs it blocks show the block page's status, not the real page.")

    def _track_burst(self, result: "CrawlResult"):
        """A sudden run of 403 answers across sections of the site, after pages loaded fine, is a firewall
        that started blocking the crawler (net_guard.HostThrottle.note_page): those 403 results get
        error_type 'waf' (category stays 4xx) and the crawl a warning; the host is slowed down."""
        if self.throttle is None or result.status_code is None or result.resource_type != "page" \
                or result.root_cause_type == "waf":
            return
        state = self.throttle.note_page(result.url, result.status_code)
        if result.status_code != 403:
            return
        host = HostThrottle.host_of(result.url)
        with self._lock:
            recent = self._recent_403.setdefault(host, deque(maxlen=10))
            recent.append(result)
            if not state:
                return
            marked = 0
            for r in recent:
                if r.status_code == 403 and not r.error_type:
                    r.set_error_type("waf", BURST_LABEL)
                    marked += 1
            recent.clear()
            self._waf_hits[BURST_LABEL] += marked
            self._waf_hosts.add(host)
            # They were counted as pages that need a login when they arrived; a firewall block is not that
            self.auth_required_count = max(0, self.auth_required_count - marked)
        if state == "new":
            self.log(f"{host}: most of the last pages answered 403 Forbidden across different sections after "
                     "earlier pages loaded fine. A firewall is probably blocking the crawler; slowing down.")

    # ------------------------------------------------------------------
    # Login, logged-in browser and session renewal
    # ------------------------------------------------------------------
    def _login(self) -> Optional[Tuple[Optional[str], Set[str]]]:
        """Runs the configured login. Returns (landing URL, member links), or None when it failed: the
        crawl then stops, since a logged-out crawl reported as a logged-in one is worse than no crawl."""
        self.log("Authenticating user credentials...")
        try:
            auth_ok, auth_msg, auth_cookies, post_login_url, auth_links = \
                self.auth_handler.authenticate(self.session, self.start_url)
        except Exception as e:   # never leave the crawl "running" (see run())
            auth_ok, auth_cookies, post_login_url, auth_links = False, {}, None, set()
            auth_msg = (f"The login stopped with an unexpected error ({self.auth_handler._safe_error(e)}). "
                        "Try again, or log in through the live login window.")
        self.auth_used = True
        self.auth_status = auth_msg
        self.auth_cookies = auth_cookies
        if not auth_ok:
            self.auth_error = auth_msg
            self.auth_used = False
            with self._lock:
                self._add_limitation(f"Login failed: {auth_msg}")
            self.log(f"Login failed, nothing was crawled: {auth_msg}")
            self.status = "auth_failed"
            self.phase = "done"
            self.end_time = time.time()
            self.browser.close()
            self._report_progress()
            return None
        self.session_state = self.auth_handler.session_state
        self._guard_logout_urls()
        self.log(f"Authentication active: {auth_msg}")
        if self.auth_handler.member_check != "verified":
            with self._lock:
                self._add_limitation(
                    "The login could not be confirmed on a members-only page (every page tried opens the same "
                    "without a login), so it is not certain this crawl saw the logged-in site, and a login that "
                    "ends during the crawl may go unnoticed. Start the crawl from a members-only page (for "
                    "example /my-account) to confirm it.")
        auth_links = {u for u in auth_links or () if not is_unsafe_url(u)}
        return post_login_url, auth_links

    def _guard_logout_urls(self):
        """A logged-in crawl never requests a sign-out address (it would end the session)."""
        patterns = list(self.config.exclude_patterns or [])
        have = {p.lower() for p in patterns}
        self.config.exclude_patterns = patterns + [p for p in LOGOUT_EXCLUDE_PATTERNS if p not in have]

    @contextmanager
    def browser_lease(self, timeout: float = 60):
        """A logged-in Selenium driver for one task: the crawl's shared browser when free, else a new
        Chrome with the same session and the sign-out blocklist; None when Chrome is unavailable.
        Never quit the driver you are given."""
        with self.browser.lease(timeout) as driver:
            yield driver

    def close_browser(self):
        """Closes the crawl's browser now (otherwise it closes itself BROWSER_IDLE_CLOSE seconds after use)."""
        self.browser.close()

    def _logged_in_response(self, item: tuple, resp):
        """Logged-in crawl, and the server turned this page away as it does for logged-out visitors (a
        redirect to the login page, or 401/403 once the probe page confirms the login has ended): log in
        again and queue the page once more, or (sites that accept the login only in Chrome) load it through
        the browser. Returns the response to use, or None when the page went back to the queue. Once the login
        is gone for good, the answer is kept and marked (resp.crawler_note = LOGGED_OUT_NOTE)."""
        if not self.auth_used:
            return resp
        url = item[0]
        location = resp.headers.get("location") if resp.is_redirect else None
        to_login = bool(location) and redirected_to_login(url, join_url(url, location))
        refused = resp.status_code in (401, 403)
        if not (to_login or refused):
            return resp
        if self.auth_handler.browser_only:
            page = self._browser_fetch(url)
            if page is not None:
                resp.close()
                return page
            return resp
        if refused and not self.session_dead and not self._session_ended():
            return resp   # this page is off limits for the account (the login itself still works)
        # A page turned away again right after a new login is off limits for the account, unless the probe page
        # shows that this login ended too (sites that end every session after a few pages)
        retries = MAX_RELOGINS if self.auth_handler.probe_url else 1
        if self.session_dead or self._requeued_after_login[url] >= retries or \
                not self._recover_session(url, check_http=not refused):
            if self.session_dead:
                resp.crawler_note = LOGGED_OUT_NOTE
            return resp
        resp.close()
        self._requeue_after_login(item)
        return None

    def _requeue_after_login(self, item: tuple):
        """Puts a page that met the ended login back at the front of the queue (once per new login), uncounted."""
        with self._lock:
            self._requeued_after_login[item[0]] += 1
            self.visited_urls.discard(item[0])
            self.queue.appendleft(item)

    def _session_ended(self) -> bool:
        """True when the login-protected probe page no longer opens with the session (the answer is reused
        for SESSION_CHECK_SECONDS; False when there is no probe page to tell by)."""
        now = time.time()
        with self._lock:
            cached = self._session_check
            if cached and cached[0] == self._session_gen and now - cached[1] < SESSION_CHECK_SECONDS:
                return cached[2]
            gen = self._session_gen
        ended = self.auth_handler.session_valid(self.session) is False
        with self._lock:
            self._session_check = (gen, time.time(), ended)
        return ended

    def _login_form_instead(self, url: str, soup) -> bool:
        """Logged-in crawl, and this 200 page shows a login form (WooCommerce's /my-account pages do when the
        session has ended) where logged-in pages show none, and the probe page confirms the login is gone."""
        return bool(self.auth_used and not self.auth_handler.browser_only and self.auth_handler.form_signal
                    and not is_login_url(url) and shows_login_form(soup)
                    and (self.session_dead or self._session_ended()))

    def _session_lost_for_good(self, note: str):
        """No more re-logins: the rest of the crawl runs logged out. Recorded once as a limitation, logged, and
        each member page answered after this is marked (LOGGED_OUT_NOTE)."""
        if self.session_dead:
            return
        self.session_dead = True
        with self._lock:
            self._add_limitation(note)
        self.log(note)
        self.auth_status = f"{self.auth_status} - the login ended during the crawl"

    def _recover_session(self, where: str, check_http: bool = True) -> bool:
        """Logs in again after the site ended the session (at most MAX_RELOGINS times per crawl); True when
        the session works again. Workers that saw the same expiry wait and reuse the new login. When the
        HTTP session still opens the login-protected probe page, `where` is simply off limits for this
        account and nothing is done. When no new login is possible (MAX_RELOGINS reached, a pasted session,
        or logging in again failed) the crawl records that it continues logged out."""
        if self.session_dead:
            return False
        gen = self._session_gen
        with self.browser.lock, self._relogin_lock:
            if self._session_gen != gen:
                return True
            if self.session_dead:
                return False
            if check_http and self.auth_handler.session_valid(self.session):
                return False
            if self._relogins >= MAX_RELOGINS:
                self._session_lost_for_good(
                    f"The site ended the login session again (seen on {where}) after {MAX_RELOGINS} automatic "
                    "re-logins, so the crawler stopped logging in and the rest of the crawl ran logged out: member "
                    "pages crawled after that show the site's answer for logged-out visitors (marked \"Logged out "
                    "here\"). Crawl again, or re-check those pages.")
                return False
            self._relogins += 1
            self.log(f"The login session ended (seen on {where}); logging in again "
                     f"({self._relogins}/{MAX_RELOGINS})...")
            try:
                ok, msg = self.auth_handler.relogin(self.session)
            except Exception as e:
                ok, msg = False, self.auth_handler._safe_error(e)
            if not ok:
                self._session_lost_for_good(
                    f"The site ended the login session and logging in again failed ({str(msg).rstrip('.')}). The "
                    "rest of the crawl ran logged out: member pages crawled after that show the site's answer for "
                    "logged-out visitors (marked \"Logged out here\").")
                return False
            self.session_state = self.auth_handler.session_state
            self.auth_cookies = {c["name"]: c["value"] for c in (self.session_state or {}).get("cookies", [])}
            self._session_gen += 1
            self.log("Logged in again; the crawl continues with the new session.")
            return True

    def recover_browser_session(self) -> Optional[dict]:
        """The browser was sent to the login page during a logged-in crawl. First hands it the HTTP session's
        cookies (the site may have rotated them in HTTP answers); when it already had those, logs in again.
        Returns the session_state to retry with, or None."""
        gen = self._session_gen
        with self.browser.lock:
            if self._session_gen != gen:
                return self.session_state
            state = dict(self.session_state or {"local": {}, "session": {}})
            jar = jar_to_cookies(self.session.cookies, self.start_url)
            fresher = {(c["name"], c["value"]) for c in jar} != \
                {(c["name"], c["value"]) for c in state.get("cookies") or []}
            if jar and fresher and not self.auth_handler.browser_only and \
                    self.auth_handler.session_valid(self.session) is not False:
                state["cookies"] = jar
                self.session_state = state
                self.browser.set_session(state)
                return state
        return self.session_state if self._recover_session("a page in the browser", check_http=False) else None

    def _browser_fetch(self, url: str):
        """Loads a page in the logged-in browser and returns it as a requests.Response (the status comes from
        the browser's navigation timing; a page that ended on another URL is reported as a redirect)."""
        for attempt in (1, 2):
            with self.browser.hold(timeout=120) as driver:
                if driver is None:
                    return None
                try:
                    driver.get(url)
                except Exception:
                    try:
                        driver.execute_script("window.stop();")
                    except Exception:
                        pass
                end = time.time() + 8
                while time.time() < end:
                    try:
                        if driver.execute_script("return document.readyState") == "complete":
                            break
                    except Exception:
                        break
                    time.sleep(0.25)
                time.sleep(1.0)
                final = driver.current_url or url
                html = driver.page_source or ""
                try:
                    status = int(driver.execute_script(
                        "const n = performance.getEntriesByType('navigation')[0]; return n ? n.responseStatus : 0") or 0)
                except Exception:
                    status = 0
            if attempt == 1 and redirected_to_login(url, final) and self._recover_session(url):
                continue
            break
        resp = requests.Response()
        resp.url = url
        resp.encoding = "utf-8"
        same = (normalize_url(final) or final) == (normalize_url(url) or url)
        if same:
            resp.status_code = status or 200
            resp.reason = "OK" if resp.status_code == 200 else ""
            resp.headers["content-type"] = "text/html; charset=utf-8"
            body = html.encode("utf-8")
        else:
            resp.status_code, resp.reason = 302, "Found (in browser)"
            resp.headers["location"] = final
            body = b""
        resp.raw = io.BytesIO(body)
        return resp

    @staticmethod
    def _label_code_only(result: "CrawlResult"):
        """A path read only from JavaScript that answers 4xx has no element a visitor can click."""
        if result.link_tag == "bundle" and result.status_code and 400 <= result.status_code < 500:
            result.link_region = "Found in code, not clickable"

    def _new_menu_labels(self, soup) -> List[str]:
        """Menu openers on a logged-in, server-built page that no earlier page showed (see _menu_trigger_labels):
        the page is then opened in Chrome, whose click exploration opens them. Pages built from the same
        template share their menus, so each template costs one render; at most MENU_RENDER_LIMIT per crawl."""
        if self._menu_renders >= MENU_RENDER_LIMIT:
            return []
        labels = _menu_trigger_labels(soup)
        with self._lock:
            new = sorted(label for label in labels if label not in self._menu_labels_seen)
            if not new or self._menu_renders >= MENU_RENDER_LIMIT:
                return []
            self._menu_labels_seen.update(labels)
            self._menu_renders += 1
        return new

    def _rendered_links(self, rendered: Dict[str, Any], page_url: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """({internal url: link meta}, {external url: info}) for the links only the rendered page has."""
        try:
            from .render_capture import js_link_discoveries
            return js_link_discoveries(
                rendered, page_url,
                is_internal=lambda u: is_internal_url(u, self.base_domain, self.config.include_subdomains,
                                                      self.allowed_domains),
                exclude_patterns=self.config.exclude_patterns,
                collect_external=self.config.collect_external_links,
                logged_in=self.auth_used,
            )
        except Exception as e:
            self.log(f"Reading the JavaScript links of {page_url} failed: {type(e).__name__}")
            return {}, {}

    def _get_dynamic_extractor(self) -> Optional[DynamicExtractor]:
        """Starts the shared headless Chrome once (workers may ask for it concurrently)."""
        with self._dynamic_init_lock:
            if self.dynamic_extractor is None:
                parsed = urlparse(self.start_url)
                try:
                    self.dynamic_extractor = DynamicExtractor(
                        base_domain=self.base_domain,
                        user_agent=self.config.user_agent,
                        timeout=self.config.timeout,
                        logger=self.log,
                        origin=f"{parsed.scheme}://{parsed.netloc}",
                        browser=self.browser,
                        session_state=self.session_state,
                        on_session_lost=self.recover_browser_session if self.auth_used else None,
                        explore_clicks=self.auth_used,   # menus behind a login hide whole app sections
                        logged_in=self.auth_used,
                    )
                except Exception as e:
                    self.log(f"Dynamic extractor init exception: {e}")
            return self.dynamic_extractor

    def _extract_spa_bundle_routes(self, html_content: str, current_url: str, soup=None) -> Dict[str, str]:
        """
        Parses first-party JavaScript bundles referenced in the HTML to discover client-side
        routes that are not plain <a href> links: router configs (path: "/dashboard"), nav
        arrays (href: "/projects"), programmatic navigation (router.push("/ai")) and Next.js
        App Router page chunks (/_next/static/chunks/app/(group)/profile/page-<hash>.js).
        Returns {route_url: url of the JS file it was read from}.
        Each bundle URL is fetched at most once per crawl.
        """
        routes: Dict[str, str] = {}
        if not html_content:
            return routes

        try:
            if soup is None:
                soup = BeautifulSoup(html_content, "html.parser")
            parsed_curr = urlparse(current_url)
            base_origin = f"{parsed_curr.scheme}://{parsed_curr.netloc}"

            def add_route(raw: str, source: str, prefix_ok: bool = False):
                if not raw or any(c in raw for c in (":", "[", "$", "{")):
                    return  # dynamic segments (/projects/:id, /[id], template strings)
                if not prefix_ok and len(raw) > 1 and raw.endswith("/"):
                    return  # "/task/" in code is almost always a prefix: "/task/" + id
                lower = raw.lower()
                if any(lower.endswith(ext) or f"{ext}?" in lower for ext in BUNDLE_ASSET_EXTENSIONS):
                    return
                if any(seg in lower for seg in BUNDLE_SKIP_SEGMENTS):
                    return
                clean = raw.rstrip('*').rstrip('/') or '/'
                route_url = normalize_url(clean, base_url=base_origin)
                if route_url and is_internal_url(route_url, self.base_domain, self.config.include_subdomains, self.allowed_domains):
                    if not is_excluded_path(route_url, self.config.exclude_patterns):
                        routes.setdefault(route_url, source)

            # Only inspect first-party scripts (same domain or relative path)
            for s in soup.find_all("script", src=True):
                if self._stop_event.is_set():
                    break
                src = str(s.get("src") or "").strip()
                if not src:
                    continue
                # Skip external third-party tracking scripts
                if src.startswith("//") or any(tp in src.lower() for tp in ["google", "analytics", "facebook", "sentry", "hotjar", "clarity", "intercom"]):
                    continue
                if src.startswith("http://") or src.startswith("https://"):
                    script_domain = get_domain(src)
                    if script_domain != self.base_domain and script_domain not in self.allowed_domains:
                        continue

                full_js_url = normalize_url(src, base_url=current_url)
                if not full_js_url:
                    continue

                # Next.js App Router: the chunk path itself names the route
                chunk = NEXT_APP_CHUNK.search(urlparse(full_js_url).path)
                if chunk:
                    segments = [seg for seg in chunk.group(1).split("/") if seg and not (seg.startswith("(") or seg.startswith("@"))]
                    add_route("/" + "/".join(segments), full_js_url, prefix_ok=True)

                with self._lock:
                    if full_js_url in self._scanned_bundles:
                        continue
                    self._scanned_bundles.add(full_js_url)

                try:
                    if self.rate_limiter and not self.rate_limiter.acquire(self._stop_event):
                        continue
                    js_resp = self.session.get(full_js_url, timeout=min(self.config.timeout, 8))
                    if js_resp.status_code == 200 and js_resp.text:
                        for m in bundle_route_strings(js_resp.text):
                            add_route(m, full_js_url)
                except Exception:
                    pass
        except Exception:
            pass

        return routes
