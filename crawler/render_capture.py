"""
"Render every page in Chrome" (CrawlConfig.render_all) and the Rendered Page / Chrome Console Log detail tabs.

render_page(driver, url, options) opens one page in Chrome and returns PageData["rendered"]:
  title, titles, meta_description, meta_descriptions, h1, canonical, canonicals, meta_robots, word_count,
  html_lang, dom_nodes
                     read from the rendered DOM with the same text rules extract.py uses on the raw HTML, so
                     raw-versus-rendered comparisons (JavaScript tab) compare like with like
  links              every <a href> / <area href> URL in the rendered DOM (absolute, normalized), at most MAX_LINKS
  js_links           [{"url", "raw", "text", "nofollow"}] links that exist only after JavaScript ran (not in the
                     raw HTML's links); js_link_discoveries() turns them into the crawl's discovered links
  final_url          where the tab ended (fragment removed)
  http_status        HTTP status of the document Chrome ended on (None without DevTools); a page that answers
                     the crawler 200 but Chrome 403 (bot protection) shows here
  redirect_type      "" | "javascript" | "meta_refresh" | "http_refresh" | "history_api" | "unknown"
  js_redirect        True when a script (not the server, not a meta refresh) sent the page elsewhere
  js_redirect_url    the address the script navigated to ("" when js_redirect is False)
  console            [{"level": "SEVERE"|"WARNING"|"INFO"|"DEBUG", "source": "javascript"|"console-api"|"network"|...,
                       "text", "url", "line"}] Chrome's console (goog:loggingPrefs browser log). "javascript" =
                     uncaught exception, "console-api" = console.error/warn/log, "network" = a resource that failed
                     to load. Requests the crawler itself refused (robots.txt, sign-out guard) are left out.
  chrome_issues      [{"code", "category", "detail"}] DevTools Issues (Audits.issueAdded); chrome_issues_checked
                     False when the DevTools connection was not available (then the list is empty, not "no issues")
  resources          [{"url", "type", "status", "status_text", "blocked"}] every request the page made (Rendered
                     Page table); "blocked" = "" | "robots.txt" | "crawler"
  blocked_resources  [{"url", "type", "rule"}] requests refused because robots.txt disallows them for the crawl's
                     robots user-agent (Screaming Frog's "Blocked Resource"); always [] when robots.txt is ignored
  failed_resources   [{"url", "type", "status", "error"}] requests that answered 4xx/5xx or failed on the network
  img_sizes          [{"src", "natural_w", "natural_h", "display_w", "display_h", "wasted_pct"}] images shown on
                     the page (loaded, visible, not SVG); display sizes in device pixels; wasted_pct = share of the
                     file's pixels never shown (0 when the image is shown at or above its own size)
  background_images  [urls] CSS background images applied after rendering
  mobile             {"tap_targets_small", "tap_target_examples", "tap_targets_checked",
                      "content_wider_than_viewport", "content_width", "viewport_width", "small_font_pct",
                      "text_chars", "viewport_meta"} measured at a 412x823 CSS px, DPR 2.625 phone viewport
                     (approximations of the Lighthouse checks: targets under 24x24 CSS px whose 24 px circle touches
                     another target, scrollWidth beyond 412 px, share of visible text under 12 px); None = not measured
  a11y_rendered      checks on Chrome's computed accessibility tree (see _a11y); None = not checked
  custom_js          {snippet name: JSON value} of the extraction snippets; custom_js_errors {name: message};
                     names are unique (snippet_names: a repeated name gets " (2)", " (3)"...)
  device             the rendering device: "desktop" | "mobile" | "tablet" (Configuration > Rendering; window
                     size, touch and User-Agent from DEVICE_VIEWPORTS / device_user_agent)
  flattened          with flatten_shadow_dom / flatten_iframes only: {"shadow_dom", "iframes", "shadow_roots",
                     "iframes_flattened", "cut"} - the rendered HTML (View Source, the tabs, Custom Search /
                     Extraction) then has open shadow roots and same-origin iframes in place, and links inside them
                     are discovered
  cookies            [{"name", "domain", "path", "expires", "max_age", "secure", "httponly", "samesite", "source",
                       "set_by"}] cookies Chrome received while the page loaded: "request" = a Set-Cookie of one of
                     the page's requests (document, XHR, pixel, third party; set_by = that address without its
                     query), "script" = new or changed in the browser without a Set-Cookie (document.cookie).
                     Names and attributes only: values are compared as hashes in memory and never kept.
                     cookies_checked False when the DevTools connection was not available
  screenshot         file name of the JPEG in options.evidence_dir ('' = none taken): the first screen of the
                     device's window (a phone / tablet screenshot when the device is mobile / tablet)
  devtools           True when the DevTools connection (issues, resources, navigation reasons) was used
  render_ms, timings_ms ({phase: ms}), notes (plain-language remarks about what could not be read)
  promoted           True when the crawl read the page's main SEO values from this render (see below)
  link_targets       [{"url", "rel"}] canonical / rel next / rel prev / hreflang URLs only the rendered page has
                     (js_link_discoveries() hands them to the crawl like the HTML's own <link> elements)
  axe                with options.axe: the axe-core result (run_axe; the pool moves it to PageData["axe"])

RenderPool renders pages for the engine (one Chrome per worker slot, MAX_CHROME_INSTANCES and the memory guard
respected, the crawl's login and sign-out blocking carried over, drivers recycled). render_one() renders a single
page on demand (GET /api/crawl/render, the Rendered Page tab of a crawl made without render_all).

Rendering options (Configuration > Rendering; RenderPool.from_crawler reads them from the CrawlConfig, and the
Accessibility audit uses the same device and timeout):
  render_device      "desktop" (1366x768), "mobile" (a Googlebot smartphone-size phone, 412x823 CSS px, touch, a
                     phone User-Agent) or "tablet" (768x1024, touch, an Android tablet User-Agent); the screenshot
                     is of that window
  render_timeout     how long a render waits, at most, for late content (Screaming Frog's AJAX timeout, 2-30 s,
                     default 8). A render ends as soon as the document is complete, its element count is steady
                     and no AJAX request (fetch / XHR) of the page is pending, so most pages take far less; content
                     a script adds on a plain timer, without a request, is not waited for (see _settle)
  flatten_shadow_dom / flatten_iframes   the rendered HTML is read with open shadow roots and same-origin iframes
                     in place (as Google flattens them), and links inside them are discovered

axe-core (crawler/vendor/axe, the Accessibility tab's rules): in a render_all crawl every render runs it once the page
settled and was scrolled, after the rendered HTML, links and accessibility tree were read (never on a render that
ended on another document or on an error page), and RenderPool.render() stores the result as PageData["axe"]. Finite CSS animations are moved to their end first, so
colour contrast is judged on the still page (fade-in effects made it change between runs). The Accessibility audit (a11y_audit on, render_all off)
uses RenderPool.audit() through AuditHook: axe-core only, with nothing else read and the page's values left exactly
as crawled. Each run is bounded (AXE_TIMEOUT, AXE_MAX_NODES examples per rule, snippets cut to AXE_SNIPPET_CHARS
with form values and tokens masked); a page that could not be checked gets ran False with the reason.

JavaScript rendering mode (Screaming Frog): in a render_all crawl the page's main SEO values come from the rendered
HTML, not the raw HTML. RenderPool.render() re-reads the rendered HTML with extract.extract_page() and
promote_rendered() writes the result into the page's PageData in place: titles, meta description, headings,
content (word count, readability, duplicate hashes, top terms), Open Graph / Twitter, structured data, language,
viewport, canonical / pagination / hreflang (the rendered ones when the rendered page has any), the directives
(the raw HTML's AND the rendered page's: Google acts on a noindex it sees in the HTML before it renders), the
<img> list (the rendered DOM's: alt, width / height, "from": "rendered"), and the images and insecure files a
script added (joined to the resources, mixed content and protocol-relative lists, so the crawl requests and checks
them; see _rendered_files). The raw-HTML
values of every key it changed stay in PageData["original_html"] (the JavaScript tab compares those with
PageData["rendered"]). Links, response headers, validation and static accessibility checks keep their raw-HTML
values (the crawl's link discovery adds the rendered links on its own). A render that ended on
another document (JavaScript / meta refresh redirect) or that Chrome got as an error page is not promoted, and
a pool promotes only while the JavaScript tab reads PageData["original_html"] (js_tab_reads_original), so its
raw-versus-rendered filters never end up comparing the render with itself.
"""
import base64
import contextlib
import hashlib
import json
import os
import re
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

from .browser import (LOW_MEMORY, MAX_CHROME_INSTANCES, SharedBrowser, _cdp, apply_proxy_auth, attach_tap,
                      block_dangerous, chrome_alive, chrome_available, chrome_error, close_chrome, hide_overlays,
                      import_session, media_blocked, new_chrome, restore_overlays, set_media_blocked)
from .seo.extract import (MAX_COOKIES, MAX_IDS, ExtractOptions, directive_tokens, extract_page,
                          parse_set_cookie)
from .seo.model import LEFT_PAGE_REDIRECTS, rendered_left_page
from .url_utils import is_internal_url, is_logout_url, is_unsafe_url, normalize_url, redirected_to_login
from .vendor import axe_core

MAX_LINKS = 5000
MAX_JS_LINKS = 1000
MAX_IMAGES = 200
MAX_BACKGROUND_IMAGES = 100
MAX_CONSOLE = 100
MAX_ISSUES = 50
MAX_RESOURCES = 300
MAX_CUSTOM_JS = 10
CUSTOM_JS_TIMEOUT = 10          # seconds per snippet
CUSTOM_JS_VALUE_CHARS = 2000    # a longer JSON value is kept as truncated text
MOBILE_VIEWPORT = (412, 823, 2.625)
DESKTOP_WINDOW = (1366, 768)
TABLET_VIEWPORT = (768, 1024, 2.0)
# Configuration > Rendering > device (CrawlConfig.render_device): CSS width, height and pixel ratio of the window a
# page is rendered in; "mobile" is a Googlebot smartphone-size phone and, like "tablet", has touch and a phone /
# tablet User-Agent (device_user_agent)
DEVICE_VIEWPORTS: Dict[str, Tuple[int, int, float]] = {
    "desktop": (DESKTOP_WINDOW[0], DESKTOP_WINDOW[1], 1.0), "mobile": MOBILE_VIEWPORT, "tablet": TABLET_VIEWPORT}
SCREENSHOT_MAX_DPR = 2.0        # a phone screenshot is saved at most at 2 image pixels per CSS pixel (file size)
# How long a render waits for late content (CrawlConfig.render_timeout, Screaming Frog's AJAX timeout): the render
# ends earlier once the page is complete, its element count is steady and no AJAX request of the page is pending
RENDER_TIMEOUT_DEFAULT = 8.0
RENDER_TIMEOUT_RANGE = (2.0, 30.0)
MAX_FLAT_NODES = 100_000        # nodes copied into the flattened HTML (shadow DOM / iframes flattened)
MAX_RENDERED_RESOURCES = 300    # files only the rendered page loads, added to its resources (render_all)

RENDER_RECYCLE_PAGES = 40       # a render Chrome restarts after this many pages (a long-lived tab keeps growing)
# A page that keeps reloading itself (a meta refresh to itself) or never stops navigating makes every WebDriver call
# wait for the navigation. After RENDER_BUDGET_SECONDS the optional checks (scroll, accessibility tree, snippets,
# screenshot, phone size) are skipped with a note; each call then waits at most RENDER_COMMAND_WAIT seconds for a
# navigation; a render still running after RENDER_HARD_SECONDS has its Chrome closed so the crawl moves on.
RENDER_BUDGET_SECONDS = 60
RENDER_COMMAND_WAIT = 5
RENDER_HARD_SECONDS = 120
RENDER_SCREENSHOT_CAP = 1000    # screenshots kept per crawl; later pages get theirs on demand (render_one)
RENDER_POOL_SIZE = int(os.environ.get("RENDER_POOL_SIZE") or 0)   # 0 = automatic (see default_pool_size)
ONDEMAND_IDLE_SECONDS = 90      # the on-demand Chrome closes after this long without a request
# "Render this page now" (render_one) is a look at one page: the screenshot is taken as soon as the page settled and
# scrolled, the phone-size checks and accessibility tree (never shown for it) are skipped, late content is waited
# for at most ONDEMAND_SETTLE_MAX s, and a render gives up within these limits with a clear reason. Measured on
# aristasystems.in pages: 21-43 s per page before (phone checks 8-15 s, cold Chrome ~6 s, renders queued behind
# each other), about 6-12 s after with a warm Chrome (see warm_ondemand).
ONDEMAND_POOL_SIZE = 2          # renders at once (two people, or quick clicks), within MAX_CHROME_INSTANCES
ONDEMAND_SETTLE_MAX = 5.0       # seconds of waiting for late content (Screaming Frog's default AJAX timeout)
ONDEMAND_BUDGET_SECONDS = 25    # optional checks after this are skipped (the screenshot is already taken)
ONDEMAND_HARD_SECONDS = 60      # a render still running after this has its Chrome closed
ONDEMAND_WAIT_SECONDS = 45      # longest wait for a free render Chrome (others are rendering)
# axe-core (the Accessibility tab's rules): run_axe() bounds every page's checks
AXE_TIMEOUT = 15                # seconds axe.run may take on one page (then: not checked, with the reason)
AXE_MAX_NODES = 5               # example elements kept per failed rule (CSS selector and HTML snippet)
AXE_SNIPPET_CHARS = 200
AXE_TARGET_CHARS = 300
AXE_PRELOAD_MS = 3000           # how long axe waits for audio / video details (the autoplay rule)
AXE_STATS_KEPT = 2000           # per-page axe times kept for the median in RenderPool.stats()


class RenderError(Exception):
    """The page could not be rendered (the message says why, in plain words)."""


class SessionLost(RenderError):
    """A logged-in page opened the login page instead (the site ended the session)."""


class BrowserGone(Exception):
    """The Chrome under the driver was closed or crashed (the pool starts another one)."""


_GONE_WORDS = ("invalid session", "not reachable", "disconnected", "no such window", "session deleted",
               "target window already closed", "connection refused", "max retries exceeded",
               "failed to establish a new connection", "remote end closed connection")


def _gone(driver) -> bool:
    """A Chrome started by browser.new_chrome() that has been closed since (by its owner or the memory
    watchdog). Drivers made elsewhere are assumed alive."""
    return hasattr(driver, "_crawler_console_log") and not chrome_alive(driver)


@dataclass
class RenderOptions:
    """What render_page() does besides reading the page. A dict with these keys works too."""
    load_url: str = ""                      # address to open (the HTTP final URL); '' = the url argument
    raw: Optional[Dict[str, Any]] = None    # the page's raw-HTML PageData: its links (js_links) and meta refresh
    evidence_dir: str = ""                  # where the screenshot goes
    screenshot: bool = False
    hide_popups: bool = True                # cookie/newsletter pop-ups hidden for the screenshot only
    custom_js: List[Dict[str, str]] = field(default_factory=list)   # [{"name", "code", "type": extraction|action}]
    robots_check: Optional[Callable[[str], Tuple[bool, str]]] = None  # (allowed, rule); None = robots.txt ignored
    extra_headers: Optional[Dict[str, str]] = None   # sent with requests to the crawled site only
    is_internal: Optional[Callable[[str], bool]] = None  # which requests get extra_headers
    normalize: Optional[Callable[[str, str], Optional[str]]] = None  # (href, base) -> URL; default normalize_url
    logged_in: bool = False
    mobile: bool = True
    a11y: bool = True
    scroll: bool = True                     # scroll down once so lazy content and images load
    settle_seconds: float = RENDER_TIMEOUT_DEFAULT   # longest wait for late content (render_timeout)
    on_html: Optional[Callable[[str, str], None]] = None   # (url, rendered HTML), e.g. for View Source
    devtools: bool = True                   # use the DevTools connection (issues, resources, navigation reasons)
    axe: bool = False                       # run axe-core once the page settled (rendered["axe"], see run_axe)
    device: str = "desktop"                 # "desktop" | "mobile" | "tablet" (DEVICE_VIEWPORTS)
    user_agent: str = ""                    # User-Agent for this device ('' = the browser's own; device_user_agent)
    flatten_shadow_dom: bool = False        # the rendered HTML read with open shadow roots in place (flat_html)
    flatten_iframes: bool = False           # ... and same-origin iframes' content in place of the <iframe>
    screenshot_early: bool = False          # screenshot right after the page settled and scrolled (render_one)
    budget_seconds: float = RENDER_BUDGET_SECONDS   # after this the optional checks are skipped (see over_budget)


def _as_options(options: Any) -> RenderOptions:
    if isinstance(options, RenderOptions):
        return options
    if isinstance(options, dict):
        known = set(RenderOptions.__dataclass_fields__)
        return RenderOptions(**{k: v for k, v in options.items() if k in known})
    return RenderOptions()


# ----------------------------------------------------------------------------------------------------
# Rendering device (Configuration > Rendering)
# ----------------------------------------------------------------------------------------------------
_CHROME_TOKEN = re.compile(r"Chrome/(\d+)((?:\.\d+)*)")
_COMPATIBLE_PART = re.compile(r"compatible;\s*([^()]*?)\s*\)?\s*(?:Chrome/|$)", re.I)
_BOT_PART = re.compile(r"\(([^()]*bot[^()]*)\)\s*$", re.I)
_PHONE_UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} "
             "Mobile Safari/537.36")
_TABLET_UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} "
              "Safari/537.36")
_GOOGLEBOT_PHONE_UA = ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like "
                       "Gecko) Chrome/{version} Mobile Safari/537.36 (compatible; Googlebot/2.1; "
                       "+http://www.google.com/bot.html)")
_DEFAULT_CHROME = "124.0.0.0"       # the crawl presets' Chrome version (config.USER_AGENTS)


def render_device(name: Any) -> str:
    """A known rendering device name ("desktop" for anything else)."""
    name = str(name or "").strip().lower()
    return name if name in DEVICE_VIEWPORTS else "desktop"


def render_timeout_seconds(seconds: Any) -> float:
    """CrawlConfig.render_timeout kept within RENDER_TIMEOUT_RANGE (the default when it is not a number)."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return RENDER_TIMEOUT_DEFAULT
    if value != value:     # NaN
        return RENDER_TIMEOUT_DEFAULT
    return min(RENDER_TIMEOUT_RANGE[1], max(RENDER_TIMEOUT_RANGE[0], value))


def device_user_agent(user_agent: str, device: str) -> str:
    """The User-Agent a render on `device` sends ('' = the browser's own, which is the crawl's). A desktop render
    keeps the crawl's. A phone or tablet render keeps a crawl User-Agent that already is a phone (tablet) one; a
    Googlebot one becomes Googlebot smartphone (a tablet: an Android tablet with the Googlebot part); any other
    becomes Chrome on an Android phone (tablet) with the crawl's Chrome version and its bot part ("compatible; ..."
    or a closing "(...bot...)"), so the site serves the pages it would serve that crawler on that device."""
    device = render_device(device)
    ua = str(user_agent or "").strip()
    if device == "desktop":
        return ""
    if device == "mobile" and re.search(r"\bMobile\b|iPhone", ua):
        return ""
    if device == "tablet" and ("iPad" in ua or ("Android" in ua and not re.search(r"\bMobile\b", ua))):
        return ""
    m = _CHROME_TOKEN.search(ua)
    version = (m.group(1) + (m.group(2) or ".0.0.0")) if m else _DEFAULT_CHROME
    if device == "mobile" and "googlebot" in ua.lower():
        return _GOOGLEBOT_PHONE_UA.format(version=version)
    base = (_PHONE_UA if device == "mobile" else _TABLET_UA).format(version=version)
    if ua and not ua.startswith("Mozilla/"):
        bot = "compatible; " + re.sub(r"[()]", "", ua)[:200].strip()     # a bare crawler User-Agent: keep it whole
    else:
        c = _COMPATIBLE_PART.search(ua)
        bot = f"compatible; {c.group(1).strip().rstrip(';').strip()}" if c and c.group(1).strip() else ""
        if not bot:
            b = _BOT_PART.search(ua)
            bot = b.group(1).strip() if b else ""
    return f"{base} ({bot})" if bot else base


def _ua_metadata(user_agent: str, device: str) -> Dict[str, Any]:
    """User-Agent client hints matching an Android phone / tablet User-Agent (navigator.userAgentData)."""
    m = _CHROME_TOKEN.search(user_agent or "")
    major = m.group(1) if m else _DEFAULT_CHROME.split(".")[0]
    full = (m.group(1) + (m.group(2) or ".0.0.0")) if m else _DEFAULT_CHROME
    return {"brands": [{"brand": "Chromium", "version": major}, {"brand": "Google Chrome", "version": major},
                       {"brand": "Not.A/Brand", "version": "99"}],
            "fullVersion": full, "platform": "Android", "platformVersion": "10.0.0", "architecture": "",
            "model": "K" if device == "mobile" else "", "mobile": device == "mobile"}


def _set_user_agent(driver, user_agent: str, device: str) -> bool:
    """The device's User-Agent (and client hints) for the tab's next loads; True when it was set."""
    if not user_agent:
        return False
    try:
        _cdp(driver, "Emulation.setUserAgentOverride", {
            "userAgent": user_agent, "platform": "Linux armv8l", "userAgentMetadata": _ua_metadata(user_agent, device)})
        return True
    except Exception:
        return False


def _reset_user_agent(driver):
    """Back to the browser's own User-Agent (an empty override clears it)."""
    try:
        _cdp(driver, "Emulation.setUserAgentOverride", {"userAgent": ""})
    except Exception:
        pass


# ----------------------------------------------------------------------------------------------------
# One page's DevTools events
# ----------------------------------------------------------------------------------------------------
_LONG_LIVED = {"EventSource", "WebSocket", "Ping", "Media", "Manifest", "SignedExchange", "Preflight"}
# fetch / XHR requests that stream media (video players) or talk to captcha, analytics, ads and monitoring services
# rather than load page content: never waited for (a reCAPTCHA request alone held real renders up to 8 s)
_MEDIA_PATH = re.compile(r"\.(?:m3u8|mpd|ts|m4s|m4a|m4v|mp4|webm|mp3|aac|ogg|cmfv|cmfa)(?:$|[?#])", re.I)
_NOT_CONTENT = re.compile(
    r"^https?://(?:[^/]*\.)?(?:google\.com/recaptcha|gstatic\.com/recaptcha|recaptcha\.net|hcaptcha\.com|"
    r"challenges\.cloudflare\.com|google-analytics\.com|analytics\.google\.com|googletagmanager\.com|"
    r"doubleclick\.net|googlesyndication\.com|googleadservices\.com|facebook\.(?:com|net)|hotjar\.(?:com|io)|"
    r"clarity\.ms|segment\.(?:io|com)|mixpanel\.com|amplitude\.com|intercom\.io|hs-analytics\.net|"
    r"sentry\.io|nr-data\.net|newrelic\.com|datadoghq\.(?:com|eu)|bat\.bing\.com|linkedin\.com/px|"
    r"snap\.licdn\.com|px\.ads\.linkedin\.com|tiktok\.com/api/v2/pixel|analytics\.tiktok\.com|"
    r"ct\.pinterest\.com|analytics\.twitter\.com|t\.co/i/adsct|tr\.snapchat\.com|alb\.reddit\.com|"
    r"quantserve\.com|scorecardresearch\.com|yandex\.ru/watch|mc\.yandex)", re.I)
_MEDIA_TYPE = re.compile(r"(?:video|audio)/|application/(?:vnd\.apple\.mpegurl|x-mpegurl|dash\+xml)", re.I)


class _PageWatch:
    """Collects one render's network requests, DevTools issues and main-frame navigations from the tap, and
    decides which requests go through (robots.txt; the crawl's own headers on the site's requests)."""

    def __init__(self, main_frame: str, robots_check=None, extra_headers=None, is_internal=None):
        self.main_frame = main_frame
        self.robots_check = robots_check
        self.extra_headers = dict(extra_headers or {})
        self.is_internal = is_internal
        self.lock = threading.Lock()
        self.reqs: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []
        self.inflight: set = set()
        self.last_activity = time.time()
        self.issues: List[Dict[str, str]] = []
        self.navs: List[Tuple[str, str]] = []       # (reason, url) navigations the page itself asked for
        self.same_doc: List[str] = []               # history API URL changes
        self.robots_blocked: Dict[str, str] = {}    # url -> deciding rule
        self._robots_cache: Dict[str, Tuple[bool, str]] = {}
        self.main_loaders: set = set()              # loader ids of this render's page loads
        self.set_cookies: List[Tuple[str, str]] = []  # (request id, Set-Cookie line) the browser stored

    @property
    def intercepts(self) -> bool:
        return self.robots_check is not None or bool(self.extra_headers)

    # -- events (reader thread) ----------------------------------------------------------------------
    def on_event(self, method: str, params: dict):
        if method.startswith("Network."):
            self._network(method, params)
        elif method == "Audits.issueAdded":
            issue = _issue_summary(params.get("issue") or {})
            with self.lock:
                if issue and issue not in self.issues and len(self.issues) < MAX_ISSUES:
                    self.issues.append(issue)
        elif method == "Page.frameRequestedNavigation":
            if params.get("frameId") == self.main_frame:
                self.navs.append((str(params.get("reason") or ""), str(params.get("url") or "")))
        elif method == "Page.navigatedWithinDocument":
            if params.get("frameId") == self.main_frame:
                self.same_doc.append(str(params.get("url") or ""))

    def _network(self, method: str, p: dict):
        rid = p.get("requestId") or ""
        with self.lock:
            self.last_activity = time.time()
            if method == "Network.requestWillBeSent":
                url = str((p.get("request") or {}).get("url") or "")
                if not url.startswith(("http://", "https://")):
                    return
                rtype = str(p.get("type") or "Other")
                entry = self.reqs.get(rid)
                if entry is None:
                    self.order.append(rid)
                main_doc = rtype == "Document" and p.get("frameId") == self.main_frame
                in_main = p.get("frameId") == self.main_frame
                entry = {"url": url, "type": rtype, "status": None, "status_text": "", "error": "",
                         "blocked_reason": "", "canceled": False, "main_doc": main_doc,
                         "main_frame": in_main, "loader": p.get("loaderId") or "",
                         # content the page is still loading (not a video / audio stream fetched in pieces)
                         "ajax": in_main and rtype in ("XHR", "Fetch") and not _MEDIA_PATH.search(url)
                         and not _NOT_CONTENT.match(url),
                         "redirects": (entry or {}).get("redirects", 0) + (1 if p.get("redirectResponse") else 0)}
                if main_doc and entry["loader"]:
                    self.main_loaders.add(entry["loader"])
                self.reqs[rid] = entry
                if rtype not in _LONG_LIVED:
                    self.inflight.add(rid)
            elif method == "Network.responseReceived":
                entry = self.reqs.get(rid)
                resp = p.get("response") or {}
                if entry is not None:
                    entry["status"] = resp.get("status")
                    entry["status_text"] = str(resp.get("statusText") or "")
                    if entry.get("ajax") and _MEDIA_TYPE.match(str(resp.get("mimeType") or "")):
                        entry["ajax"] = False      # a media stream read by script (HLS / DASH players)
            elif method == "Network.responseReceivedExtraInfo":
                lines = _set_cookie_lines(p)
                if lines and len(self.set_cookies) < MAX_COOKIES * 4:
                    self.set_cookies.extend((rid, line) for line in lines)
            elif method == "Network.loadingFinished":
                self.inflight.discard(rid)
            elif method == "Network.loadingFailed":
                self.inflight.discard(rid)
                entry = self.reqs.get(rid)
                if entry is not None:
                    entry["error"] = str(p.get("errorText") or "")
                    entry["blocked_reason"] = str(p.get("blockedReason") or "")
                    entry["canceled"] = bool(p.get("canceled"))

    # -- request decisions (tap worker threads) -------------------------------------------------------
    def intercept(self, params: dict):
        """None = let it through; "BlockedByClient" = robots.txt disallows it; {"headers": [...]} = let it
        through with the crawl's own request headers (site requests only)."""
        req = params.get("request") or {}
        url = str(req.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return None
        main_doc = params.get("resourceType") == "Document" and params.get("frameId") == self.main_frame
        if self.robots_check is not None and not main_doc:
            with self.lock:
                verdict = self._robots_cache.get(url)
            if verdict is None:
                try:
                    verdict = self.robots_check(url)
                except Exception:
                    verdict = (True, "")
                with self.lock:
                    if len(self._robots_cache) < 5000:
                        self._robots_cache[url] = verdict
            if not verdict[0]:
                with self.lock:
                    self.robots_blocked[url] = verdict[1] or "Disallow"
                return "BlockedByClient"
        if self.extra_headers and self.is_internal is not None:
            try:
                internal = self.is_internal(url)
            except Exception:
                internal = False
            if internal:
                headers = dict(req.get("headers") or {})
                headers.update(self.extra_headers)
                return {"headers": [{"name": k, "value": str(v)} for k, v in headers.items()]}
        return None

    # -- results ---------------------------------------------------------------------------------------
    def main_document(self) -> Dict[str, Any]:
        """The tab's last page load: {"url", "status", "error"} ({} when none was seen)."""
        with self.lock:
            docs = [self.reqs[r] for r in self.order if r in self.reqs and self.reqs[r]["main_doc"]]
        if not docs:
            return {}
        last = docs[-1]
        return {"url": last["url"], "status": last["status"], "error": last["error"]}

    def cookie_lines(self) -> List[Tuple[str, str]]:
        """(address of the request, Set-Cookie line) for the page's own requests (not the previous page's)."""
        with self.lock:
            loaders = set(self.main_loaders)
            out = []
            for rid, line in self.set_cookies:
                e = self.reqs.get(rid)
                if e is None:
                    continue
                if loaders and e["main_frame"] and e["loader"] and e["loader"] not in loaders:
                    continue
                out.append((e["url"], line))
        return out

    def quiet(self, seconds: float = 0.5) -> bool:
        """The network has been (almost) idle for `seconds`: no AJAX request (fetch / XHR) of the page itself is
        pending, and at most two other long requests (analytics, frames) are."""
        with self.lock:
            if any(self.reqs.get(r, {}).get("ajax") for r in self.inflight):
                return False
            return len(self.inflight) <= 2 and time.time() - self.last_activity >= seconds

    def resources(self) -> Tuple[List[dict], List[dict], List[dict], set]:
        """(resources, blocked by robots.txt, failed, URLs the crawler refused) for the page's own requests."""
        with self.lock:
            entries = [self.reqs[r] for r in self.order if r in self.reqs]
            blocked_rules = dict(self.robots_blocked)
            loaders = set(self.main_loaders)
        by_url: Dict[str, Dict[str, Any]] = {}
        for e in entries:
            if e["main_doc"] or (e["type"] == "Other" and urlparse(e["url"]).path == "/favicon.ico"):
                continue    # the page itself, and the favicon Chrome asks for on its own
            if loaders and e["main_frame"] and e["loader"] and e["loader"] not in loaders:
                continue    # sent by the previous page while it unloaded
            by_url[e["url"]] = e   # the last request for a URL wins (a retry or a later load)
        resources, blocked, failed, refused = [], [], [], set()
        for url, e in by_url.items():
            status = e["status"]
            err = e["error"]
            mark = ""
            if url in blocked_rules:
                mark = "robots.txt"
                text = "Blocked by robots.txt"
                status = None
                blocked.append({"url": url, "type": e["type"], "rule": blocked_rules[url]})
                refused.add(url)
            elif err and e["blocked_reason"] == "inspector":
                mark = "crawler"
                text = "Blocked by the crawler (sign-out or data-changing address)"
                status = None
                refused.add(url)
            elif err and status is None:
                text = "Aborted" if (e["canceled"] or "ERR_ABORTED" in err) else err
                if not (e["canceled"] or "ERR_ABORTED" in err):
                    failed.append({"url": url, "type": e["type"], "status": None, "error": err})
            else:
                text = e["status_text"] or _status_phrase(status)
                if isinstance(status, int) and status >= 400:
                    failed.append({"url": url, "type": e["type"], "status": status, "error": text})
            if len(resources) < MAX_RESOURCES:
                resources.append({"url": url, "type": e["type"], "status": status, "status_text": text,
                                  "blocked": mark})
        return resources, blocked, failed[:MAX_RESOURCES], refused


def _status_phrase(status: Any) -> str:
    try:
        return HTTPStatus(int(status)).phrase
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------------------------
# Cookies Chrome received while a page rendered (names and attributes only)
# ---------------------------------------------------------------------------------------------
def _set_cookie_lines(params: dict) -> List[str]:
    """The Set-Cookie lines of a Network.responseReceivedExtraInfo event that Chrome stored (the ones it
    blocked, e.g. third-party cookies it refuses, are left out)."""
    lines: List[str] = []
    for k, v in (params.get("headers") or {}).items():
        if str(k).lower() == "set-cookie":
            lines += [s for s in str(v or "").split("\n") if s.strip()]
    if not lines:
        return []
    blocked = {str(b.get("cookieLine") or "") for b in params.get("blockedCookies") or []
               if isinstance(b, dict) and b.get("blockedReasons")}
    return [s for s in lines if s not in blocked]


def _cookie_jar(tap) -> Optional[Dict[Tuple[str, str, str], Tuple[str, Dict[str, Any]]]]:
    """The browser's cookies as {(name, domain, path): (value digest, attributes)}. The value is only hashed
    (in memory) to notice that a page changed it; it is never kept. None when DevTools cannot read them."""
    for method in ("Network.getAllCookies", "Storage.getCookies"):
        try:
            got = tap.call(method, {}, timeout=5)
        except Exception:
            continue
        jar: Dict[Tuple[str, str, str], Tuple[str, Dict[str, Any]]] = {}
        for c in got.get("cookies") or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            key = (str(c["name"]), str(c.get("domain") or "").lstrip(".").lower(), str(c.get("path") or "/"))
            digest = hashlib.sha256(str(c.get("value") or "").encode("utf-8", "replace")).hexdigest()[:16]
            jar[key] = (digest, {k: c.get(k) for k in ("expires", "session", "secure", "httpOnly", "sameSite")})
        return jar
    return None


def _http_date(seconds: Any) -> str:
    try:
        from email.utils import formatdate
        return formatdate(float(seconds), usegmt=True) if float(seconds) > 0 else ""
    except (TypeError, ValueError, OverflowError):
        return ""


def _rendered_cookies(watch: "_PageWatch", before: Optional[dict], after: Optional[dict]) -> List[Dict[str, Any]]:
    """Cookies set while the page rendered: every stored Set-Cookie of its requests, then cookies that are new
    or changed in the browser without one (set by a script). Once each by name + domain, at most MAX_COOKIES."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for req_url, line in watch.cookie_lines():
        c = parse_set_cookie(line)
        if not c:
            continue
        u = urlparse(req_url)
        c["domain"] = (c.get("domain") or u.hostname or "").lstrip(".").lower()
        key = (c["name"], c["domain"])
        if key in seen:
            continue
        seen.add(key)
        c["source"] = "request"
        c["set_by"] = f"{u.scheme}://{u.netloc}{u.path}"        # no query: it can carry a token
        out.append(c)
        if len(out) >= MAX_COOKIES:
            return out
    if before is None or after is None:
        return out
    for (name, domain, path), (digest, attrs) in after.items():
        if (name, domain) in seen or (before.get((name, domain, path)) or ("",))[0] == digest:
            continue
        seen.add((name, domain))
        out.append({"name": name[:200], "domain": domain, "path": path,
                    "expires": "" if attrs.get("session") else _http_date(attrs.get("expires")), "max_age": "",
                    "secure": bool(attrs.get("secure")), "httponly": bool(attrs.get("httpOnly")),
                    "samesite": str(attrs.get("sameSite") or ""),
                    # an HttpOnly cookie cannot come from a script: a response set it (one the tap missed)
                    "source": "request" if attrs.get("httpOnly") else "script", "set_by": ""})
        if len(out) >= MAX_COOKIES:
            break
    return out


def _issue_summary(issue: dict) -> Optional[Dict[str, str]]:
    """A DevTools issue as {"code", "category", "detail"} (cookie names only, never their values)."""
    code = str(issue.get("code") or "")
    if not code:
        return None
    details = issue.get("details") or {}
    inner: Dict[str, Any] = next((v for v in details.values() if isinstance(v, dict)), {})
    parts: List[str] = []
    for key in ("violatedDirective", "errorType", "type", "reason", "violationType"):
        val = inner.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    for key in ("cookieExclusionReasons", "cookieWarningReasons"):
        vals = inner.get(key)
        if isinstance(vals, list) and vals:
            parts.append(", ".join(str(v) for v in vals[:3]))
    cors = (inner.get("corsErrorStatus") or {}).get("corsError")
    if cors:
        parts.append(str(cors))
    cookie = inner.get("cookie") or {}
    if isinstance(cookie, dict) and cookie.get("name"):
        parts.append(f"cookie {cookie.get('name')}")
    where = (inner.get("insecureURL") or inner.get("blockedURL") or (inner.get("request") or {}).get("url")
             or inner.get("url") or (inner.get("sourceCodeLocation") or {}).get("url") or "")
    if where:
        parts.append(str(where))
    category = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", code[:-5] if code.endswith("Issue") else code)
    return {"code": code, "category": category, "detail": "; ".join(parts)[:300]}


# ----------------------------------------------------------------------------------------------------
# Scripts run in the page
# ----------------------------------------------------------------------------------------------------
_STATE_JS = "return [document.readyState, document.getElementsByTagName('*').length];"

# Flattened rendered HTML (flatten_shadow_dom / flatten_iframes): the page as a visitor sees it, copied node by node
# into an inert document (nothing in the copy runs or loads): an element with an open shadow root gets the root's
# content in place of its own children, each <slot> the elements assigned to it (or its fallback content), and a
# same-origin <iframe> its document's body inside <div data-slickup-frame="src">. Closed shadow roots and other
# sites' iframes cannot be read by a script and stay as they are.
_FLAT_HTML_JS = """
const flatShadow = !!arguments[0], flatFrames = !!arguments[1], maxNodes = arguments[2];
const inert = document.implementation.createHTMLDocument('');
let count = 0, shadows = 0, frames = 0;
function compose(n, out, depth) {
  if (count >= maxNodes || depth > 400) { count = maxNodes; return; }
  count++;
  if (n.nodeType === 1) {
    const tag = n.localName;
    if (flatShadow && tag === 'slot' && n.getRootNode() instanceof ShadowRoot) {
      const assigned = n.assignedNodes({flatten: true});
      for (const c of (assigned.length ? assigned : Array.from(n.childNodes))) compose(c, out, depth + 1);
      return;
    }
    if (flatFrames && (tag === 'iframe' || tag === 'frame')) {
      let d = null;
      try { d = n.contentDocument; } catch (e) {}
      if (d && d.body) {
        frames++;
        const box = inert.createElement('div');
        box.setAttribute('data-slickup-frame', n.getAttribute('src') || '');
        out.appendChild(box);
        for (const c of Array.from(d.body.childNodes)) compose(c, box, depth + 1);
        return;
      }
    }
    const copy = inert.importNode(n, false);
    out.appendChild(copy);
    let kids = n.childNodes, into = copy;
    if (tag === 'template') { kids = n.content.childNodes; into = copy.content; }
    else if (flatShadow && n.shadowRoot) { kids = n.shadowRoot.childNodes; shadows++; }
    for (const c of Array.from(kids)) compose(c, into, depth + 1);
  } else if (n.nodeType === 3 || n.nodeType === 8) {
    out.appendChild(inert.importNode(n, false));
  }
}
const holder = inert.createElement('div');
compose(document.documentElement, holder, 0);
const root = holder.firstElementChild;
const dt = document.doctype ? '<!DOCTYPE ' + document.doctype.name + '>' : '';
return {html: root ? dt + root.outerHTML : '', shadow_roots: shadows, iframes: frames, cut: count >= maxNodes};
"""

_NAV_JS = """
const n = performance.getEntriesByType('navigation')[0];
return {href: location.href, doc: n ? n.name : '', http_redirects: n ? n.redirectCount : 0};
"""

_SCROLL_JS = """
const h = Math.max(document.body ? document.body.scrollHeight : 0, document.documentElement.scrollHeight);
const step = Math.max(400, innerHeight);
const y = arguments[0] * step;
window.scrollTo(0, Math.min(y, h));
return y < h;
"""

# Links, image sizes, CSS background images and page facts from the rendered DOM (links also inside open shadow
# roots and same-origin iframes when the rendered HTML is flattened)
_DOM_JS = """
const maxImgs = arguments[0], maxLinks = arguments[1], maxBg = arguments[2];
const flatShadow = !!arguments[3], flatFrames = !!arguments[4];
const dpr = window.devicePixelRatio || 1;
const meta = document.querySelector('meta[name=viewport]');
const out = {links: [], imgs: [], bg: [], dpr: dpr, viewport: meta ? (meta.getAttribute('content') || '') : '',
             lang: document.documentElement.getAttribute('lang') || ''};
const roots = [document];
if (flatShadow || flatFrames) {
  let scanned = 0;
  for (let r = 0; r < roots.length && roots.length < 500; r++) {
    let all = [];
    try { all = roots[r].querySelectorAll('*'); } catch (e) {}
    for (const el of all) {
      if (++scanned > 50000) break;
      if (flatShadow && el.shadowRoot) roots.push(el.shadowRoot);
      if (flatFrames && (el.localName === 'iframe' || el.localName === 'frame')) {
        try { const d = el.contentDocument; if (d && d.documentElement) roots.push(d); } catch (e) {}
      }
    }
  }
}
const anchors = [];
for (const root of roots) {
  try { for (const a of root.querySelectorAll('a[href], area[href]')) anchors.push(a); } catch (e) {}
}
for (let i = 0; i < anchors.length && out.links.length < maxLinks; i++) {
  const a = anchors[i];
  let href = '';
  try { href = typeof a.href === 'string' ? a.href : new URL(a.href.baseVal, document.baseURI).href; } catch (e) {}
  if (!href) continue;
  let text = '';
  try { text = (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '').trim(); } catch (e) {}
  if (!text) { const im = a.querySelector && a.querySelector('img[alt]'); if (im) text = im.getAttribute('alt') || ''; }
  out.links.push([href, a.getAttribute('href') || '', text.replace(/\\s+/g, ' ').slice(0, 100),
                  (a.getAttribute('rel') || '').toLowerCase()]);
}
for (const img of document.images) {
  if (out.imgs.length >= maxImgs) break;
  const src = img.currentSrc || img.src;
  if (!src) continue;
  const r = img.getBoundingClientRect(), s = getComputedStyle(img);
  out.imgs.push([src, img.naturalWidth, img.naturalHeight, r.width * dpr, r.height * dpr,
                 s.display !== 'none' && s.visibility !== 'hidden' && img.getClientRects().length > 0,
                 img.complete]);
}
const seen = new Set();
let scanned = 0;
for (const el of document.querySelectorAll('body, body *')) {
  if (++scanned > 4000 || seen.size >= maxBg) break;
  const bg = getComputedStyle(el).backgroundImage;
  if (!bg || bg === 'none') continue;
  for (const m of bg.matchAll(/url\\(["']?([^"')]+)["']?\\)/g)) {
    if (/^https?:/i.test(m[1])) seen.add(m[1]);
  }
}
out.bg = Array.from(seen);
return out;
"""

# Phone-viewport measurements (run while the 412x823 viewport is emulated)
_MOBILE_JS = """
const VW = arguments[0];
const de = document.documentElement, body = document.body;
const scrollW = Math.max(de ? de.scrollWidth : 0, body ? body.scrollWidth : 0);
const meta = document.querySelector('meta[name=viewport]');
let total = 0, small = 0, nodes = 0;
const styles = new Map();
const walker = document.createTreeWalker(body || de, NodeFilter.SHOW_TEXT);
while (walker.nextNode() && nodes < 8000) {
  const node = walker.currentNode, t = (node.nodeValue || '').trim();
  const el = node.parentElement;
  if (!t || !el || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TITLE)$/.test(el.tagName)) continue;
  nodes++;
  let st = styles.get(el);
  if (!st) {
    const cs = getComputedStyle(el);
    st = {size: parseFloat(cs.fontSize) || 16,
          visible: el.getClientRects().length > 0 && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0};
    styles.set(el, st);
  }
  if (!st.visible) continue;
  total += t.length;
  if (st.size < 12) small += t.length;
}
const SEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], ' +
            '[role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=switch], [onclick]';
const list = [];
for (const el of document.querySelectorAll(SEL)) {
  if (list.length >= 800) break;
  const cs = getComputedStyle(el);
  if (cs.visibility === 'hidden' || cs.pointerEvents === 'none' || parseFloat(cs.opacity) === 0) continue;
  const r = el.getBoundingClientRect();
  if (r.width <= 1 || r.height <= 1) continue;
  let inline = false;
  if (el.tagName === 'A' && cs.display === 'inline') {
    const p = el.parentElement, own = (el.innerText || '').length;
    inline = !!p && (p.innerText || '').trim().length > own + 20;
  }
  list.push({el: el, x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height, inline: inline});
}
const undersized = t => !t.inline && (t.w < 24 || t.h < 24);
let failing = 0;
const examples = [];
for (const t of list) {
  if (!undersized(t)) continue;
  const cx = t.x + t.w / 2, cy = t.y + t.h / 2;
  let bad = false;
  for (const o of list) {
    if (o === t || o.el.contains(t.el) || t.el.contains(o.el)) continue;
    const nx = Math.max(o.x, Math.min(cx, o.x + o.w)), ny = Math.max(o.y, Math.min(cy, o.y + o.h));
    if ((nx - cx) ** 2 + (ny - cy) ** 2 < 144) { bad = true; break; }
    if (undersized(o)) {
      const ox = o.x + o.w / 2, oy = o.y + o.h / 2;
      if ((ox - cx) ** 2 + (oy - cy) ** 2 < 576) { bad = true; break; }
    }
  }
  if (bad) {
    failing++;
    if (examples.length < 10) {
      const label = (t.el.innerText || t.el.getAttribute('aria-label') || t.el.getAttribute('title') ||
                     t.el.getAttribute('href') || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
      examples.push({tag: t.el.tagName.toLowerCase(), text: label, w: Math.round(t.w), h: Math.round(t.h)});
    }
  }
}
return {scroll_width: scrollW, inner_width: innerWidth, viewport_meta: meta ? (meta.getAttribute('content') || '') : '',
        text_chars: total, small_chars: small, tap_small: failing, tap_examples: examples, targets: list.length};
"""

# Custom JavaScript snippet wrapper: the user's code runs as the body of an async function (so `return` and
# `await` work and a Screaming Frog style `return seoSpider.data(x)` works too); the value comes back as JSON.
# The code is inlined (not eval'd), so a strict Content-Security-Policy does not stop it.
_SNIPPET_HEAD = """
const __done = arguments[arguments.length - 1];
const seoSpider = {data: v => v, error: m => { throw new Error(String(m)); }};
const __json = v => {
  if (v === undefined) return 'null';
  try {
    const seen = new WeakSet();
    return JSON.stringify(v, (k, x) => {
      if (typeof Node !== 'undefined' && x instanceof Node) return (x.outerHTML || x.textContent || '').slice(0, 500);
      if (x && typeof x === 'object') { if (seen.has(x)) return undefined; seen.add(x); }
      return x;
    });
  } catch (e) { return JSON.stringify(String(v)); }
};
(async function () {
"""
_SNIPPET_TAIL = """
})().then(v => __done({ok: true, json: __json(v)}), e => __done({ok: false, error: String((e && e.message) || e)}));
"""


# ----------------------------------------------------------------------------------------------------
# Rendering one page
# ----------------------------------------------------------------------------------------------------
def render_page(driver, url: str, options: Any = None) -> Dict[str, Any]:
    """
    Opens `url` (or options.load_url) in `driver` and returns the rendered dict described in the module
    docstring. Raises SessionLost when a logged-in page lands on the login page and RenderError when the page
    cannot be opened at all. The driver stays on the page; it is never quit here.
    """
    opts = _as_options(options)
    started = time.time()
    load = opts.load_url or url
    if not str(load).startswith(("http://", "https://")):
        raise RenderError("Only http(s) pages can be rendered.")
    if is_logout_url(load) or (opts.logged_in and is_unsafe_url(load)):
        raise RenderError("This address signs out or changes data, so the browser does not open it.")
    if _gone(driver):
        raise BrowserGone("Chrome was closed")
    normalize = opts.normalize or (lambda href, base: normalize_url(href, base_url=base))
    notes: List[str] = []
    rendered: Dict[str, Any] = _empty_rendered()
    timings: Dict[str, int] = {}
    clock = [time.time()]

    def mark(phase: str):
        now = time.time()
        timings[phase] = timings.get(phase, 0) + int((now - clock[0]) * 1000)
        clock[0] = now

    tap = attach_tap(driver) if opts.devtools else None
    watch = _PageWatch(tap.target_id if tap is not None else "", opts.robots_check, opts.extra_headers,
                       opts.is_internal)
    if tap is not None:
        tap.enable("Network", "Page", "Audits")
        tap.listener = watch.on_event
        tap.set_interceptor(watch.intercept if watch.intercepts else None)
    elif opts.robots_check is not None:
        notes.append("Resources disallowed by robots.txt were not blocked (DevTools connection unavailable).")
    live = watch if tap is not None else None
    rendered["devtools"] = tap is not None
    jar_before = _cookie_jar(tap) if tap is not None else None
    _console_entries(driver)            # drop messages left by earlier pages
    device = render_device(opts.device)
    rendered["device"] = device
    _set_viewport(driver, device)
    ua_set = _set_user_agent(driver, opts.user_agent, device)
    mark("setup")
    skipped: List[str] = []
    budget = float(opts.budget_seconds or RENDER_BUDGET_SECONDS)

    def over_budget(what: str) -> bool:
        """True (and `what` is noted as not checked) once the render has used its time budget."""
        if time.time() - started <= budget:
            return False
        skipped.append(what)
        return True

    load_wait = None
    try:
        _load(driver, load, notes)
        mark("load")
        load_wait = _short_navigation_wait(driver)
        _settle(driver, live, opts.settle_seconds)
        mark("settle")
        current = _current_url(driver)
        if opts.logged_in and redirected_to_login(load, current):
            raise SessionLost(f"{load} opened the login page")

        nav = _js(driver, _NAV_JS) or {}
        doc = watch.main_document() if tap is not None else {}
        rendered["http_status"] = doc.get("status")
        if str(nav.get("href") or current).startswith("chrome-error:"):
            raise RenderError(_net_error_text(doc.get("error") or ""))
        final = _strip_fragment(str(nav.get("href") or current or load))
        rendered["final_url"] = final
        kind, target = _redirect_kind(load, final, str(nav.get("doc") or ""), watch, opts.raw, normalize)
        rendered["redirect_type"] = kind
        rendered["js_redirect"] = kind == "javascript"
        rendered["js_redirect_url"] = target if kind == "javascript" else ""

        snippets = [s for s in (opts.custom_js or []) if isinstance(s, dict) and str(s.get("code") or "").strip()]
        snippets = snippets[:MAX_CUSTOM_JS]
        names = snippet_names(snippets)
        errors: Dict[str, str] = {}
        actions = [i for i, s in enumerate(snippets) if str(s.get("type") or "extraction").lower() == "action"]
        for i in actions:
            _run_snippet(driver, snippets[i], errors, names[i])
        if actions:
            _settle(driver, live, min(4.0, opts.settle_seconds))
            mark("custom_js")
        if opts.scroll and not over_budget("scrolling for lazy content"):
            _scroll(driver)
            mark("scroll")
        early_shot = bool(opts.screenshot and opts.evidence_dir and opts.screenshot_early)
        if early_shot:          # the picture first: what the person is waiting for (never skipped by the budget)
            rendered["screenshot"] = _screenshot(driver, url, opts.evidence_dir, opts.hide_popups, notes, device)
            mark("screenshot")

        html = ""
        if opts.flatten_shadow_dom or opts.flatten_iframes:
            html = _flat_html(driver, opts, rendered, notes)
        if not html:
            try:
                html = driver.page_source or ""
            except Exception:
                notes.append("The rendered HTML could not be read.")
        if html and opts.on_html is not None:
            try:
                opts.on_html(url, html)
            except Exception:
                pass
        rendered.update(read_rendered_html(html, final, normalize))
        mark("html")

        dom = _js(driver, _DOM_JS, MAX_IMAGES, MAX_LINKS, MAX_BACKGROUND_IMAGES, opts.flatten_shadow_dom,
                  opts.flatten_iframes) or {}
        rendered["links"], rendered["js_links"] = _links(dom.get("links") or [], final, normalize, opts.raw)
        rendered["img_sizes"] = _img_sizes(dom.get("imgs") or [])
        rendered["background_images"] = [u for u in (dom.get("bg") or []) if isinstance(u, str)][:MAX_BACKGROUND_IMAGES]
        if not rendered["html_lang"]:
            rendered["html_lang"] = str(dom.get("lang") or "")
        mark("dom")

        if opts.a11y and not over_budget("the accessibility tree"):
            rendered["a11y_rendered"] = _a11y(driver, notes)
            mark("a11y")
        if opts.axe:
            # once the page settled and was scrolled, before anything resizes it (the phone-size checks) or a
            # carousel moves on; everything above was read before (axe-core ends finite CSS animations first)
            why = axe_skip_reason(kind, target, rendered.get("http_status"))
            if not why and over_budget("the axe-core accessibility checks"):
                why = f"The page was still loading or reloading after {budget:g} s."
            rendered["axe"] = axe_not_run(why) if why else run_axe(driver)
            rendered["axe"]["device"] = device
            mark("axe")
        values: Dict[str, Any] = {}
        for i, s in enumerate(snippets):
            if i in actions or over_budget("Custom JavaScript"):
                continue
            ok, value = _run_snippet(driver, s, errors, names[i])
            if ok:
                values[names[i]] = value
        rendered["custom_js"] = values
        rendered["custom_js_errors"] = errors
        mark("custom_js")

        resources, blocked, failed, refused = watch.resources() if tap is not None else _resources_from_js(driver)
        rendered["resources"], rendered["blocked_resources"], rendered["failed_resources"] = resources, blocked, failed
        rendered["console"] = _console(driver, refused)
        if tap is not None:
            with watch.lock:
                rendered["chrome_issues"] = list(watch.issues)
            rendered["chrome_issues_checked"] = True
            rendered["cookies"] = _rendered_cookies(watch, jar_before, _cookie_jar(tap))
            rendered["cookies_checked"] = True
        else:
            notes.append("Chrome issues and cookies were not captured (DevTools connection unavailable).")
        mark("logs")

        if opts.screenshot and opts.evidence_dir and not early_shot and not over_budget("the screenshot"):
            rendered["screenshot"] = _screenshot(driver, url, opts.evidence_dir, opts.hide_popups, notes, device)
            mark("screenshot")
        if opts.mobile and not over_budget("the phone-size checks"):
            rendered["mobile"] = _mobile(driver, live, notes, device)
            mark("mobile")
        if skipped:
            parts = list(dict.fromkeys(skipped))
            listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
            notes.append(f"The page was still loading or reloading after {budget:g} s, so {listed} "
                         + ("was" if len(parts) == 1 else "were") + " skipped (not checked).")
    finally:
        if tap is not None:
            tap.listener = None
            tap.set_interceptor(None)
        _set_viewport(driver, None)
        if ua_set:
            _reset_user_agent(driver)
        _close_popups(driver)
        if load_wait is not None:      # last: the clean-up calls above still wait only briefly
            _restore_navigation_wait(driver, load_wait)
    rendered["render_ms"] = int((time.time() - started) * 1000)
    rendered["timings_ms"] = timings
    rendered["notes"] = notes
    return rendered


def audit_page(driver, url: str, options: Any = None) -> Dict[str, Any]:
    """
    The Accessibility audit of one page (a crawl without render_all): opens `url` (or options.load_url) like
    render_page does (the same request headers, robots.txt rules, sign-out blocking and window size), waits until
    it settles, scrolls down and back as render_page does (options.scroll) and runs axe-core. Nothing else is read
    or kept: no links, HTML, cookies, console, screenshot or Custom JavaScript. options.raw is only read (its meta
    refresh target). Returns PageData["axe"] plus "page_ms" (load + settle + scroll + axe). Raises SessionLost,
    RenderError and BrowserGone as render_page does.
    """
    opts = _as_options(options)
    started = time.time()
    load = opts.load_url or url
    if not str(load).startswith(("http://", "https://")):
        raise RenderError("Only http(s) pages can be rendered.")
    if is_logout_url(load) or (opts.logged_in and is_unsafe_url(load)):
        raise RenderError("This address signs out or changes data, so the browser does not open it.")
    if _gone(driver):
        raise BrowserGone("Chrome was closed")
    normalize = opts.normalize or (lambda href, base: normalize_url(href, base_url=base))
    tap = attach_tap(driver) if opts.devtools else None
    watch = _PageWatch(tap.target_id if tap is not None else "", opts.robots_check, opts.extra_headers,
                       opts.is_internal)
    if tap is not None:
        tap.enable("Network", "Page")
        tap.listener = watch.on_event
        tap.set_interceptor(watch.intercept if watch.intercepts else None)
    live = watch if tap is not None else None
    _console_entries(driver)            # drained so the browser log never grows over a long audit
    device = render_device(opts.device)
    _set_viewport(driver, device)
    ua_set = _set_user_agent(driver, opts.user_agent, device)
    load_wait = None
    try:
        _load(driver, load, [])
        load_wait = _short_navigation_wait(driver)
        _settle(driver, live, opts.settle_seconds)
        current = _current_url(driver)
        if opts.logged_in and redirected_to_login(load, current):
            raise SessionLost(f"{load} opened the login page")
        nav = _js(driver, _NAV_JS) or {}
        doc = watch.main_document() if tap is not None else {}
        if str(nav.get("href") or current).startswith("chrome-error:"):
            raise RenderError(_net_error_text(doc.get("error") or ""))
        final = _strip_fragment(str(nav.get("href") or current or load))
        kind, target = _redirect_kind(load, final, str(nav.get("doc") or ""), watch, opts.raw, normalize)
        why = axe_skip_reason(kind, target, doc.get("status"))
        if not why and time.time() - started > RENDER_BUDGET_SECONDS:
            why = f"The page was still loading or reloading after {RENDER_BUDGET_SECONDS} s."
        if not why and opts.scroll:
            _scroll(driver)         # as render_page does: content and effects that wait for scrolling show up
        result = axe_not_run(why) if why else run_axe(driver)
        result["device"] = device
    finally:
        if tap is not None:
            tap.listener = None
            tap.set_interceptor(None)
        _set_viewport(driver, None)
        if ua_set:
            _reset_user_agent(driver)
        _close_popups(driver)
        if load_wait is not None:
            _restore_navigation_wait(driver, load_wait)
    result["page_ms"] = int((time.time() - started) * 1000)
    return result


def _empty_rendered() -> Dict[str, Any]:
    return {
        "title": "", "titles": [], "meta_description": "", "meta_descriptions": [], "h1": [], "canonical": "",
        "canonicals": [], "meta_robots": [], "word_count": 0, "html_lang": "", "dom_nodes": 0,
        "links": [], "js_links": [], "final_url": "", "http_status": None, "redirect_type": "", "js_redirect": False,
        "js_redirect_url": "", "console": [], "chrome_issues": [], "chrome_issues_checked": False,
        "resources": [], "blocked_resources": [], "failed_resources": [], "img_sizes": [],
        "background_images": [], "mobile": None, "a11y_rendered": None, "custom_js": {}, "custom_js_errors": {},
        "cookies": [], "cookies_checked": False,
        "screenshot": "", "devtools": False, "render_ms": 0, "timings_ms": {}, "notes": [],
        "promoted": False, "link_targets": [], "device": "desktop",
    }


def _short_navigation_wait(driver) -> Optional[float]:
    """After the page loaded, WebDriver calls wait for a pending navigation at most RENDER_COMMAND_WAIT s (a
    page that keeps reloading itself would otherwise hold each call for the whole page-load timeout).
    Returns the previous page-load timeout (None when it could not be changed)."""
    try:
        before = getattr(getattr(driver, "timeouts", None), "page_load", None)
        driver.set_page_load_timeout(RENDER_COMMAND_WAIT)
        return float(before) if before else None
    except Exception:
        return None


def _restore_navigation_wait(driver, seconds: float):
    try:
        driver.set_page_load_timeout(seconds)
    except Exception:
        pass


def _js(driver, script: str, *args):
    if _gone(driver):
        raise BrowserGone("Chrome was closed")
    try:
        return driver.execute_script(script, *args)
    except Exception as e:
        if any(w in str(e).lower() for w in _GONE_WORDS):
            raise BrowserGone(str(e).splitlines()[0][:160] if str(e) else type(e).__name__)
        return None


def _current_url(driver) -> str:
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


_NET_ERRORS = {
    "ERR_NAME_NOT_RESOLVED": "the site's address could not be found (DNS)",
    "ERR_CONNECTION_REFUSED": "the server refused the connection",
    "ERR_CONNECTION_RESET": "the connection was reset",
    "ERR_CONNECTION_TIMED_OUT": "the server did not answer in time",
    "ERR_TIMED_OUT": "the server did not answer in time",
    "ERR_PROXY_CONNECTION_FAILED": "the proxy could not be reached",
    "ERR_TUNNEL_CONNECTION_FAILED": "the proxy could not open a connection to the site",
    "ERR_HTTP_RESPONSE_CODE_FAILURE": "the page (or a redirect it made) answered with an HTTP error and no page "
                                      "to show (behind a proxy: check the proxy's user name and password)",
    "ERR_CERT_AUTHORITY_INVALID": "the site's certificate is not trusted",
    "ERR_SSL_PROTOCOL_ERROR": "the secure connection failed",
    "ERR_TOO_MANY_REDIRECTS": "the page redirects in a loop",
    "ERR_BLOCKED_BY_CLIENT": "the crawler blocked the address",
}


def _net_error_text(error: str) -> str:
    """A Chrome net::ERR_ code as a plain sentence."""
    code = (re.search(r"ERR_[A-Z0-9_]+", error or "") or [""])[0]
    why = _NET_ERRORS.get(code, "a network or proxy error")
    return f"Chrome could not open the page: {why}" + (f" ({code})." if code else ".")


def _load(driver, url: str, notes: List[str]):
    """driver.get(); a page that keeps loading past the timeout is stopped and read as it is."""
    if _gone(driver):
        raise BrowserGone("Chrome was closed")
    try:
        driver.get(url)
    except Exception as e:
        msg = (str(e).strip().splitlines() or [type(e).__name__])[0]
        if re.search(r"net::ERR_[A-Z0-9_]+", msg):
            raise RenderError(_net_error_text(msg))
        if _gone(driver) or any(w in str(e).lower() for w in _GONE_WORDS):
            raise BrowserGone(msg[:160])
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        notes.append("The page was still loading when the time limit ran out; what had loaded was read.")


def _settle(driver, watch: Optional[_PageWatch], max_wait: float, stable: float = 0.6):
    """Waits until the document is complete, its element count has not changed for `stable` seconds, the
    network is quiet (when DevTools events are available: no AJAX request of the page pending, see
    _PageWatch.quiet), at most `max_wait` seconds (the render timeout). Survives a script navigation. Content a
    script adds on a plain timer, without a request, is not waited for: real pages set dozens of timers while they
    load (analytics flushes, animations, captcha guards), and waiting for them made renders of real pages about
    3.6 s slower with nothing more read."""
    start = time.time()
    last, stable_since = -1, time.time()
    while time.time() - start < max_wait:
        time.sleep(0.15)
        state = _js(driver, _STATE_JS)
        if not state:
            last, stable_since = -1, time.time()
            continue
        ready, count = state[0] == "complete", state[1]
        if count != last:
            last, stable_since = count, time.time()
            continue
        if ready and time.time() - stable_since >= stable and (watch is None or watch.quiet(min(0.5, stable))):
            return


def _set_viewport(driver, device: Optional[str]):
    """The window a page is rendered in: "desktop" (DESKTOP_WINDOW CSS px, as Screaming Frog renders), "mobile"
    (MOBILE_VIEWPORT) or "tablet" (TABLET_VIEWPORT), the last two with touch and the mobile viewport rules;
    None = Chrome's own window again."""
    try:
        if device is None:
            _cdp(driver, "Emulation.clearDeviceMetricsOverride")
            _cdp(driver, "Emulation.setTouchEmulationEnabled", {"enabled": False})
            return
        name = render_device(device)
        width, height, dpr = DEVICE_VIEWPORTS[name]
        touch = name != "desktop"
        _cdp(driver, "Emulation.setDeviceMetricsOverride",
             {"width": width, "height": height, "deviceScaleFactor": dpr, "mobile": touch})
        _cdp(driver, "Emulation.setTouchEmulationEnabled",
             {"enabled": True, "maxTouchPoints": 5} if touch else {"enabled": False})
    except Exception:
        pass


def _flat_html(driver, opts: RenderOptions, rendered: Dict[str, Any], notes: List[str]) -> str:
    """The rendered HTML with open shadow roots and / or same-origin iframes flattened in (_FLAT_HTML_JS);
    rendered["flattened"] says how many of each were. '' (the caller reads page_source) when it failed."""
    res = _js(driver, _FLAT_HTML_JS, bool(opts.flatten_shadow_dom), bool(opts.flatten_iframes), MAX_FLAT_NODES)
    if not isinstance(res, dict) or not res.get("html"):
        notes.append("The shadow DOM and iframes could not be flattened; the rendered HTML was read as it is.")
        return ""
    rendered["flattened"] = {"shadow_dom": bool(opts.flatten_shadow_dom), "iframes": bool(opts.flatten_iframes),
                             "shadow_roots": int(res.get("shadow_roots") or 0),
                             "iframes_flattened": int(res.get("iframes") or 0), "cut": bool(res.get("cut"))}
    if res.get("cut"):
        notes.append(f"The page has more than {MAX_FLAT_NODES:,} elements and text nodes; the flattened HTML "
                     "stops there.")
    return str(res["html"])


def _scroll(driver):
    """Scrolls down (at most 6 screens) so lazy-loaded content and images load, then back to the top."""
    for i in range(1, 7):
        if not _js(driver, _SCROLL_JS, i):
            break
        time.sleep(0.1)
    _js(driver, "window.scrollTo(0, 0);")
    time.sleep(0.25)


def _same_url(a: str, b: str, normalize) -> bool:
    if not a or not b:
        return False
    if _strip_fragment(a) == _strip_fragment(b):
        return True
    try:
        na, nb = normalize(a, ""), normalize(b, "")
    except Exception:
        return False
    return bool(na) and na == nb


def _redirect_kind(load: str, final: str, doc: str, watch: _PageWatch, raw: Optional[dict],
                   normalize) -> Tuple[str, str]:
    """(redirect type, the address the page sent the browser to) for a tab that ended on `final` after
    opening `load`. DevTools navigation reasons decide when available; otherwise the Navigation Timing entry
    of the document (a new document = a navigation; the same document = the history API)."""
    if _same_url(final, load, normalize):
        return "", ""
    reasons = {"scriptInitiated": "javascript", "anchorClick": "javascript", "formSubmissionGet": "javascript",
               "formSubmissionPost": "javascript", "metaTagRefresh": "meta_refresh",
               "httpHeaderRefresh": "http_refresh"}
    navs = [(reasons.get(r, ""), u) for r, u in watch.navs if reasons.get(r)]
    if navs:
        return navs[-1][0], _strip_fragment(navs[-1][1]) or final
    if watch.same_doc:
        return "history_api", final
    if watch.main_frame:
        if doc and not _same_url(doc, load, normalize):
            return "unknown", final
        return "history_api", final
    if doc and not _same_url(doc, load, normalize):
        refresh = str((raw or {}).get("meta_refresh_url") or "")
        if refresh and (_same_url(refresh, doc, normalize) or _same_url(refresh, final, normalize)):
            return "meta_refresh", final
        return "javascript", final
    return "history_api", final


def read_rendered_html(html: str, final_url: str, normalize=None) -> Dict[str, Any]:
    """Titles, meta description, meta robots, canonical, H1s and the content word count of rendered HTML,
    read with extract.py's own text rules (so they compare exactly with the raw-HTML PageData)."""
    out: Dict[str, Any] = {"title": "", "titles": [], "meta_description": "", "meta_descriptions": [],
                           "h1": [], "canonical": "", "canonicals": [], "meta_robots": [], "word_count": 0,
                           "html_lang": "", "dom_nodes": 0}
    if not html:
        return out
    normalize = normalize or (lambda href, base: normalize_url(href, base_url=base))
    from .page_parser import make_soup, tag_index
    from .seo.extract import ROBOTS_META_NAMES, _el_text, _in_svg, clean_text, content_lines, text_stats
    try:
        soup = make_soup(html)
        idx = tag_index(soup)
    except Exception:
        return out
    base = final_url
    base_el = next((b for b in idx.tags("base") if b.get("href")), None)
    if base_el is not None:
        try:
            base = urljoin(base, str(base_el["href"]).strip())
        except Exception:
            pass
    out["titles"] = [_el_text(t) for t in idx.tags("title") if not _in_svg(t)]
    for m in idx.tags("meta"):
        name = str(m.get("name") or "").strip().lower()
        if name == "description":
            out["meta_descriptions"].append(clean_text(m.get("content")))
        elif name in ROBOTS_META_NAMES:
            out["meta_robots"].append(clean_text(m.get("content")).lower())
    for lk in idx.tags("link"):
        rel = lk.get("rel") or []
        rels = [str(v).lower() for v in (rel if isinstance(rel, list) else str(rel).split())]
        if "canonical" in rels and lk.get("href"):
            try:
                canon = normalize(urljoin(base, str(lk.get("href")).strip()), "")
            except Exception:
                canon = None
            if canon:
                out["canonicals"].append(canon)
    out["h1"] = [_el_text(h) for h in idx.tags("h1")]
    html_el = next(iter(idx.tags("html")), None)
    if html_el is not None:
        out["html_lang"] = str(html_el.get("lang") or html_el.get("xml:lang") or "").strip()
    body = next(iter(idx.tags("body")), None) or soup
    lines, list_items = content_lines(body)
    out["word_count"] = text_stats(lines, extra_words=list_items)["word_count"]
    out["dom_nodes"] = sum(1 for _ in soup.find_all(True))
    out["title"] = out["titles"][0] if out["titles"] else ""
    out["meta_description"] = out["meta_descriptions"][0] if out["meta_descriptions"] else ""
    out["canonical"] = out["canonicals"][0] if out["canonicals"] else ""
    return out


def _links(items: List[list], final: str, normalize, raw: Optional[dict]) -> Tuple[List[str], List[dict]]:
    """(every rendered link URL, links only the rendered DOM has) - normalized like the crawl's own links."""
    raw_urls = {str(l.get("url") or "") for l in ((raw or {}).get("links") or []) if isinstance(l, dict)}
    have_raw = raw is not None
    links: List[str] = []
    seen: set = set()
    js_links: List[dict] = []
    final_norm = normalize(final, "") if final else None
    for item in items:
        try:
            href, raw_href, text, rel = (list(item) + ["", "", "", ""])[:4]
            url = normalize(str(href), final)
        except Exception:
            continue
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(url)
        if url == final_norm or (have_raw and url in raw_urls) or len(js_links) >= MAX_JS_LINKS:
            continue
        js_links.append({"url": url, "raw": str(raw_href)[:500], "text": str(text)[:100],
                         "nofollow": "nofollow" in str(rel).split()})
    return links[:MAX_LINKS], js_links


def js_link_discoveries(rendered: Optional[Dict[str, Any]], page_url: str, is_internal: Callable[[str], bool],
                        exclude_patterns: Optional[list] = None, collect_external: bool = True,
                        logged_in: bool = False) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, Any]]]:
    """The links only the rendered page has, as ({internal url: link meta}, {external url: info}) in the format
    DynamicExtractor.render_and_extract() returns, ready to merge into the page's discovered links. Sign-out
    links (and data-changing ones on a logged-in crawl) and excluded paths are left out. A render that ended on
    another document (a JavaScript / meta refresh redirect) adds nothing: those are the destination's links.
    Internal canonical / next / prev / hreflang targets only the rendered page has (rendered["link_targets"]) are
    added as <link> elements, as the raw HTML's own are."""
    from .url_utils import get_domain, is_excluded_path
    from .seo.model import rendered_left_page
    internal: Dict[str, Dict[str, str]] = {}
    external: Dict[str, Dict[str, Any]] = {}
    if rendered_left_page(rendered):
        return internal, external
    for item in (rendered or {}).get("js_links") or []:
        url = str(item.get("url") or "")
        if not url or is_logout_url(url) or (logged_in and is_unsafe_url(url)):
            continue
        text, raw = str(item.get("text") or "")[:100], str(item.get("raw") or "")
        if is_internal(url):
            if url in internal or is_excluded_path(url, exclude_patterns or []):
                continue
            internal[url] = {"anchor_text": text, "tag": "a", "region": "Rendered by JavaScript", "selector": "",
                             "html": f'<a href="{raw[:80]}">{text[:60]}</a>', "raw_href": raw}
            if item.get("nofollow"):
                internal[url]["rel"] = "nofollow"
        elif collect_external and url not in external:
            external[url] = {"url": url, "domain": get_domain(url), "parent_url": page_url,
                             "anchor_text": text or "-", "discovery_source": "Rendered JavaScript"}
    # <link> targets (canonical, next / prev, hreflang) only the rendered page has: crawled like the HTML's own
    # (a rendered <a> link to the same URL wins)
    for item in (rendered or {}).get("link_targets") or []:
        url, rel = str(item.get("url") or ""), str(item.get("rel") or "")
        if not url or url in internal or not is_internal(url) or is_logout_url(url) or \
                (logged_in and is_unsafe_url(url)) or is_excluded_path(url, exclude_patterns or []):
            continue
        internal[url] = {"anchor_text": f"{rel} link", "tag": "link", "region": "Page <head> metadata",
                         "selector": "", "html": f'<link rel="{rel}" href="{url[:80]}"> (added by JavaScript)',
                         "raw_href": url}
    return internal, external


def _img_sizes(items: List[list]) -> List[Dict[str, Any]]:
    out = []
    for item in items:
        try:
            src, nw, nh, dw, dh, visible, complete = (list(item) + [None] * 7)[:7]
            nw, nh, dw, dh = int(nw or 0), int(nh or 0), round(float(dw or 0)), round(float(dh or 0))
        except (TypeError, ValueError):
            continue
        src = str(src or "")
        low = src.lower().split("?", 1)[0]
        if not visible or not complete or nw <= 0 or nh <= 0 or dw <= 0 or dh <= 0 or \
                low.endswith(".svg") or src.startswith("data:image/svg"):
            continue
        natural, shown = nw * nh, dw * dh
        wasted = round(100 * (1 - shown / natural)) if natural > shown else 0
        out.append({"src": src[:1000], "natural_w": nw, "natural_h": nh, "display_w": dw, "display_h": dh,
                    "wasted_pct": wasted})
    return out


# -- console ------------------------------------------------------------------------------------------
_CONSOLE_LINE = re.compile(r"^(\S+) (\d+):(\d+) (.*)$", re.S)
_NETWORK_LINE = re.compile(r"^(\S+) - (.*)$", re.S)
_SECRET_KV = re.compile(r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|token|api[_-]?key|apikey|secret|"
                        r"password|passwd|pwd|session[_-]?id|sessionid|authorization|bearer)(\"?'?\s*[:=]\s*\"?'?|\s+)"
                        r"([^\s\"'&,;}]{4,})")
_JWT = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")


def mask_console_text(text: str) -> str:
    """Token-like values in a console message (JWTs, token=..., api_key: ...) shown as ***."""
    return _JWT.sub("***", _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text or ""))


def _console_entries(driver) -> List[dict]:
    try:
        return driver.get_log("browser") or []
    except Exception:
        return []


def _console(driver, refused: set) -> List[Dict[str, Any]]:
    """The page's console messages, errors and warnings first when there are more than MAX_CONSOLE. Failed
    loads of requests the crawler itself refused (robots.txt, sign-out guard) are not the page's errors."""
    out: List[Dict[str, Any]] = []
    for e in _console_entries(driver):
        level = str(e.get("level") or "INFO").upper()
        source = str(e.get("source") or "")
        msg = str(e.get("message") or "")
        where, line, text = "", None, msg
        m = _CONSOLE_LINE.match(msg)
        if m:
            where, line, text = m.group(1), int(m.group(2)), m.group(4)
        else:
            n = _NETWORK_LINE.match(msg)        # "url - text" (network, security, other)
            if n:
                where, text = n.group(1), n.group(2)
        if where in ("console-api", "javascript", "network", source):
            where = ""
        if source == "network" and ("ERR_BLOCKED_BY_CLIENT" in text or where in refused
                                    or urlparse(where).path == "/favicon.ico"):
            continue    # requests the crawler refused, and the favicon Chrome asks for on its own
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            try:
                text = json.loads(text)
            except ValueError:
                pass
        out.append({"level": level, "source": source, "text": mask_console_text(str(text))[:500],
                    "url": where[:500], "line": line})
    if len(out) > MAX_CONSOLE:
        keep = [x for x in out if x["level"] in ("SEVERE", "WARNING")][:MAX_CONSOLE]
        rest = [x for x in out if x["level"] not in ("SEVERE", "WARNING")][:MAX_CONSOLE - len(keep)]
        chosen = {id(x) for x in keep + rest}
        out = [x for x in out if id(x) in chosen]
    return out


# -- resources without DevTools -----------------------------------------------------------------------
_RESOURCES_JS = """
return performance.getEntriesByType('resource').slice(0, arguments[0]).map(e =>
  [e.name, e.initiatorType, typeof e.responseStatus === 'number' ? e.responseStatus : 0]);
"""


def _resources_from_js(driver) -> Tuple[List[dict], List[dict], List[dict], set]:
    """Resource list from the Resource Timing API (no robots.txt blocking or network errors without
    DevTools; cross-origin answers without Timing-Allow-Origin report no status)."""
    resources, failed = [], []
    for item in _js(driver, _RESOURCES_JS, MAX_RESOURCES) or []:
        try:
            url, kind, status = str(item[0]), str(item[1] or "other"), int(item[2] or 0) or None
        except (TypeError, ValueError, IndexError):
            continue
        if not url.startswith(("http://", "https://")) or urlparse(url).path == "/favicon.ico":
            continue
        text = _status_phrase(status) if status else ""
        resources.append({"url": url, "type": kind, "status": status, "status_text": text, "blocked": ""})
        if status and status >= 400:
            failed.append({"url": url, "type": kind, "status": status, "error": text})
    return resources, [], failed, set()


# -- accessibility ---------------------------------------------------------------------------------------
_FIELD_ROLES = {"textbox", "searchbox", "combobox", "listbox", "checkbox", "radio", "spinbutton", "slider", "switch"}
_WIDGET_ROLES = {"menuitem", "menuitemcheckbox", "menuitemradio", "tab", "treeitem", "option"}
_IMAGE_ROLES = {"image", "img"}
_FRAME_ROLES = {"iframe", "Iframe"}


def _ax_value(node: dict, key: str) -> str:
    return str(((node.get(key) or {}).get("value")) or "").strip()


def _ax_prop(node: dict, name: str):
    for p in node.get("properties") or []:
        if p.get("name") == name:
            return (p.get("value") or {}).get("value")
    return None


def _describe(driver, backend_id: Optional[int]) -> str:
    """A short opening tag for an element (for examples)."""
    if not backend_id:
        return ""
    try:
        node = _cdp(driver, "DOM.describeNode", {"backendNodeId": backend_id}).get("node") or {}
    except Exception:
        return ""
    attrs = node.get("attributes") or []
    pairs = []
    for i in range(0, len(attrs) - 1, 2):
        name, value = str(attrs[i]), str(attrs[i + 1])
        if name in ("id", "class", "href", "src", "type", "name", "role", "aria-label"):
            pairs.append(f'{name}="{value[:60]}"')
    tag = str(node.get("localName") or node.get("nodeName") or "").lower()
    return (f"<{tag} " + " ".join(pairs[:4]) + ">").replace(" >", ">")[:200] if tag else ""


def _a11y(driver, notes: List[str]) -> Optional[Dict[str, Any]]:
    """Checks on Chrome's computed accessibility tree (roles and accessible names as assistive technology
    gets them, after CSS and JavaScript): links, buttons, form fields, other widgets and images without a
    name, frames without a title, the main landmark and skipped heading levels. Colour contrast and keyboard
    checks are not run."""
    try:
        nodes = _cdp(driver, "Accessibility.getFullAXTree").get("nodes") or []
    except Exception as e:
        notes.append(f"Accessibility tree not available ({type(e).__name__}).")
        return None
    counts = {"empty_links": 0, "empty_buttons": 0, "inputs_without_label": 0, "controls_without_name": 0,
              "images_without_alt": 0, "frames_without_title": 0}
    examples: Dict[str, List[int]] = {k: [] for k in counts}
    levels: List[int] = []
    has_main = False
    title = ""
    for node in nodes:
        role = _ax_value(node, "role")
        if role == "RootWebArea" and not title:
            title = _ax_value(node, "name")
        if node.get("ignored"):
            continue
        if role == "main":
            has_main = True
        if role == "heading":
            try:
                levels.append(int(_ax_prop(node, "level") or 0))
            except (TypeError, ValueError):
                pass
            continue
        named = bool(_ax_value(node, "name"))
        key = None
        if role == "link" and not named:
            key = "empty_links"
        elif role in ("button", "menubutton") and not named:
            key = "empty_buttons"
        elif role in _FIELD_ROLES and not named:
            key = "inputs_without_label"
        elif role in _WIDGET_ROLES and not named:
            key = "controls_without_name"
        elif role in _IMAGE_ROLES and not named:
            key = "images_without_alt"
        elif role in _FRAME_ROLES and not named:
            key = "frames_without_title"
        if key:
            counts[key] += 1
            if len(examples[key]) < 5:
                examples[key].append(node.get("backendDOMNodeId"))
    skips: List[str] = []
    prev = 0
    for lv in [l for l in levels if 1 <= l <= 6]:
        if prev and lv > prev + 1:
            label = f"h{prev}->h{lv}"
            if label not in skips:
                skips.append(label)
        prev = lv
    out: Dict[str, Any] = dict(counts)
    out.update({
        "has_main_landmark": has_main, "heading_skips": skips[:20], "h1_count": levels.count(1),
        "page_title": title[:200], "nodes": len(nodes),
        "examples": {k: [d for d in (_describe(driver, b) for b in ids) if d] for k, ids in examples.items() if ids},
        "not_checked": ["colour contrast", "keyboard access"],
    })
    return out


# -- axe-core ---------------------------------------------------------------------------------------------
# axe.min.js runs as the body of a function: the local define / module / exports keep its loader code away from
# the page's own module loaders, and the page's own window.axe (if it has one) is put back at once; ours is kept
# as window.__slickupAxe until the run ends.
_AXE_HEAD = "var define, module, exports;\nvar __slickupPrev = window.axe;\n"
_AXE_TAIL = """
;window.__slickupAxe = window.axe;
if (__slickupPrev === undefined) { try { delete window.axe; } catch (e) { window.axe = undefined; } }
else { window.axe = __slickupPrev; }
return !!(window.__slickupAxe && typeof window.__slickupAxe.run === 'function');
"""
# One axe.run over the page itself (not inside frames), with a hard time limit; only what the tab keeps comes back.
# Finite CSS animations and transitions (fade-in effects started by scrolling) are first moved to their end, so
# colours are judged as a visitor sees them once the page is still, not half-way through a fade (which made the
# colour contrast results change from one run to the next); endless ones (spinners, sliders) are left running.
_AXE_RUN_JS = """
const done = arguments[arguments.length - 1], cfg = arguments[0], ax = window.__slickupAxe;
let finished = false, timer = null, ended = 0;
const endAll = list => {
  for (const a of list || []) {
    try {
      const t = a.effect && a.effect.getComputedTiming();
      if (t && isFinite(t.endTime) && a.playState !== 'finished') { a.finish(); ended++; }
    } catch (e) {}
  }
};
// effects that start while axe-core runs (it scrolls elements into view) end at once too
const onStart = e => { try { endAll(e.target && e.target.getAnimations ? e.target.getAnimations() : []); } catch (x) {} };
const finish = v => {
  if (finished) return;
  finished = true;
  if (timer) clearTimeout(timer);
  document.removeEventListener('animationstart', onStart, true);
  document.removeEventListener('transitionrun', onStart, true);
  try { delete window.__slickupAxe; } catch (e) { window.__slickupAxe = undefined; }
  done(v);
};
if (!ax || typeof ax.run !== 'function') { finish({error: 'axe-core did not load in the page'}); return; }
timer = setTimeout(() => finish({error: 'timeout'}), cfg.timeout_ms);
const sel = t => (Array.isArray(t) ? t : [t]).map(x => Array.isArray(x) ? x.join(' >>> ') : String(x || ''))
  .join(' >>> ').slice(0, cfg.target_chars);
const nodes = v => v.nodes.slice(0, cfg.max_nodes);
try { endAll(document.getAnimations ? document.getAnimations() : []); } catch (e) {}
document.addEventListener('animationstart', onStart, true);
document.addEventListener('transitionrun', onStart, true);
const still = new Promise(res => {
  setTimeout(res, 250);
  requestAnimationFrame(() => requestAnimationFrame(res));
});
let t0 = 0;
still.then(() => {
  t0 = performance.now();
  return ax.run(document, {runOnly: {type: 'rule', values: cfg.rules}, resultTypes: ['violations', 'incomplete'],
                           iframes: false, elementRef: false, selectors: true, ancestry: false, xpath: false,
                           absolutePaths: false, performanceTimer: false,
                           preload: {assets: ['media'], timeout: cfg.preload_ms}});
}).then(res => finish({
  ok: true, version: (res.testEngine || {}).version || ax.version || '',
  run_ms: Math.round(performance.now() - t0), animations_ended: ended,
  violations: res.violations.map(v => ({
    id: v.id, impact: v.impact || '', tags: v.tags || [], nodes: v.nodes.length,
    targets: nodes(v).map(n => sel(n.target)),
    snippets: nodes(v).map(n => String(n.html || '').slice(0, cfg.snippet_chars))})),
  incomplete: res.incomplete.map(v => v.id), passes: res.passes.map(v => v.id),
  inapplicable: res.inapplicable.length}),
  e => finish({error: String((e && e.message) || e).slice(0, 300)}));
"""
_INPUT_VALUE = re.compile(r"""(<input\b[^>]*?\bvalue\s*=\s*)("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_KEPT_TAGS = ("wcag", "best-practice", "cat.")


def axe_not_run(reason: str) -> Dict[str, Any]:
    """PageData["axe"] of a page axe-core did not check (the reason in plain words)."""
    return {"version": axe_core.AXE_VERSION, "ran": False, "reason": reason, "ms": 0, "violations": [],
            "incomplete": [], "passes": []}


def axe_skip_reason(redirect_type: str, target: str, http_status: Any) -> str:
    """Why axe-core must not judge what Chrome shows ("" = it may): the render ended on another document (a
    JavaScript / meta refresh / HTTP refresh redirect) or Chrome got an error page."""
    if redirect_type in LEFT_PAGE_REDIRECTS:
        where = f" ({target})" if target else ""
        return (f"The page sent the browser to another address{where}, so its own accessibility was not checked "
                "(that address is checked as its own page).")
    if isinstance(http_status, int) and http_status >= 400:
        return f"Chrome got HTTP {http_status} for this page, so its accessibility was not checked."
    return ""


def mask_snippet(html: str) -> str:
    """An element's HTML as stored: form field values and token-like values shown as ***."""
    text = _INPUT_VALUE.sub(lambda m: m.group(1) + ('"***"' if m.group(2).strip("\"'") else m.group(2)), html or "")
    return mask_console_text(text)


def _axe_compact(res: Dict[str, Any], ms: int, rule_count: int) -> Dict[str, Any]:
    """PageData["axe"] of a finished run: violations sorted by impact (critical first), ids interned (a crawl keeps
    the same few ids for thousands of pages)."""
    known = axe_core.rules()
    order = {name: i for i, name in enumerate(axe_core.IMPACTS)}
    violations: List[Dict[str, Any]] = []
    for v in res.get("violations") or []:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        rid = sys.intern(str(v["id"]))
        meta = known.get(rid) or {}
        tags = [sys.intern(str(t)) for t in (v.get("tags") or meta.get("tags") or []) if str(t).startswith(_KEPT_TAGS)]
        impact = str(v.get("impact") or "")
        impact = sys.intern(impact if impact in order else str(meta.get("impact") or "moderate"))
        violations.append({
            "id": rid, "impact": impact, "level": sys.intern(axe_core.wcag_level(tags)), "tags": tags,
            "nodes": int(v.get("nodes") or 0),
            "targets": [str(t)[:AXE_TARGET_CHARS] for t in (v.get("targets") or [])[:AXE_MAX_NODES]],
            "snippets": [mask_snippet(str(s))[:AXE_SNIPPET_CHARS] for s in (v.get("snippets") or [])[:AXE_MAX_NODES]],
        })
    violations.sort(key=lambda x: (order.get(x["impact"], 9), x["id"]))

    def ids(key: str) -> List[str]:
        return sorted({sys.intern(str(i)) for i in res.get(key) or [] if i})

    return {"version": str(res.get("version") or axe_core.AXE_VERSION), "ran": True, "reason": "", "ms": ms,
            "run_ms": int(res.get("run_ms") or 0), "rules": rule_count, "violations": violations,
            "incomplete": ids("incomplete"), "passes": ids("passes")}


def run_axe(driver, timeout: float = AXE_TIMEOUT) -> Dict[str, Any]:
    """Runs axe-core's rules (axe_core.run_ids) in the page the driver shows and returns PageData["axe"]:
    {"version", "ran", "reason", "ms" (load + run), "run_ms", "rules", "violations": [{"id", "impact", "level",
    "tags", "nodes", "targets" (<= AXE_MAX_NODES CSS selectors), "snippets" (<= AXE_MAX_NODES, AXE_SNIPPET_CHARS,
    values masked)}], "incomplete": [rule ids needing a person's review], "passes": [rule ids]}.
    A run that fails or takes longer than `timeout` gives ran False with the reason. Raises BrowserGone when the
    Chrome died (the pool then tries again in a fresh one)."""
    js, why = axe_core.source()
    if not js:
        return axe_not_run(why)
    rules = axe_core.run_ids()
    if not rules:
        return axe_not_run(f"The axe-core rule list is missing ({axe_core.FETCH_HINT}).")
    if _gone(driver):
        raise BrowserGone("Chrome was closed")
    started = time.time()
    try:
        loaded = driver.execute_script(_AXE_HEAD + js + _AXE_TAIL)
    except Exception as e:
        if any(w in str(e).lower() for w in _GONE_WORDS):
            raise BrowserGone(_first_line(e))
        return axe_not_run(f"axe-core could not be loaded into the page ({_first_line(e)}).")
    if not loaded:
        return axe_not_run("axe-core could not be loaded into the page.")
    cfg = {"rules": rules, "timeout_ms": int(timeout * 1000), "max_nodes": AXE_MAX_NODES,
           "snippet_chars": AXE_SNIPPET_CHARS * 2, "target_chars": AXE_TARGET_CHARS, "preload_ms": AXE_PRELOAD_MS}
    try:
        driver.set_script_timeout(int(timeout) + 5)
        res = driver.execute_async_script(_AXE_RUN_JS, cfg)
    except Exception as e:
        if any(w in str(e).lower() for w in _GONE_WORDS):
            raise BrowserGone(_first_line(e))
        if "timeout" in str(e).lower():
            return axe_not_run(f"axe-core did not finish within {timeout:g} s on this page.")
        return axe_not_run(f"axe-core stopped with an error ({mask_console_text(_first_line(e))}).")
    finally:
        try:
            driver.set_script_timeout(30)
        except Exception:
            pass
    ms = int((time.time() - started) * 1000)
    if not isinstance(res, dict) or not res.get("ok"):
        err = str((res or {}).get("error") if isinstance(res, dict) else "no result")
        if err == "timeout":
            return axe_not_run(f"axe-core did not finish within {timeout:g} s on this page.")
        return axe_not_run(f"axe-core stopped with an error ({mask_console_text(err)[:200]}).")
    return _axe_compact(res, ms, len(rules))


# -- custom JavaScript ------------------------------------------------------------------------------------
def snippet_names(snippets: List[Dict[str, Any]]) -> List[str]:
    """The name each Custom JavaScript snippet's value and error are stored under (rendered["custom_js"] /
    ["custom_js_errors"]), in the order given: its own name (else "Snippet <n>") cut to 80 characters, with
    " (2)", " (3)"... added when an earlier snippet already uses it, so one snippet never overwrites another's
    value. `snippets` = the crawl's snippets that have code (as render_page runs them)."""
    out: List[str] = []
    used: set = set()
    for i, s in enumerate(snippets or []):
        base = str((s or {}).get("name") or f"Snippet {i + 1}")[:80]
        name, n = base, 2
        while name in used:
            name, n = f"{base} ({n})", n + 1
        used.add(name)
        out.append(name)
    return out


def _run_snippet(driver, snippet: Dict[str, str], errors: Dict[str, str], name: str = "") -> Tuple[bool, Any]:
    """Runs one Custom JavaScript snippet. (True, JSON value) or (False, None) with the error recorded under
    `name` (snippet_names)."""
    name = name or str(snippet.get("name") or "Snippet")[:80]
    code = str(snippet.get("code") or "")
    try:
        driver.set_script_timeout(CUSTOM_JS_TIMEOUT)
        res = driver.execute_async_script(_SNIPPET_HEAD + code + "\n" + _SNIPPET_TAIL)
    except Exception as e:
        first = (str(e).strip().splitlines() or [type(e).__name__])[0]
        errors[name] = ("Timed out after %ds" % CUSTOM_JS_TIMEOUT) if "timeout" in first.lower() else \
            re.sub(r"^Message:\s*", "", first)[:300]
        return False, None
    finally:
        try:
            driver.set_script_timeout(30)
        except Exception:
            pass
    if not isinstance(res, dict) or not res.get("ok"):
        errors[name] = str((res or {}).get("error") if isinstance(res, dict) else "No result")[:300]
        return False, None
    text = str(res.get("json") or "null")
    if len(text) > CUSTOM_JS_VALUE_CHARS:
        return True, text[:CUSTOM_JS_VALUE_CHARS] + "..."
    try:
        return True, json.loads(text)
    except ValueError:
        return True, text


# -- screenshot and phone viewport ---------------------------------------------------------------------
def screenshot_name(url: str) -> str:
    """File name of a page's rendered screenshot (served by /api/crawl/evidence/<name>)."""
    p = urlparse(url)
    base = re.sub(r"[^A-Za-z0-9]+", "-", (p.path + "-" + p.query).strip("/-"))[:50].strip("-") or "home"
    return f"render-{base}-{hashlib.md5(url.encode('utf-8')).hexdigest()[:10]}.jpg"


def _screenshot(driver, url: str, out_dir: str, hide: bool, notes: List[str], device: str = "desktop") -> str:
    """A JPEG of the first screen of the render device's window (quality 60) with pop-ups hidden: the desktop
    window, or the phone / tablet screen at most SCREENSHOT_MAX_DPR image pixels per CSS pixel; '' on failure."""
    try:
        os.makedirs(out_dir, exist_ok=True)
        _js(driver, "window.scrollTo(0, 0);")
        hidden = hide_overlays(driver) if hide else {}
        if hidden.get("hidden") or hidden.get("matched"):
            time.sleep(0.15)
        params: Dict[str, Any] = {"format": "jpeg", "quality": 60}
        width, height, dpr = DEVICE_VIEWPORTS[render_device(device)]
        if dpr > SCREENSHOT_MAX_DPR:
            params["clip"] = {"x": 0, "y": 0, "width": width, "height": height, "scale": SCREENSHOT_MAX_DPR / dpr}
        try:
            data = _cdp(driver, "Page.captureScreenshot", params).get("data") or ""
        finally:
            if hide:
                restore_overlays(driver)
        if not data:
            notes.append("The screenshot came back empty.")
            return ""
        name = screenshot_name(url)
        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(base64.b64decode(data))
        return name
    except Exception as e:
        notes.append(f"No screenshot ({type(e).__name__}).")
        return ""


def _mobile(driver, watch: Optional[_PageWatch], notes: List[str], device: str = "desktop") -> Optional[Dict[str, Any]]:
    """Tap targets, content width and font sizes at a phone viewport (412x823 CSS px, DPR 2.625, touch). These
    approximate Lighthouse's Target Size, Content Not Sized Correctly and Illegible Font Size checks. The render
    device's window is set again afterwards."""
    width = MOBILE_VIEWPORT[0]
    try:
        _set_viewport(driver, "mobile")
        time.sleep(0.1)
        _settle(driver, watch, 1.5, stable=0.3)
        data = _js(driver, _MOBILE_JS, width)
    except Exception as e:
        notes.append(f"Phone viewport not measured ({type(e).__name__}).")
        data = None
    finally:
        _set_viewport(driver, device)
    if not isinstance(data, dict):
        return None
    chars = int(data.get("text_chars") or 0)
    small = int(data.get("small_chars") or 0)
    scroll_w = int(data.get("scroll_width") or 0)
    return {
        "tap_targets_small": int(data.get("tap_small") or 0),
        "tap_target_examples": list(data.get("tap_examples") or [])[:10],
        "tap_targets_checked": int(data.get("targets") or 0),
        "content_wider_than_viewport": scroll_w > width + 1,
        "content_width": scroll_w,
        "viewport_width": width,
        "small_font_pct": round(100.0 * small / chars, 1) if chars else 0.0,
        "text_chars": chars,
        "viewport_meta": str(data.get("viewport_meta") or ""),
    }


def _close_popups(driver):
    """Closes tabs the page opened (window.open) and returns to the first one."""
    try:
        handles = driver.window_handles
        if len(handles) <= 1:
            return
        main = getattr(driver, "_crawler_main_handle", None) or handles[0]
        for h in handles:
            if h != main:
                driver.switch_to.window(h)
                driver.close()
        driver.switch_to.window(main)
    except Exception:
        pass


# ----------------------------------------------------------------------------------------------------
# JavaScript rendering mode: the rendered page's values become the page's own
# ----------------------------------------------------------------------------------------------------
# PageData keys always read from the rendered HTML in a render_all crawl
RENDERED_KEYS = (
    "titles", "title_outside_head", "meta_descriptions", "meta_description_outside_head", "meta_keywords",
    "h1", "h2", "h1_alt_only", "h1_img_alts", "headings",
    "word_count", "sentence_count", "avg_words_per_sentence", "flesch_reading_ease", "readability", "text_ratio",
    "content_hash", "simhash", "minhash", "lorem_ipsum", "soft_404_signals", "top_terms",
)
# ... only when the rendered page has a value (one the script removed stays: search engines read the HTML first)
RENDERED_IF_SET = ("language", "viewport")
# ... taken together, when the rendered page has the group's element (canonical / pagination / hreflang /
# structured data); otherwise the raw HTML's group stays
CANONICAL_KEYS = ("canonicals", "canonicals_raw", "canonical_outside_head", "canonical_invalid_attrs")
PAGINATION_KEYS = ("rel_next_all", "rel_prev_all", "rel_next", "rel_prev")
HREFLANG_KEYS = ("hreflang", "hreflang_outside_head")
SCHEMA_KEYS = ("structured_data", "schema_visible_mismatch", "schema_visible_unverified", "schema_values_checked")


_TAB_CHECK: Dict[str, bool] = {}


def js_tab_reads_original() -> bool:
    """True when the JavaScript tab takes a page's raw-HTML values from PageData["original_html"]. Its filters
    compare the raw HTML with the render (Page Title Only in Rendered HTML, Canonical Mismatch, Contains
    JavaScript Content...); a tab that still read the page's own values after promotion would compare the
    render with itself and silently show 0, so render_all pools promote only when this holds (checked once)."""
    if "ok" not in _TAB_CHECK:
        try:
            from .seo.tabs import javascript as js_tab
            raw = {"titles": ["raw"], "meta_descriptions": ["raw"], "h1": ["raw"],
                   "canonicals": ["https://raw.example/"], "word_count": 1}
            page = {"titles": ["js"], "meta_descriptions": ["js"], "h1": ["js"],
                    "canonicals": ["https://js.example/"], "word_count": 2, "rendered": None, "original_html": raw}
            probe = type("Probe", (), {"seo": page, "url": "https://raw.example/"})()
            _TAB_CHECK["ok"] = (js_tab.raw_title(probe) == "raw" and js_tab.raw_description(probe) == "raw"
                                and js_tab.raw_h1(probe) == ["raw"] and js_tab.word_counts(probe)[0] == 1
                                and "raw.example" in js_tab.raw_canonical(probe))
        except Exception:
            _TAB_CHECK["ok"] = False
    return _TAB_CHECK["ok"]


def read_rendered_page(html: str, final_url: str, url: str = "", base_domain: str = "",
                       include_subdomains: bool = False, allowed_domains: Optional[set] = None,
                       options: Optional[ExtractOptions] = None) -> Optional[Dict[str, Any]]:
    """PageData of rendered HTML, read by extract.extract_page() exactly as the crawl reads raw HTML (no HTTP
    response: header values stay empty). None when there is no HTML or it cannot be read."""
    if not html:
        return None
    try:
        return extract_page(html, final_url or url, None, base_domain, include_subdomains, allowed_domains,
                            url=url or final_url, options=options)
    except Exception:
        return None


def promotable(page: Any, rendered: Any) -> bool:
    """A render whose values may become the page's own: an HTML page not promoted yet, a render that stayed on
    the page (no JavaScript / meta refresh redirect fired) and that Chrome did not get as an error page."""
    if not isinstance(page, dict) or not isinstance(rendered, dict) or not page.get("is_html"):
        return False
    if isinstance(page.get("original_html"), dict) or rendered_left_page(rendered):
        return False
    status = rendered.get("http_status")
    return not (isinstance(status, int) and status >= 400)


def _link_element_urls(page: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(url, rel) of a PageData's canonical, rel next / prev and hreflang <link> elements."""
    out = [(u, "canonical") for u in page.get("canonicals") or []]
    out += [(u, "next") for u in page.get("rel_next_all") or []]
    out += [(u, "prev") for u in page.get("rel_prev_all") or []]
    out += [(h.get("url") or "", "alternate") for h in page.get("hreflang") or [] if isinstance(h, dict)]
    return [(u, rel) for u, rel in out if u]


def _rendered_files(page: Dict[str, Any], rp: Dict[str, Any]) -> Dict[str, Any]:
    """Images and files in JavaScript rendering mode. The page's <img> list becomes the rendered page's (alt, width
    and height as the visitor's browser has them, so images a script adds or changes are checked; each entry gets
    "from": "rendered" - srcset / picture candidates keep their own "from" - and "element", the place of its <img>);
    a tracking pixel a script inserted (an analytics, ads or captcha host, _NOT_CONTENT) is not a page image and is
    left out. Of the files only the rendered page has, the images and every insecure one (http:// on an https page,
    any type) join the page's resources, so the crawl requests and checks them; the rendered page's mixed content and
    protocol-relative files join those lists. Other files a script added (analytics and tag-manager scripts, captcha
    frames, often with a random query per load) stay out of the resources, so they never add a new External row per
    page. The raw HTML's own entries stay first (the browser loads those too). A rendered image keeps the source line
    of the same image in the HTML the server sent; an entry only the rendered page has gets none (0). At most
    MAX_RENDERED_RESOURCES files are added per list and page."""
    out: Dict[str, Any] = {}
    raw_lines: Dict[str, List[int]] = {}
    for img in page.get("images") or []:
        if isinstance(img, dict) and img.get("src"):
            raw_lines.setdefault(str(img["src"]), []).append(int(img.get("line") or 0))
    images: List[Dict[str, Any]] = []
    for i, img in enumerate(rp.get("images") or []):
        src = str(img.get("src") or "") if isinstance(img, dict) else ""
        if not src or (src not in raw_lines and _NOT_CONTENT.match(src)):
            continue
        entry = dict(img)
        if entry.get("from") not in ("srcset", "picture"):
            entry["from"] = "rendered"
        if not isinstance(entry.get("element"), int):
            entry["element"] = i
        lines = raw_lines.get(src)
        entry["line"] = lines.pop(0) if lines else 0
        images.append(entry)
    out["images"] = images

    insecure = {u for u in rp.get("mixed_content") or [] if isinstance(u, str)}
    raw_res = [r for r in page.get("resources") or [] if isinstance(r, dict)]
    have = {str(r.get("url") or "") for r in raw_res}
    extra: List[Dict[str, Any]] = []
    for r in rp.get("resources") or []:
        url = str(r.get("url") or "") if isinstance(r, dict) else ""
        if not url or url in have:
            continue
        if url not in insecure and (r.get("type") != "image" or _NOT_CONTENT.match(url)):
            continue
        have.add(url)
        extra.append(dict(r, region="Rendered by JavaScript", line=0))
        if len(extra) >= MAX_RENDERED_RESOURCES:
            break
    if extra:
        out["resources"] = raw_res + extra
    for key in ("mixed_content", "protocol_relative"):
        raw = [u for u in page.get(key) or [] if isinstance(u, str)]
        seen = set(raw)
        more = [u for u in dict.fromkeys(rp.get(key) or []) if isinstance(u, str) and u not in seen]
        if more:
            out[key] = raw + more[:MAX_RENDERED_RESOURCES]
    return out


def promote_rendered(page: Dict[str, Any], rendered: Dict[str, Any], rendered_page: Dict[str, Any]) -> bool:
    """
    JavaScript rendering mode: makes the rendered page's values `page`'s own (Screaming Frog reads Page Titles,
    Meta Description, H1, H2, Content, Canonicals, Directives and the rest from the rendered HTML when it
    renders). `page` is the raw-HTML PageData, changed in place; `rendered` its PageData["rendered"];
    `rendered_page` the PageData read_rendered_page() made of the rendered HTML. The raw-HTML value of every key
    it changes goes to page["original_html"] ({key: raw value}; a key not there has the same value in both), so
    raw-versus-rendered comparisons keep working.
    Directives are the raw HTML's and the rendered page's together (a noindex in the HTML stops Google before it
    renders; one added by a script applies once it renders). Images come from the rendered DOM, and the images and
    insecure files a script added join the resources (_rendered_files). False (page untouched) when the render
    cannot be used (see promotable).
    """
    if not promotable(page, rendered) or not isinstance(rendered_page, dict):
        return False
    rp = rendered_page
    new: Dict[str, Any] = {k: rp.get(k, page.get(k)) for k in RENDERED_KEYS}
    for k in RENDERED_IF_SET:
        if rp.get(k):
            new[k] = rp[k]
    if rp.get("canonicals"):
        new.update({k: rp.get(k) for k in CANONICAL_KEYS})
    if rp.get("rel_next_all") or rp.get("rel_prev_all"):
        new.update({k: rp.get(k) for k in PAGINATION_KEYS})
    if rp.get("hreflang"):
        new.update({k: rp.get(k) for k in HREFLANG_KEYS})
    sd = rp.get("structured_data") or {}
    if sd.get("types") or sd.get("items") or sd.get("jsonld_parse_errors"):
        new.update({k: rp.get(k) for k in SCHEMA_KEYS})
    for k in ("og", "twitter"):       # per property: the rendered value, else the HTML's
        raw_v, js_v = page.get(k) or {}, rp.get(k) or {}
        if any(js_v.values()):
            new[k] = {key: js_v.get(key) or raw_v.get(key) or "" for key in {**raw_v, **js_v}}

    # Directives: the HTML's and the rendered page's together
    meta = list(page.get("meta_robots") or [])
    meta += [v for v in rp.get("meta_robots") or [] if v not in meta]
    new["meta_robots"] = meta
    new["meta_robots_outside_head"] = bool(page.get("meta_robots_outside_head") or rp.get("meta_robots_outside_head"))
    new["directives"] = directive_tokens(meta + list(page.get("x_robots_tag") or []))
    if "noindex" in new["directives"] or "none" in new["directives"]:
        new["scoped_noindex"] = []
    else:
        scoped = list(page.get("scoped_noindex") or [])
        new["scoped_noindex"] = scoped + [b for b in rp.get("scoped_noindex") or [] if b not in scoped]

    # Bookmark targets: an id a script adds is there when a visitor follows a #fragment link
    ids = list(page.get("ids") or [])
    every = set(ids)
    for i in rp.get("ids") or []:
        if i not in every:
            every.add(i)
            if len(ids) < MAX_IDS:
                ids.append(i)
    new["ids"] = ids
    new["dead_fragments"] = [f for f in page.get("dead_fragments") or []
                             if f not in every and unquote(f) not in every]

    new.update(_rendered_files(page, rp))

    raw_targets = {u for u, _ in _link_element_urls(page)}
    changed = {k: v for k, v in new.items() if page.get(k) != v}
    page["original_html"] = {k: page.get(k) for k in changed}
    page.update(changed)
    targets: List[Dict[str, str]] = []
    for u, rel in _link_element_urls(page):
        if u not in raw_targets and all(t["url"] != u for t in targets):
            targets.append({"url": u, "rel": rel})
    rendered["link_targets"] = targets[:MAX_JS_LINKS]
    rendered["promoted"] = True
    return True


# ----------------------------------------------------------------------------------------------------
# The engine's pool of render browsers
# ----------------------------------------------------------------------------------------------------
def default_pool_size(concurrency: int = 4) -> int:
    """Render Chromes for a crawl: RENDER_POOL_SIZE, else one per spare CPU core (at most 4), never more than
    the crawl's workers, and one fewer than MAX_CHROME_INSTANCES (the crawl's shared browser keeps one)."""
    n = RENDER_POOL_SIZE or max(1, min(4, (os.cpu_count() or 2) - 1))
    n = min(n, max(1, int(concurrency or 1)))
    if MAX_CHROME_INSTANCES > 0:
        n = min(n, max(1, MAX_CHROME_INSTANCES - 1))
    return max(1, n)


class _Slot:
    def __init__(self, driver):
        self.driver = driver
        self.renders = 0
        self.overrun = False      # closed by the RENDER_HARD_SECONDS watchdog while rendering


def _first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:160]


def _dead_driver(exc: BaseException, driver) -> bool:
    return isinstance(exc, BrowserGone) or _gone(driver) or any(w in str(exc).lower() for w in _GONE_WORDS)


class RenderPool:
    """
    Renders pages for a crawl with render_all on. Each render takes a free Chrome (started on demand up to
    `size`; MAX_CHROME_INSTANCES and the low-memory guard of browser.new_chrome apply), carrying the crawl's
    login (the shared browser's session state), proxy and sign-out blocking. Chromes restart after
    RENDER_RECYCLE_PAGES pages. When no Chrome of its own can start (a Chrome cap of 1 on a small server), the
    crawl's shared browser is borrowed for the render instead. With `promote` (a render_all crawl's pool) each
    render's values also become the page's own (promote_rendered: JavaScript rendering mode). Every render and
    audit uses the pool's rendering options: `device` (window, touch and User-Agent), `render_timeout` (how long
    it waits for late content) and the shadow DOM / iframe flattening of the rendered HTML.
    """

    START_RETRY_SECONDS = 30   # after Chrome failed to start with none running, renders fail fast this long

    def __init__(self, user_agent: str = "", timeout: int = 15, size: int = 1, site_url: str = "",
                 shared: Optional[SharedBrowser] = None, session_source: Optional[Callable[[], Optional[dict]]] = None,
                 logged_in: Any = False, proxy: Optional[Dict[str, Any]] = None, evidence_dir: str = "",
                 custom_js: Optional[List[Dict[str, str]]] = None,
                 robots_check: Optional[Callable[[str], Tuple[bool, str]]] = None,
                 extra_headers: Optional[Dict[str, str]] = None, is_internal: Optional[Callable[[str], bool]] = None,
                 normalize: Optional[Callable[[str, str], Optional[str]]] = None,
                 on_session_lost: Optional[Callable[[], Optional[dict]]] = None,
                 stop_event: Optional[threading.Event] = None, logger: Optional[Callable[[str], None]] = None,
                 screenshots: bool = True, screenshot_cap: int = RENDER_SCREENSHOT_CAP, mobile: bool = True,
                 a11y: bool = True, promote: bool = False, base_domain: str = "", include_subdomains: bool = False,
                 allowed_domains: Optional[set] = None, extract_options: Optional[ExtractOptions] = None,
                 cpu_gate: Any = None, axe: bool = False, device: str = "desktop",
                 render_timeout: float = RENDER_TIMEOUT_DEFAULT, flatten_shadow_dom: bool = False,
                 flatten_iframes: bool = False):
        self.user_agent = user_agent
        self.timeout = max(5, int(timeout or 15))
        self.size = max(1, int(size or 1))
        self.site_url = site_url
        self.shared = shared
        self.session_source = session_source or (lambda: shared.session_state if shared is not None else None)
        # bool, or a callable read at each render (the crawl may log in after the pool was made)
        self._logged_in = logged_in
        self.proxy = proxy
        self.evidence_dir = evidence_dir
        self.custom_js = [s for s in (custom_js or []) if isinstance(s, dict)
                          and str(s.get("code") or "").strip()][:MAX_CUSTOM_JS]
        self.robots_check = robots_check
        self.extra_headers = {k: v for k, v in (extra_headers or {}).items() if k and v is not None}
        self.is_internal = is_internal
        self.normalize = normalize
        self.on_session_lost = on_session_lost
        self.stop_event = stop_event
        self.logger = logger or (lambda msg: None)
        self.screenshots = screenshots and bool(evidence_dir)
        self.screenshot_cap = max(0, int(screenshot_cap))
        self.mobile = mobile
        self.a11y = a11y
        # JavaScript rendering mode: the rendered HTML is read like the raw HTML (same site scope and options)
        # and its values replace the page's raw-HTML ones (promote_rendered); only while the JavaScript tab can
        # still compare them with the raw HTML (js_tab_reads_original)
        self.promote = bool(promote) and js_tab_reads_original()
        self.base_domain = base_domain
        self.include_subdomains = include_subdomains
        self.allowed_domains = allowed_domains
        self.extract_options = extract_options
        self.cpu_gate = cpu_gate               # the crawl's parse limiter (one parse per CPU slot)
        # axe-core in every render (a render_all crawl): the result goes to the page's PageData["axe"]
        self.axe = bool(axe)
        # Configuration > Rendering: the device every page is rendered (and audited) on, how long a render waits
        # for late content, and the flattening of the rendered HTML
        self.device = render_device(device)
        self.settle_seconds = render_timeout_seconds(render_timeout)
        self.flatten_shadow_dom = bool(flatten_shadow_dom)
        self.flatten_iframes = bool(flatten_iframes)
        self.promoted = 0
        self.audited = 0                       # pages checked by audit() (the Accessibility audit)
        self.axe_ran = 0
        self.axe_not_run = 0
        self._axe_ms: List[int] = []           # axe-core time per checked page (the last AXE_STATS_KEPT)
        self._page_ms: List[int] = []          # the same pages' whole time (load + settle + axe; audits)
        self._cond = threading.Condition()
        self._idle: List[_Slot] = []
        self._count = 0
        self._starting = 0
        self._closed = False
        self._stats_lock = threading.Lock()
        self.rendered = 0
        self.failed = 0
        self.total_ms = 0
        self.screenshots_taken = 0
        self.failures: Dict[str, str] = {}     # url -> why it was not rendered (at most 500 kept)
        self.last_error = ""
        self.login_lost = False
        self.borrowed = 0                      # renders done in the crawl's shared browser
        self._said_borrow = False
        self._retry_at = 0.0
        # limits (render_one's on-demand pool sets shorter ones, see ONDEMAND_*)
        self.screenshot_early = False
        self.budget_seconds = float(RENDER_BUDGET_SECONDS)
        self.hard_seconds = float(RENDER_HARD_SECONDS)
        self.acquire_wait = 180.0

    @property
    def device_user_agent(self) -> str:
        """The User-Agent renders send on the pool's device ('' = the crawl's own; device_user_agent)."""
        return device_user_agent(self.user_agent, self.device)

    @property
    def logged_in(self) -> bool:
        value = self._logged_in
        try:
            return bool(value() if callable(value) else value)
        except Exception:
            return False

    # ------------------------------------------------------------------------------------------------
    @classmethod
    def from_crawler(cls, crawler, evidence_dir: Optional[str] = None, size: Optional[int] = None,
                     screenshots: Optional[bool] = None) -> "RenderPool":
        """A pool set up from a WebsiteCrawler: its User-Agent, timeout, proxy, login, robots.txt policy,
        custom JavaScript snippets, request headers, stop event and log; `promote` and `axe` follow
        config.render_all (JavaScript rendering mode: renders feed the page's own values, and axe-core checks
        every rendered page). `evidence_dir` None = the crawler's
        `evidence_dir` attribute; `screenshots` None = on, except on a small server (MAX_CHROME_INSTANCES set),
        where the Rendered Page tab takes its picture on demand instead."""
        if screenshots is None:
            screenshots = not LOW_MEMORY
        config = crawler.config
        base_domain = getattr(crawler, "base_domain", "") or ""
        include_subdomains = bool(getattr(config, "include_subdomains", False)) or \
            getattr(config, "crawl_scope", "") == "all_subdomains"
        allowed = getattr(crawler, "allowed_domains", None)

        def is_internal(u: str) -> bool:
            return is_internal_url(u, base_domain, include_subdomains=include_subdomains, allowed_domains=allowed)

        robots_check = None
        if getattr(config, "respect_robots", True):
            robots_check = _robots_checker(crawler, is_internal)
        headers: Dict[str, str] = {}
        for h in getattr(config, "request_headers", None) or []:
            if isinstance(h, dict) and str(h.get("name") or "").strip():
                headers[str(h["name"]).strip()] = str(h.get("value") or "")
        auth = getattr(config, "auth", None)
        if auth is not None and getattr(auth, "enabled", False):
            name = str(getattr(auth, "custom_header_name", "") or "").strip()
            value = str(getattr(auth, "custom_header_value", "") or "").strip()
            session = getattr(crawler, "session", None)
            if name and value and session is not None and name in getattr(session, "headers", {}):
                headers[name] = value     # the same header the crawl's own requests carry (value never logged)
        shared = getattr(crawler, "browser", None)
        stop = getattr(crawler, "_stop_event", None)
        if evidence_dir is None:
            evidence_dir = getattr(crawler, "evidence_dir", "") or getattr(config, "evidence_dir", "") or ""

        def logged_in() -> bool:   # read at each render: the login may finish after the pool was made
            return bool(getattr(crawler, "auth_used", False)) and not getattr(crawler, "session_dead", False)

        # the rendered HTML is read with the crawl's own settings (top terms, shared string pool), never with its
        # custom searches / extractors (those run on the rendered HTML separately and are the engine's to place)
        crawl_opts = getattr(crawler, "extract_options", None)
        extract_options = ExtractOptions(top_terms=bool(getattr(crawl_opts, "top_terms", True)),
                                         pool=getattr(crawl_opts, "pool", None))
        return cls(
            user_agent=getattr(config, "user_agent", ""), timeout=getattr(config, "timeout", 15),
            size=size or default_pool_size(getattr(crawler, "concurrency", getattr(config, "concurrency", 4))),
            site_url=getattr(crawler, "start_url", "") or getattr(config, "start_url", ""),
            shared=shared if isinstance(shared, SharedBrowser) else None,
            session_source=lambda: getattr(shared, "session_state", None) or getattr(crawler, "session_state", None),
            logged_in=logged_in, proxy=getattr(config, "proxy", None), evidence_dir=evidence_dir,
            custom_js=getattr(config, "custom_js", None), robots_check=robots_check, extra_headers=headers,
            is_internal=is_internal, on_session_lost=getattr(crawler, "recover_browser_session", None),
            stop_event=stop, logger=getattr(crawler, "log", None), screenshots=screenshots,
            promote=bool(getattr(config, "render_all", False)), base_domain=base_domain,
            include_subdomains=include_subdomains, allowed_domains=allowed, extract_options=extract_options,
            cpu_gate=getattr(crawler, "_cpu_gate", None), axe=bool(getattr(config, "render_all", False)),
            device=getattr(config, "render_device", "desktop"),
            render_timeout=getattr(config, "render_timeout", RENDER_TIMEOUT_DEFAULT),
            flatten_shadow_dom=bool(getattr(config, "flatten_shadow_dom", False)),
            flatten_iframes=bool(getattr(config, "flatten_iframes", False)),
        )

    def log(self, msg: str):
        self.logger(f"[Render] {msg}")

    @property
    def closed(self) -> bool:
        return self._closed

    def _stopped(self) -> bool:
        return self._closed or (self.stop_event is not None and self.stop_event.is_set())

    # -- drivers ---------------------------------------------------------------------------------------
    def _start_slot(self) -> Optional[_Slot]:
        if not chrome_available():
            self.last_error = "Chrome is not available on this server."
            return None
        width, height = DESKTOP_WINDOW
        driver = new_chrome(self.user_agent, self.timeout, eager=True, width=width, height=height,
                            wait=5.0 if MAX_CHROME_INSTANCES > 0 else 0.0, console_log=True, proxy=self.proxy,
                            page_cache=False)      # each page is freed when the next one opens
        if driver is None:
            self.last_error = chrome_error() or "Chrome could not start."
            return None
        try:
            driver._crawler_main_handle = driver.current_window_handle
        except Exception:
            pass
        apply_proxy_auth(driver, self.proxy, self.logger)
        block_dangerous(driver)
        state = self.session_source() if self.logged_in else None
        if state:
            import_session(driver, state, self.site_url)
        return _Slot(driver)

    def _acquire(self, timeout: float = 180.0) -> Optional[_Slot]:
        deadline = time.time() + timeout
        while True:
            with self._cond:
                while True:
                    if self._stopped():
                        return None
                    while self._idle:
                        slot = self._idle.pop()
                        if chrome_alive(slot.driver):
                            return slot
                        self._count -= 1        # closed while idle (memory watchdog): start another
                    if self._count + self._starting < self.size:
                        self._starting += 1
                        break
                    left = deadline - time.time()
                    if left <= 0:
                        self.last_error = "Every render browser stayed busy."
                        return None
                    self._cond.wait(min(left, 1.0))
            slot = None
            if time.time() >= self._retry_at:
                try:
                    slot = self._start_slot()
                finally:
                    with self._cond:
                        self._starting -= 1
                        if slot is not None:
                            self._count += 1
                        elif self._count > 0:
                            self.size = self._count   # a Chrome cap or memory limit: make do with the ones running
                        else:
                            self._retry_at = time.time() + self.START_RETRY_SECONDS
                        self._cond.notify_all()
            else:
                with self._cond:
                    self._starting -= 1
                    self._cond.notify_all()
            if slot is not None or self._count == 0:
                return slot
            # others are running: wait for one of them

    def _release(self, slot: _Slot, broken: bool = False):
        slot.renders += 1
        recycle = broken or slot.renders >= RENDER_RECYCLE_PAGES or not chrome_alive(slot.driver)
        with self._cond:
            if not recycle and not self._closed:
                self._idle.append(slot)
                self._cond.notify()
                return
            self._count -= 1
            self._cond.notify()
        close_chrome(slot.driver)

    # -- rendering -------------------------------------------------------------------------------------
    def _options(self, raw: Optional[dict], load_url: str, screenshot: Optional[bool], on_html,
                 evidence_dir: Optional[str], axe: bool = False) -> RenderOptions:
        out_dir = self.evidence_dir if evidence_dir is None else evidence_dir
        if screenshot is None:
            screenshot = self.screenshots and self.screenshots_taken < self.screenshot_cap
        return RenderOptions(load_url=load_url or "", raw=raw, evidence_dir=out_dir,
                             screenshot=bool(screenshot and out_dir), custom_js=self.custom_js,
                             robots_check=self.robots_check, extra_headers=self.extra_headers,
                             is_internal=self.is_internal, normalize=self.normalize, logged_in=self.logged_in,
                             mobile=self.mobile, a11y=self.a11y, on_html=on_html, axe=bool(axe),
                             settle_seconds=self.settle_seconds, device=self.device,
                             user_agent=self.device_user_agent, flatten_shadow_dom=self.flatten_shadow_dom,
                             flatten_iframes=self.flatten_iframes, screenshot_early=self.screenshot_early,
                             budget_seconds=self.budget_seconds)

    def render(self, url: str, raw: Optional[dict] = None, load_url: str = "", screenshot: Optional[bool] = None,
               on_html: Optional[Callable[[str, str], None]] = None,
               evidence_dir: Optional[str] = None, promote: Optional[bool] = None,
               axe: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """The rendered dict for `url` (opened at `load_url`, the HTTP final URL, when given), or None when
        it could not be rendered (failures[url] says why). `raw` is the page's raw-HTML PageData. `screenshot`
        None = one per page until screenshot_cap. `promote` (None = the pool's setting): the render's values
        become `raw`'s own, changed in place (promote_rendered). `axe` (None = the pool's setting): axe-core runs
        once the page settled and its result becomes raw["axe"] (also when the page could not be rendered: ran
        False with the reason). Thread-safe; blocks until a render browser is free."""
        promote = self.promote if promote is None else bool(promote)
        axe = self.axe if axe is None else bool(axe)
        if not promote or not isinstance(raw, dict):
            rendered = self._render(url, raw, load_url, screenshot, on_html, evidence_dir, axe)
        else:
            seen: Dict[str, str] = {}

            def keep_html(page_url: str, html: str):
                seen["html"] = html
                if on_html is not None:
                    on_html(page_url, html)

            rendered = self._render(url, raw, load_url, screenshot, keep_html, evidence_dir, axe)
            if rendered is not None:
                self._promote(url, raw, rendered, seen.pop("html", ""))
        if axe:
            self._place_axe(url, raw, rendered)
        return rendered

    def _place_axe(self, url: str, raw: Optional[dict], rendered: Optional[Dict[str, Any]]):
        """Moves the render's axe-core result to the page's PageData["axe"] (a page that could not be rendered
        gets ran False with the reason; nothing is set when the crawl was stopped)."""
        result = rendered.pop("axe", None) if isinstance(rendered, dict) else None
        if result is None:
            if rendered is not None or self._stopped():
                return
            why = self.failures.get(url) or self.last_error or "The page could not be rendered."
            result = axe_not_run(mask_console_text(f"The page could not be rendered in Chrome: {why}"))
        self._count_axe(result)
        if isinstance(raw, dict):
            raw["axe"] = result

    def audit(self, url: str, raw: Optional[dict] = None) -> Optional[Dict[str, Any]]:
        """axe-core only, for the Accessibility audit (a crawl without render_all; the engine's audit_hook): the
        page is opened with the pool's session, request headers, robots.txt rules and window size, left to settle,
        and axe-core runs (audit_page). It never promotes, reads links, takes cookies or screenshots or runs
        Custom Search / Extraction / JavaScript, and `raw` (the page's PageData) is never changed: its final URL and
        meta refresh target are only read. Returns PageData["axe"] (ran False with the reason when the page could
        not be checked), or None when the pool is closed or the crawl was stopped. Thread-safe."""
        if self._stopped():
            return None
        if not axe_core.available():
            result = axe_not_run(axe_core.source()[1])
            self._count_axe(result)
            return result
        page = raw if isinstance(raw, dict) else {}
        load_url = str(page.get("final_url") or "")
        opts = RenderOptions(load_url=load_url, raw={"meta_refresh_url": page.get("meta_refresh_url") or ""},
                             robots_check=self.robots_check, extra_headers=self.extra_headers,
                             is_internal=self.is_internal, normalize=self.normalize, logged_in=self.logged_in,
                             mobile=False, a11y=False, scroll=True, axe=True, settle_seconds=self.settle_seconds,
                             device=self.device, user_agent=self.device_user_agent)
        if is_logout_url(url) or is_logout_url(load_url) or (self.logged_in and is_unsafe_url(url)):
            result = axe_not_run("This address signs out or changes data, so the browser does not open it.")
        else:
            result = self._run(url, opts, audit_page, audit=True)
            if result is None:
                if self._stopped():
                    return None
                why = self.failures.get(url) or self.last_error or "The page could not be opened."
                result = axe_not_run(mask_console_text(f"The page could not be opened in Chrome: {why}"))
        self._count_axe(result)
        return result

    def _count_axe(self, result: Dict[str, Any]):
        with self._stats_lock:
            if result.get("ran"):
                self.axe_ran += 1
                self._axe_ms.append(int(result.get("ms") or 0))
                if result.get("page_ms"):
                    self._page_ms.append(int(result["page_ms"]))
                del self._axe_ms[:-AXE_STATS_KEPT]
                del self._page_ms[:-AXE_STATS_KEPT]
            else:
                self.axe_not_run += 1

    def _promote(self, url: str, raw: Dict[str, Any], rendered: Dict[str, Any], html: str):
        """JavaScript rendering mode for one page: its rendered HTML read as PageData replaces its raw values."""
        status = rendered.get("http_status")
        if isinstance(status, int) and status >= 400:
            rendered.setdefault("notes", []).append(
                f"Chrome got HTTP {status} for this page, so its tabs use the HTML the crawler received.")
            return
        if not promotable(raw, rendered):
            return
        final = str(rendered.get("final_url") or raw.get("final_url") or url)
        with (self.cpu_gate if self.cpu_gate is not None else contextlib.nullcontext()):
            page = read_rendered_page(html, final, url=str(raw.get("url") or url), base_domain=self.base_domain,
                                      include_subdomains=self.include_subdomains,
                                      allowed_domains=self.allowed_domains, options=self.extract_options)
        if page is None:
            rendered.setdefault("notes", []).append(
                "The rendered HTML could not be read, so this page's tabs use the HTML the crawler received.")
            return
        if promote_rendered(raw, rendered, page):
            with self._stats_lock:
                self.promoted += 1

    def _render(self, url: str, raw: Optional[dict], load_url: str, screenshot: Optional[bool],
                on_html: Optional[Callable[[str, str], None]],
                evidence_dir: Optional[str], axe: bool = False) -> Optional[Dict[str, Any]]:
        if self._stopped():
            return None
        if is_logout_url(url) or (self.logged_in and is_unsafe_url(url)):
            return self._fail(url, "This address signs out or changes data, so the browser does not open it.")
        opts = self._options(raw, load_url, screenshot, on_html, evidence_dir, axe)
        return self._run(url, opts, render_page)

    def _run(self, url: str, opts: RenderOptions, work: Callable[[Any, str, RenderOptions], Dict[str, Any]],
             audit: bool = False) -> Optional[Dict[str, Any]]:
        """work(driver, url, opts) (render_page or audit_page) in a free render browser, with the hard time limit,
        the login carried over and one more try in a fresh Chrome when the browser died. None when it could not
        run (failures[url] says why)."""
        slot = self._acquire(self.acquire_wait)
        if slot is None:
            if self._stopped():
                return None
            if self._count == 0 and self.shared is not None:
                return self._render_borrowed(url, opts, work, audit)
            return self._fail(url, self.last_error or "No render browser could start.")
        for attempt in (1, 2):
            broken = False
            watchdog = threading.Timer(self.hard_seconds, self._overrun, args=(slot, url))
            watchdog.daemon = True
            watchdog.start()
            try:
                rendered = self._render_with_login(slot.driver, url, opts, work)
                if rendered is None:
                    return None
                self._done(rendered, audit)
                return rendered
            except RenderError as e:
                return self._fail(url, str(e))
            except Exception as e:
                if slot.overrun:          # stopped on purpose: trying again would hang the same way
                    return self._fail(url, f"The page was still loading after {self.hard_seconds:g} s (it keeps "
                                           "reloading or running scripts), so it was not rendered.")
                broken = _dead_driver(e, slot.driver)
                if not broken or attempt == 2:
                    return self._fail(url, f"Rendering failed ({_first_line(e)})")
            finally:
                watchdog.cancel()
                self._release(slot, broken=broken or slot.overrun)
            slot = self._acquire(self.acquire_wait)   # the browser died: one more try in a fresh one
            if slot is None:
                return self._fail(url, self.last_error or "No render browser could start.")
        return None

    def _render_with_login(self, driver, url: str, opts: RenderOptions,
                           work: Callable[[Any, str, RenderOptions], Dict[str, Any]] = render_page
                           ) -> Optional[Dict[str, Any]]:
        for attempt in (1, 2):
            try:
                rendered = work(driver, url, opts)
                self.login_lost = False
                return rendered
            except SessionLost:
                state = self.on_session_lost() if (attempt == 1 and self.on_session_lost) else None
                if not state:
                    if not self.login_lost:
                        self.log(f"The render browser is not logged in ({url} opened the login page); logged-in "
                                 "pages are not rendered until the login works again.")
                    self.login_lost = True
                    self._fail(url, "The page opened the login page (the login session ended).")
                    return None
                import_session(driver, state, self.site_url)
        return None

    def _render_borrowed(self, url: str, opts: RenderOptions,
                         work: Callable[[Any, str, RenderOptions], Dict[str, Any]] = render_page,
                         audit: bool = False) -> Optional[Dict[str, Any]]:
        """Renders (or audits) in the crawl's shared browser (no Chrome of our own could start)."""
        if not self._said_borrow:
            self._said_borrow = True
            self.log(f"No separate render browser could start ({self.last_error or 'Chrome limit'}); pages are "
                     "rendered one at a time in the crawl's own browser.")
        with self.shared.lease(timeout=180) as driver:
            if driver is None:
                return self._fail(url, self.shared.last_error or "The crawl's browser is not available.")
            was_blocked = media_blocked(driver)
            set_media_blocked(driver, False)
            try:
                rendered = self._render_with_login(driver, url, opts, work)
            except RenderError as e:
                return self._fail(url, str(e))
            except Exception as e:
                return self._fail(url, f"Rendering failed ({_first_line(e)})")
            finally:
                set_media_blocked(driver, was_blocked)
        if rendered is not None:
            self.borrowed += 1
            self._done(rendered, audit)
        return rendered

    def _overrun(self, slot: "_Slot", url: str):
        """RENDER_HARD_SECONDS watchdog: closes the Chrome of a render that never finishes, so its pending
        WebDriver call fails and the crawl worker moves on."""
        slot.overrun = True
        self.log(f"{url} was still rendering after {self.hard_seconds:g} s; its browser was closed.")
        close_chrome(slot.driver)

    def _done(self, rendered: Dict[str, Any], audit: bool = False):
        with self._stats_lock:
            if audit:
                self.audited += 1
                return
            self.rendered += 1
            self.total_ms += int(rendered.get("render_ms") or 0)
            if rendered.get("screenshot"):
                self.screenshots_taken += 1

    def _fail(self, url: str, reason: str) -> None:
        with self._stats_lock:
            self.failed += 1
            self.last_error = reason
            if len(self.failures) < 500:
                self.failures[url] = reason
        return None

    def stats(self) -> Dict[str, Any]:
        """Counts for the crawl log and coverage notes; "axe": pages axe-core checked / could not check and the
        median and slowest axe time per page (ms; page_ms = load + settle + axe of the audited pages)."""
        with self._stats_lock:
            axe_ms, page_ms = sorted(self._axe_ms), sorted(self._page_ms)
            return {"rendered": self.rendered, "failed": self.failed, "promoted": self.promoted,
                    "browsers": self._count, "size": self.size,
                    "avg_ms": int(self.total_ms / self.rendered) if self.rendered else 0,
                    "screenshots": self.screenshots_taken, "borrowed": self.borrowed,
                    "login_lost": self.login_lost, "last_error": self.last_error, "audited": self.audited,
                    "device": self.device, "render_timeout": self.settle_seconds,
                    "axe": {"ran": self.axe_ran, "not_run": self.axe_not_run,
                            "median_ms": axe_ms[len(axe_ms) // 2] if axe_ms else 0,
                            "max_ms": axe_ms[-1] if axe_ms else 0,
                            "page_median_ms": page_ms[len(page_ms) // 2] if page_ms else 0}}

    def close(self):
        """Quits every render Chrome (renders still running finish first and then close theirs)."""
        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._count -= len(idle)
            self._cond.notify_all()
        for slot in idle:
            close_chrome(slot.driver)


def _robots_checker(crawler, is_internal: Callable[[str], bool]) -> Optional[Callable[[str], Tuple[bool, str]]]:
    """robots.txt verdicts for render requests: the crawl's own policy for the site, and a separate cookie-less
    policy for other hosts (CDNs, third-party scripts), so no login or custom header goes to them."""
    internal_policy = getattr(crawler, "robots", None)
    holder: Dict[str, Any] = {"ext": getattr(crawler, "_ext_robots", None)}
    lock = threading.Lock()

    def external_policy():
        with lock:
            if holder["ext"] is None:
                import requests
                from .robots import RobotsPolicy
                s = requests.Session()
                s.headers["User-Agent"] = getattr(crawler.config, "user_agent", "") or "Mozilla/5.0"
                holder["ext"] = RobotsPolicy(s, agent=getattr(crawler.config, "robots_user_agent", "Googlebot"),
                                             timeout=min(int(getattr(crawler.config, "timeout", 10) or 10), 10),
                                             verify_ssl=bool(getattr(crawler.config, "verify_ssl", False)))
            return holder["ext"]

    def check(url: str) -> Tuple[bool, str]:
        if is_internal(url):
            return internal_policy.check(url) if internal_policy is not None else (True, "")
        return external_policy().check(url)

    return check


# ----------------------------------------------------------------------------------------------------
# On demand: one page for the Rendered Page tab
# ----------------------------------------------------------------------------------------------------
_ondemand_lock = threading.Lock()
_ondemand: Dict[str, Any] = {"crawler": None, "pool": None, "timer": None, "dir": ""}


def _close_ondemand():
    with _ondemand_lock:
        pool, timer = _ondemand.get("pool"), _ondemand.get("timer")
        _ondemand.update(crawler=None, pool=None, timer=None, dir="")
    if timer is not None:
        timer.cancel()
    if pool is not None:
        pool.close()


def _ondemand_pool(crawler, evidence_dir: str) -> RenderPool:
    with _ondemand_lock:
        ref = _ondemand.get("crawler")
        pool = _ondemand.get("pool")
        same = ref is not None and ref() is crawler and pool is not None and not pool.closed \
            and _ondemand.get("dir") == evidence_dir
        old = None if same else pool
        if not same:
            pool = RenderPool.from_crawler(crawler, evidence_dir=evidence_dir, size=ONDEMAND_POOL_SIZE)
            pool.stop_event = None          # a stopped crawl can still show its pages
            pool.promote = False            # a look at one page never rewrites the crawl's stored values
            pool.axe = False                # ... nor its accessibility results
            # a look at one page: the picture first, no phone-size checks or accessibility tree (not shown for
            # it), shorter waits (ONDEMAND_*)
            pool.mobile = pool.a11y = False
            pool.screenshot_early = True
            pool.settle_seconds = min(pool.settle_seconds, ONDEMAND_SETTLE_MAX)
            pool.budget_seconds = float(ONDEMAND_BUDGET_SECONDS)
            pool.hard_seconds = float(ONDEMAND_HARD_SECONDS)
            pool.acquire_wait = float(ONDEMAND_WAIT_SECONDS)
            _ondemand.update(crawler=weakref.ref(crawler), pool=pool, dir=evidence_dir)
        timer = _ondemand.get("timer")
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(ONDEMAND_IDLE_SECONDS, _close_ondemand)
        timer.daemon = True
        _ondemand["timer"] = timer
        timer.start()
    if old is not None:
        old.close()
    return pool


def _ondemand_allowed(crawler) -> str:
    """'' when render_one may start a Chrome for this crawl, else why not."""
    if not getattr(crawler, "base_domain", ""):
        return "Only pages of the crawled site can be rendered."
    if not chrome_available():
        return "Chrome is not available on this server, so pages cannot be rendered."
    return ""


def warm_ondemand(crawler, evidence_dir: str = "") -> bool:
    """Starts the on-demand render Chrome in the background (the URL details pane is open on a page without a
    render), so "Render this page now" does not wait for Chrome to start (~2-6 s). Nothing happens when one is
    already idle or starting. True when a start was begun."""
    if _ondemand_allowed(crawler):
        return False
    pool = _ondemand_pool(crawler, evidence_dir)
    with pool._cond:
        if pool._idle or pool._starting or pool._count:
            return False

    def run():
        slot = pool._acquire(5.0)
        if slot is not None:
            pool._release(slot)
    threading.Thread(target=run, name="render-warm", daemon=True).start()
    return True


def _crawl_result(crawler, url: str):
    for r in list(getattr(crawler, "results_list", None) or []):
        if getattr(r, "url", None) == url:
            return r
    return None


def render_one(url: str, crawler, evidence_dir: str = "", screenshot: bool = True,
               load_url: str = "", on_html: Optional[Callable[[str, str], None]] = None
               ) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Renders one crawled page now (GET /api/crawl/render): (rendered dict, '') or (None, why not). Only pages
    of the crawled site are opened. Uses the crawl's RenderPool when render_all is running, else a one-Chrome
    pool that closes itself after ONDEMAND_IDLE_SECONDS. The screenshot is saved in `evidence_dir`.
    """
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None, "Only http(s) pages can be rendered."
    base_domain = getattr(crawler, "base_domain", "") or ""
    config = getattr(crawler, "config", None)
    include_subdomains = bool(getattr(config, "include_subdomains", False)) or \
        getattr(config, "crawl_scope", "") == "all_subdomains"
    if not base_domain or not is_internal_url(url, base_domain, include_subdomains=include_subdomains,
                                              allowed_domains=getattr(crawler, "allowed_domains", None)):
        return None, "Only pages of the crawled site can be rendered."
    if not chrome_available():
        return None, "Chrome is not available on this server, so pages cannot be rendered."
    result = _crawl_result(crawler, url)
    raw = getattr(result, "seo", None) if result is not None else None
    raw = raw if isinstance(raw, dict) else None
    if not load_url and raw is not None:
        load_url = str(raw.get("final_url") or "")
    # A Chrome of its own answers at once; the crawl's render pool (busy with the crawl) only when no
    # further Chrome may start
    pool = _ondemand_pool(crawler, evidence_dir)
    pool.failures.pop(url, None)
    pool.last_error = ""
    rendered = pool.render(url, raw=raw, load_url=load_url, screenshot=bool(screenshot and evidence_dir),
                           on_html=on_html, evidence_dir=evidence_dir)
    why = "" if rendered is not None else (pool.failures.get(url) or pool.last_error)
    crawl_pool = getattr(crawler, "render_pool", None)
    if rendered is None and pool._count == 0 and isinstance(crawl_pool, RenderPool) and not crawl_pool._stopped():
        rendered = crawl_pool.render(url, raw=raw, load_url=load_url, screenshot=bool(screenshot and evidence_dir),
                                     on_html=on_html, evidence_dir=evidence_dir, promote=False, axe=False)
        if rendered is None:
            why = crawl_pool.failures.get(url) or crawl_pool.last_error or why
    if rendered is None:
        if why == "Every render browser stayed busy.":
            why = (f"Chrome stayed busy with other pages for {ONDEMAND_WAIT_SECONDS} s (other renders or the "
                   "screenshots taken after the crawl). Try again in a moment.")
        return None, why or "The page could not be rendered."
    return rendered, ""


# ----------------------------------------------------------------------------------------------------
# The Accessibility audit (a crawl with a11y_audit on and render_all off)
# ----------------------------------------------------------------------------------------------------
class AuditHook:
    """
    crawler.audit_hook for the Accessibility audit: hook(url, seo) -> PageData["axe"]. Its RenderPool starts on
    the first page (after the login, so it carries the session) and only runs axe-core (RenderPool.audit): no
    screenshots, phone checks, promotion or Custom JavaScript, and the page's PageData is never changed here (the
    engine stores the result as seo["axe"]). close() quits its Chromes and returns stats() for the crawl log.
    """

    def __init__(self, crawler, size: Optional[int] = None):
        self.crawler = crawler
        self.size = size
        self._pool: Optional[RenderPool] = None
        self._lock = threading.Lock()
        self._closed = False

    def pool(self) -> Optional[RenderPool]:
        with self._lock:
            if self._pool is None and not self._closed:
                p = RenderPool.from_crawler(self.crawler, evidence_dir="", size=self.size, screenshots=False)
                p.promote, p.axe, p.mobile, p.a11y, p.custom_js = False, False, False, False, []
                self._pool = p
                if getattr(self.crawler, "render_pool", None) is None:
                    self.crawler.render_pool = p    # render_one() may borrow it when no other Chrome can start
            return self._pool

    def __call__(self, url: str, seo: Optional[dict]) -> Optional[Dict[str, Any]]:
        pool = self.pool()
        return pool.audit(url, raw=seo) if pool is not None else None

    def stats(self) -> Dict[str, Any]:
        return self._pool.stats() if self._pool is not None else {}

    def close(self) -> Dict[str, Any]:
        with self._lock:
            self._closed = True
            pool = self._pool
        if pool is None:
            return {}
        pool.close()
        return pool.stats()
