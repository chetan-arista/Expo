"""
Headless Chrome rendering for link discovery: pages built by JavaScript (React, Next.js, Vue, SPA menus,
lazy loading) and, on logged-in crawls, pages reachable only by clicking menus and buttons.

Renders run in the crawl's shared browser (browser.SharedBrowser), which carries the login (cookies plus
web storage) and refuses sign-out URLs. Labels that sign out or change data are never clicked.
Click exploration is ported from the sibling project's dynamic_extractor.py.
"""
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from .browser import SharedBrowser, chrome_available, media_blocked, set_media_blocked
from .url_utils import (get_domain, is_excluded_path, is_internal_url, is_logout_url, is_unsafe_url,
                        normalize_url, redirected_to_login)

# Limits for clicking menus/buttons to reveal hidden links. Labels already tried are skipped, so a page
# whose menus were all seen elsewhere costs almost nothing; the page cap only counts pages with new ones.
CLICK_MAX_PAGES = 40          # pages with new menus/buttons explored per crawl
CLICK_PAGE_SECONDS = 45       # per page
CLICK_TOTAL_SECONDS = 240     # per crawl
CLICK_MAX_SUBITEMS = 8        # items tried inside each opened menu

CHROME_RECYCLE_PAGES = 25     # restart Chrome (same profile and session) after this many renders

# Never click anything whose label suggests it signs out or changes data
UNSAFE_LABEL = r"\b(?:" + (
    r"log ?out|sign ?out|log ?off|delete|remove|deactivate|destroy|unsubscribe|cancel|submit|save|send|"
    r"pay|buy|purchase|checkout|order|confirm|approve|reject|publish|upload|reset|disconnect|revoke|leave|"
    r"block|invite|create|add|new|post|apply|accept|decline|archive|mark|clear|dismiss|close|edit|update|"
    r"rename|move|duplicate|copy|share|download|export|print|import|sync|refresh|retry|install|enable|"
    r"disable|toggle|theme|dark|light|language|like|follow|star|pin|vote|assign|complete|done|start|stop"
) + r")\b"
# Sign-out markers in an element's own markup (icon class names, hrefs, test ids)
DANGER_MARKUP = r"log[-_ ]?out|sign[-_ ]?out|log[-_ ]?off|right-from-bracket|exit-to-app"

# The page's links; with arguments[0] / [1] also those inside open shadow roots / same-origin iframes
_LINKS_JS = """
    const items = [], roots = [document];
    const flatShadow = !!arguments[0], flatFrames = !!arguments[1];
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
        try { root.querySelectorAll('a[href]').forEach(a => anchors.push(a)); } catch (e) {}
    }
    anchors.forEach(a => {
        try {
            if (a.href) {
                items.push({
                    href: a.href,
                    raw: a.getAttribute('href') || '',
                    text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '').trim()
                });
            }
        } catch(e) {}
    });
    return items;
"""

# What changes when a click does something: URL, element count, links, visible text, open menus
_SIGNATURE_JS = """
    return [location.href, document.getElementsByTagName('*').length, document.querySelectorAll('a[href]').length,
            document.body ? document.body.innerText.length : 0,
            document.querySelectorAll('[aria-expanded=true], [data-state=open], [open]').length].join('|');
"""

# Finds visible clickable elements that are not links and tags each with data-crawler-key (same key for
# the same element after a reload). `nav` marks menu openers and elements in navigation areas (nav,
# sidebars, headers, fixed/sticky panels, menus): only those are clicked on a fresh page, so buttons in
# page content (task cards, forms) are left alone.
_CLICKABLES_JS = """
    const UNSAFE = new RegExp(arguments[0], 'i');
    const DANGER = new RegExp(arguments[1], 'i');
    const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width >= 8 && r.height >= 8 && r.width <= 600 && r.height <= 200
            && s.visibility !== 'hidden' && s.display !== 'none' && s.pointerEvents !== 'none';
    };
    const inNavArea = el => {
        if (el.closest('nav, aside, header, footer, [role=navigation], [role=menu], [role=menubar], [role=listbox], [role=dialog]')) return true;
        for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
            const pos = getComputedStyle(p).position;
            if (pos === 'fixed' || pos === 'sticky') return true;
            const r = p.getBoundingClientRect();
            if (r.height >= innerHeight * 0.8 && r.width > 0 && r.width <= 360 && r.left <= 10) return true;
            if (r.top <= 10 && r.height > 0 && r.height <= 120 && r.width >= innerWidth * 0.6) return true;
        }
        return false;
    };
    const realLink = a => {
        const h = (a.getAttribute('href') || '').trim().toLowerCase();
        return h && h !== '#' && !h.startsWith('#!') && !h.startsWith('javascript:');
    };
    const hasHandler = el => {
        for (const k of Object.keys(el)) {
            if (k.startsWith('__reactProps$')) {
                const p = el[k] || {};
                if (p.onClick || p.onMouseDown || p.onPointerDown || p.onMouseUp || p.onPointerUp) return true;
            } else if (k === '_vei') {
                const v = el[k] || {};
                if (v.onClick || v.onMousedown || v.onPointerdown) return true;
            }
        }
        return typeof el.onclick === 'function';
    };
    const els = Array.from(document.querySelectorAll(
        'button, [role=button], [role=menuitem], [role=tab], [aria-haspopup], [aria-expanded], summary, a[href="#"], a[href^="javascript:"], a:not([href])'));
    document.querySelectorAll('body *').forEach(el => {
        if (el.tagName === 'A' || el.tagName === 'BUTTON') return;
        if (hasHandler(el)) { els.push(el); return; }
        if (/^(DIV|SPAN|LI|IMG|SVG)$/i.test(el.tagName)) {
            const p = el.parentElement;
            if (getComputedStyle(el).cursor === 'pointer' && !(p && getComputedStyle(p).cursor === 'pointer')) els.push(el);
        }
    });
    let out = [];
    const counts = {}, seen = new Set();
    for (const el of els) {
        if (seen.has(el)) continue;
        seen.add(el);
        const link = el.closest('a');
        if ((link && realLink(link)) || el.closest('form, [contenteditable=true]') || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
        const role = el.getAttribute('role') || '';
        if (/checkbox|switch|radio|slider|option/.test(role) || (el.type === 'submit' && el.form) || !visible(el)) continue;
        const svgTitle = el.querySelector('svg title');
        const label = (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.getAttribute('alt')
            || (svgTitle && svgTitle.textContent) || el.getAttribute('data-tooltip') || el.getAttribute('data-tip') || '')
            .trim().replace(/\\s+/g, ' ').slice(0, 60);
        if (UNSAFE.test(label) || DANGER.test(el.outerHTML.slice(0, 800))) continue;
        const opener = el.hasAttribute('aria-haspopup') || el.hasAttribute('aria-expanded') || el.tagName === 'SUMMARY';
        const nav = opener || inNavArea(el);
        if (!label && !nav) continue;
        const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\\s+/)[0] || '';
        let name = label || ('icon ' + el.tagName.toLowerCase() + (cls ? '.' + cls.slice(0, 20) : ''));
        counts[name] = (counts[name] || 0) + 1;
        if (!label) name += ' #' + counts[name];
        const key = (name + '-' + counts[name]).replace(/[^A-Za-z0-9-]/g, '_');
        el.setAttribute('data-crawler-key', key);
        out.push({key: key, label: name, nav: nav});
    }
    out = out.filter(c => c.nav).concat(out.filter(c => !c.nav)).slice(0, 80);
    return out;
"""

# Greyed-out (disabled) items in navigation areas: menu entries a visitor sees but cannot click, e.g.
# <span title="Create a project first" class="cursor-not-allowed opacity-40">Planner</span>. Used to explain
# paths that exist only in the site's JavaScript ("found in code, not clickable") and to box that item.
_DISABLED_JS = """
    const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width >= 8 && r.height >= 8 && r.width <= 600 && r.height <= 200
            && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const inNavArea = el => {
        if (el.closest('nav, aside, header, [role=navigation], [role=menu], [role=menubar], [role=tablist]')) return true;
        for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
            const pos = getComputedStyle(p).position;
            if (pos === 'fixed' || pos === 'sticky') return true;
            const r = p.getBoundingClientRect();
            if (r.height >= innerHeight * 0.8 && r.width > 0 && r.width <= 360 && r.left <= 10) return true;
        }
        return false;
    };
    const greyed = el => {
        if (el.disabled === true || el.getAttribute('aria-disabled') === 'true' || el.hasAttribute('data-disabled')) return true;
        const s = getComputedStyle(el);
        return s.cursor === 'not-allowed' || (s.pointerEvents === 'none' && parseFloat(s.opacity) <= 0.6);
    };
    const cssPath = el => {
        const parts = [];
        for (let e = el; e && e.nodeType === 1 && e !== document.body; e = e.parentElement) {
            if (e.id && /^[A-Za-z][\\w-]*$/.test(e.id)) { parts.unshift('#' + e.id); return parts.join(' > '); }
            let i = 1;
            for (let p = e.previousElementSibling; p; p = p.previousElementSibling) if (p.tagName === e.tagName) i++;
            parts.unshift(e.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
        }
        return 'body > ' + parts.join(' > ');
    };
    const SKIP = /^(SCRIPT|STYLE|INPUT|SELECT|TEXTAREA|OPTION|LABEL|FORM|IMG|PATH|SVG|G|USE)$/i;
    const out = [], seen = new Set();
    for (const el of document.querySelectorAll('body *')) {
        if (out.length >= 40) break;
        if (SKIP.test(el.tagName) || el.closest('form, svg') || !greyed(el)) continue;
        if (el.parentElement && greyed(el.parentElement)) continue;   // the outermost greyed element only
        if (!visible(el) || !inNavArea(el)) continue;
        const label = (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
        if (!label || label.length > 40) continue;
        const title = (el.getAttribute('title') || el.getAttribute('data-tooltip') || '').trim().slice(0, 100);
        if (seen.has(label + '|' + title)) continue;
        seen.add(label + '|' + title);
        const outer = el.outerHTML, tag = el.tagName.toLowerCase();   // opening tag + its text, no icon markup
        const html = outer.slice(0, outer.indexOf('>') + 1).replace(/\\s+/g, ' ').slice(0, 260) + label + '</' + tag + '>';
        out.push({label: label, title: title === label ? '' : title, selector: cssPath(el), html: html});
    }
    return out;
"""

RenderResult = Tuple[str, str, Dict[str, Dict[str, str]], Dict[str, Dict[str, Any]]]


class _SessionLost(Exception):
    """A logged-in page opened the login page instead (the site ended the session)."""


def _state_from_cookies(cookies: Dict[str, str], origin: str) -> Dict[str, Any]:
    """A name -> value cookie dict (older callers) as a session_state for the site's host."""
    host = urlparse(origin).hostname or ""
    secure = origin.startswith("https://")
    return {"cookies": [{"name": k, "value": v, "domain": host, "path": "/",
                         "secure": secure or k.startswith(("__Secure-", "__Host-"))} for k, v in cookies.items()],
            "local": {}, "session": {}}


class DynamicExtractor:
    """
    Renders pages in headless Chrome to find links that only exist after JavaScript runs, and (with
    explore_clicks) the pages that menus and buttons lead to.

    browser:         the crawl's SharedBrowser (preferred); renders take its lock one at a time.
    driver:          an already running, logged-in Chrome to use instead (never quit by this class).
    session_state:   login imported into a Chrome this class starts itself.
    on_session_lost: called when a logged-in render lands on the login page; returns a fresh session_state
                     after logging in again (or None), and the page is then tried once more.
    proxy:           the crawl's proxy setting, used when this class starts its own browser (a shared
                     browser already carries it).
    """

    def __init__(
        self,
        base_domain: str,
        user_agent: str,
        timeout: int = 15,
        cookies: Optional[Dict[str, str]] = None,
        logger: Optional[Callable[[str], None]] = None,
        origin: Optional[str] = None,
        driver=None,
        session_state: Optional[Dict[str, Any]] = None,
        on_session_lost: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        browser: Optional[SharedBrowser] = None,
        explore_clicks: bool = False,
        logged_in: Optional[bool] = None,
        proxy: Optional[Dict[str, Any]] = None,
    ):
        self.base_domain = base_domain
        self.origin = origin or f"https://{base_domain}"
        self.user_agent = user_agent
        self.timeout = timeout
        self.logger = logger or (lambda msg: None)
        self.on_session_lost = on_session_lost
        # Clicking menus finds pages that logged-in apps hide behind popups; public sites list their pages
        # in links and sitemaps, and clicking is slow
        self.explore_clicks = explore_clicks
        if session_state is None and cookies:
            session_state = _state_from_cookies(cookies, self.origin)
        self.logged_in = bool(session_state or cookies) if logged_in is None else logged_in
        self._owns_browser = browser is None and driver is None
        if browser is None:
            browser = SharedBrowser(user_agent, timeout, site_url=self.origin, logger=self.logger, proxy=proxy)
            if driver is not None:
                browser.adopt(driver, session_state)
            elif session_state:
                browser.set_session(session_state)
        elif session_state and not browser.session_state:
            browser.set_session(session_state)
        self.browser = browser
        self._external_driver = driver
        # Set when a logged-in crawl's browser stays on the login page (the session could not be restored)
        self.login_lost = False
        # Configuration > Rendering: links inside open shadow roots / same-origin iframes are found too
        # (the crawl sets these from CrawlConfig.flatten_shadow_dom / flatten_iframes)
        self.flatten_shadow_dom = False
        self.flatten_iframes = False
        # Greyed-out navigation items seen per rendered page: {page url: [{"label", "title", "selector", "html"}]}
        self.disabled_items: Dict[str, List[Dict[str, str]]] = {}
        self._renders = 0
        self._explored_labels: Set[str] = set()
        self._click_pages_left = CLICK_MAX_PAGES
        self._click_time_left = CLICK_TOTAL_SECONDS
        self._stop_event: Optional[threading.Event] = None
        self._retry_at = 0.0  # Chrome could not start (busy server): renders are skipped until then
        if chrome_available():
            with self.browser.hold(timeout=60, start_wait=5) as d:
                if d is not None:
                    self.log("Headless Chrome ready for dynamic link discovery.")
                else:
                    self._retry_at = time.time() + 60
                    self.log(f"Headless Chrome could not be started ({self.browser.last_error}); "
                             "falling back to standard requests.")

    def log(self, message: str):
        self.logger(f"[Dynamic Browser] {message}")

    @property
    def driver(self):
        """The Chrome driver in use (None when Chrome is unavailable)."""
        return self.browser.driver if self.browser.alive() else None

    @property
    def available(self) -> bool:
        """Chrome can render (the shared browser restarts by itself if it was closed; after a failed start,
        e.g. on a busy server, it is tried again a minute later)."""
        return self.browser.alive() or (chrome_available() and time.time() >= self._retry_at)

    # ------------------------------------------------------------------
    def render_and_extract(
        self,
        url: str,
        include_subdomains: bool = False,
        exclude_patterns: Optional[list] = None,
        collect_external: bool = True,
        stop_event: Optional[threading.Event] = None
    ) -> RenderResult:
        """
        Loads the page in the browser, scrolls to trigger lazy loading, and returns
        (rendered title, rendered HTML, {internal url: link meta}, {external url: info}).
        Renders run one at a time; once `stop_event` is set, waiting renders are skipped (empty result).
        """
        empty: RenderResult = ("", "", {}, {})
        if (stop_event is not None and stop_event.is_set()) or is_logout_url(url) or \
                (self.logged_in and is_unsafe_url(url)):
            return empty
        self._stop_event = stop_event
        with self.browser.hold(timeout=max(60, self.timeout * 6), start_wait=5) as driver:
            if driver is None:
                self._retry_at = time.time() + 60
                return empty
            if stop_event is not None and stop_event.is_set():
                return empty
            self._recycle_if_due()
            if not self.browser.alive():
                return empty
            return self._render(url, include_subdomains, exclude_patterns or [], collect_external)

    def _recycle_if_due(self):
        """A tab that loads page after page keeps growing; restart Chrome now and then (same profile and
        session, so the login survives). A driver handed in by the caller is never restarted."""
        self._renders += 1
        if self._external_driver is not None or self._renders <= CHROME_RECYCLE_PAGES:
            return
        self.browser.restart(f"after {CHROME_RECYCLE_PAGES} pages to free memory")
        self._renders = 1

    def _prepare(self, driver):
        if not media_blocked(driver):
            set_media_blocked(driver, True)

    def _load(self, driver, url: str):
        try:
            driver.get(url)
        except Exception:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        self._wait_settled(driver)

    def _render(self, url: str, include_subdomains: bool, exclude_patterns: list,
                collect_external: bool) -> RenderResult:
        for attempt in (1, 2):
            driver = self.browser.driver
            if driver is None:
                break
            try:
                return self._render_once(driver, url, include_subdomains, exclude_patterns, collect_external)
            except _SessionLost:
                # The site ended the session: log in again (the crawl decides how) and try the page once more
                if attempt == 2 or not self.on_session_lost or not self.on_session_lost():
                    break
                self.log(f"Login session restored; reopening {url}")
        if not self.login_lost:
            self.log(f"Headless Chrome is not logged in ({url} opened the login page); "
                     "its links are skipped until the login works again.")
        self.login_lost = True
        return "", "", {}, {}

    def _check_logged_in(self, driver, url: str):
        if self.logged_in and redirected_to_login(url, driver.current_url):
            raise _SessionLost(url)

    def _render_once(self, driver, url: str, include_subdomains: bool, exclude_patterns: list,
                     collect_external: bool) -> RenderResult:
        discovered_internal: Dict[str, Dict[str, str]] = {}
        discovered_external: Dict[str, Dict[str, Any]] = {}
        try:
            self._prepare(driver)
            self._load(driver, url)
            self._check_logged_in(driver, url)
            self.login_lost = False

            driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
            time.sleep(0.4)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.4)
            rendered_title = (driver.title or "").strip()
            rendered_html = driver.page_source or ""
            self._note_disabled(driver, url)   # before any click changes the page

            items = [dict(i, via="dom") for i in self._dom_links(driver)]
            if self.explore_clicks:
                items += self._explore_clicks(driver, url)

            for item in items:
                norm = normalize_url(item.get("href") or "", base_url=url)
                if not norm or is_logout_url(norm) or (self.logged_in and is_unsafe_url(norm)):
                    continue
                txt = (item.get("text") or "").strip()[:100]
                if is_internal_url(norm, self.base_domain, include_subdomains=include_subdomains):
                    if is_excluded_path(norm, exclude_patterns) or norm in discovered_internal:
                        continue
                    if item.get("via") == "click":
                        discovered_internal[norm] = {
                            "anchor_text": txt, "tag": "click", "region": "Menu opened by a click",
                            "selector": "", "html": f'Reached by clicking "{txt[:80]}"', "raw_href": "",
                        }
                    else:
                        discovered_internal[norm] = {
                            "anchor_text": txt, "tag": "a", "region": "Rendered by JavaScript", "selector": "",
                            "html": f'<a href="{(item.get("raw") or "")[:80]}">{txt[:60]}</a>',
                            "raw_href": item.get("raw") or "",
                        }
                elif collect_external and norm not in discovered_external:
                    discovered_external[norm] = {
                        "url": norm, "domain": get_domain(norm), "parent_url": url,
                        "anchor_text": txt or "-", "discovery_source": "Dynamic JS DOM",
                    }
            return rendered_title, rendered_html, discovered_internal, discovered_external
        except _SessionLost:
            raise
        except Exception as e:
            self.log(f"Error rendering {url} with dynamic browser: {str(e).splitlines()[0][:200]}")
            return "", "", discovered_internal, discovered_external

    # ------------------------------------------------------------------
    # Menu click-through
    # ------------------------------------------------------------------
    def _dom_links(self, driver) -> List[Dict[str, str]]:
        return driver.execute_script(_LINKS_JS, bool(self.flatten_shadow_dom), bool(self.flatten_iframes)) or []

    def _note_disabled(self, driver, url: str):
        """Records the page's greyed-out navigation items (see _DISABLED_JS)."""
        try:
            items = driver.execute_script(_DISABLED_JS) or []
        except Exception:
            return
        if items:
            self.disabled_items[normalize_url(url) or url] = [
                {k: str(i.get(k) or "") for k in ("label", "title", "selector", "html")} for i in items[:40]]

    def _clickables(self, driver) -> List[Dict[str, Any]]:
        return driver.execute_script(_CLICKABLES_JS, UNSAFE_LABEL, DANGER_MARKUP) or []

    def _signature(self, driver) -> str:
        try:
            return str(driver.execute_script(_SIGNATURE_JS))
        except Exception:
            return ""

    def _click(self, driver, key: str) -> bool:
        """Real mouse click first (menus often open on pointerdown, which a script click does not fire);
        when nothing changes, a script el.click() (some React menus only react to that)."""
        from selenium.webdriver.common.by import By
        sel = f'[data-crawler-key="{key}"]'
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
        except Exception:
            return False
        before = self._signature(driver)
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            el.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", el)
            except Exception:
                return False
            time.sleep(0.8)
            return True
        time.sleep(0.8)
        if self._signature(driver) == before:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                driver.execute_script("arguments[0].click();", el)
                time.sleep(0.8)
            except Exception:
                pass
        return True

    def _take_navigation(self, driver, url: str, label: str, found: List[Dict[str, str]]) -> bool:
        """Records where a click led (same tab or a new tab). True if the page changed (reload needed)."""
        handles = driver.window_handles
        if len(handles) > 1:
            main = handles[0]
            for h in handles[1:]:
                driver.switch_to.window(h)
                time.sleep(0.5)
                found.append({"href": driver.current_url, "text": label, "via": "click"})
                driver.close()
            driver.switch_to.window(main)
        current = driver.current_url
        if current.split("#")[0] != url.split("#")[0]:
            found.append({"href": current, "text": label, "via": "click"})
            return True
        return False

    def _reload(self, driver, url: str):
        try:
            driver.get(url)
        except Exception:
            pass
        self._wait_settled(driver, max_wait=5.0)
        self._check_logged_in(driver, url)

    def _wait_settled(self, driver, max_wait: float = 8.0):
        """Waits until the page has loaded and stopped adding elements for ~1s (apps draw after fetching data)."""
        start, last, stable_since = time.time(), -1, time.time()
        while time.time() - start < max_wait:
            time.sleep(0.4)
            try:
                ready = driver.execute_script("return document.readyState") == "complete"
                count = driver.execute_script("return document.getElementsByTagName('*').length")
            except Exception:
                continue
            if count != last:
                last, stable_since = count, time.time()
            elif ready and time.time() - stable_since >= 1.0:
                return

    def _stopped(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    def _explore_clicks(self, driver, url: str) -> List[Dict[str, str]]:
        """
        Opens menus and presses navigation buttons that are not links (e.g. an avatar menu with "Help",
        "Profile"), and records the links that appear and the pages the clicks lead to. Labels that look
        like they sign out or change data are never clicked, and sign-out URLs are blocked by the browser.
        Each label is tried once per crawl (menus repeat on every page); time and page budgets apply.
        """
        found: List[Dict[str, str]] = []
        if self._click_pages_left <= 0 or self._click_time_left <= 0:
            return found
        url = driver.current_url or url
        start = time.time()
        # Undone when the session ends mid-way, so the same menus are explored again after logging in
        labels_before, pages_before = set(self._explored_labels), self._click_pages_left
        deadline = start + min(CLICK_PAGE_SECONDS, self._click_time_left)
        clicked = 0
        report: List[str] = []
        try:
            known = {l["href"] for l in self._dom_links(driver)}
            candidates = self._clickables(driver)
            triggers = [c for c in candidates if c["nav"] and c["label"] not in self._explored_labels]
            if triggers:
                self._click_pages_left -= 1
                self.log(f"{len(triggers)} new menu/navigation buttons on {url} "
                         f"({sum(1 for c in candidates if not c['nav'])} content buttons left alone): "
                         + ", ".join(f"'{t['label']}'" for t in triggers[:20]))
            for trig in triggers:
                if time.time() > deadline or self._stopped():
                    report.append("time budget reached")
                    break
                self._explored_labels.add(trig["label"])
                before = {c["key"] for c in self._clickables(driver)}  # also re-marks keys after a reload
                if not self._click(driver, trig["key"]):
                    report.append(f"'{trig['label']}' could not be clicked")
                    continue
                clicked += 1
                n_found = len(found)
                if self._take_navigation(driver, url, trig["label"], found):
                    report.append(f"'{trig['label']}' -> {found[-1]['href']}")
                    self._reload(driver, url)
                    continue
                for link in self._dom_links(driver):
                    if link["href"] not in known:
                        found.append(dict(link, text=f"{trig['label']} > {link.get('text') or ''}".strip(" >"),
                                          via="click"))
                        known.add(link["href"])
                appeared = [c for c in self._clickables(driver) if c["key"] not in before]
                items = [c for c in appeared if c["label"] not in self._explored_labels][:CLICK_MAX_SUBITEMS]
                report.append(f"'{trig['label']}' opened " + (", ".join(f"'{c['label']}'" for c in appeared[:10])
                                                             or "nothing")
                              + (f" (+{len(found) - n_found} links)" if len(found) > n_found else ""))
                for item in items:
                    if time.time() > deadline or self._stopped():
                        break
                    self._explored_labels.add(item["label"])
                    self._reload(driver, url)
                    self._clickables(driver)
                    if not self._click(driver, trig["key"]):
                        break
                    self._clickables(driver)
                    if self._click(driver, item["key"]):
                        clicked += 1
                        time.sleep(0.5)
                        if self._take_navigation(driver, url, f"{trig['label']} > {item['label']}", found):
                            report.append(f"'{trig['label']} > {item['label']}' -> {found[-1]['href']}")
                self._reload(driver, url)
        except _SessionLost:
            self._explored_labels, self._click_pages_left = labels_before, pages_before
            raise
        except Exception as e:
            self.log(f"Menu exploration stopped on {url}: {str(e).splitlines()[0][:160]}")
            try:
                self._reload(driver, url)
            except _SessionLost:
                self._explored_labels, self._click_pages_left = labels_before, pages_before
                raise
            except Exception:
                pass
        finally:
            self._click_time_left -= time.time() - start
        if clicked:
            self.log(f"Opened {clicked} menus/buttons on {url}; found {len(found)} hidden link(s). "
                     + "; ".join(report[:25]))
        return found

    def close(self):
        """Closes Chrome when this extractor started it (a shared or handed-in browser stays open)."""
        if self._owns_browser:
            self.browser.close()
