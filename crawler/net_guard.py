"""
Network guards for the crawl engine:

  classify_error(exc)        root cause of a request that got no HTTP answer (dns, ssl, timeout, refused,
                             reset, too_many_redirects, other), shown as "Root cause"
  detect_waf(status, ...)    firewall / CDN bot-challenge answers (Cloudflare, Akamai, Sucuri, ...)
  HostThrottle               per-host adaptive pacing: 429/503 pause the host (Retry-After honoured) and slow
                             it down, firewall answers slow it hard, normal answers bring it back gradually
  trap_reason(url)           URLs that only exist because of a crawl trap (repeated folders, endless
                             calendars, page-builder pagination, session ids, URLs over 2,000 characters)
  SiteSession                a requests session that sends the crawl's custom HTTP headers (e.g. the owner's
                             X-ARACHNIX-Key) only to the crawled site, on redirect hops too, and keeps host-only
                             cookies (pasted / imported, HOST_ONLY_COOKIE) on their own host

The category of a URL still comes from its HTTP status alone: a firewall's 403 is a 4xx, a timeout is
"No Response". error_type only explains why.

Ideas from open-seo-crawler and LibreCrawl (MIT, see THIRD_PARTY_NOTICES.md); the code is ours.
"""
import email.utils
import http.client
import re
import socket
import ssl
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, Iterator, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

import requests
import urllib3
from http.cookiejar import DefaultCookiePolicy
from requests.cookies import RequestsCookieJar

# ---------------------------------------------------------------------------------------------
# Custom request headers and host-only cookies: the crawled site only
# ---------------------------------------------------------------------------------------------
# Non-standard attribute on a cookie jar entry that must go to its own host only, as a browser keeps a host-only
# cookie (browser.cookies_to_jar marks pasted / imported cookies without a leading-dot domain). requests' jar would
# send a host-only "example.com" cookie to every *.example.com host.
HOST_ONLY_COOKIE = "X-SlickUP-HostOnly"


def _cookie_host_ok(cookie, host: str) -> bool:
    domain = (cookie.domain or "").lower()
    return domain == host or domain == host + ".local"      # http.cookiejar names "localhost" localhost.local


class HostOnlyCookiePolicy(DefaultCookiePolicy):
    """The standard cookie policy, except that a cookie marked HOST_ONLY_COOKIE is returned to its own host only.
    Set on a jar that holds pasted / imported cookies (browser.cookies_to_jar), so reading the jar directly keeps
    them on their host too; a request that requests merges into a fresh jar is filtered by SiteSession."""

    def return_ok_domain(self, cookie, request):
        if cookie.has_nonstandard_attr(HOST_ONLY_COOKIE):
            try:
                host = (urlparse(request.get_full_url()).hostname or "").lower()
            except Exception:
                return False
            if not _cookie_host_ok(cookie, host):
                return False
        return super().return_ok_domain(cookie, request)


class SiteSession(requests.Session):
    """A requests session whose custom headers (Configuration > HTTP Header, e.g. the owner's X-ARACHNIX-Key) go
    only to URLs `host_ok` accepts. A request to any other host, and a redirect hop that leaves the site, is sent
    without them (requests itself strips only Authorization on a cross-host redirect). A custom header that
    replaced one of the session's own (User-Agent) falls back to the session's value there. Cookies marked
    HOST_ONLY_COOKIE are sent to their own host only, on every request and redirect hop."""

    def __init__(self):
        super().__init__()
        self.site_headers: Dict[str, str] = {}
        self._fallback: Dict[str, Optional[str]] = {}
        self.host_ok: Optional[Callable[[str], bool]] = None

    def set_site_headers(self, headers: Optional[Dict[str, str]], host_ok: Callable[[str], bool]):
        """Sends `headers` with every request whose URL host_ok(url) accepts (and nowhere else)."""
        for name, value in (headers or {}).items():
            if name not in self.site_headers:
                self._fallback[name] = self.headers.get(name)
            self.site_headers[name] = value
            self.headers[name] = value
        self.host_ok = host_ok

    def scope_like(self, other: "SiteSession", names) -> None:
        """Takes `other`'s custom headers named in `names` (and what they replaced elsewhere), for the same hosts."""
        for name in names:
            if name in other.site_headers:
                self.site_headers[name] = other.site_headers[name]
                self._fallback[name] = other._fallback.get(name)
                self.headers[name] = other.site_headers[name]
        self.host_ok = other.host_ok

    def _on_site(self, url: str) -> bool:
        try:
            return bool(self.host_ok is not None and self.host_ok(url))
        except Exception:
            return False

    @staticmethod
    def _host_only_cookies(prepared) -> None:
        """Leaves host-only cookies of other hosts out of the request's Cookie header."""
        jar = getattr(prepared, "_cookies", None)
        if jar is None:
            return
        try:
            host = (urlparse(prepared.url or "").hostname or "").lower()
            cookies = list(jar)
        except Exception:
            return
        if not any(c.has_nonstandard_attr(HOST_ONLY_COOKIE) and not _cookie_host_ok(c, host) for c in cookies):
            return
        kept = RequestsCookieJar()
        for c in cookies:
            if not c.has_nonstandard_attr(HOST_ONLY_COOKIE) or _cookie_host_ok(c, host):
                kept.set_cookie(c)
        prepared.headers.pop("Cookie", None)
        prepared.prepare_cookies(kept)

    def _scope(self, prepared) -> None:
        self._host_only_cookies(prepared)
        if not self.site_headers or self._on_site(prepared.url or ""):
            return
        for name in self.site_headers:
            fallback = self._fallback.get(name)
            if fallback is None:
                prepared.headers.pop(name, None)
            else:
                prepared.headers[name] = fallback

    def prepare_request(self, request):
        prepared = super().prepare_request(request)
        self._scope(prepared)
        return prepared

    def rebuild_auth(self, prepared_request, response):
        """Called by requests on every redirect hop (after its cookies are set): drops the custom headers when the
        hop leaves the site, and host-only cookies of other hosts."""
        super().rebuild_auth(prepared_request, response)
        self._scope(prepared_request)


# ---------------------------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------------------------
ERROR_TYPES = ("dns", "ssl", "timeout", "refused", "reset", "waf", "too_many_redirects", "rate_limited",
               "blocked_address", "other")
# Types that mean "no HTTP answer at all" (category "No Response")
NETWORK_ERROR_TYPES = frozenset({"dns", "ssl", "timeout", "refused", "reset", "too_many_redirects",
                                 "blocked_address", "other"})

ROOT_CAUSE_LABELS: Dict[str, str] = {
    "dns": "DNS lookup failed: the domain name does not resolve",
    "ssl": "SSL/TLS problem: the secure connection could not be set up",
    "timeout": "Timed out: the server did not answer in time",
    "refused": "Connection refused: nothing accepted the connection",
    "reset": "Connection reset: the server dropped the connection",
    "waf": "Blocked by a firewall or bot protection",
    "too_many_redirects": "Too many redirects (redirect loop)",
    "rate_limited": "Rate limited: the server kept asking the crawler to slow down",
    "blocked_address": "Not opened: the address is on a private or cloud-metadata network this crawl may not reach",
    "other": "Network error",
}
# Lead of the error_message written for each type (issues.diagnose reads these words)
_ERROR_LEADS = {
    "dns": "DNS lookup failed: the host name could not be resolved",
    "ssl": "SSL/TLS error: the secure connection could not be set up",
    "refused": "Connection refused: the server did not accept the connection",
    "reset": "Connection reset: the server closed the connection before answering",
    "too_many_redirects": "Too many redirects (redirect loop)",
    "blocked_address": "Not opened: the address is on a network this crawl may not reach",
    "other": "Unable to connect",
}
_TEXT_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("too_many_redirects", ("too many redirects", "exceeded 30 redirects", "redirect loop", "too_many_redirects")),
    ("dns", ("getaddrinfo", "name or service not known", "nodename nor servname", "name resolution",
             "failed to resolve", "no address associated", "could not be resolved", "name_not_resolved",
             "dns lookup")),
    ("ssl", ("ssl", "certificate", "tls ")),
    ("refused", ("refused", "10061", "econnrefused")),
    ("timeout", ("timed out", "timeout")),
    ("reset", ("reset", "10054", "aborted", "remote end closed", "connection broken", "econnreset",
               "remotedisconnected")),
)
_OBJ_NOISE = re.compile(r"<[\w.]+ object at 0x[0-9a-f]+>:?\s*", re.I)


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception and everything it wraps (cause, context, urllib3 'reason', exceptions in args)."""
    seen = set()
    todo = [exc]
    while todo:
        cur = todo.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        reason = getattr(cur, "reason", None)
        todo += [cur.__cause__, cur.__context__, reason if isinstance(reason, BaseException) else None]
        todo += [a for a in getattr(cur, "args", ()) if isinstance(a, BaseException)]


def _types_in(exc: BaseException) -> set:
    found = set()
    u3 = urllib3.exceptions
    for cur in _chain(exc):
        if getattr(cur, "error_kind", "") == "blocked_address":      # crawler/address_guard.py (P24)
            found.add("blocked_address")
        elif isinstance(cur, requests.exceptions.TooManyRedirects):
            found.add("too_many_redirects")
        elif isinstance(cur, (socket.gaierror, getattr(u3, "NameResolutionError", ()))):
            found.add("dns")
        elif isinstance(cur, (ssl.SSLError, requests.exceptions.SSLError, u3.SSLError)):
            found.add("ssl")
        elif isinstance(cur, ConnectionRefusedError):
            found.add("refused")
        elif isinstance(cur, u3.NewConnectionError):
            pass   # urllib3 subclasses it from ConnectTimeoutError; what it wraps decides
        elif isinstance(cur, (requests.exceptions.Timeout, u3.TimeoutError, socket.timeout, TimeoutError)):
            found.add("timeout")
        elif isinstance(cur, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                              http.client.RemoteDisconnected, u3.ProtocolError)):
            found.add("reset")
    return found


def classify_error_text(text: str) -> str:
    """error_type from an error message alone ('' for an empty message, 'other' when nothing matches)."""
    low = str(text or "").lower()
    if not low.strip():
        return ""
    for kind, words in _TEXT_RULES:
        if any(w in low for w in words):
            return kind
    return "other"


def classify_error(exc: BaseException) -> str:
    """Root cause of a failed request: one of NETWORK_ERROR_TYPES."""
    found = _types_in(exc)
    for kind in ("blocked_address", "too_many_redirects", "dns", "ssl", "refused", "timeout", "reset"):
        if kind in found:
            return kind
    kind = classify_error_text(str(exc))
    return kind if kind in NETWORK_ERROR_TYPES else "other"


def error_detail(exc: BaseException, limit: int = 180) -> str:
    """The innermost readable message of an exception chain (e.g. '[Errno 11001] getaddrinfo failed')."""
    best = ""
    for cur in _chain(exc):
        text = _OBJ_NOISE.sub("", str(cur) or "").strip()
        if text and isinstance(cur, OSError) and not isinstance(cur, (requests.exceptions.RequestException,)):
            best = text
    if not best:
        best = _OBJ_NOISE.sub("", str(exc) or type(exc).__name__).strip()
    return best[:limit]


def error_message(kind: str, exc: Optional[BaseException] = None) -> str:
    """Plain error_message for a failed request: what happened, then the technical detail."""
    lead = _ERROR_LEADS.get(kind) or _ERROR_LEADS["other"]
    detail = error_detail(exc) if exc is not None else ""
    return f"{lead} ({detail})" if detail else lead


def consistent_error_type(error_type: str, status: Optional[int]) -> str:
    """error_type when it still fits the status (a re-check may have changed the status since): no-answer
    types need no status, 'waf' and 'rate_limited' an error status."""
    if error_type in NETWORK_ERROR_TYPES:
        return error_type if status is None else ""
    if error_type in ("waf", "rate_limited"):
        return error_type if status is not None and status >= 400 else ""
    return ""


def root_cause_label(error_type: str, waf: str = "", status: Optional[int] = None, retries: int = 0) -> str:
    """'Root cause' text for a result ('' when there is nothing to explain)."""
    if not error_type:
        return ""
    if error_type == "waf":
        return f"{ROOT_CAUSE_LABELS['waf']} ({waf})" if waf else ROOT_CAUSE_LABELS["waf"]
    if error_type == "rate_limited":
        tail = f" (HTTP {status} after {retries} {'retry' if retries == 1 else 'retries'})" if status else ""
        return ROOT_CAUSE_LABELS["rate_limited"] + tail
    return ROOT_CAUSE_LABELS.get(error_type, ROOT_CAUSE_LABELS["other"])


# ---------------------------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------------------------
def retry_after_seconds(value, now: Optional[float] = None) -> Optional[float]:
    """Seconds a Retry-After header asks for (delay in seconds or an HTTP date); None when absent/invalid."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - (now if now is not None else time.time()))


# ---------------------------------------------------------------------------------------------
# Firewall / CDN challenge detection
# ---------------------------------------------------------------------------------------------
WAF_STATUSES = (403, 429, 503)
WAF_SCAN_CHARS = 30000          # only the start of a block page is read for markers
BURST_LABEL = "sudden burst of 403 answers"
# (label, markers that must all appear in the lower-cased body); checked on 403/429/503 answers
_WAF_BODY: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Cloudflare", ("/cdn-cgi/challenge-platform",)),
    ("Cloudflare", ("cf-chl-",)),
    ("Cloudflare", ("cf_chl_",)),
    ("Cloudflare", ("cloudflare", "attention required")),
    ("Cloudflare", ("cloudflare", "just a moment")),
    ("Cloudflare", ("cloudflare", "ray id", "access denied")),
    ("Cloudflare", ("cloudflare", "error 1020")),
    ("Sucuri", ("sucuri", "firewall")),
    ("Wordfence", ("wordfence",)),
    ("Imperva (Incapsula)", ("incapsula incident",)),
    ("Imperva (Incapsula)", ("_incapsula_resource",)),
    ("DataDome", ("captcha-delivery.com",)),
    ("HUMAN (PerimeterX)", ("px-captcha",)),
    ("Akamai", ("access denied", "reference #", "edgesuite")),
    ("SiteGround", ("sgcaptcha",)),
    ("AWS WAF", ("awswaf",)),
    ("Bot challenge (CAPTCHA)", ("g-recaptcha",)),
    ("Bot challenge (CAPTCHA)", ("h-captcha",)),
    ("Bot challenge (CAPTCHA)", ("cf-turnstile",)),
)
# Markers strong enough to call a 2xx page a challenge (Cloudflare also injects its challenge script into
# normal pages, so that one only counts on error answers)
_WAF_BODY_2XX: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("SiteGround", ("sgcaptcha",)),
    ("Imperva (Incapsula)", ("incapsula incident",)),
    ("DataDome", ("captcha-delivery.com", "datadome")),
)


def challenge_header(headers: Dict[str, str]) -> str:
    """Vendor whose response header says this answer is a challenge or a block ('' when none)."""
    h = headers or {}
    if "challenge" in h.get("cf-mitigated", "").lower():
        return "Cloudflare"
    if h.get("x-vercel-mitigated"):
        return "Vercel"
    action = h.get("x-amzn-waf-action", "").lower()
    if action in ("challenge", "captcha", "block"):
        return "AWS WAF"
    if h.get("x-sucuri-block"):
        return "Sucuri"
    return ""


def detect_waf(status: Optional[int], headers: Dict[str, str], body: str = "") -> str:
    """Label of the firewall / bot protection that produced this answer, '' when it looks like a normal
    answer. `headers` has lower-case keys; `body` is the start of the response body."""
    if status is None:
        return ""
    h = headers or {}
    vendor = challenge_header(h)
    if vendor:
        return vendor
    low = (body or "")[:WAF_SCAN_CHARS].lower()
    if status in WAF_STATUSES:
        server = h.get("server", "").lower()
        if "datadome" in server or h.get("x-datadome"):
            return "DataDome"
        if status == 403 and "ddos-guard" in server:
            return "DDoS-Guard"
        if status == 403 and "akamaighost" in server and "reference #" in low:
            return "Akamai"
        rules = _WAF_BODY
    elif 200 <= status < 300:
        rules = _WAF_BODY_2XX
    else:
        return ""
    for label, markers in rules:
        if all(m in low for m in markers):
            return label
    return ""


# ---------------------------------------------------------------------------------------------
# Per-host adaptive pacing
# ---------------------------------------------------------------------------------------------
@dataclass
class _HostState:
    factor: float = 1.0          # slow-down multiplier (1 = full speed, no gap between requests)
    pause_until: float = 0.0     # time.monotonic() before which no request starts
    next_at: float = 0.0         # earliest start of the next request while slowed down
    streak: int = 0              # 429/503 answers since the last normal answer
    throttled: int = 0           # 429/503 answers in total
    blocked: int = 0             # firewall answers in total
    ok: int = 0                  # normal answers in total
    success: int = 0             # pages that answered 2xx/3xx (a 403 burst needs some first)
    escalated_at: float = -1e9   # time.monotonic() of the last factor increase
    recent: Deque[Tuple[bool, str]] = field(default_factory=lambda: deque(maxlen=10))  # (403?, folder)
    burst: bool = False          # a 403 burst is going on


class HostThrottle:
    """Adaptive pacing per host. A host starts at full speed. A 429/503 answer pauses every request to
    that host (Retry-After when given, else 1, 2, 4, 8, 15 s as they repeat) and doubles its factor; a
    firewall answer pauses it 10 s and slows it hard; a timeout or reset slows it a little. While the
    factor is above 1, requests to the host are spaced base_gap * factor seconds apart. Every normal
    answer lowers the factor by 10%, so the speed comes back gradually."""

    def __init__(self, base_gap: float = 0.25, max_factor: float = 16.0, max_pause: float = 120.0,
                 waf_max_factor: float = 16.0):
        self.base_gap = base_gap
        self.max_factor = max_factor
        self.max_pause = max_pause
        self.waf_max_factor = waf_max_factor
        self._hosts: Dict[str, _HostState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def host_of(url: str) -> str:
        try:
            return urlparse(url).netloc.lower()
        except Exception:
            return ""

    def _state(self, url: str) -> _HostState:
        host = self.host_of(url)
        st = self._hosts.get(host)
        if st is None:
            st = self._hosts[host] = _HostState()
        return st

    def gap(self, factor: float) -> float:
        return self.base_gap * factor if factor > 1.0 else 0.0

    def _escalate(self, st: _HostState, factor: float):
        """Raises the factor at most once per second: workers that were already waiting on answers when the
        host started refusing all see the same refusal, which should count once."""
        now = time.monotonic()
        if factor > st.factor and now - st.escalated_at >= 1.0:
            st.factor = factor
            st.escalated_at = now

    def wait_turn(self, url: str, stop_event: Optional[threading.Event] = None) -> bool:
        """Blocks until a request to url's host may start; False when stop_event was set meanwhile."""
        host = self.host_of(url)
        if host not in self._hosts:
            return True
        while True:
            with self._lock:
                st = self._hosts.get(host)
                now = time.monotonic()
                if st is None:
                    return True
                start = max(now, st.pause_until, st.next_at if st.factor > 1.0 else 0.0)
                if start <= now:
                    st.next_at = now + self.gap(st.factor)
                    return True
                wait = start - now
            if stop_event is not None:
                if stop_event.wait(min(wait, 0.5)):
                    return False
            else:
                time.sleep(min(wait, 0.5))

    def throttled(self, url: str, retry_after: Optional[float] = None) -> float:
        """A 429 / 503 answer: pauses the host and doubles its factor. Returns the pause in seconds."""
        with self._lock:
            st = self._state(url)
            st.throttled += 1
            st.streak += 1
            self._escalate(st, min(self.max_factor, max(2.0, st.factor * 2.0)))
            pause = retry_after if retry_after is not None else min(15.0, 2.0 ** (st.streak - 1))
            pause = max(0.0, min(float(pause), self.max_pause))
            st.pause_until = max(st.pause_until, time.monotonic() + pause)
            return pause

    def blocked(self, url: str, retry_after: Optional[float] = None) -> float:
        """A firewall / bot-challenge answer: retrying fast only makes the block last longer."""
        with self._lock:
            st = self._state(url)
            st.blocked += 1
            first = st.blocked == 1
            self._escalate(st, min(self.waf_max_factor, max(8.0, st.factor * 1.5)))
            pause = retry_after if retry_after is not None else (10.0 if first else 0.0)
            pause = max(0.0, min(float(pause), self.max_pause))
            if pause:
                st.pause_until = max(st.pause_until, time.monotonic() + pause)
            return pause

    def error(self, url: str, kind: str):
        """A timeout or reset often means an overloaded server: slow down a little (at most 4x)."""
        if kind not in ("timeout", "reset"):
            return
        with self._lock:
            st = self._state(url)
            self._escalate(st, min(4.0, max(1.5, st.factor * 1.5)))

    def ok(self, url: str):
        """A normal answer: the slow-down fades by 10%."""
        host = self.host_of(url)
        if host not in self._hosts:
            return
        with self._lock:
            st = self._hosts[host]
            st.ok += 1
            st.streak = 0
            if st.factor > 1.0:
                st.factor = st.factor * 0.9
                if st.factor < 1.1:
                    st.factor = 1.0

    def hopeless(self, url: str, limit: int = 25) -> bool:
        """The host answered 429/503 `limit` times in a row: retrying more only slows the crawl down."""
        st = self._hosts.get(self.host_of(url))
        return st is not None and st.streak >= limit

    def note_page(self, url: str, status: Optional[int]) -> str:
        """Tracks page answers for 403 bursts: 6 of the last 10 answered 403, across 3+ top folders, after
        pages of the host loaded fine. Returns 'new' when a burst starts, 'ongoing' for a 403 during a
        burst, '' otherwise. A burst slows the host down like a firewall answer."""
        if status is None:
            return ""
        try:
            path = urlparse(url).path or "/"
        except Exception:
            path = "/"
        folder = path.strip("/").split("/", 1)[0].lower()
        with self._lock:
            st = self._state(url)
            st.recent.append((status == 403, folder))
            if status < 400:
                st.success += 1
            if status != 403:
                if st.burst and len(st.recent) >= 3 and not any(f for f, _ in list(st.recent)[-3:]):
                    st.burst = False
                return ""
            if st.burst:
                return "ongoing"
            forbidden = [f for is403, f in st.recent if is403]
            if st.success > 0 and len(forbidden) >= 6 and len(set(forbidden)) >= 3:
                st.burst = True
                st.blocked += 1
                st.escalated_at = -1e9
                self._escalate(st, min(self.waf_max_factor, max(8.0, st.factor * 2.0)))
                st.pause_until = max(st.pause_until, time.monotonic() + 10.0)
                return "new"
            return ""

    def summary(self) -> Dict[str, dict]:
        """{host: {factor, throttled, blocked, burst}} for hosts that were ever slowed down."""
        with self._lock:
            return {h: {"factor": round(s.factor, 2), "throttled": s.throttled, "blocked": s.blocked,
                        "burst": s.burst}
                    for h, s in self._hosts.items() if s.throttled or s.blocked or s.factor > 1.0}


# ---------------------------------------------------------------------------------------------
# Crawl traps
# ---------------------------------------------------------------------------------------------
TRAP_LABELS: Dict[str, str] = {
    "repeated_segment": "Repeated folder in the path (a relative link that loops)",
    "calendar": "Calendar page far in the past or future (endless next/previous links)",
    "pagination_widget": "Page-builder pagination or filter parameter (endless variants of one page)",
    "session_id": "Session ID in the URL (endless duplicates of the same page)",
    "repeated_param": "The same parameter and value repeated 3 or more times (a link that appends to itself)",
    "too_long": "URL longer than the maximum length",
    # Not a URL pattern: set by the engine's loop guard (siteone-crawler's idea) after 5 error answers
    "loop_guard": "Linked only from an error page, and URLs with the same ending failed 5+ times (a link loop: "
                  "URLs ending in the same path segment kept answering 4xx/5xx)",
}
_CALENDAR_CONTEXT = re.compile(r"(?<![a-z])(?:calendars?|events?|agenda|schedules?|ical|tribe)(?![a-z])", re.I)
_CALENDAR_KEY = re.compile(r"date|month|year|yr|week|day|cal", re.I)
_DATE = re.compile(r"(?<!\d)((?:19|20|21)\d{2})(?:[-/_.]?(0[1-9]|1[0-2]))?(?:[-/_.]?(0[1-9]|[12]\d|3[01]))?(?!\d)")
_VIEW_WORDS = {"month", "week", "day", "list", "photo", "map", "summary", "today", "upcoming", "past"}
_SESSION_KEYS = {"phpsessid", "jsessionid", "sessionid", "session_id", "sid", "oscsid", "zenid", "cfid", "cftoken"}
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,}$")


def _calendar_year_out(year: int, now_year: int) -> bool:
    return year < now_year - 2 or year > now_year + 1


def _calendar_trap(path: str, params, now_year: int) -> bool:
    keys = " ".join(k for k, _ in params)
    if not (_CALENDAR_CONTEXT.search(path) or _CALENDAR_CONTEXT.search(keys)):
        return False
    # A date that ends the path (optionally followed by a view word): /events/2031-04/, /calendar/2031/04/
    segs = [s for s in path.lower().split("/") if s]
    while segs and segs[-1] in _VIEW_WORDS:
        segs.pop()
    tail = "/".join(segs[-3:])
    m = None
    for m in _DATE.finditer(tail):
        pass
    if m and m.end() == len(tail) and (m.start() == 0 or tail[m.start() - 1] == "/")             and _calendar_year_out(int(m.group(1)), now_year):
        return True
    for key, value in params:
        if _CALENDAR_KEY.search(key or ""):
            dm = _DATE.search(value or "")
            if dm and _calendar_year_out(int(dm.group(1)), now_year):
                return True
    return False


def trap_reason(url: str, max_length: int = 2000, now_year: Optional[int] = None) -> str:
    """Code of TRAP_LABELS when `url` looks like a crawl trap, '' otherwise."""
    if max_length and len(url) > max_length:
        return "too_long"
    try:
        p = urlparse(url)
    except Exception:
        return ""
    path = p.path or "/"
    if ";jsessionid=" in url.lower():
        return "session_id"
    segs = [s.lower() for s in path.split("/") if s and not s.isdigit()]
    if segs and Counter(segs).most_common(1)[0][1] >= 3:
        return "repeated_segment"
    params = parse_qsl(p.query, keep_blank_values=True)
    if params:
        keys = [k.lower() for k, _ in params]
        if any(k.startswith(("e-page-", "e-filter-")) or k == "infinity" for k in keys):
            return "pagination_widget"
        if Counter((k.lower(), v) for k, v in params).most_common(1)[0][1] >= 3:
            return "repeated_param"
        if any(k in _SESSION_KEYS and _TOKEN.match(v or "") for k, v in ((k.lower(), v) for k, v in params)):
            return "session_id"
    year = now_year or datetime.now(timezone.utc).year
    if _calendar_trap(path, params, year):
        return "calendar"
    return ""


_DIGITS = re.compile(r"\d+")


def trap_example_key(url: str, code: str) -> str:
    """Key of the trap pattern `url` belongs to, for crawling ONE example of it (the Hygiene and Parameters
    tabs then show the pattern with a real status) while every further URL of the same pattern is skipped.
    '' when no example is crawled: a repeated folder's two-level version is crawled anyway, and a URL over
    the length limit is never requested."""
    if code in ("repeated_segment", "too_long", ""):
        return ""
    try:
        p = urlparse(url)
    except Exception:
        return ""
    keys = sorted({k.lower() for k, _ in parse_qsl(p.query, keep_blank_values=True)})
    if code == "session_id":
        return "session_id"
    if code == "pagination_widget":
        keys = [k for k in keys if k.startswith(("e-page-", "e-filter-")) or k == "infinity"]
    return f"{code}|{p.netloc.lower()}|{_DIGITS.sub('#', (p.path or '/').lower())}|{','.join(keys)}"
