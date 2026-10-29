"""
Visual evidence for broken links, captured with headless Chrome (Selenium) after a crawl:
  1. the parent page, scrolled to the link that points at the broken URL, with that link spotlighted
  2. the broken URL itself (what a visitor actually sees)
plus the link's position on the parent page. Degrades to a no-op when Selenium/Chrome are missing.
On a logged-in crawl the screenshots are taken in the crawl's own logged-in browser (crawler.browser_lease),
or in a new Chrome that imports the same session; sign-out URLs are never opened.
Waits end when the page has fired its load event and painted (wait_ready / wait_frames, shared with the
pinpointed pictures of seo_evidence.py) instead of fixed sleeps; cookie-consent and newsletter pop-ups are
hidden before a screenshot (nothing is clicked).
"""
import hashlib
import io
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import ExitStack
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .browser import (block_dangerous, chrome_available, chrome_error, close_chrome, import_session,
                      media_blocked, new_chrome, set_media_blocked)
from .url_utils import is_unsafe_url, redirected_to_login

SHOT_WIDTH = 820   # px width of stored JPEGs: the PDF shows them about half a page wide
SHOT_QUALITY = 64

# Resolves two animation frames after the load event (or after `cap` seconds): layout and paint are done.
# Web fonts get up to 0.4 s; a timer backs up animation frames a hidden tab may throttle.
_READY_JS = r"""
const done = arguments[arguments.length - 1];
const cap = (arguments[0] || 6) * 1000, t0 = Date.now();
let finished = false;
const end = () => { if (!finished) { finished = true; done(document.readyState); } };
function frames(n) { if (n <= 0) { end(); return; } requestAnimationFrame(() => frames(n - 1)); }
function after() {
  setTimeout(end, 300);
  const fonts = (document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve();
  Promise.race([fonts, new Promise(r => setTimeout(r, 400))]).then(() => frames(2), () => frames(2));
}
(function poll() {
  if (document.readyState === 'complete' || Date.now() - t0 > cap) after(); else setTimeout(poll, 50);
})();
"""

_FRAMES_JS = r"""
const done = arguments[arguments.length - 1];
let n = arguments[0] || 1, finished = false;
const end = () => { if (!finished) { finished = true; done(true); } };
setTimeout(end, 200);
(function f() { if (n-- <= 0) end(); else requestAnimationFrame(f); })();
"""


def ensure_script_timeout(driver, seconds: float = 10.0):
    """Lets the waiting scripts run up to `seconds` (Selenium's default for async scripts is short)."""
    try:
        current = getattr(getattr(driver, "timeouts", None), "script", None)
        if current is None or float(current) < seconds:
            driver.set_script_timeout(seconds)
    except Exception:
        pass


def wait_ready(driver, cap: float = 6.0) -> bool:
    """Waits until the page fired its load event (at most `cap` seconds) and painted twice. Falls back to a
    short sleep when the browser cannot run the waiting script. False when the browser is gone."""
    try:
        driver.execute_async_script(_READY_JS, cap)
        return True
    except Exception:
        try:
            end = time.time() + cap
            while time.time() < end and driver.execute_script("return document.readyState") != "complete":
                time.sleep(0.1)
            time.sleep(0.15)
            return True
        except Exception:
            return False


def wait_frames(driver, n: int = 1):
    """Waits `n` animation frames (the page painted what a script just changed)."""
    try:
        driver.execute_async_script(_FRAMES_JS, n)
    except Exception:
        time.sleep(0.05)


def hide_popups(driver, keep_selector: str = "") -> dict:
    """browser.hide_overlays (cookie-consent and newsletter pop-ups hidden for a screenshot), or {} when the
    browser module does not have it."""
    try:
        from .browser import hide_overlays
    except Exception:
        return {}
    try:
        return hide_overlays(driver, keep_selector) or {}
    except Exception:
        return {}

# Finds the element linking to the target, spotlights it, and reports its position.
_HIGHLIGHT_JS = r"""
const [rawHref, targetUrl, selector, label] = arguments;
const old = document.getElementById('__crawler_hl'); if (old) old.remove();
const oldTag = document.getElementById('__crawler_hl_tag'); if (oldTag) oldTag.remove();
function norm(u) {
  try { const x = new URL(u, location.href); x.hash = ''; return x.href.replace(/\/$/, '').toLowerCase(); }
  catch (e) { return (u || '').toLowerCase(); }
}
function refsOf(e) {
  // every address the element points at (lazy-loaded images keep theirs in data-src; data: URIs are placeholders)
  return ['href', 'action', 'data-href', 'data-url', 'data-link', 'src', 'data-src', 'data-lazy-src']
    .map(a => (e.getAttribute(a) || '').trim()).filter(v => v && !v.startsWith('data:'));
}
function visible(e) {
  const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
  return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
}
const target = norm(targetUrl);
const cands = Array.from(document.querySelectorAll('a[href],area[href],form[action],[data-href],[data-url],[data-link],' +
                                                   'img[src],img[data-src],img[data-lazy-src],iframe[src]'));
let matches = rawHref ? cands.filter(e => refsOf(e).includes(rawHref)) : [];
let how = 'href';
if (!matches.length) matches = cands.filter(e => refsOf(e).some(v => norm(v) === target));
if (!matches.length && selector) {
  try { const s = document.querySelector(selector); if (s) { matches = [s]; how = 'selector'; } } catch (e) {}
}
if (!matches.length) return {found: false, page_height: document.documentElement.scrollHeight};
let el = matches.find(visible) || matches[0];
if (el.tagName === 'IMG' && getComputedStyle(el).display !== 'none' &&
    (!visible(el) || (el.complete && !el.naturalWidth) || (el.getAttribute('src') || '').startsWith('data:'))) {
  // a missing image is empty space (or 0 x 0 with alt=""): hatch the space so the picture shows where it should be
  el.style.cssText += ';display:inline-block;min-width:160px;min-height:90px;box-sizing:border-box;' +
                      'border:1px dashed #9CA3AF;background:repeating-linear-gradient(45deg,#F3F4F6 0 10px,#E5E7EB 10px 20px);';
}
let hidden = !visible(el);
let shown = el;
if (hidden) {  // inside a collapsed menu: spotlight the nearest visible ancestor instead
  let p = el.parentElement;
  while (p && !visible(p)) p = p.parentElement;
  if (p && p !== document.body) shown = p;
}
// jump, never animate: a smooth scroll would still be moving when the box is measured and drawn
document.documentElement.style.scrollBehavior = 'auto';
if (document.body) document.body.style.scrollBehavior = 'auto';
shown.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
const r = shown.getBoundingClientRect();
const pad = 6;
const box = document.createElement('div');
box.id = '__crawler_hl';
box.style.cssText = `position:fixed;left:${r.left - pad}px;top:${r.top - pad}px;width:${r.width + pad * 2}px;` +
  `height:${r.height + pad * 2}px;border:3px solid #E11D48;border-radius:6px;` +
  `box-shadow:0 0 0 9999px rgba(15,23,42,0.45);z-index:2147483647;pointer-events:none;`;
const tag = document.createElement('div');
tag.id = '__crawler_hl_tag';
tag.textContent = label;
const tagTop = r.top - pad - 30 > 4 ? r.top - pad - 30 : r.bottom + pad + 6;
tag.style.cssText = `position:fixed;left:${Math.max(4, r.left - pad)}px;top:${tagTop}px;background:#E11D48;color:#fff;` +
  `font:bold 13px/1 Arial,sans-serif;padding:7px 10px;border-radius:6px;z-index:2147483647;pointer-events:none;`;
document.body.appendChild(box); document.body.appendChild(tag);
const abs = shown.getBoundingClientRect();
return {
  found: true, how: how, hidden: hidden, matches: matches.length,
  top: Math.round(abs.top + window.scrollY), left: Math.round(abs.left + window.scrollX),
  width: Math.round(abs.width), height: Math.round(abs.height),
  view: {x: r.left, y: r.top, w: r.width, h: r.height, vw: window.innerWidth, vh: window.innerHeight},
  page_height: document.documentElement.scrollHeight, page_width: document.documentElement.scrollWidth
};
"""

_CLEAR_JS = """
['__crawler_hl', '__crawler_hl_tag'].forEach(id => { const e = document.getElementById(id); if (e) e.remove(); });
"""


def selenium_available() -> bool:
    try:
        import selenium  # noqa: F401
        return True
    except ImportError:
        return False


class EvidenceCollector:
    """Screenshots for issues. Pass `crawler` (a WebsiteCrawler) to use its logged-in browser; otherwise
    `session_state` (or the older name -> value `cookies`) is imported into a Chrome of its own."""

    def __init__(
        self,
        out_dir: str,
        user_agent: str,
        timeout: int = 15,
        cookies: Optional[Dict[str, str]] = None,
        logger: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        crawler: Any = None,
        session_state: Optional[Dict[str, Any]] = None,
    ):
        self.out_dir = out_dir
        self.user_agent = user_agent
        self.timeout = max(5, min(timeout, 20))
        self.cookies = cookies or {}
        self.crawler = crawler
        self.session_state = session_state or (getattr(crawler, "session_state", None) if crawler else None)
        self.logged_in = bool(self.cookies or self.session_state or getattr(crawler, "auth_used", False))
        self.logger = logger or (lambda msg: None)
        self.cancel_event = cancel_event or threading.Event()
        self.driver = None
        self.unavailable_reason = ""
        # Set when a logged-in crawl's browser is shown the login page instead of the real page
        self.login_lost = False
        self._stack: Optional[ExitStack] = None
        self._shared = False
        self._media_was_blocked = False
        self._retried_login = False

    def log(self, msg: str):
        self.logger(f"[Screenshots] {msg}")

    def start(self) -> bool:
        if not chrome_available():
            self.unavailable_reason = ("Selenium is not installed on this server (pip install selenium)."
                                       if not selenium_available() else "Chrome is not installed on this server.")
            return False
        os.makedirs(self.out_dir, exist_ok=True)
        lease = getattr(self.crawler, "browser_lease", None)
        if lease is not None:
            self._stack = ExitStack()
            try:
                driver = self._stack.enter_context(lease(timeout=90))
            except Exception:
                driver = None
            if driver is not None:
                # The crawl's own (logged-in) browser: show images, give pages time, put things back after
                self.driver, self._shared = driver, True
                self._media_was_blocked = media_blocked(driver)
                set_media_blocked(driver, False)
                try:
                    driver.set_page_load_timeout(self.timeout)
                except Exception:
                    pass
                ensure_script_timeout(driver)
                return True
            self._stack.close()
            self._stack = None
        driver = new_chrome(self.user_agent, self.timeout, eager=False, width=1366, height=860)
        if driver is None:
            self.unavailable_reason = chrome_error() or "Headless Chrome could not start."
            return False
        block_dangerous(driver)
        ensure_script_timeout(driver)
        state = self.session_state  # older name -> value cookies are imported in capture() for the site
        self.driver = driver
        if state:
            done = import_session(driver, state)
            self.log(f"Using the crawl's login ({done['cookies']} cookies, {done['storage_origins']} storage "
                     "origin(s)) for logged-in screenshots.")
        return True

    def close(self):
        if self.driver is None:
            return
        if self._shared:
            try:
                set_media_blocked(self.driver, self._media_was_blocked)
            except Exception:
                pass
            if self._stack is not None:
                self._stack.close()
                self._stack = None
        else:
            close_chrome(self.driver)
        self.driver = None
        self._shared = False

    # ------------------------------------------------------------------
    def capture(self, issues: List, on_progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, dict]:
        """
        issues: CrawlResult objects (already prioritised and capped by the caller).
        Returns {url: {"parent_shot": file|None, "error_shot": file|None, "position": {...}|None}}.
        """
        evidence: Dict[str, dict] = {r.url: {"parent_shot": None, "error_shot": None, "position": None} for r in issues}
        if not issues or not self.driver:
            return evidence

        # Group by parent so each parent page loads once
        by_parent: "OrderedDict[str, list]" = OrderedDict()
        for r in issues:
            # A link read from JavaScript has no element on the page to highlight, unless the page shows its
            # menu item greyed out (not clickable): then that item is boxed
            greyed = getattr(r, "code_greyed", None) or {}
            if getattr(r, "link_tag", "") in ("bundle", "script"):
                if greyed.get("selector") and str(greyed.get("page", "")).startswith(("http://", "https://")):
                    by_parent.setdefault(greyed["page"], []).append(r)
                continue
            if (r.parent_url or "").startswith(("http://", "https://")):
                by_parent.setdefault(r.parent_url, []).append(r)

        total = len(by_parent) + len(issues)
        done = 0
        if not self._shared and not self.session_state and self.cookies:
            self._inject_cookies(issues[0].url)

        for parent_url, children in by_parent.items():
            if self.cancel_event.is_set():
                break
            self.log(f"Locating {len(children)} broken link(s) on {parent_url}")
            if self._load(parent_url) and not self._on_login_page(parent_url):
                # pop-ups hidden once per page (the first broken link stays visible if one sits inside them)
                hide_popups(self.driver, children[0].link_selector or "")
                for r in children:
                    if self.cancel_event.is_set():
                        break
                    image = (getattr(r, "link_tag", "") or "").lower() in ("img", "source", "picture")
                    label = f"{'Missing image' if image else 'Broken link'} → {r.status_code or 'error'}"
                    selector, raw = r.link_selector or "", r.raw_href or ""
                    greyed = getattr(r, "code_greyed", None) or {}
                    if greyed.get("selector") and getattr(r, "link_tag", "") in ("bundle", "script"):
                        label = f"Greyed out, not clickable · {r.status_code or 'error'} if opened"
                        selector, raw = greyed["selector"], ""
                    try:
                        pos = self.driver.execute_script(_HIGHLIGHT_JS, raw, r.url, selector, label)
                    except Exception:
                        pos = None
                    if pos and pos.get("found"):
                        wait_frames(self.driver, 2)   # the spotlight is painted
                        evidence[r.url]["parent_shot"] = self._save(f"{self._slug(r.url)}_parent", focus=pos.get("view"))
                        evidence[r.url]["position"] = pos
                        try:
                            self.driver.execute_script(_CLEAR_JS)
                        except Exception:
                            pass
                    else:
                        evidence[r.url]["position"] = {"found": False}
            done += 1
            if on_progress:
                on_progress(done, total)

        for r in issues:
            if self.cancel_event.is_set():
                break
            self._load(r.url)  # browsers render their own error page on DNS/connection failures
            if not self._on_login_page(r.url):
                hide_popups(self.driver)
                evidence[r.url]["error_shot"] = self._save(f"{self._slug(r.url)}_error", trim=True)
            done += 1
            if on_progress:
                on_progress(done, total)

        return evidence

    # ------------------------------------------------------------------
    def _inject_cookies(self, any_url: str):
        """Older callers pass only name -> value cookies: import them for the crawled site."""
        try:
            from .dynamic_extractor import _state_from_cookies
            p = urlparse(any_url)
            done = import_session(self.driver, _state_from_cookies(self.cookies, f"{p.scheme}://{p.netloc}"))
            self.log(f"Using {done['cookies']}/{len(self.cookies)} session cookies for logged-in screenshots.")
        except Exception as e:
            self.log(f"Cookie injection notice: {e}")

    def _on_login_page(self, requested_url: str) -> bool:
        """Logged-in crawl whose browser was sent to the login page: a screenshot or a 'link not found'
        result from here would describe the login page, so callers skip it. The crawl is asked once to
        restore the session (log in again) and the page is reloaded."""
        if not self.logged_in:
            return False
        try:
            current = self.driver.current_url
        except Exception:
            return False
        if not redirected_to_login(requested_url, current):
            return False
        recover = getattr(self.crawler, "recover_browser_session", None)
        if recover is not None and not self._retried_login and not self.login_lost:
            self._retried_login = True
            try:
                if recover():
                    self._load(requested_url)
                    if not redirected_to_login(requested_url, self.driver.current_url):
                        return False
            except Exception:
                pass
        if not self.login_lost:
            self.log(f"Browser is not logged in ({requested_url} opened the login page); "
                     "screenshots of logged-in pages are skipped.")
        self.login_lost = True
        return True

    def _load(self, url: str) -> bool:
        if is_unsafe_url(url):
            return False  # a sign-out address would end the crawl's login; an admin action would change data
        try:
            self.driver.get(url)
        except Exception:
            # Timed out or failed: stop loading and keep whatever rendered
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                return False
        return wait_ready(self.driver, 5.0)

    @staticmethod
    def _trim_blank(img):
        """Error pages are often one short line on a blank viewport (Next.js: '404 | This page
        could not be found.'). Crop to the content plus a margin so it is readable when small."""
        mask = img.convert("L").point(lambda v: 255 if v < 235 else 0)
        box = mask.getbbox()
        if not box:
            return img
        left, upper, right, lower = box
        crop_w = min(img.width, max(right - left + 120, 420))
        crop_h = min(img.height, max(lower - upper + 80, 170))
        if crop_w * crop_h > 0.6 * img.width * img.height:
            return img  # content already fills the screen
        cx, cy = (left + right) / 2, (upper + lower) / 2
        x0 = int(min(max(0, cx - crop_w / 2), img.width - crop_w))
        y0 = int(min(max(0, cy - crop_h / 2), img.height - crop_h))
        return img.crop((x0, y0, x0 + crop_w, y0 + crop_h))

    def _save(self, name: str, focus: Optional[dict] = None, trim: bool = False) -> Optional[str]:
        """Save the viewport as JPEG. With `focus` (element rect in viewport px), crop a
        zoomed window around the element so the highlight stays readable when printed small.
        With `trim`, crop away blank space around sparse content."""
        try:
            png = self.driver.get_screenshot_as_png()
        except Exception:
            return None
        filename = f"{name}.jpg"
        path = os.path.join(self.out_dir, filename)
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png)).convert("RGB")
            if focus and focus.get("vw"):
                scale = img.width / float(focus["vw"])
                # the window always holds the whole highlighted element (+ its red frame and label), so a big
                # element (an image box) never fills the picture with its blank inside
                want_w = max(820, focus["w"] + 80) * scale
                want_h = max(520, focus["h"] + 100) * scale
                crop_w, crop_h = min(img.width, int(want_w)), min(img.height, int(want_h))
                if crop_w * 520 > crop_h * 820:      # keep roughly the 820 x 520 proportions
                    crop_h = min(img.height, int(crop_w * 520 / 820))
                else:
                    crop_w = min(img.width, int(crop_h * 820 / 520))
                cx = (focus["x"] + focus["w"] / 2) * scale
                cy = (focus["y"] + focus["h"] / 2) * scale
                left = int(min(max(0, cx - crop_w / 2), img.width - crop_w))
                top = int(min(max(0, cy - crop_h / 2), img.height - crop_h))
                img = img.crop((left, top, left + crop_w, top + crop_h))
            elif trim:
                img = self._trim_blank(img)
            if img.width > SHOT_WIDTH:
                img = img.resize((SHOT_WIDTH, int(img.height * SHOT_WIDTH / img.width)))
            img.save(path, "JPEG", quality=SHOT_QUALITY, optimize=True)
        except ImportError:
            filename = f"{name}.png"
            path = os.path.join(self.out_dir, filename)
            with open(path, "wb") as f:
                f.write(png)
        except Exception:
            return None
        return filename

    @staticmethod
    def _slug(url: str) -> str:
        p = urlparse(url)
        base = re.sub(r"[^A-Za-z0-9]+", "-", (p.path + "-" + p.query).strip("/-"))[:50] or "home"
        return f"{base}-{hashlib.md5(url.encode('utf-8')).hexdigest()[:8]}"
