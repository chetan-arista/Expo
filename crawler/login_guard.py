"""
Login guard: checks that pages behind the login stay behind it.

After a logged-in crawl, every member page (2xx HTML) is requested again the way a logged-out visitor
would: a fresh session with no login cookies and no custom auth header. No cache-buster is sent on
purpose: a shared cache that hands a member page to anyone is exactly what this audit must catch.
  protected  the logged-out request redirects to the login page, answers 401/403/404, sends the visitor
             elsewhere, or shows a login form instead of the page
  exposed    it answers 200 with substantially the same page (same title and simhash similarity of the
             text >= SAME_SIMILARITY) AND the page belongs to the members area: it sits under a protected
             path, is linked only from protected pages, or is where the login lands. Public pages that
             look the same for everyone are not flagged.
Pages whose HTML is an empty JavaScript shell are compared in Chrome (logged in vs. logged out); without
Chrome they are reported as not verified, never as safe.
Member pages are also flagged when listed in the XML sitemap, without "noindex", or sent without
"Cache-Control: private" / "no-store" (a shared cache could show one member's page to another).
A logged-out request answered by a bot check (a firewall / CDN challenge) is reported as not verified, never
as protected: a real visitor may pass the check and see the page.

fetch_view() (one page fetched with redirects followed by hand, never into a sign-out address) is shared
with auth.test_cookies().
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

from .browser import block_dangerous, chrome_available, close_chrome, new_chrome
from .engine import _fix_encoding, _read_body
from .net_guard import WAF_SCAN_CHARS, WAF_STATUSES, challenge_header, detect_waf
from .seo.extract import extract_page, parse_html, simhash, simhash_similarity
from .url_utils import changes_data_url, is_login_url, is_logout_url, normalize_url

SAME_SIMILARITY = 90.0       # percent of equal simhash bits for "substantially the same content"
SHELL_WORDS = 25             # fewer visible words than this in the logged-in HTML: a JavaScript shell
SHELL_SHARED = 3             # the same logged-in text on this many pages: a JavaScript shell
MAX_BROWSER_CHECKS = 12      # shell pages compared in Chrome per audit
WORKERS = 6
MAX_HOPS = 5
PAGE_MAX_BYTES = 5 * 1024 * 1024
# Pages that are public by design even inside a members area (sign-in, sign-up, password help, legal)
_PUBLIC_PAGE = re.compile(
    r"(?:^|/)(?:log-?in|sign-?in|sign[-_]?up|register|registration|join|forgot[^/]*|reset[^/]*|password[^/]*|"
    r"recover[^/]*|activate[^/]*|verify[^/]*|invite[^/]*|terms[^/]*|privacy[^/]*|cookie[^/]*|legal)(?:/|$|\.)",
    re.I)
_MEMBER_PATH = re.compile(r"(?:^|/)(?:my[-_]?account|account|dashboard|members?|profile|admin|portal|my[-_][a-z]+|"
                          r"app|console|settings|orders?)(?:/|$)", re.I)
_PASSWORD_INPUT = re.compile(r"""<input[^>]+type\s*=\s*["']?password""", re.I)
_SPA_MARKERS = ('id="__next"', 'id="root"', "_next/static", 'id="app"', "window.__INITIAL_STATE__", "ng-version")
_RENDER_JS = ("const pw = Array.from(document.querySelectorAll('input[type=password]'))"
              ".some(e => e.offsetParent !== null);"
              "return [document.title || '', (document.body && document.body.innerText) || '', pw];")


def was_logged_in(crawler) -> bool:
    """The crawl ran logged in (the audit has nothing to compare otherwise)."""
    return bool(getattr(crawler, "auth_used", False)) and getattr(crawler, "status", "") != "auth_failed"


def audit(crawler, max_urls: int = 200, progress: Optional[Callable[[int, int], Any]] = None,
          stop_event: Optional[threading.Event] = None) -> List[dict]:
    """[{"url", "logged_out_status", "exposed", "note", ...}] for the crawl's member pages; [] when the
    crawl was not logged in. Exposed pages come first."""
    if not was_logged_in(crawler):
        return []
    return LoginGuard(crawler, stop_event=stop_event, max_urls=max_urls).run(progress=progress)["rows"]


@dataclass
class _View:
    """A page as a logged-out visitor gets it."""
    url: str
    first_status: Optional[int] = None
    final_url: str = ""
    final_status: Optional[int] = None
    hops: Tuple[str, ...] = ()
    error: str = ""
    html: str = ""
    seo: Optional[Dict[str, Any]] = None
    method: str = "http"
    headers: Dict[str, str] = field(default_factory=dict)   # final answer's headers, lower-case names
    waf: str = ""              # bot check / firewall that answered instead of the page (net_guard.detect_waf)
    response: Any = field(default=None, repr=False)          # the final 2xx HTML answer (body read, closed)


def fetch_view(session: requests.Session, url: str, timeout=(10, 15), verify: bool = False,
               limiter=None, stop_event: Optional[threading.Event] = None,
               unsafe: Callable[[str], bool] = is_logout_url) -> _View:
    """`url` as `session` gets it: redirects followed by hand (at most MAX_HOPS) and never into an address
    `unsafe` names (a sign-out by default), the body of a 2xx HTML answer read (PAGE_MAX_BYTES at most), and
    `waf` set when a bot check / firewall answered instead of the page. Errors land in `error`, never raised."""
    v = _View(url=url)
    stop_event = stop_event or threading.Event()
    if stop_event.is_set():
        v.error = "stopped"
        return v
    cur = url
    hops: List[str] = []
    try:
        for hop in range(MAX_HOPS + 1):
            if limiter is not None and not limiter.acquire(stop_event):
                v.error = "stopped"
                return v
            resp = session.get(cur, timeout=timeout, verify=verify, allow_redirects=False, stream=True)
            if hop == 0:
                v.first_status = resp.status_code
            loc = resp.headers.get("location")
            if loc and 300 <= resp.status_code < 400 and hop < MAX_HOPS:
                resp.close()
                nxt = _norm(urljoin(cur, loc))
                hops.append(nxt)
                if unsafe(nxt):
                    break   # never follow a sign-out (or data-changing) address
                cur = nxt
                continue
            v.final_url, v.final_status = cur, resp.status_code
            v.headers = {str(k).lower(): str(val) for k, val in resp.headers.items()}
            ctype = v.headers.get("content-type", "").lower()
            if 200 <= resp.status_code < 300 and "html" in ctype:
                _read_body(resp, PAGE_MAX_BYTES)
                _fix_encoding(resp)
                v.html = resp.text or ""
                v.response = resp
                v.waf = detect_waf(resp.status_code, v.headers, v.html[:WAF_SCAN_CHARS])
            elif resp.status_code in WAF_STATUSES or challenge_header(v.headers):
                _read_body(resp, WAF_SCAN_CHARS * 2)
                body = (resp.content or b"")[:WAF_SCAN_CHARS * 2].decode("utf-8", "ignore")
                v.waf = detect_waf(resp.status_code, v.headers, body[:WAF_SCAN_CHARS])
                resp.close()
            else:
                v.waf = challenge_header(v.headers)
                resp.close()
            break
    except requests.exceptions.Timeout:
        v.error = "timed out"
    except requests.exceptions.ConnectionError:
        v.error = "connection failed"
    except Exception as e:
        v.error = (str(e).splitlines()[0][:120] if str(e) else "request failed")
    v.hops = tuple(hops)
    if not v.final_url and hops:
        v.final_url = hops[-1]
    return v


class LoginGuard:
    """One audit run. candidates() lists the pages it will check (for progress totals), run() checks them."""

    def __init__(self, crawler, stop_event: Optional[threading.Event] = None, max_urls: int = 200):
        self.crawler = crawler
        self.config = crawler.config
        self.stop_event = stop_event or threading.Event()
        self.max_urls = max(1, int(max_urls or 200))
        timeout = int(getattr(self.config, "timeout", 15) or 15)
        self.timeout = (min(timeout, 10), timeout)
        self.verify = bool(getattr(self.config, "verify_ssl", False))
        self.user_agent = getattr(self.config, "user_agent", "") or "Mozilla/5.0"
        self._candidates: Optional[List[Any]] = None
        self._inlinks: Dict[str, List[dict]] = {}
        self._sitemap_norm: Optional[Set[str]] = None
        handler = getattr(crawler, "auth_handler", None)
        self.landing = _norm(getattr(handler, "landing_url", "") or "") if handler is not None else ""
        self.probe = _norm(getattr(handler, "probe_url", "") or "") if handler is not None else ""
        self.login_url = _norm(getattr(handler, "login_url", "") or "") if handler is not None else ""

    # ---------------------------------------------------------------- what to check
    def candidates(self) -> List[Any]:
        """Internal HTML pages that answered 2xx logged in, member-area hints first, then by depth."""
        if self._candidates is not None:
            return self._candidates
        lock = getattr(self.crawler, "_lock", None)
        if lock is not None:
            with lock:
                results = list(self.crawler.results_list)
        else:
            results = list(getattr(self.crawler, "results_list", []) or [])
        # Never a sign-out or data-changing link (shell pages are opened in the logged-in browser); only the
        # start URL, which the person chose, may be a data-changing one (the crawl's own rule)
        start = _norm(getattr(self.crawler, "start_url", "") or "")
        pages = [(i, r) for i, r in enumerate(results)
                 if getattr(r, "resource_type", "page") == "page" and r.category == "2xx"
                 and _is_html(r) and not is_logout_url(r.url)
                 and not (changes_data_url(r.url) and _norm(r.url) != start)]

        def rank(item):
            i, r = item
            url = _norm(r.url)
            hint = 0 if url in (self.landing, self.probe) else (
                1 if r.discovery_source in ("Authenticated Landing Page", "Authenticated Dashboard") else 2)
            return hint, r.depth or 0, i

        self._candidates = [r for _, r in sorted(pages, key=rank)[:self.max_urls]]
        return self._candidates

    # ---------------------------------------------------------------- run
    def run(self, progress: Optional[Callable[[int, int], Any]] = None) -> dict:
        """{"rows": [...], "pages", "checked", "protected", "exposed", "public", "not_verified", "in_sitemap"}."""
        pages = self.candidates()
        total = len(pages)
        views: Dict[str, _View] = {}
        done = 0
        if pages:
            with ThreadPoolExecutor(max_workers=min(WORKERS, total), thread_name_prefix="login-guard") as pool:
                futures = {pool.submit(self._view, r.url): r for r in pages}
                for fut in as_completed(futures):
                    r = futures[fut]
                    try:
                        views[r.url] = fut.result()
                    except Exception as e:
                        views[r.url] = _View(url=r.url, error=str(e)[:120] or "error")
                    done += 1
                    if progress is not None:
                        try:
                            progress(done, total)
                        except Exception:
                            pass
                    if self.stop_event.is_set():
                        for f in futures:
                            f.cancel()
                        break

        shared = _shared_hashes(pages)
        states: Dict[str, Tuple[str, str]] = {}      # url -> (state, note); state: protected|same|different|shell|unknown
        strong: Set[str] = set()                     # clearly protected: login / 401 / 403 / 404 / login form
        for r in pages:
            v = views.get(r.url)
            if v is None:
                states[r.url] = ("unknown", "Not checked: the audit was stopped first.")
                continue
            state, note, is_strong = self._classify(r, v, shared)
            states[r.url] = (state, note)
            if is_strong:
                strong.add(_norm(r.url))

        shells = [r for r in pages if states[r.url][0] == "shell"]
        if shells and not self.stop_event.is_set():
            self._browser_compare(shells, views, states, strong)
        js_unverified: Set[str] = set()
        for r in pages:
            if states[r.url][0] == "shell":
                js_unverified.add(r.url)
                states[r.url] = ("unknown", "Not verified: this page is drawn by JavaScript and could not be compared "
                                            "in a browser here. Open it in a private window to check it asks for a login.")

        exposed, member = self._members_area(pages, states, strong)
        rows = []
        for r in pages:
            state = states[r.url][0]
            if state in ("same", "different") and _norm(r.url) not in exposed:
                continue   # public page: the same for everyone (or only personalised)
            if state == "unknown" and r.url not in js_unverified and not self._member_reason(
                    _norm(r.url), r, member, self._inlinks):
                continue   # an unreachable public page is not a login question
            rows.append(self._row(r, views.get(r.url), states[r.url], exposed.get(_norm(r.url))))
        rows.sort(key=lambda x: (not x["exposed"], x["verified"], -len(x["warnings"]), x["url"]))
        # pages = protected + exposed + public + not_verified; checked = pages requested logged out
        counts = {
            "pages": len(pages),
            "checked": len(views),
            "protected": sum(1 for s, _ in states.values() if s == "protected"),
            "exposed": len(exposed),
            "public": sum(1 for u, (s, _) in states.items() if s in ("same", "different") and _norm(u) not in exposed),
            "not_verified": sum(1 for s, _ in states.values() if s == "unknown"),
            "in_sitemap": sum(1 for x in rows if x["in_sitemap"]),
        }
        return {"rows": rows, **counts}

    # ---------------------------------------------------------------- logged-out request
    def _anonymous(self) -> requests.Session:
        """A fresh session per page: no login cookies, no custom header (cookies the site sets while
        redirecting this one visitor are kept for that page only)."""
        s = requests.Session()
        adapter = HTTPAdapter(max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"User-Agent": self.user_agent,
                          "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                          "Accept-Language": "en-US,en;q=0.9"})
        return s

    def _view(self, url: str) -> _View:
        s = self._anonymous()
        try:
            v = fetch_view(s, url, timeout=self.timeout, verify=self.verify,
                           limiter=getattr(self.crawler, "rate_limiter", None), stop_event=self.stop_event)
        finally:
            s.close()
        if v.html and not v.waf:
            try:
                v.seo = extract_page(v.html, v.final_url or url, v.response, getattr(self.crawler, "base_domain", ""),
                                     soup=parse_html(v.html), url=url)
            except Exception:
                v.seo = None
        v.response = None           # the page's data is in seo / html now
        return v

    def _classify(self, r, v: _View, shared: Set[str]) -> Tuple[str, str, bool]:
        """(state, note, strongly protected) for one page from its logged-out view."""
        url = r.url
        if v.error:
            return "unknown", f"Not verified: the logged-out request failed ({v.error}).", False
        if v.waf:
            # A challenge is not the site's login wall: a real visitor may pass it and see the page
            return "unknown", (f"Not verified: a bot check ({v.waf}) answered the logged-out request, so ARACHNIX "
                               "couldn't see what a signed-out visitor gets."), False
        to_login = any(is_login_url(h) for h in v.hops) and not is_login_url(url)
        status = v.first_status
        if to_login:
            return "protected", "Protected: sends logged-out visitors to the login page.", True
        if status in (401, 403):
            return "protected", f"Protected: refuses logged-out visitors (HTTP {status}).", True
        if status in (404, 410):
            return "protected", f"Protected: hidden from logged-out visitors (answers {status}).", True
        if status is not None and 300 <= status < 400:
            where = v.final_url or (v.hops[-1] if v.hops else "")
            return "protected", f"Protected: sends logged-out visitors to {where} instead.", False
        if status is None or status >= 400 or status < 200:
            return "unknown", f"Not verified: the logged-out request answered {status}.", False
        li = getattr(r, "seo", None) or {}
        lo = v.seo or {}
        form = bool(_PASSWORD_INPUT.search(v.html or ""))
        same, sim = _same_page(li, lo)
        if form and not same:
            return "protected", "Protected: shows a login form to logged-out visitors.", True
        if _is_shell(li, v.html, shared):
            return "shell", "", False
        if same:
            return "same", f"{sim:.0f}", False
        return "different", (f"Logged-out visitors see a different version of this page "
                              f"({sim:.0f}% the same text): public, with member-only parts."), False

    # ---------------------------------------------------------------- JavaScript pages
    def _browser_compare(self, shells: List[Any], views: Dict[str, _View], states: Dict[str, Tuple[str, str]],
                         strong: Set[str]):
        """Opens JavaScript-drawn pages logged out (a fresh Chrome, no session) and logged in (the crawl's
        browser) and compares what each shows."""
        lease = getattr(self.crawler, "browser_lease", None)
        if lease is None or not chrome_available():
            return
        # Logged in first (the crawl's browser), then one logged-out Chrome: never two of ours at once,
        # so a server with a Chrome cap cannot wait on itself
        members: Dict[str, Tuple[str, str, str, bool]] = {}
        for r in shells[:MAX_BROWSER_CHECKS]:
            if self.stop_event.is_set():
                break
            try:
                with lease(timeout=60) as member_driver:
                    if member_driver is None:
                        break
                    seen = _render(member_driver, r.url)
            except Exception:
                seen = None
            if seen is not None and not (is_login_url(seen[0]) and not is_login_url(r.url)):
                members[r.url] = seen
        if not members:
            return
        driver = new_chrome(self.user_agent, timeout=30, eager=True, wait=20)
        if driver is None:
            return
        try:
            block_dangerous(driver)
            for r in shells:
                if r.url not in members or self.stop_event.is_set():
                    continue
                out = _render(driver, r.url)
                if out is None:
                    continue
                final, title, text, password = out
                v = views.get(r.url)
                if v is not None:
                    v.method = "browser"
                if is_login_url(final) and not is_login_url(r.url):
                    states[r.url] = ("protected", "Protected: the page sends logged-out visitors to the login "
                                                  "page (seen in a browser).")
                    strong.add(_norm(r.url))
                    continue
                m_final, m_title, m_text, m_password = members[r.url]
                if password and not m_password:
                    states[r.url] = ("protected", "Protected: shows a login form to logged-out visitors "
                                                  "(seen in a browser).")
                    strong.add(_norm(r.url))
                    continue
                sim = simhash_similarity(simhash(text), simhash(m_text)) if text.strip() and m_text.strip() else 0.0
                if title.strip().casefold() == m_title.strip().casefold() and sim >= SAME_SIMILARITY:
                    states[r.url] = ("same", f"{sim:.0f}")
                else:
                    states[r.url] = ("different", "Logged-out visitors see a different page in the browser "
                                                  f"({sim:.0f}% the same text).")
        finally:
            close_chrome(driver)

    # ---------------------------------------------------------------- members area
    def _members_area(self, pages: List[Any], states: Dict[str, Tuple[str, str]],
                      strong: Set[str]) -> Tuple[Dict[str, str], Set[str]]:
        """({page that looks the same logged out AND belongs to the members area: why}, members area).
        Grows until stable: a page linked only from an exposed member page is a member page too."""
        start = _norm(getattr(self.crawler, "start_url", "") or "")
        parts = urlsplit(start) if start else None
        roots = {start, self.login_url}
        if parts:
            roots.add(_norm(f"{parts.scheme}://{parts.netloc}/"))
        try:
            site = self.crawler.site_index()[0]
            self._inlinks = getattr(site, "inlinks", {}) or {}
        except Exception:
            self._inlinks = {}
        by_norm = {_norm(r.url): r for r in pages}
        member = set(strong)
        exposed: Dict[str, str] = {}
        candidates = [u for u, r in by_norm.items() if states[r.url][0] == "same"]
        changed = True
        while changed:
            changed = False
            for u in candidates:
                if u in exposed or u in roots or _PUBLIC_PAGE.search(urlsplit(u).path or ""):
                    continue
                reason = self._member_reason(u, by_norm[u], member, self._inlinks)
                if reason:
                    exposed[u] = reason
                    member.add(u)
                    changed = True
        return exposed, member

    def _member_reason(self, url: str, r, member: Set[str], inlinks: Dict[str, List[dict]]) -> str:
        p = urlsplit(url)
        path = p.path or "/"
        for m in sorted(member):
            mp = urlsplit(m)
            base = (mp.path or "/").rstrip("/")
            if base and mp.netloc == p.netloc and path.startswith(base + "/") and path.rstrip("/") != base:
                return f"It sits under {base}/, which needs a login."
        # A login often lands back on a public page: only a member-looking landing page counts
        if url == self.landing and self.landing and _MEMBER_PATH.search(path):
            return "It is the page the login lands on."
        # A page the site lists in its XML sitemap is meant to be public (e.g. a product also linked from
        # order history), so being linked only from member pages does not make it a member page
        if self._in_sitemap(r.url):
            return ""
        sources = {_norm(i.get("source") or "") for i in inlinks.get(r.url, []) or inlinks.get(url, [])}
        if not sources and (r.parent_url or "").startswith(("http://", "https://")):
            sources = {_norm(r.parent_url)}
        sources.discard(url)
        sources.discard("")
        if sources and sources <= member:
            return "It is only linked from pages behind the login."
        return ""

    # ---------------------------------------------------------------- output
    def _row(self, r, v: Optional[_View], state_note: Tuple[str, str], exposed_reason: Optional[str]) -> dict:
        state, note = state_note
        seo_li = getattr(r, "seo", None) or {}
        exposed = bool(exposed_reason)
        protected = state == "protected"
        verified = state != "unknown"
        if exposed:
            note = (f"Exposed: opens without login and shows the same page ({note}% the same text, same title). "
                    f"{exposed_reason} Anyone with the link can read it.")
        directives = seo_li.get("directives") or []
        noindex = "noindex" in directives or "none" in directives
        headers = getattr(r, "response_headers", None) or {}
        cache = (headers.get("cache-control") or "").lower()
        cache_private: Optional[bool] = ("private" in cache or "no-store" in cache) if headers else None
        in_sitemap = self._in_sitemap(r.url)
        warnings: List[str] = []
        if protected or exposed:
            if in_sitemap:
                warnings.append("Listed in the XML sitemap, which invites search engines to a members-only page.")
            if not noindex:
                warnings.append("No 'noindex': if it ever opens without login, search engines may index it.")
            if cache_private is False:
                warnings.append("Sent without 'Cache-Control: private' or 'no-store', so a shared cache (CDN) "
                                "could keep this member page and show it to someone else.")
        if warnings and protected:
            note = f"{note} {warnings[0]}" if len(warnings) == 1 else f"{note} {len(warnings)} warnings."
        return {
            "url": r.url,
            "logged_out_status": v.first_status if v is not None else None,
            "exposed": exposed,
            "note": note,
            "protected": protected,
            "verified": verified,
            "final_url": (v.final_url if v is not None else "") or "",
            "method": v.method if v is not None else "http",
            "in_sitemap": in_sitemap,
            "noindex": noindex,
            "cache_private": cache_private,
            "warnings": warnings,
        }

    def _in_sitemap(self, url: str) -> bool:
        index = getattr(self.crawler, "sitemap_index", None) or {}
        if url in index:
            return True
        if self._sitemap_norm is None:
            self._sitemap_norm = {_norm(u) for u in index}
        return _norm(url) in self._sitemap_norm


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def _norm(url: str) -> str:
    return (normalize_url(url) or url or "") if url else ""


def _is_html(r) -> bool:
    seo = getattr(r, "seo", None) or {}
    return bool(seo.get("is_html")) or "html" in (getattr(r, "content_type", "") or "").lower()


def _shared_hashes(pages: List[Any]) -> Set[str]:
    """Logged-in text hashes that SHELL_SHARED or more pages have in common."""
    counts: Dict[str, int] = {}
    for r in pages:
        h = (getattr(r, "seo", None) or {}).get("content_hash") or ""
        if h:
            counts[h] = counts.get(h, 0) + 1
    return {h for h, n in counts.items() if n >= SHELL_SHARED}


def _is_shell(li: Dict[str, Any], html: str, shared: Set[str]) -> bool:
    """The logged-in HTML has no real content of its own: the page is drawn by JavaScript."""
    if int(li.get("word_count") or 0) < SHELL_WORDS:
        return True
    return (li.get("content_hash") or "") in shared and any(m in (html or "")[:200000] for m in _SPA_MARKERS)


def _same_page(li: Dict[str, Any], lo: Dict[str, Any]) -> Tuple[bool, float]:
    """(same title and text similarity >= SAME_SIMILARITY, similarity in percent)."""
    t1 = ((li.get("titles") or [""])[0] or "").strip().casefold()
    t2 = ((lo.get("titles") or [""])[0] or "").strip().casefold()
    if li.get("content_hash") and li.get("content_hash") == lo.get("content_hash"):
        sim = 100.0
    elif li.get("simhash") and lo.get("simhash"):
        sim = simhash_similarity(int(li["simhash"]), int(lo["simhash"]))
    else:
        sim = 0.0
    return t1 == t2 and sim >= SAME_SIMILARITY, sim


def _render(driver, url: str) -> Optional[Tuple[str, str, str, bool]]:
    """(final URL, title, visible text, password field shown) once the page settles; None on failure."""
    try:
        try:
            driver.get(url)
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
        # Client-side apps redirect or fill in after load: wait until URL and text stop changing
        last = None
        settle_end = time.time() + 6
        while time.time() < settle_end:
            time.sleep(0.75)
            title, text, password = driver.execute_script(_RENDER_JS)
            snap = (driver.current_url, len(text or ""))
            if snap == last:
                break
            last = snap
        title, text, password = driver.execute_script(_RENDER_JS)
        return _norm(driver.current_url or url), str(title or ""), str(text or ""), bool(password)
    except Exception:
        return None
