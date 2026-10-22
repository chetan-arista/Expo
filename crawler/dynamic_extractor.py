import threading
import time
from typing import Set, Tuple, Dict, Optional, Callable, Any

from .url_utils import normalize_url, is_internal_url, is_excluded_path, get_domain


def browser_cookie(name: str, value: str, origin: str) -> Dict[str, Any]:
    """Selenium cookie for the page currently loaded at `origin`.
    Host-only (no Domain attribute) so Chrome accepts `__Host-` cookies, and Secure on https or
    whenever the name prefix demands it (`__Secure-next-auth.session-token` etc.); Chrome silently
    drops prefixed cookies that break these rules."""
    secure = origin.startswith("https://") or name.startswith(("__Secure-", "__Host-"))
    return {"name": name, "value": value, "path": "/", "secure": secure}


def inject_cookies(driver, cookies: Dict[str, str], origin: str) -> int:
    """Loads `origin` and adds each cookie; returns how many Chrome accepted."""
    driver.get(origin.rstrip("/") + "/")
    accepted = 0
    for name, value in cookies.items():
        try:
            driver.add_cookie(browser_cookie(name, value, origin))
            accepted += 1
        except Exception:
            pass
    return accepted

class DynamicExtractor:
    """
    Headless Chrome browser driver to discover links rendered dynamically
    by JavaScript frameworks (React, Angular, Vue, Next.js, SPA menus, lazy loading).
    """
    def __init__(
        self,
        base_domain: str,
        user_agent: str,
        timeout: int = 15,
        cookies: Optional[Dict[str, str]] = None,
        logger: Optional[Callable[[str], None]] = None,
        origin: Optional[str] = None
    ):
        self.base_domain = base_domain
        self.origin = origin or f"https://{base_domain}"
        # One Chrome instance is shared by all crawl workers; WebDriver is not thread-safe
        self._lock = threading.Lock()
        self.user_agent = user_agent
        self.timeout = timeout
        self.cookies = cookies or {}
        self._cookies_injected = False
        self.logger = logger or (lambda msg: None)
        self.driver = None
        self._init_driver()

    def log(self, message: str):
        self.logger(f"[Dynamic Browser] {message}")

    def _init_driver(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--ignore-certificate-errors")
            options.add_argument(f"user-agent={self.user_agent}")
            options.add_argument("--window-size=1920,1080")
            options.page_load_strategy = "eager"

            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(self.timeout)
            self.log("Headless Chrome initialized successfully for dynamic link discovery.")
        except Exception as e:
            self.log(f"Headless Chrome could not be initialized: {e}. Falling back to standard requests.")
            self.driver = None

    def render_and_extract(
        self,
        url: str,
        include_subdomains: bool = False,
        exclude_patterns: Optional[list] = None,
        collect_external: bool = True
    ) -> Tuple[str, str, Dict[str, Dict[str, str]], Dict[str, Dict[str, Any]]]:
        """
        Loads page in headless browser, scrolls down to trigger lazy loading,
        extracts rendered title, dynamic page HTML, and all rendered internal and external links.
        Returns: (title, rendered_html, {internal_url: element_meta}, dict_of_external_links)
        """
        if not self.driver:
            return "", "", {}, {}
        with self._lock:
            return self._render_and_extract(url, include_subdomains, exclude_patterns, collect_external)

    def _render_and_extract(self, url, include_subdomains, exclude_patterns, collect_external):
        # Inject session cookies on initial navigation if available
        if self.cookies and not self._cookies_injected:
            self._cookies_injected = True
            try:
                accepted = inject_cookies(self.driver, self.cookies, self.origin)
                self.log(f"Injected {accepted}/{len(self.cookies)} authenticated session cookies into Headless Chrome.")
            except Exception as e:
                self.log(f"Session cookie injection into Chrome notice: {e}")

        discovered_internal: Dict[str, Dict[str, str]] = {}
        discovered_external: Dict[str, Dict[str, Any]] = {}
        rendered_title = ""
        rendered_html = ""
        exclude_patterns = exclude_patterns or []

        try:
            self.driver.get(url)
            time.sleep(1.0)

            # Auto-scroll to trigger lazy loading elements
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
            time.sleep(0.5)
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.5)

            rendered_title = (self.driver.title or "").strip()
            rendered_html = self.driver.page_source or ""

            # Extract all anchor tags and text resolved directly by browser DOM
            dom_links = self.driver.execute_script("""
                const items = [];
                document.querySelectorAll('a[href]').forEach(a => {
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
            """) or []

            for item in dom_links:
                raw = item.get("href")
                norm = normalize_url(raw, base_url=url)
                if not norm:
                    continue

                if is_internal_url(norm, self.base_domain, include_subdomains=include_subdomains):
                    if not is_excluded_path(norm, exclude_patterns) and norm not in discovered_internal:
                        txt = (item.get("text") or "").strip()[:100]
                        discovered_internal[norm] = {
                            "anchor_text": txt,
                            "tag": "a",
                            "region": "Rendered by JavaScript",
                            "selector": "",
                            "html": f'<a href="{(item.get("raw") or "")[:80]}">{txt[:60]}</a>',
                            "raw_href": item.get("raw") or "",
                        }
                elif collect_external:
                    if norm not in discovered_external:
                        txt = (item.get("text") or "").strip()[:100]
                        discovered_external[norm] = {
                            "url": norm,
                            "domain": get_domain(norm),
                            "parent_url": url,
                            "anchor_text": txt or "-",
                            "discovery_source": "Dynamic JS DOM"
                        }

        except Exception as e:
            self.log(f"Error rendering {url} with dynamic browser: {e}")

        return rendered_title, rendered_html, discovered_internal, discovered_external

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
