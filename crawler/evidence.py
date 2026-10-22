"""
Visual evidence for broken links, captured with headless Chrome (Selenium) after a crawl:
  1. the parent page, scrolled to the link that points at the broken URL, with that link spotlighted
  2. the broken URL itself (what a visitor actually sees)
plus the link's position on the parent page. Degrades to a no-op when Selenium/Chrome are missing.
"""
import hashlib
import io
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

SHOT_WIDTH = 1000  # px width of stored JPEGs (keeps PDF and API payloads small)

# Finds the element linking to the target, spotlights it, and reports its position.
_HIGHLIGHT_JS = r"""
const [rawHref, targetUrl, selector, label] = arguments;
const old = document.getElementById('__crawler_hl'); if (old) old.remove();
const oldTag = document.getElementById('__crawler_hl_tag'); if (oldTag) oldTag.remove();
function norm(u) {
  try { const x = new URL(u, location.href); x.hash = ''; return x.href.replace(/\/$/, '').toLowerCase(); }
  catch (e) { return (u || '').toLowerCase(); }
}
function attrOf(e) {
  return e.getAttribute('href') || e.getAttribute('action') || e.getAttribute('data-href') ||
         e.getAttribute('data-url') || e.getAttribute('data-link') || '';
}
function visible(e) {
  const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
  return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
}
const target = norm(targetUrl);
const cands = Array.from(document.querySelectorAll('a[href],area[href],form[action],[data-href],[data-url],[data-link]'));
let matches = rawHref ? cands.filter(e => attrOf(e).trim() === rawHref) : [];
let how = 'href';
if (!matches.length) matches = cands.filter(e => norm(attrOf(e)) === target);
if (!matches.length && selector) {
  try { const s = document.querySelector(selector); if (s) { matches = [s]; how = 'selector'; } } catch (e) {}
}
if (!matches.length) return {found: false, page_height: document.documentElement.scrollHeight};
let el = matches.find(visible) || matches[0];
let hidden = !visible(el);
let shown = el;
if (hidden) {  // inside a collapsed menu: spotlight the nearest visible ancestor instead
  let p = el.parentElement;
  while (p && !visible(p)) p = p.parentElement;
  if (p && p !== document.body) shown = p;
}
shown.scrollIntoView({block: 'center', inline: 'nearest'});
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
    def __init__(
        self,
        out_dir: str,
        user_agent: str,
        timeout: int = 15,
        cookies: Optional[Dict[str, str]] = None,
        logger: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.out_dir = out_dir
        self.user_agent = user_agent
        self.timeout = max(5, min(timeout, 20))
        self.cookies = cookies or {}
        self.logger = logger or (lambda msg: None)
        self.cancel_event = cancel_event or threading.Event()
        self.driver = None
        self.unavailable_reason = ""

    def log(self, msg: str):
        self.logger(f"[Screenshots] {msg}")

    def start(self) -> bool:
        if not selenium_available():
            self.unavailable_reason = "Selenium is not installed on this server (pip install selenium)."
            return False
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--ignore-certificate-errors")
            options.add_argument("--hide-scrollbars")
            options.add_argument(f"user-agent={self.user_agent}")
            options.add_argument("--window-size=1366,860")
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(self.timeout)
            os.makedirs(self.out_dir, exist_ok=True)
            return True
        except Exception as e:
            self.unavailable_reason = f"Headless Chrome could not start: {str(e).splitlines()[0][:160]}"
            self.driver = None
            return False

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

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
            if (r.parent_url or "").startswith(("http://", "https://")):
                by_parent.setdefault(r.parent_url, []).append(r)

        total = len(by_parent) + len(issues)
        done = 0
        self._inject_cookies(issues[0].url)

        for parent_url, children in by_parent.items():
            if self.cancel_event.is_set():
                break
            self.log(f"Locating {len(children)} broken link(s) on {parent_url}")
            if self._load(parent_url):
                for r in children:
                    if self.cancel_event.is_set():
                        break
                    label = f"Broken link → {r.status_code or 'error'}"
                    try:
                        pos = self.driver.execute_script(
                            _HIGHLIGHT_JS, r.raw_href or "", r.url, r.link_selector or "", label
                        )
                    except Exception:
                        pos = None
                    if pos and pos.get("found"):
                        time.sleep(0.25)
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
            evidence[r.url]["error_shot"] = self._save(f"{self._slug(r.url)}_error")
            done += 1
            if on_progress:
                on_progress(done, total)

        return evidence

    # ------------------------------------------------------------------
    def _inject_cookies(self, any_url: str):
        if not self.cookies:
            return
        try:
            from .dynamic_extractor import inject_cookies
            p = urlparse(any_url)
            accepted = inject_cookies(self.driver, self.cookies, f"{p.scheme}://{p.netloc}")
            self.log(f"Using {accepted}/{len(self.cookies)} session cookies for logged-in screenshots.")
        except Exception as e:
            self.log(f"Cookie injection notice: {e}")

    def _load(self, url: str) -> bool:
        try:
            self.driver.get(url)
        except Exception:
            # Timed out or failed: stop loading and keep whatever rendered
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                return False
        time.sleep(0.8)
        return True

    def _save(self, name: str, focus: Optional[dict] = None) -> Optional[str]:
        """Save the viewport as JPEG. With `focus` (element rect in viewport px), crop a
        zoomed window around the element so the highlight stays readable when printed small."""
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
                crop_w, crop_h = min(img.width, int(820 * scale)), min(img.height, int(520 * scale))
                cx = (focus["x"] + focus["w"] / 2) * scale
                cy = (focus["y"] + focus["h"] / 2) * scale
                left = int(min(max(0, cx - crop_w / 2), img.width - crop_w))
                top = int(min(max(0, cy - crop_h / 2), img.height - crop_h))
                img = img.crop((left, top, left + crop_w, top + crop_h))
            if img.width > SHOT_WIDTH:
                img = img.resize((SHOT_WIDTH, int(img.height * SHOT_WIDTH / img.width)))
            img.save(path, "JPEG", quality=72, optimize=True)
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
