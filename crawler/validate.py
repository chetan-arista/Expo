"""
Validation agent: re-checks reported errors live before anyone acts on them.

For each target URL it makes up to ATTEMPTS fresh requests (cache-busted, no-cache headers, through the
crawl's own logged-in session), spaced a second or two apart, and gives one verdict:
  confirmed       every attempt shows the reported problem again (for an SEO issue sample: the issue's own
                  test, re-run on the fresh page, still finds it)
  not_reproduced  every attempt works now: fixed after the crawl, a temporary problem at crawl time, or (an
                  external link) only automated checkers are refused and a real browser opens it
  intermittent    some attempts fail and some work
  fixed           re-check of chosen URLs only: the URL was an error and now answers 2xx every time
                  (or redirects to a page that does)
Each result also says what it was checked as ("checked_as": error / seo_issue / status) and what its verdict
means for that URL in plain words ("meaning"), so a 200 page whose SEO issue is still there is never read
as "still broken".
For broken links it also opens the page that linked to the URL and checks the link is still there
(link_on_parent; in the rendered page for JavaScript sites, in the bundle for routes read from code).
For one sample URL per SEO issue it re-reads the page and re-runs the issue's own test when that test
looks only at the page itself. A test that needs other pages (orphans, duplicates, alt text read from the
pages using an image...) cannot be re-run on one URL: that issue is "not checked", and a sample with no
issue re-tested is listed under "skipped" (summary.not_checked), never counted as confirmed.
A logged-in re-check never opens a sign-out link or a link that changes data (url_utils.is_logout_url /
changes_data_url, the crawl's own rule): those are "Not checked", so the member session stays alive.

apply_rechecks() writes re-check results back into the crawl (status, category, page data) under the
crawler's lock and rebuilds the SEO index, so every count still adds up.
Never logs or returns cookie values or passwords.
"""
import inspect
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, fields as dc_fields, is_dataclass, replace
from datetime import datetime, timezone
from html import unescape
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import urllib3
from requests.adapters import HTTPAdapter

from . import login_guard
from .engine import (
    NEXT_APP_CHUNK, _fix_encoding, _lower_headers, _read_body, bundle_route_strings,
    external_check_user_agent, external_fallback_user_agent,
)
from .issues import classify
from .page_parser import PageParser
from .robots import RobotsPolicy
from .seo.extract import extract_headers_only, extract_page, parse_html
from .url_utils import (
    changes_data_url, get_domain, is_internal_url, is_logout_url, normalize_url, redirected_to_login,
)

VERDICTS = ("confirmed", "not_reproduced", "intermittent", "fixed")
SCOPES = ("errors", "issues", "urls")
ERROR_CATEGORIES = ("4xx", "5xx", "error", "blocked")
ATTEMPTS = 3
ATTEMPT_GAP = (1.0, 2.0)         # seconds between two attempts on the same URL
WORKERS = 8                      # URLs checked at the same time
MAX_RENDERS = 20                 # parent pages opened in Chrome per run (JavaScript sites)
MAX_BROWSER_CHECKS = 25          # external links opened in a real browser per run (bot walls)
BOT_WALL_CODES = (401, 403, 405, 406, 429, 999)   # answers sites give automated clients they do not trust
_CHALLENGE_RE = re.compile(r"just a moment|attention required|access denied|verify you are (a )?human|are you a robot|"
                           r"captcha|security check|pardon our interruption|request unsuccessful|checking your browser",
                           re.I)
MAX_BUNDLES = 40                 # JavaScript files read per parent page when a route came from code
BUNDLE_MAX_BYTES = 8 * 1024 * 1024
PAGE_MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECT_HOPS = 5
NO_CACHE_HEADERS = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
CACHE_BUST_PARAM = "_cb"
# Signed URLs break when a parameter is added: they get no cache-buster
_SIGNED_QUERY = re.compile(r"(^|&)(x-amz-[a-z-]+|signature|sig|expires|token|policy|key-pair-id)=", re.I)
_CB_PARAM = re.compile(r"(^|&)" + CACHE_BUST_PARAM + r"=[^&]*")
_SPA_MARKERS = ('id="__next"', "id='__next'", 'id="root"', "id='root'", "_next/static", 'id="app"',
                "window.__INITIAL_STATE__", "ng-version")
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_MODULE_LINK = re.compile(r"""<link[^>]+rel=["']?(?:modulepreload|preload)["']?[^>]*>""", re.I)
_HREF_ATTR = re.compile(r"""href=["']([^"']+\.m?js(?:\?[^"']*)?)["']""", re.I)
# Site attributes a page-local SEO test may read; any other one means the test depends on other pages
_PAGE_SITE_ATTRS = {"indexable", "indexability_status", "config", "start_url", "base_domain"}
_ATTRS_JS = ("return Array.from(document.querySelectorAll('a[href],area[href],link[href],img[src],script[src],"
             "iframe[src],source[src],video[src],audio[src]')).map(e => e.href || e.src || '');")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Fresh page data of the last re-check per crawler (id(crawler) -> url -> data), read by apply_rechecks.
# Kept out of the JSON results: page data is large.
_FRESH: Dict[int, Dict[str, dict]] = {}
_FRESH_LOCK = threading.Lock()
_FRESH_KEEP = 4


# ---------------------------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------------------------
def cache_bust(url: str, stamp: str = "") -> str:
    """`url` with ?_cb=<stamp> (or &_cb= after an existing query); the fragment is dropped (never sent).
    Signed URLs (S3, CloudFront...) are returned unchanged."""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    query = p.query or ""
    if _SIGNED_QUERY.search(query):
        return urlunsplit((p.scheme, p.netloc, p.path or "/", query, ""))
    query = _CB_PARAM.sub("", query).lstrip("&")
    stamp = stamp or f"{int(time.time() * 1000)}{random.randint(100, 999)}"
    query = f"{query}&{CACHE_BUST_PARAM}={stamp}" if query else f"{CACHE_BUST_PARAM}={stamp}"
    return urlunsplit((p.scheme, p.netloc, p.path or "/", query, ""))


def strip_cache_bust(url: str) -> str:
    """Removes the cache-buster again (a redirect's Location may carry it along)."""
    if not url or CACHE_BUST_PARAM + "=" not in url:
        return url
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    query = _CB_PARAM.sub("", p.query or "").lstrip("&")
    return urlunsplit((p.scheme, p.netloc, p.path, query, p.fragment))


def _norm(url: str) -> str:
    return normalize_url(url) or url or ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status_text(code: Optional[int], reason: str = "") -> str:
    return f"{code} {reason or ''}".strip() if code is not None else (reason or "No Response")


def _looks_spa(html: str) -> bool:
    head = html[:200000]
    return any(m in head for m in _SPA_MARKERS)


# ---------------------------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------------------------
@dataclass
class Attempt:
    """One live request for a target URL."""
    status: Optional[int] = None
    text: str = ""
    ms: int = 0
    via: str = "http"                      # "http" | "browser"
    location: str = ""                     # absolute Location of a 3xx (cache-buster removed)
    content_type: str = ""
    error: str = ""
    blocked: bool = False                  # robots.txt disallows it: no request was sent
    login_wall: bool = False               # sent to the login page and the session could not be renewed
    headers: Dict[str, str] = field(default_factory=dict, repr=False)
    seo: Optional[Dict[str, Any]] = field(default=None, repr=False)
    title: str = ""

    @property
    def category(self) -> str:
        return classify(self.status, self.location or None, self.blocked)

    def public(self) -> dict:
        out = {"status": self.status, "ms": self.ms, "via": self.via, "text": self.text}
        if self.blocked:
            out["blocked"] = True
        return out


@dataclass
class Target:
    """A URL to re-check and what the crawl reported for it."""
    url: str
    kind: str = "internal"                 # "internal" | "external"
    result: Any = None                     # CrawlResult | ExternalLink | other tab row | None
    reported_status: Optional[int] = None
    reported_category: Optional[str] = None   # None: the URL is not in this crawl
    reported_text: str = ""
    parent: str = ""
    source: str = ""                       # how the crawl found it (discovery source)
    link_tag: str = ""
    raw_href: str = ""
    source_file: str = ""
    status_check: bool = False             # an error (or a chosen URL): the verdict is about its status
    issues: List[Tuple[Any, Any]] = field(default_factory=list)   # (Tab, Filter) samples on this URL


@dataclass
class ParentPage:
    """The page that linked to a target, as it is now."""
    url: str
    ok: bool = False
    status: Optional[int] = None
    final_url: str = ""
    note: str = ""
    html: str = ""
    links: Set[str] = field(default_factory=set)
    refresh: str = ""
    locs: Set[str] = field(default_factory=set)
    location: str = ""
    spa: bool = False
    rendered: Optional[Set[str]] = None
    rendered_html: str = ""


class _SiteSpy:
    """Wraps a Site and records which of its attributes a filter test reads."""
    __slots__ = ("_site", "used")

    def __init__(self, site):
        object.__setattr__(self, "_site", site)
        object.__setattr__(self, "used", set())

    def __getattr__(self, name):
        self.used.add(name)
        return getattr(self._site, name)


# ---------------------------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------------------------
class Validator:
    """
    Re-checks a crawl's reported errors live. Use from a background thread:
        v = Validator(crawler, progress=cb, stop_event=ev)
        out = v.run(scope="errors")          # or run(urls=[...], scope="urls") for a re-check
        apply_rechecks(crawler, out["results"])   # re-checks only: writes the new statuses into the crawl
    `progress` is called after every URL, with (done, total) when it takes two arguments, else with
    snapshot() (the same shape as run() returns). snapshot() may also be polled from another thread.
    """

    def __init__(self, crawler, progress: Optional[Callable[..., Any]] = None,
                 stop_event: Optional[threading.Event] = None, workers: int = WORKERS):
        self.crawler = crawler
        self.config = crawler.config
        self.progress = progress
        self._progress_pair = _takes_two_args(progress)
        self.stop_event = stop_event or threading.Event()
        self.workers = max(1, min(WORKERS, int(workers or WORKERS)))
        self.scope = "errors"
        self.recheck = False
        self.done = 0
        self.total = 0
        self.results: Dict[str, dict] = {}
        self.skipped: List[dict] = []
        self.login_guard: List[dict] = []
        self.login_guard_summary: Dict[str, int] = {}
        self.started_at = ""
        self.finished_at = ""
        self._fresh: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._parents: Dict[Tuple[str, bool], ParentPage] = {}
        self._parent_locks: Dict[Tuple[str, bool], threading.Lock] = {}
        self._bundles: Dict[str, Optional[str]] = {}
        self._bundle_routes: Dict[str, Set[str]] = {}
        self._browser_lock = threading.Lock()
        self._renders = 0
        self._ext_browser_lock = threading.Lock()
        self._browser_checks = 0
        self._relogin_lock = threading.Lock()
        self._relogin_failed = False
        self._relogged = False

        self.auth_used = bool(getattr(crawler, "auth_used", False))
        handler = getattr(crawler, "auth_handler", None)
        self.browser_only = bool(self.auth_used and handler is not None and getattr(handler, "browser_only", False)
                                 and hasattr(crawler, "_browser_fetch"))
        # The member session is in use: sign-out and data-changing links are never opened (see _unsafe_reason)
        self.logged_in = self.auth_used or bool(getattr(crawler, "session_state", None))
        self.start_url = _norm(getattr(crawler, "start_url", "") or "")
        timeout = int(getattr(self.config, "timeout", 15) or 15)
        self.timeout = getattr(crawler, "request_timeout", None) or (min(timeout, 10), timeout)
        self.ext_timeout = (min(timeout, 5), min(timeout, 10))
        self.verify = bool(getattr(self.config, "verify_ssl", False))
        self.limiter = getattr(crawler, "rate_limiter", None)
        self.session = self._member_session()
        self.ext_session = self._external_session()
        self.robots: Optional[RobotsPolicy] = None
        self.ext_robots: Optional[RobotsPolicy] = None
        if getattr(self.config, "respect_robots", False):
            agent = getattr(self.config, "robots_user_agent", "Googlebot")
            self.robots = RobotsPolicy(self.session, agent=agent, timeout=min(timeout, 10), verify_ssl=self.verify)
            self.ext_robots = RobotsPolicy(self.ext_session, agent=agent, timeout=min(timeout, 10),
                                           verify_ssl=self.verify)

    # ---------------------------------------------------------------- sessions
    def _member_session(self) -> requests.Session:
        """Same cookies (the very same jar), headers and auth as the crawl, without the crawl's automatic
        retries on 5xx: every attempt must see the server's own first answer."""
        base = getattr(self.crawler, "http_session", None) or getattr(self.crawler, "session", None)
        s = requests.Session()
        if base is not None:
            s.headers.clear()
            s.headers.update(base.headers)
            s.cookies = base.cookies
            s.auth = base.auth
        else:
            s.headers["User-Agent"] = getattr(self.config, "user_agent", "") or "Mozilla/5.0"
        adapter = HTTPAdapter(max_retries=0, pool_connections=10, pool_maxsize=self.workers + 4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def _external_session(self) -> requests.Session:
        """Plain session for other sites: no login cookies or auth headers leave the crawled site."""
        s = requests.Session()
        adapter = HTTPAdapter(max_retries=0, pool_connections=10, pool_maxsize=self.workers + 4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"User-Agent": external_check_user_agent(getattr(self.config, "user_agent", "")),
                          "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
        return s

    def _is_internal(self, url: str) -> bool:
        """The crawler's own rule for what belongs to the crawled site."""
        try:
            return is_internal_url(url, getattr(self.crawler, "base_domain", ""),
                                   bool(getattr(self.config, "include_subdomains", False)),
                                   set(getattr(self.crawler, "allowed_domains", set()) or ()))
        except Exception:
            return False

    def _unsafe_reason(self, url: str) -> str:
        """Why a logged-in re-check must not open this internal URL, "" when it may. The crawl's own rule
        (url_utils.is_unsafe_url): a sign-out link would end the member session the re-check (and the person)
        uses; a data-changing link (admin row action, one-time token, cart change, delete step) would act on the
        site as the member. Only the start URL, which the person chose, may be a data-changing one."""
        if not self.logged_in or not url or not self._is_internal(url):
            return ""
        if is_logout_url(url):
            return ("Not checked: this is a sign-out link. Opening it would end the login session the re-check "
                    "uses, so it was not requested.")
        if changes_data_url(url) and _norm(url) != self.start_url:
            return ("Not checked: opening this link while logged in would change data on the site (an admin "
                    "action, a one-time token link, a cart change or a delete step), so it was not requested.")
        return ""

    def _log(self, msg: str):
        try:
            self.crawler.log(f"[Validation] {msg}")
        except Exception:
            pass

    # ---------------------------------------------------------------- run
    def run(self, urls: Optional[List[str]] = None, scope: str = "errors") -> dict:
        """Re-checks the targets of `scope` and returns a dict shaped like /api/validate/status without
        "state". errors = every 4xx/5xx/no-response/blocked internal URL and every broken external link;
        issues = errors + one sample URL per SEO issue filter; urls = exactly `urls` (a re-check)."""
        scope = scope if scope in SCOPES else "errors"
        self.scope, self.recheck = scope, scope == "urls"
        self.started_at = _now_iso()
        targets = self._targets(urls or [], scope)
        guard = None
        guard_urls: List[Any] = []
        if scope != "urls" and login_guard.was_logged_in(self.crawler):
            guard = login_guard.LoginGuard(self.crawler, stop_event=self.stop_event)
            guard_urls = guard.candidates()
        with self._lock:
            self.total = len(targets) + len(guard_urls)
            self.done = 0
        self._log(f"Re-checking {len(targets)} URL(s) live ({scope})"
                  + (f" and {len(guard_urls)} member page(s) logged out" if guard_urls else "") + "...")
        self._notify()

        if targets:
            pool = ThreadPoolExecutor(max_workers=min(self.workers, len(targets)), thread_name_prefix="validate")
            futures = {pool.submit(self._check_safe, t): t for t in targets}
            try:
                for fut in as_completed(futures):
                    t = futures[fut]
                    try:
                        entry, fresh = fut.result()
                    except Exception as e:     # never lose the run for one URL
                        entry, fresh = None, None
                        self._skip(t.url, f"Could not be re-checked ({str(e)[:120]})")
                    with self._lock:
                        if entry is not None:
                            self.results[t.url] = entry
                            if fresh is not None:
                                self._fresh[t.url] = fresh
                        self.done += 1
                    self._notify()
                    if self.stop_event.is_set():
                        break
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
            with self._lock:
                checked = set(self.results) | {s["url"] for s in self.skipped}
            for t in targets:
                if t.url not in checked:
                    self._skip(t.url, "Not checked: the validation was stopped first.")

        if guard is not None and guard_urls and not self.stop_event.is_set():
            base_done = self.done

            def guard_progress(done: int, _total: int):
                with self._lock:
                    self.done = base_done + done
                self._notify()

            try:
                report = guard.run(progress=guard_progress)
                self.login_guard = report.get("rows") or []
                self.login_guard_summary = {k: v for k, v in report.items() if k != "rows"}
            except Exception as e:
                self._log(f"Login guard could not run: {str(e)[:160]}")
                self.login_guard_summary = {"not_checked": f"The login check could not run ({str(e)[:120]})."}
        elif guard is not None and guard_urls:
            self.login_guard_summary = {"not_checked": "Not checked: the validation was stopped first."}
        with self._lock:
            self.done = self.total if not self.stop_event.is_set() else self.done
        self.finished_at = _now_iso()
        with _FRESH_LOCK:
            _FRESH[id(self.crawler)] = dict(self._fresh)
            while len(_FRESH) > _FRESH_KEEP:
                _FRESH.pop(next(iter(_FRESH)))
        snap = self.snapshot()
        s = snap["summary"]
        self._log(f"Done: {s['confirmed']} confirmed, {s['not_reproduced']} not reproduced, "
                  f"{s['intermittent']} intermittent, {s['fixed']} fixed"
                  + (f", {len(self.skipped)} not checked" if self.skipped else "") + ".")
        self._notify()
        return snap

    def apply(self) -> int:
        """Writes this run's results into the crawl (see apply_rechecks)."""
        return apply_rechecks(self.crawler, self.results)

    def snapshot(self) -> dict:
        """Current progress and results, shaped like /api/validate/status without "state"."""
        with self._lock:
            results = {u: dict(e) for u, e in self.results.items()}
            done, total = self.done, self.total
            skipped = list(self.skipped)
        summary = {v: 0 for v in VERDICTS}
        for e in results.values():
            summary[e["verdict"]] = summary.get(e["verdict"], 0) + 1
        summary["link_removed"] = sum(1 for e in results.values() if e.get("link_on_parent") is False)
        summary["not_checked"] = len(skipped)
        guard = list(self.login_guard)
        return {
            "done": done, "total": total, "scope": self.scope,
            "summary": summary, "results": results, "skipped": skipped,
            "login_guard": guard, "login_guard_summary": dict(self.login_guard_summary),
            "started_at": self.started_at, "finished_at": self.finished_at,
        }

    def _notify(self):
        if not self.progress:
            return
        try:
            if self._progress_pair:
                self.progress(self.done, self.total)
            else:
                self.progress(self.snapshot())
        except Exception:
            pass

    def _skip(self, url: str, reason: str):
        with self._lock:
            if not any(s["url"] == url for s in self.skipped):
                self.skipped.append({"url": url, "reason": reason})

    # ---------------------------------------------------------------- targets
    def _targets(self, urls: List[str], scope: str) -> List[Target]:
        crawler = self.crawler
        lock = getattr(crawler, "_lock", None)
        if lock is not None:
            with lock:
                results = list(crawler.results_list)
                externals = list(getattr(crawler, "external_links_list", []) or [])
        else:
            results = list(getattr(crawler, "results_list", []) or [])
            externals = list(getattr(crawler, "external_links_list", []) or [])
        by_url = {r.url: r for r in results}
        ext_by_url = {e.url: e for e in externals}
        chosen: Dict[str, Target] = {}

        if scope == "urls":
            for raw in urls:
                raw = (raw or "").strip()
                if not raw.startswith(("http://", "https://")):
                    continue
                url = raw if (raw in by_url or raw in ext_by_url) else _norm(raw)
                if url in chosen:
                    continue
                if url in by_url:
                    t = self._internal_target(by_url[url])
                elif url in ext_by_url:
                    t = self._external_target(ext_by_url[url])
                else:
                    t = Target(url=url, kind="internal" if self._is_internal(url) else "external")
                t.status_check = True
                chosen[url] = t
            return self._safe_targets(chosen)

        for r in results:
            if r.category in ERROR_CATEGORIES:
                t = self._internal_target(r)
                t.status_check = True
                chosen[r.url] = t
        for e in externals:
            if _external_broken(e) and e.url not in chosen:
                t = self._external_target(e)
                t.status_check = True
                chosen[e.url] = t
        if scope == "issues":
            self._add_issue_samples(chosen, by_url, ext_by_url)
        return self._safe_targets(chosen)

    def _safe_targets(self, chosen: Dict[str, Target]) -> List[Target]:
        """The targets a logged-in re-check may open; sign-out and data-changing links are listed as not
        checked instead of being requested (in every scope, also URLs posted for a re-check)."""
        out: List[Target] = []
        for url, t in chosen.items():
            reason = self._unsafe_reason(url)
            if reason:
                self._skip(url, reason)
                continue
            out.append(t)
        if len(out) < len(chosen):
            self._log(f"{len(chosen) - len(out)} sign-out or data-changing link(s) not opened (logged in).")
        return out

    def _internal_target(self, r) -> Target:
        return Target(url=r.url, kind="internal", result=r, reported_status=r.status_code,
                      reported_category=r.category, reported_text=r.http_status or "",
                      parent=r.parent_url or "", source=r.discovery_source or "", link_tag=r.link_tag or "",
                      raw_href=r.raw_href or "", source_file=r.source_file or "")

    def _external_target(self, e) -> Target:
        return Target(url=e.url, kind="external", result=e, reported_status=e.status_code,
                      reported_category=classify(e.status_code, getattr(e, "redirect_destination", None)),
                      reported_text=e.http_status or "", parent=e.parent_url or "",
                      source=e.discovery_source or "", link_tag="a")

    def _add_issue_samples(self, chosen: Dict[str, Target], by_url: dict, ext_by_url: dict):
        """One sample URL per SEO issue filter; a URL already chosen is preferred, so one set of requests
        answers several questions."""
        try:
            _site, index = self.crawler.site_index()
        except Exception as e:
            self._log(f"SEO issues could not be listed ({str(e)[:120]}); re-checking errors only.")
            return
        for issue in getattr(index, "issues", None) or []:
            tr = index.tabs.get(issue.get("tab"))
            if tr is None:
                continue
            flt = next((f for f in tr.tab.filters if f.id == issue.get("filter")), None)
            hits = [h for h in (tr.filter_rows.get(issue.get("filter")) or []) if getattr(h, "url", "")]
            if flt is None or not hits:
                continue
            row = next((h for h in hits if h.url in chosen), None) or hits[0]
            t = chosen.get(row.url)
            if t is None:
                if row.url in by_url and by_url[row.url] is row:
                    t = self._internal_target(row)
                elif row.url in ext_by_url and ext_by_url[row.url] is row:
                    t = self._external_target(row)
                else:
                    code = getattr(row, "status_code", None)
                    t = Target(url=row.url, kind="internal" if self._is_internal(row.url) else "external", result=row,
                               reported_status=code,
                               # a row of another tab (a sitemap-only URL...) that the crawl never requested
                               reported_category=classify(code) if code is not None else None)
                chosen[row.url] = t
            t.issues.append((tr.tab, flt))

    # ---------------------------------------------------------------- one target
    def _check_safe(self, t: Target) -> Tuple[Optional[dict], Optional[dict]]:
        if self.stop_event.is_set():
            return None, None
        return self._check(t)

    def _check(self, t: Target) -> Tuple[Optional[dict], Optional[dict]]:
        attempts: List[Attempt] = []
        for i in range(ATTEMPTS):
            if self.stop_event.is_set():
                break
            if i and self.stop_event.wait(random.uniform(*ATTEMPT_GAP)):
                break
            # The body is read on the last attempt (fresh page data for apply_rechecks) and on every
            # attempt of an SEO issue sample (its test runs on each)
            a = self._attempt(t, read_body=bool(t.issues) or i == ATTEMPTS - 1)
            if a is None:
                break
            attempts.append(a)
            if a.login_wall:
                self._skip(t.url, "Not checked: the login session ended and logging in again did not work, "
                                  "so this page could not be opened as a member.")
                return None, None
            if a.blocked:
                if t.reported_category != "blocked":
                    self._skip(t.url, f"Not checked: robots.txt now blocks this URL ({a.error or 'Disallow'}), "
                                      "so it was not requested again.")
                    return None, None
                break   # robots.txt still disallows it: nothing is requested, one look is enough
        if not attempts or (len(attempts) < ATTEMPTS and not attempts[-1].blocked):
            self._skip(t.url, "Not checked: the validation was stopped first.")
            return None, None

        final = self._follow(attempts) if all(300 <= (a.status or 0) < 400 for a in attempts) else None
        if final is not None and final[1] is None and self._unsafe_reason(final[0]):
            self._skip(t.url, f"Not checked: it now redirects to a sign-out or data-changing link ({final[0][:160]}), "
                              "which a logged-in re-check never opens.")
            return None, None
        verdict, note = self._status_verdict(t, attempts, final)
        http_attempts = list(attempts)
        bot_wall = False
        if t.status_check and self._bot_wall(t, attempts) and not self.stop_event.is_set():
            b = self._browser_check(t.url)
            if b is not None:
                attempts.append(b)
                verdict, note = _bot_wall_verdict(verdict, note, http_attempts, b, t.url)
                bot_wall = b.status is not None and b.status < 400
        issue_rows: List[dict] = []
        if t.issues:
            issue_rows = [self._issue_check(t, tab, flt, attempts) for tab, flt in t.issues]
            if not t.status_check:
                combined, note = _combine_issue_verdicts(issue_rows)
                if combined is None:
                    # No issue of this sample could be re-tested: "not checked", never "confirmed"
                    self._skip(t.url, _untested_reason(issue_rows, attempts))
                    return None, None
                verdict = combined

        link_on_parent, link_note = None, ""
        if t.status_check and t.reported_category in ERROR_CATEGORIES and t.parent.startswith(("http://", "https://")):
            try:
                link_on_parent, link_note = self._link_on_parent(t)
            except Exception as e:
                link_on_parent, link_note = None, f"The linking page could not be checked ({str(e)[:100]})."
        if link_note:
            note = f"{note} {link_note}".strip()

        applied = _applied_attempt(verdict, http_attempts)   # the crawl keeps what a plain HTTP client gets
        checked_as = ("seo_issue" if not t.status_check else
                      "error" if t.reported_category in ERROR_CATEGORIES else "status")
        entry = {
            "verdict": verdict,
            "checked_as": checked_as,
            "meaning": _meaning(verdict, checked_as, bot_wall),
            "bot_wall": bot_wall,
            "attempts": [a.public() for a in attempts],
            "link_on_parent": link_on_parent,
            "parent": t.parent if t.parent.startswith(("http://", "https://")) else "",
            "checked_at": _now_iso(),
            "note": note,
            "kind": t.kind,
            "reported": {"status": t.reported_status, "category": t.reported_category,
                         "http_status": t.reported_text},
            "now": {"status": applied.status, "category": applied.category, "http_status": applied.text,
                    "location": applied.location, "blocked": applied.blocked, "error": applied.error,
                    "ms": applied.ms},
        }
        if final:
            entry["now"]["final_url"], entry["now"]["final_status"] = final
        if issue_rows:
            entry["issues"] = issue_rows
        fresh = {"content_type": applied.content_type, "headers": applied.headers, "seo": applied.seo,
                 "title": applied.title}
        return entry, fresh

    def _status_verdict(self, t: Target, attempts: List[Attempt],
                        final: Optional[Tuple[str, Optional[int]]]) -> Tuple[str, str]:
        n = len(attempts)
        cats = [a.category for a in attempts]
        errs = [c in ERROR_CATEGORIES for c in cats]
        reported = t.reported_category
        codes = _codes(attempts)
        if attempts[-1].blocked and n == 1:
            rule = attempts[-1].error
            return "confirmed", f"Still blocked by robots.txt{f' ({rule})' if rule else ''}; it was not requested."
        redirect_to_error = bool(final and final[1] is not None and final[1] >= 400)
        if reported in ERROR_CATEGORIES:
            if all(errs):
                same = all(c == reported for c in cats)
                if same:
                    return "confirmed", f"Still broken: answered {codes}."
                return "confirmed", f"Still failing, now with a different answer: {codes} (the crawl saw {_short_reported(t)})."
            if not any(errs):
                if redirect_to_error:
                    return "confirmed", (f"Now redirects to {final[0]}, which answers {final[1]}: "
                                         "still broken for visitors.")
                works = all(c == "2xx" for c in cats) or bool(final and final[1] is not None and 200 <= final[1] < 300)
                via = f" (redirects to {final[0]})" if final else ""
                if self.recheck and works:
                    return "fixed", f"Fixed: answered {codes}{via}."
                if reported == "blocked":
                    return "not_reproduced", f"robots.txt no longer blocks it: answered {codes}{via}."
                return "not_reproduced", (f"Works now: answered {codes}{via}. It was fixed after the crawl, "
                                          "or the crawl hit a temporary problem.")
            failed = sum(errs)
            return "intermittent", (f"Unstable: {failed} of {n} checks failed ({codes}). Visitors sometimes "
                                    "get an error here.")
        if reported is None:
            if all(errs):
                return "confirmed", f"Not in this crawl. Broken: {codes}."
            if not any(errs):
                if redirect_to_error:
                    return "confirmed", f"Not in this crawl. Redirects to {final[0]}, which answers {final[1]}."
                return "not_reproduced", f"Not in this crawl. Works: {codes}."
            return "intermittent", f"Not in this crawl. Unstable: {codes}."
        # The crawl saw this URL working
        if not any(errs):
            if redirect_to_error:
                return "not_reproduced", f"Now redirects to {final[0]}, which answers {final[1]}."
            return "confirmed", f"Answers as reported: {codes}."
        if all(errs):
            return "not_reproduced", f"Broken now: {codes} (the crawl saw {_short_reported(t)})."
        return "intermittent", f"Unstable: {codes} (the crawl saw {_short_reported(t)})."

    # ---------------------------------------------------------------- requests
    def _attempt(self, t: Target, read_body: bool) -> Optional[Attempt]:
        robots = self.robots if t.kind == "internal" else self.ext_robots
        if robots is not None:
            try:
                allowed, rule = robots.check(t.url)
            except Exception:
                allowed, rule = True, ""
            if not allowed:
                return Attempt(status=None, text="Blocked by robots.txt", blocked=True, error=rule)
        if t.kind == "external":
            a = self._http(t.url, internal=False, read_body=read_body)
            if a is not None and (a.status is None or a.status >= 400) and not self.stop_event.is_set():
                retry = self._http(t.url, internal=False, read_body=read_body,
                                   user_agent=external_fallback_user_agent(getattr(self.config, "user_agent", "")))
                if retry is not None and retry.status is not None and retry.status < 400:
                    a = retry
            return a
        if self.browser_only:
            return self._browser_attempt(t.url, read_body)
        a = self._http(t.url, internal=True, read_body=read_body)
        if a is not None and self.auth_used and a.location and redirected_to_login(t.url, a.location):
            state = self._renew_session()
            if state == "renewed":
                b = self._http(t.url, internal=True, read_body=read_body)
                if b is None:
                    return None
                if b.location and redirected_to_login(t.url, b.location):
                    b.login_wall = True
                return b
            if state == "failed":
                a.login_wall = True
        return a

    def _http(self, url: str, internal: bool, read_body: bool, user_agent: str = "") -> Optional[Attempt]:
        """One GET without following redirects. None when the run was stopped while waiting for its turn."""
        if internal and self.limiter is not None and not self.limiter.acquire(self.stop_event):
            return None
        sess = self.session if internal else self.ext_session
        target = cache_bust(url) if internal else url
        headers = dict(NO_CACHE_HEADERS)
        if user_agent:
            headers["User-Agent"] = user_agent
        start = time.time()
        try:
            resp = sess.get(target, headers=headers, timeout=self.timeout if internal else self.ext_timeout,
                            verify=self.verify, allow_redirects=False, stream=True)
        except requests.exceptions.Timeout:
            return Attempt(status=None, text="Connection Timeout", ms=_ms(start),
                           error=f"Timed out after {self.timeout[1] if internal else self.ext_timeout[1]}s")
        except requests.exceptions.SSLError as e:
            return Attempt(status=None, text="SSL Certificate Error", ms=_ms(start), error=str(e)[:200])
        except requests.exceptions.ConnectionError as e:
            cause = e.args[0] if e.args else None
            timeout = isinstance(cause, urllib3.exceptions.ReadTimeoutError) or \
                isinstance(getattr(cause, "reason", None), urllib3.exceptions.ReadTimeoutError)
            return Attempt(status=None, text="Connection Timeout" if timeout else "Connection Failed",
                           ms=_ms(start), error=str(e).splitlines()[0][:200] if str(e) else "")
        except Exception as e:
            return Attempt(status=None, text="Request Exception", ms=_ms(start), error=str(e)[:200])
        return self._from_response(resp, url, start, read_body, "http")

    def _from_response(self, resp, url: str, start: float, read_body: bool, via: str) -> Attempt:
        status = resp.status_code
        ctype = (resp.headers.get("content-type") or "").strip()
        location = ""
        loc = resp.headers.get("location")
        if loc and 300 <= status < 400:
            location = _norm(strip_cache_bust(urljoin(url, loc)))
        a = Attempt(status=status, text=_status_text(status, resp.reason or ""), via=via, location=location,
                    content_type=ctype, headers=_lower_headers(resp.headers))
        seo = None
        if read_body and "html" in ctype.lower() and not location:
            try:
                if resp.raw is not None and not getattr(resp, "_content_consumed", False):
                    _read_body(resp, self._page_limit())
                _fix_encoding(resp)
                html = resp.text or ""
                seo = self._extract(html, resp, url)
            except Exception:
                seo = None
        else:
            try:
                resp.close()
            except Exception:
                pass
        a.ms = _ms(start)
        if seo is None:
            seo = extract_headers_only(resp, url)
            seo["final_url"] = location or url
        seo["url"] = url
        seo["response_time_ms"] = a.ms
        a.seo = seo
        titles = seo.get("titles") or []
        a.title = titles[0] if titles else ""
        return a

    def _bot_wall(self, t: Target, attempts: List[Attempt]) -> bool:
        """An external link that every HTTP check was refused or got no answer at all: many sites (news,
        shops, LinkedIn) answer 403 / 429 / 999 or drop the connection for automated clients only."""
        if t.kind != "external" or t.reported_category not in ("4xx", "5xx", "error") or not attempts:
            return False
        fails = [a for a in attempts if a.category in ERROR_CATEGORIES]
        return bool(fails) and all(not a.blocked and (a.status is None or a.status in BOT_WALL_CODES or a.status >= 600)
                                   for a in fails)

    def _browser_check(self, url: str) -> Optional[Attempt]:
        """Opens an external link once in a real Chrome (the crawl's browser lease) and reads the status of
        the navigation. None when Chrome is unavailable or the per-run budget is used up."""
        lease = getattr(self.crawler, "browser_lease", None)
        if lease is None:
            return None
        with self._ext_browser_lock:     # one Chrome at a time for these checks
            if self._browser_checks >= MAX_BROWSER_CHECKS or self.stop_event.is_set():
                return None
            self._browser_checks += 1
            start = time.time()
            try:
                with lease(timeout=60) as driver:
                    if driver is None:
                        return None
                    try:
                        driver.set_page_load_timeout(25)
                    except Exception:
                        pass
                    try:
                        driver.get(url)
                    except Exception:
                        try:
                            driver.execute_script("window.stop();")
                        except Exception:
                            pass
                    end = time.time() + 8
                    while time.time() < end and not self.stop_event.is_set():
                        try:
                            if driver.execute_script("return document.readyState") == "complete":
                                break
                        except Exception:
                            break
                        time.sleep(0.3)
                    info = driver.execute_script(
                        "const n = performance.getEntriesByType('navigation')[0];"
                        "const b = document.body ? (document.body.innerText || '') : '';"
                        "return {status: n ? (n.responseStatus || 0) : 0, title: document.title || '',"
                        " text: b.slice(0, 600), len: b.length, url: location.href};") or {}
            except Exception as e:
                return Attempt(status=None, text="Browser Error", via="browser", ms=_ms(start), error=str(e)[:200])
        status = int(info.get("status") or 0)
        text = f"{info.get('title', '')} {info.get('text', '')}"
        if not str(info.get("url") or "").startswith(("http://", "https://")):
            # Chrome's own error page (chrome-error://...): the site did not answer the browser either
            return Attempt(status=None, text="No page in the browser", via="browser", ms=_ms(start),
                           error="Chrome showed its own error page")
        if _CHALLENGE_RE.search(text):
            return Attempt(status=status if status >= 400 else 403, text="Bot challenge page", via="browser",
                           ms=_ms(start), title=str(info.get("title") or "")[:200],
                           error="The site showed a robot check to the browser too")
        if not status:
            if int(info.get("len") or 0) < 200:
                return Attempt(status=None, text="No page in the browser", via="browser", ms=_ms(start))
            status = 200     # the browser shows a real page but did not expose the status
        final = str(info.get("url") or "")
        a = Attempt(status=status, text=_status_text(status), via="browser", ms=_ms(start),
                    title=str(info.get("title") or "")[:200])
        if final and _norm(final) != _norm(url) and status < 400:
            a.location = final[:500]
        return a

    def _browser_attempt(self, url: str, read_body: bool) -> Optional[Attempt]:
        """Sites that accept the login only in Chrome: the page is loaded in the crawl's browser."""
        start = time.time()
        try:
            resp = self.crawler._browser_fetch(cache_bust(url))
        except Exception as e:
            return Attempt(status=None, text="Browser Error", via="browser", ms=_ms(start), error=str(e)[:200])
        if resp is None:
            return Attempt(status=None, text="Browser unavailable", via="browser", ms=_ms(start),
                           error="Chrome could not be started")
        try:
            resp._content = resp.raw.read() if resp.raw is not None else b""
            resp._content_consumed = True
        except Exception:
            resp._content = b""
        return self._from_response(resp, url, start, read_body, "browser")

    def _extract(self, html: str, resp, url: str) -> Optional[Dict[str, Any]]:
        crawler = self.crawler
        try:
            lock = getattr(crawler, "_lock", None)
            if lock is not None:
                with lock:
                    allowed = frozenset(crawler.allowed_domains)
            else:
                allowed = frozenset(getattr(crawler, "allowed_domains", set()) or ())
            return extract_page(html, url, resp, crawler.base_domain,
                                bool(getattr(self.config, "include_subdomains", False)), allowed,
                                soup=parse_html(html), url=url)
        except Exception:
            return None

    def _page_limit(self) -> int:
        try:
            kb = int(float(getattr(self.config, "max_page_size_kb", 0) or 0))
        except (TypeError, ValueError):
            kb = 0
        return kb * 1024 if kb > 0 else PAGE_MAX_BYTES

    def _renew_session(self) -> str:
        """The server sent a member page to the login page. "valid" when the session works (the page is
        simply off limits for this account: the answer is real), "renewed" after logging in again (the
        caller asks once more), "failed" when the session ended and could not be renewed (not checked).
        Logs in again at most once per run; after that, login redirects count as real answers."""
        handler = getattr(self.crawler, "auth_handler", None)
        with self._relogin_lock:
            if self._relogin_failed:
                return "failed"
            if self._relogged:
                return "valid"
            try:
                valid = handler.session_valid(self.session) if handler is not None else None
            except Exception:
                valid = None
            if valid is True:
                return "valid"
            self._relogged = True
            recover = getattr(self.crawler, "_recover_session", None)
            ok = False
            if recover is not None:
                self._log("The login session may have ended; logging in again to finish the re-check...")
                try:
                    ok = bool(recover("a re-check", check_http=False))
                except Exception:
                    ok = False
            if not ok:
                self._relogin_failed = True
                return "failed"
            return "renewed"

    def _follow(self, attempts: List[Attempt]) -> Optional[Tuple[str, Optional[int]]]:
        """Where a URL that now redirects ends up: (final URL, its status), followed once. A hop to a sign-out
        or data-changing link is not requested: (that URL, None)."""
        url = attempts[-1].location
        if not url:
            return None
        status: Optional[int] = None
        for _ in range(MAX_REDIRECT_HOPS):
            if self._unsafe_reason(url):
                return url, None
            a = self._http(url, internal=self._is_internal(url), read_body=False)
            if a is None:
                return None
            status = a.status
            if a.location and a.status and 300 <= a.status < 400:
                url = a.location
                continue
            break
        return url, status

    # ---------------------------------------------------------------- SEO issue samples
    def _issue_check(self, t: Target, tab, flt, attempts: List[Attempt]) -> dict:
        name = f"{tab.name}: {flt.name}"
        row = {"tab": tab.id, "filter": flt.id, "name": name}
        site = None
        try:
            site = self.crawler.site_index()[0]
        except Exception:
            site = None
        values: List[Optional[bool]] = []
        page_local = site is not None and is_dataclass(t.result) and t.result is not None
        original_ok = t.reported_category == "2xx"
        for a in attempts:
            if not page_local:
                break
            if original_ok and a.category != "2xx":
                values.append(None)
                continue
            copy = _fresh_copy(t.result, a)
            spy = _SiteSpy(site)
            try:
                hit = bool(flt.test(copy, spy))
            except Exception:
                page_local = False
                break
            if not spy.used <= _PAGE_SITE_ATTRS:
                page_local = False
                break
            values.append(hit)
        if not page_local:
            # The test needs other pages of the crawl (inlinks, duplicates, sitemaps, the pages using an image):
            # opening this one URL again cannot show whether the issue is still there. Not checked.
            changed = _content_changed(t.result, attempts[-1])
            extra = "" if changed is None else (" The page's content changed since the crawl." if changed
                                                 else " The page's content is unchanged since the crawl.")
            row.update(verdict="not_checked", evaluated=False, cause="needs_other_pages",
                       note=f"Not checked: this test needs other pages of the crawl, so it cannot be re-run by "
                            f"opening this URL alone (it answered {_codes(attempts)}).{extra}")
            return row
        known = [v for v in values if v is not None]
        if not known:
            row.update(verdict="not_checked", evaluated=False, cause="page_not_loading",
                       note=f"Not checked: the page now answers {_codes(attempts)} (the crawl saw "
                            f"{_short_reported(t)}), so this issue could not be re-tested on it.")
            return row
        failed = len(values) - len(known)
        when = f" whenever the page loaded (it answered {_codes(attempts)})" if failed else ""
        if all(known):
            row.update(verdict="confirmed", evaluated=True,
                       note=(f"Still there{when}." if failed else
                             "Still there on every check." if len(values) > 1 else "Still there."))
        elif not any(known):
            row.update(verdict="not_reproduced", evaluated=True,
                       note=f"Gone: not seen on any check{when}; the page has changed since the crawl.")
        else:
            row.update(verdict="intermittent", evaluated=True,
                       note=f"Seen on {sum(1 for v in known if v)} of {len(known)} checks"
                            + (f" (the page did not load on {_plural(failed, 'other check')})" if failed else "")
                            + ".")
        return row

    # ---------------------------------------------------------------- link still on the parent page?
    def _link_on_parent(self, t: Target) -> Tuple[Optional[bool], str]:
        follow = t.source != "Redirect"
        page = self._parent_page(t.parent, follow=follow)
        if not page.ok:
            return None, page.note
        if follow and page.location:
            return None, f"The linking page keeps redirecting (to {page.location}), so the link could not be checked."
        target = _norm(t.url)
        moved = follow and page.final_url and _norm(page.final_url) != _norm(t.parent)
        if moved and t.source not in ("XML Sitemap",):
            found, note = self._link_on_page(t, page, target)
            return found, f"{note} (The linking page now redirects to {page.final_url}; checked there.)".strip()
        if t.source == "Redirect":
            present = page.location == target
            return present, ("The page that redirected here still does." if present else
                             "The redirect that led here has changed.")
        if t.source == "Meta Refresh":
            present = page.refresh == target
            return present, ("" if present else "The meta refresh that led here was removed.")
        if t.source == "XML Sitemap":
            present = target in page.locs or t.url in page.locs
            return present, ("Still listed in the XML sitemap." if present else
                             "No longer listed in the XML sitemap.")
        return self._link_on_page(t, page, target)

    def _link_on_page(self, t: Target, page: ParentPage, target: str) -> Tuple[Optional[bool], str]:
        """The link (or code route) to the target is still on this HTML page."""
        if t.link_tag == "bundle":
            present = self._in_bundles(page, t)
            if present is None:
                return None, "The JavaScript files of the linking page could not be read."
            return present, ("The route is still in the site's JavaScript code." if present else
                             "The route is no longer in the linking page's JavaScript code.")
        if t.link_tag == "script":
            path = urlsplit(t.url).path or "/"
            present = _quoted(path, page.html) or bool(t.raw_href and _quoted(t.raw_href, page.html))
            return present, ("" if present else "The link was removed from the page's code.")
        present = target in page.links or t.url in page.links or _raw_in_html(t.raw_href, page.html)
        if present:
            return True, ""
        wants_render = getattr(self.config, "use_dynamic_js", True) and (
            page.spa or t.source in ("Authenticated Dashboard", "Authenticated Landing Page"))
        if wants_render:
            rendered = self._rendered_links(page)
            if rendered is not None:
                present = target in rendered or t.url in rendered or _raw_in_html(t.raw_href, page.rendered_html)
                return present, ("Found in the page as the browser shows it." if present else
                                 "The link was removed from the page that had it (checked in the browser too).")
        return False, "The link was removed from the page that had it, so visitors no longer reach this URL from there."

    def _parent_page(self, url: str, follow: bool = True) -> ParentPage:
        key = (url, follow)
        with self._lock:
            page = self._parents.get(key)
            if page is not None:
                return page
            plock = self._parent_locks.setdefault(key, threading.Lock())
        with plock:
            with self._lock:
                page = self._parents.get(key)
            if page is None:
                page = self._fetch_parent(url, follow)
                with self._lock:
                    self._parents[key] = page
            return page

    def _fetch_parent(self, url: str, follow: bool) -> ParentPage:
        page = ParentPage(url=url)
        if not self._is_internal(url):
            page.note = "The linking page is on another site and was not opened."
            return page
        if self._unsafe_reason(url):
            page.note = "The linking page is a sign-out or data-changing link, so it was not opened while logged in."
            return page
        if self.browser_only:
            resp = None
            try:
                resp = self.crawler._browser_fetch(cache_bust(url))
            except Exception:
                resp = None
            if resp is None:
                page.note = "The linking page could not be opened in the browser."
                return page
            try:
                body = resp.raw.read() if resp.raw is not None else b""
            except Exception:
                body = b""
            return self._parse_parent(page, url, resp.status_code, url, resp.headers, body.decode("utf-8", "replace"))
        cur = url
        resp = None
        try:
            for _ in range(MAX_REDIRECT_HOPS if follow else 1):
                if self.limiter is not None and not self.limiter.acquire(self.stop_event):
                    page.note = "Stopped."
                    return page
                resp = self.session.get(cache_bust(cur), headers=dict(NO_CACHE_HEADERS), timeout=self.timeout,
                                        verify=self.verify, allow_redirects=False, stream=True)
                loc = resp.headers.get("location")
                if follow and loc and 300 <= resp.status_code < 400:
                    nxt = _norm(strip_cache_bust(urljoin(cur, loc)))
                    if self._unsafe_reason(nxt):
                        resp.close()
                        page.note = (f"The linking page now redirects to a sign-out or data-changing link ({nxt[:120]}), "
                                     "which is not opened while logged in.")
                        return page
                    resp.close()
                    cur = nxt
                    continue
                break
        except Exception as e:
            page.note = f"The linking page did not answer ({str(e).splitlines()[0][:100] if str(e) else 'no answer'})."
            return page
        if resp is None:
            return page
        html = ""
        if "html" in (resp.headers.get("content-type") or "").lower() or "xml" in (resp.headers.get("content-type") or "").lower():
            try:
                _read_body(resp, self._page_limit())
                _fix_encoding(resp)
                html = resp.text or ""
            except Exception:
                html = ""
        else:
            resp.close()
        return self._parse_parent(page, url, resp.status_code, cur, resp.headers, html, resp)

    def _parse_parent(self, page: ParentPage, url: str, status: int, final: str, headers, html: str,
                      resp=None) -> ParentPage:
        page.status, page.final_url, page.html = status, final, html
        loc = headers.get("location") if headers is not None else None
        if loc and 300 <= status < 400:
            page.location = _norm(strip_cache_bust(urljoin(final, loc)))
            page.ok = True
            if self.auth_used and redirected_to_login(url, page.location):
                page.ok = False
                page.note = "The linking page now sends us to the login page, so the link could not be checked."
            return page
        if final != url and self.auth_used and redirected_to_login(url, final):
            page.note = "The linking page now sends us to the login page, so the link could not be checked."
            return page
        if status >= 400:
            page.note = f"The linking page itself now answers {status}."
            return page
        page.ok = True
        page.spa = _looks_spa(html)
        page.locs = {_norm(unescape(m)) for m in _LOC.findall(html)} | {unescape(m) for m in _LOC.findall(html)}
        if "<" in html and "html" in html[:2000].lower():
            seo = None
            try:
                seo = self._extract(html, resp, final) if resp is not None else extract_page(
                    html, final, None, self.crawler.base_domain, False, None, url=final)
            except Exception:
                seo = None
            links: Set[str] = set()
            try:
                # The crawl's own link discovery (anchors, <link rel>, data-href, forms, image maps)
                internal, external = PageParser.extract_links(
                    html_content=html, current_url=final, base_domain=self.crawler.base_domain,
                    include_subdomains=bool(getattr(self.config, "include_subdomains", False)),
                    allowed_domains=set(getattr(self.crawler, "allowed_domains", set()) or ()),
                    exclude_patterns=None, collect_external=True, known_urls=set())
                links.update(internal)
                links.update(external)
            except Exception:
                pass
            if seo:
                for item in seo.get("links") or []:
                    links.add(item.get("url") or "")
                for item in seo.get("resources") or []:
                    links.add(_norm(item.get("url") or ""))
                for item in seo.get("images") or []:
                    links.add(_norm(item.get("src") or ""))
                page.refresh = _norm(seo.get("meta_refresh_url") or "") if seo.get("meta_refresh_url") else ""
            page.links = {u for u in links if u}
        return page

    def _rendered_links(self, page: ParentPage) -> Optional[Set[str]]:
        """Every link and source URL of the parent page as Chrome shows it (logged in when the crawl was)."""
        if page.rendered is not None:
            return page.rendered
        lease = getattr(self.crawler, "browser_lease", None)
        if lease is None:
            return None
        with self._browser_lock:
            if page.rendered is not None:
                return page.rendered
            if self._renders >= MAX_RENDERS or self.stop_event.is_set():
                return None
            self._renders += 1
            try:
                with lease(timeout=60) as driver:
                    if driver is None:
                        return None
                    try:
                        driver.get(page.final_url or page.url)
                    except Exception:
                        try:
                            driver.execute_script("window.stop();")
                        except Exception:
                            pass
                    end = time.time() + 10
                    while time.time() < end:
                        try:
                            if driver.execute_script("return document.readyState") == "complete":
                                break
                        except Exception:
                            break
                        time.sleep(0.25)
                    time.sleep(1.5)
                    raw = driver.execute_script(_ATTRS_JS) or []
                    page.rendered_html = driver.page_source or ""
            except Exception:
                return None
            page.rendered = {_norm(u) for u in raw if isinstance(u, str) and u.startswith(("http://", "https://"))}
            return page.rendered

    def _in_bundles(self, page: ParentPage, t: Target) -> Optional[bool]:
        """The code-found route is still in a first-party JavaScript file the linking page loads."""
        html = page.html or ""
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(page.final_url or page.url))
        srcs: List[str] = []
        try:
            soup = parse_html(html)
            for s in soup.find_all("script", src=True):
                srcs.append(str(s.get("src") or ""))
        except Exception:
            pass
        for tag in _MODULE_LINK.findall(html):
            m = _HREF_ATTR.search(tag)
            if m:
                srcs.append(m.group(1))
        target = _norm(t.url)
        path = urlsplit(t.url).path or "/"
        files: List[str] = []
        for src in srcs:
            full = urljoin(page.final_url or page.url, src.strip())
            if not full.startswith(("http://", "https://")) or get_domain(full) != get_domain(origin):
                continue
            if full not in files:
                files.append(full)
        if not files:
            return None if not html else False
        # The file the crawl read the route from goes first while the page still loads it
        source = urlsplit(t.source_file).path if t.source_file else ""
        files.sort(key=lambda f: not (source and urlsplit(f).path == source))
        read_any = False
        for js in files[:MAX_BUNDLES]:
            chunk = NEXT_APP_CHUNK.search(urlsplit(js).path)
            if chunk:
                segs = [s for s in chunk.group(1).split("/") if s and not s.startswith(("(", "@"))]
                if _norm(urljoin(origin, "/" + "/".join(segs))) == target:
                    return True
            routes = self._bundle_route_set(js, origin)
            if routes is None:
                continue
            read_any = True
            if target in routes or _quoted(path, self._bundles.get(js) or ""):
                return True
        return False if read_any else None

    def _bundle_route_set(self, js_url: str, origin: str) -> Optional[Set[str]]:
        with self._lock:
            if js_url in self._bundle_routes:
                return self._bundle_routes[js_url]
            if js_url in self._bundles and self._bundles[js_url] is None:
                return None
        text: Optional[str] = None
        try:
            resp = self.session.get(js_url, timeout=self.timeout, verify=self.verify, stream=True,
                                    headers=dict(NO_CACHE_HEADERS))
            if resp.status_code == 200:
                _read_body(resp, BUNDLE_MAX_BYTES)
                text = resp.content.decode("utf-8", "replace")
            else:
                resp.close()
        except Exception:
            text = None
        routes: Optional[Set[str]] = None
        if text is not None:
            routes = set()
            for raw in bundle_route_strings(text):
                clean = raw.rstrip("*").rstrip("/") or "/"
                routes.add(_norm(urljoin(origin, clean)))
        with self._lock:
            self._bundles[js_url] = text
            if routes is not None:
                self._bundle_routes[js_url] = routes
        return routes


# ---------------------------------------------------------------------------------------------
# Writing re-checks back into the crawl
# ---------------------------------------------------------------------------------------------
def apply_rechecks(crawler, results: Dict[str, dict]) -> int:
    """Updates the crawl's results for re-checked URLs (status_code, http_status, redirect target, page data
    and so the category) under the crawler's lock, keeps the running counts in step, and rebuilds the SEO
    index. An intermittent URL keeps its latest failing answer (it still fails for some visitors).
    Returns how many URLs changed. Safe to call twice with the same results."""
    with _FRESH_LOCK:
        fresh_all = _FRESH.pop(id(crawler), {})
    changed = 0
    lock = getattr(crawler, "_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        by_url = getattr(crawler, "results", {}) or {}
        externals = getattr(crawler, "external_links", {}) or {}
        for url, entry in (results or {}).items():
            if not isinstance(entry, dict) or entry.get("verdict") not in VERDICTS:
                continue
            now = entry.get("now") or {}
            if "status" not in now:
                continue
            r = by_url.get(url)
            if r is not None:
                changed += _apply_internal(crawler, r, entry, now, fresh_all.get(url))
                continue
            ext = externals.get(url)
            if ext is not None:
                changed += _apply_external(ext, entry, now)
    finally:
        if lock is not None:
            lock.release()
    if changed:
        _refresh_index(crawler)
    return changed


def _apply_internal(crawler, r, entry: dict, now: dict, fresh: Optional[dict]) -> int:
    status = now.get("status")
    location = now.get("location") or None
    blocked = bool(now.get("blocked"))
    if blocked and status is None and r.blocked:
        return 0
    before = (r.status_code, r.http_status, r.redirect_destination, r.blocked)
    old_seo = r.seo
    tally = getattr(crawler, "_tally", None)
    if tally is not None:
        tally(r, -1)
    try:
        r.status_code = status
        r.http_status = now.get("http_status") or _status_text(status)
        r.redirect_destination = location
        r.blocked = blocked
        if now.get("ms"):
            r.response_time_ms = int(now["ms"])
        if status is None and not blocked:
            r.error_message = now.get("error") or r.http_status
        elif entry.get("verdict") == "intermittent":
            r.error_message = entry.get("note") or None
        else:
            r.error_message = None
        if fresh:
            if fresh.get("content_type") is not None and status is not None:
                r.content_type = fresh.get("content_type") or ""
            if fresh.get("headers") is not None and status is not None:
                r.response_headers = dict(fresh.get("headers") or {})
            seo = fresh.get("seo")
            if seo and (seo.get("is_html") or not (r.seo or {}).get("is_html") or status is None or status >= 300):
                r.seo = seo
            if fresh.get("title"):
                r.page_title = fresh["title"]
        if blocked:
            r.page_title = "[Blocked by robots.txt]"
        elif status is None:
            r.page_title = f"[Failed] {r.http_status}"
        elif status >= 400 and not (fresh and fresh.get("title")):
            r.page_title = f"[Status {status}]"
        elif status < 400 and (r.page_title or "").startswith(("[Failed]", "[Status", "[Blocked")):
            r.page_title = (fresh or {}).get("title") or (urlsplit(r.url).path or "/")
        if r.link_region == "Found in code, not clickable" and not (status and 400 <= status < 500):
            r.link_region = "JavaScript bundle"
        label = getattr(crawler, "_label_code_only", None)
        if label is not None:
            label(r)
    finally:
        if tally is not None:
            tally(r, +1)
    return int(before != (r.status_code, r.http_status, r.redirect_destination, r.blocked) or r.seo is not old_seo)


def _apply_external(ext, entry: dict, now: dict) -> int:
    if now.get("blocked"):
        return 0
    before = (ext.status_code, ext.http_status, ext.redirect_destination)
    ext.status_code = now.get("status")
    ext.http_status = now.get("http_status") or _status_text(ext.status_code)
    ext.redirect_destination = now.get("location") or None
    if now.get("ms"):
        ext.response_time_ms = int(now["ms"])
    ext.validation_status = ext.http_status
    if entry.get("verdict") == "intermittent":
        ext.validation_status = f"{ext.http_status} (intermittent)"
    return int(before != (ext.status_code, ext.http_status, ext.redirect_destination))


def _refresh_index(crawler):
    """The SEO index is cached by result count; the count did not change, so the cache key is dropped
    first and the index rebuilt at once."""
    try:
        idx_lock = getattr(crawler, "_index_lock", None)
        if idx_lock is not None:
            with idx_lock:
                crawler._index_key = None
        elif hasattr(crawler, "_index_key"):
            crawler._index_key = None
        crawler.site_index(force=True)
    except Exception as e:
        try:
            crawler.log(f"[Validation] The SEO index could not be rebuilt after the re-check: {str(e)[:160]}")
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def _takes_two_args(fn) -> bool:
    if fn is None:
        return False
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2


def _external_broken(e) -> bool:
    """A checked external link that failed (robots.txt-blocked ones were never requested: not broken)."""
    if not getattr(e, "http_status", ""):
        return False
    if (e.http_status or "").startswith("Blocked by robots.txt"):
        return False
    return e.status_code is None or e.status_code >= 400


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)


def _short(a: Attempt) -> str:
    if a.blocked:
        return "blocked by robots.txt"
    if a.status is None:
        return (a.text or "no answer").lower()
    return str(a.status)


def _short_reported(t: Target) -> str:
    if t.reported_category == "blocked":
        return "blocked by robots.txt"
    if t.reported_status is None:
        return (t.reported_text or "no answer").lower()
    return str(t.reported_status)


def _codes(attempts: List[Attempt]) -> str:
    """"404 on all 3 checks" or "200, 500, 200"."""
    shorts = [_short(a) for a in attempts]
    if len(set(shorts)) == 1:
        return shorts[0] if len(shorts) == 1 else f"{shorts[0]} on all {len(shorts)} checks"
    return ", ".join(shorts)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _quoted(path: str, text: str) -> bool:
    """`path` appears in code as a string literal ("/x", '/x' or `/x`)."""
    if not path or not text:
        return False
    return any(f"{q}{path}{q}" in text for q in ('"', "'", "`"))


def _raw_in_html(raw: str, html: str) -> bool:
    """The href as written (e.g. "../old-page") still appears as an attribute value."""
    if not raw or len(raw) < 2 or not html:
        return False
    return re.search(r"""(?:href|src)\s*=\s*["']?""" + re.escape(raw) + r"""["'\s>]""", html, re.I) is not None


def _fresh_copy(result, a: Attempt):
    """The crawl's row with this attempt's answer, for re-running a filter test."""
    names = {f.name for f in dc_fields(result)}
    values = {"status_code": a.status, "http_status": a.text, "redirect_destination": a.location or None,
              "content_type": a.content_type, "response_headers": dict(a.headers), "seo": a.seo,
              "blocked": a.blocked, "response_time_ms": a.ms}
    if a.title:
        values["page_title"] = a.title
    return replace(result, **{k: v for k, v in values.items() if k in names})


def _content_changed(result, a: Attempt) -> Optional[bool]:
    old = getattr(result, "seo", None) or {}
    new = a.seo or {}
    if not old.get("content_hash") or not new.get("content_hash"):
        return None
    return old.get("content_hash") != new.get("content_hash") or (old.get("titles") or []) != (new.get("titles") or [])


def _combine_issue_verdicts(rows: List[dict]) -> Tuple[Optional[str], str]:
    """A URL checked only as an SEO issue sample: only issues whose own test was re-run decide (confirmed
    when any is still there). (None, note) when none could be re-tested: the URL is not checked, never
    "confirmed" from its status alone."""
    tested = [r for r in rows if r.get("evaluated")]
    untested = len(rows) - len(tested)
    verdicts = [r["verdict"] for r in tested]
    if not tested:
        verdict: Optional[str] = None
    elif "confirmed" in verdicts:
        verdict = "confirmed"
    elif "intermittent" in verdicts:
        verdict = "intermittent"
    else:
        verdict = "not_reproduced"
    still = verdicts.count("confirmed")
    gone = verdicts.count("not_reproduced")
    flaky = verdicts.count("intermittent")
    parts = [f"{still} still there"] if still else []
    if gone:
        parts.append(f"{gone} gone")
    if flaky:
        parts.append(f"{flaky} only sometimes")
    if untested:
        parts.append(f"{untested} not checked (the test needs other pages, or the page did not load)")
    return verdict, (f"{_plural(len(rows), 'SEO issue')} on this page, re-tested live: {', '.join(parts)}."
                     if parts else "")


def _untested_reason(rows: List[dict], attempts: List[Attempt]) -> str:
    """Why an SEO issue sample got no verdict: the issues' names and what kept their tests from running."""
    names = list(dict.fromkeys(r.get("name") or "" for r in rows if r.get("name")))
    shown = ", ".join(f"“{n}”" for n in names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    shown = shown or "its SEO issue"
    if rows and all(r.get("cause") == "page_not_loading" for r in rows):
        return (f"Not checked: the page now answers {_codes(attempts)}, so {shown} could not be re-tested on it. "
                "Re-check it as an error to see whether it is broken.")
    return (f"Not checked: {shown} cannot be re-tested by opening this URL alone (the test needs other pages of "
            f"the crawl, such as the pages linking to it or sharing its text). The URL answered {_codes(attempts)}; "
            "the issue itself was not re-tested.")


def _meaning(verdict: str, checked_as: str, bot_wall: bool = False) -> str:
    """What the verdict means for this URL, in plain words (an SEO issue sample usually loads fine: its
    "confirmed" is about the issue, not about the page being broken)."""
    if checked_as == "seo_issue":
        return {"confirmed": "The SEO issue is still there on a live re-check (the page itself loads).",
                "not_reproduced": "The SEO issue is gone on a live re-check.",
                "intermittent": "The SEO issue shows on some re-checks only.",
                "fixed": "The SEO issue is gone on a live re-check."}.get(verdict, "")
    if verdict == "not_reproduced" and bot_wall:
        return "Opens normally in a real browser; only automated checkers are refused. Not broken for visitors."
    if checked_as == "status":
        return {"confirmed": "Answers the same as in the crawl.",
                "not_reproduced": "Answers differently now than in the crawl (see the note).",
                "intermittent": "Sometimes fails, sometimes works: check the server.",
                "fixed": "Works now."}.get(verdict, "")
    return {"confirmed": "Still broken on a live re-check.",
            "not_reproduced": "Works now: fixed after the crawl, or the crawl hit a temporary problem.",
            "intermittent": "Sometimes fails, sometimes works: check the server.",
            "fixed": "Works now."}.get(verdict, "")


def _bot_wall_verdict(verdict: str, note: str, http_attempts: List[Attempt], b: Attempt,
                      url: str = "") -> Tuple[str, str]:
    """Verdict of an external link refused to the HTTP checker once a real browser has opened it too."""
    codes = _codes(http_attempts)
    if b.status is not None and b.status < 400:
        final = b.location
        if final and _last_segment(final) != _last_segment(url or final):
            # the linked page itself is gone: the other site sends visitors to another page
            return "not_reproduced", (f"Only automated checkers are refused ({codes}); in a real browser the link now "
                                      f"lands on another page ({final[:120]}). Not a dead end, but check it still "
                                      "fits and link straight to the right page.")
        moved = f", now at {final[:120]}" if final else ""
        return "not_reproduced", (f"Opens normally in a real browser ({b.status}{moved}); only automated checkers "
                                  f"are refused ({codes}). Not broken for visitors"
                                  + ("; point the link at the new address." if final else ": no fix needed."))
    if b.text == "Bot challenge page":
        return verdict, (f"{note} A real browser got a robot check (\u201c{_short_title(b.title)}\u201d) too: the "
                         "site blocks automated visits. Open it yourself to be sure.").strip()
    if b.status is not None:
        return verdict, f"{note} A real browser got {b.status} too.".strip()
    return verdict, f"{note} It did not load in a real browser either.".strip()


def _last_segment(url: str) -> str:
    """The page's own name in its address (last path segment, without extension), for "same page moved"."""
    segs = [x for x in (urlsplit(url).path or "").lower().split("/") if x]
    return re.sub(r"\.[a-z0-9]{2,5}$", "", segs[-1]) if segs else ""


def _short_title(text: str, n: int = 50) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def _applied_attempt(verdict: str, attempts: List[Attempt]) -> Attempt:
    """The answer written into the crawl: the latest failing one for an unstable URL, else the latest."""
    if verdict == "intermittent":
        for a in reversed(attempts):
            if a.category in ERROR_CATEGORIES:
                return a
    return attempts[-1]
