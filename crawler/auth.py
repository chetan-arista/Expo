import json
import time
from typing import Optional, Tuple, Dict, Set, Callable
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
from .config import AuthConfig
from .url_utils import normalize_url, is_internal_url, get_domain

class AuthHandler:
    """
    Handles authentication for login-protected websites:
    - Session Cookies (Option A - bypasses CAPTCHA, Turnstile, 2FA, SSO)
    - Standard Form Login (HTTP POST with CSRF detection)
    - Single-Page App (SPA) / Dynamic Form Login (Selenium Headless Chrome)
    Extracts post-login cookies, redirects, and member links for the crawler.
    """
    def __init__(
        self,
        auth_config: AuthConfig,
        user_agent: str,
        timeout: int = 15,
        verify_ssl: bool = False,
        logger: Optional[Callable[[str], None]] = None
    ):
        self.config = auth_config
        self.user_agent = user_agent
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.logger = logger or (lambda msg: None)

    def log(self, msg: str):
        self.logger(f"[Auth] {msg}")

    def authenticate(
        self, session: requests.Session, start_url: str
    ) -> Tuple[bool, str, Dict[str, str], Optional[str], Set[str]]:
        """
        Executes authentication for session cookies or username/password login.
        Returns: (success: bool, status_message: str, extracted_cookies: dict, post_login_url: Optional[str], initial_links: Set[str])
        """
        if not self.config.enabled:
            return True, "No authentication required (Public Crawl)", {}, None, set()

        # 1. Option A: Session Cookies Authentication (Bypasses Cloudflare Turnstile, CAPTCHA, 2FA)
        has_cookies = bool(self.config.cookies and self.config.cookies.strip())
        if self.config.auth_type == "cookies" or has_cookies:
            return self._apply_cookies(session, start_url)

        # 2. Form Login (Username / Email & Password)
        username = (self.config.username or "").strip()
        password = self.config.password or ""

        if not username or not password:
            return False, "Username/email and password (or session cookies) are required for authentication", {}, None, set()

        # Determine login URL
        login_url = (self.config.login_url or "").strip()
        if not login_url:
            parsed = urlparse(start_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            # Check standard login paths
            for path in ["/login", "/signin", "/sign-in", "/auth/login"]:
                test_url = urljoin(origin, path)
                try:
                    r = session.get(test_url, timeout=5, verify=self.verify_ssl)
                    if r.status_code == 200:
                        login_url = test_url
                        break
                except Exception:
                    pass
            if not login_url:
                login_url = start_url

        self.log(f"Attempting authentication at: {login_url}")

        # Try standard HTTP POST first
        http_success, http_msg, http_cookies, post_url, http_links = self._try_http_post_login(session, login_url, username, password)
        if http_success:
            return True, http_msg, http_cookies, post_url, http_links

        # If HTTP POST didn't succeed (common for Next.js, React, SPA forms), use Headless Chrome
        self.log("Standard HTTP form submission did not authenticate. Attempting Headless Browser login...")
        return self._try_headless_browser_login(session, login_url, username, password)

    def _apply_cookies(
        self, session: requests.Session, start_url: str
    ) -> Tuple[bool, str, Dict[str, str], Optional[str], Set[str]]:
        """
        Parses and injects user-provided session cookies into the requests.Session.
        Bypasses Cloudflare Turnstile, CAPTCHA, and SPA auth barriers.
        Probes start_url and common member portals to discover initial internal links.
        """
        raw = (self.config.cookies or "").strip()
        if not raw:
            return False, "No session cookies were provided", {}, None, set()

        cookies_dict: Dict[str, str] = {}

        # 1. Try JSON format first (e.g. from Cookie-Editor extension or browser DevTools)
        if raw.startswith("[") or raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and "name" in item and "value" in item:
                            cookies_dict[str(item["name"])] = str(item["value"])
                elif isinstance(parsed, dict):
                    cookies_dict = {str(k): str(v) for k, v in parsed.items()}
            except Exception as e:
                self.log(f"JSON cookie parse notice: {e}")

        # 2. Try standard HTTP Cookie string: "key1=val1; key2=val2"
        if not cookies_dict:
            for pair in raw.split(";"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k:
                        cookies_dict[k] = v

        if not cookies_dict:
            return False, "Could not parse provided cookie string. Please paste key=val pairs or JSON.", {}, None, set()

        base_dom = get_domain(start_url)
        parsed = urlparse(start_url)

        # Apply to session cookies & header
        for k, v in cookies_dict.items():
            session.cookies.set(k, v, domain=parsed.netloc)
            if base_dom:
                session.cookies.set(k, v, domain=f".{base_dom}")

        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
        session.headers["Cookie"] = cookie_header

        self.log(f"Injected {len(cookies_dict)} session cookies into crawler HTTP session.")

        # Extract initial authenticated member links by visiting start_url and common member portals
        initial_links: Set[str] = set()
        landing_url = start_url

        try:
            r = session.get(start_url, timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True)
            landing_url = str(r.url)
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = str(a.get("href") or "").strip()
                if href and not href.startswith("#") and not href.startswith("javascript:"):
                    clean_u = normalize_url(urljoin(landing_url, href))
                    if clean_u and is_internal_url(clean_u, base_dom):
                        initial_links.add(clean_u)
        except Exception as e:
            self.log(f"Notice probing start_url with cookies: {e}")

        # Probe member destinations if start_url was root
        for probe_path in ["/dashboard", "/account", "/predictions"]:
            probe_url = urljoin(start_url, probe_path)
            if probe_url not in initial_links:
                try:
                    pr = session.get(probe_url, timeout=5, verify=self.verify_ssl, allow_redirects=True)
                    if pr.status_code == 200 and "login" not in str(pr.url).lower():
                        initial_links.add(probe_url)
                        pr_soup = BeautifulSoup(pr.text, "html.parser")
                        for a in pr_soup.find_all("a", href=True):
                            href = str(a.get("href") or "").strip()
                            if href and not href.startswith("#") and not href.startswith("javascript:"):
                                clean_u = normalize_url(urljoin(str(pr.url), href))
                                if clean_u and is_internal_url(clean_u, base_dom):
                                    initial_links.add(clean_u)
                except Exception:
                    pass

        self.log(f"Session cookies validated. Discovered {len(initial_links)} initial member links.")
        return True, f"Loaded {len(cookies_dict)} session cookies ({len(initial_links)} member links discovered)", cookies_dict, landing_url, initial_links

    def _try_http_post_login(
        self, session: requests.Session, login_url: str, username: str, password: str
    ) -> Tuple[bool, str, Dict[str, str], Optional[str], Set[str]]:
        try:
            r = session.get(login_url, timeout=self.timeout, verify=self.verify_ssl)
            if r.status_code != 200:
                return False, f"Login page returned HTTP {r.status_code}", {}, None, set()

            soup = BeautifulSoup(r.text, "html.parser")
            form = soup.find("form")
            if not form or not form.get("action"):
                return False, "No standard HTML form action found on login page (likely an SPA)", {}, None, set()

            action = str(form.get("action") or "")
            post_url = urljoin(login_url, action)

            payload = {}
            user_field_found = False
            pass_field_found = False

            for inp in form.find_all("input"):
                name = str(inp.get("name") or "")
                if not name:
                    continue
                itype = str(inp.get("type") or "text").lower()
                val = str(inp.get("value") or "")

                if itype == "password" and not pass_field_found:
                    payload[name] = password
                    pass_field_found = True
                elif itype in ("text", "email") and not user_field_found and any(w in name.lower() for w in ["user", "mail", "login", "id"]):
                    payload[name] = username
                    user_field_found = True
                else:
                    payload[name] = val

            if not user_field_found or not pass_field_found:
                return False, "Could not map username and password fields in HTML form", {}, None, set()

            resp = session.post(post_url, data=payload, timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True)
            cookies_dict: Dict[str, str] = {str(k): str(v) for k, v in session.cookies.get_dict().items() if v is not None}

            if len(cookies_dict) > 0 and (resp.url != login_url or resp.status_code in (200, 302)):
                self.log(f"HTTP form login successful. {len(cookies_dict)} session cookies obtained.")
                cookie_header = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
                session.headers["Cookie"] = cookie_header

                # Extract links from authenticated response
                links = set()
                try:
                    resp_soup = BeautifulSoup(resp.text, "html.parser")
                    base_dom = get_domain(str(resp.url))
                    for a in resp_soup.find_all("a", href=True):
                        href = str(a.get("href") or "").strip()
                        if href and not href.startswith("#") and not href.startswith("javascript:"):
                            clean_u = normalize_url(urljoin(str(resp.url), href))
                            if clean_u and is_internal_url(clean_u, base_dom):
                                links.add(clean_u)
                except Exception:
                    pass
                return True, "HTTP form authentication succeeded", cookies_dict, str(resp.url), links

        except Exception as e:
            self.log(f"HTTP POST login attempt notice: {e}")

        return False, "HTTP POST login did not complete", {}, None, set()

    def _try_headless_browser_login(
        self, session: requests.Session, login_url: str, username: str, password: str
    ) -> Tuple[bool, str, Dict[str, str], Optional[str], Set[str]]:
        driver = None
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.by import By

            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--ignore-certificate-errors")
            options.add_argument(f"user-agent={self.user_agent}")
            options.add_argument("--window-size=1920,1080")

            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(self.timeout)

            self.log(f"Loading login page in Headless Chrome: {login_url}")
            driver.get(login_url)
            time.sleep(1.5)

            # Check for CAPTCHA / Cloudflare challenge
            page_src = driver.page_source.lower()
            if any(term in page_src for term in ["cf-challenge", "hcaptcha", "g-recaptcha", "recaptcha", "turnstile", "datadome", "challenges.cloudflare.com"]):
                self.log("CAPTCHA / Cloudflare Turnstile bot protection detected on login page.")
                return False, "Login blocked: Website is protected by Cloudflare Turnstile / CAPTCHA. Please use Option A (Session Cookies) from your browser to crawl member pages.", {}, None, set()

            # 1. Locate email/username input
            user_input = None
            for sel in [
                "#login-email", "input[type='email']", "input[name='email']",
                "input[name='username']", "input[name='user']", "input[name='login']",
                "input[placeholder*='email' i]", "input[placeholder*='user' i]", "input[type='text']"
            ]:
                try:
                    elems = driver.find_elements(By.CSS_SELECTOR, sel)
                    if elems and elems[0].is_displayed():
                        user_input = elems[0]
                        break
                except Exception:
                    pass

            if not user_input:
                return False, "Could not locate email/username input field on page", {}, None, set()

            user_input.clear()
            user_input.send_keys(username)

            # 2. Locate password input
            pass_input = None
            for sel in ["#login-password", "input[type='password']", "input[name='password']", "input[name='pass']"]:
                try:
                    elems = driver.find_elements(By.CSS_SELECTOR, sel)
                    if elems and elems[0].is_displayed():
                        pass_input = elems[0]
                        break
                except Exception:
                    pass

            if not pass_input:
                return False, "Could not locate password input field on page", {}, None, set()

            pass_input.clear()
            pass_input.send_keys(password)

            # 3. Locate and click submit button. Prefer the one inside the password's own form:
            #    pages often have look-alike "Sign in" / "Create account" tab buttons above the form.
            submit_btn = None
            try:
                form = pass_input.find_element(By.XPATH, "./ancestor::form")
                for el in form.find_elements(By.CSS_SELECTOR, "button[type='submit'], input[type='submit'], button:not([type])"):
                    txt = (el.text or el.get_attribute("value") or "").lower()
                    if el.is_displayed() and not any(w in txt for w in ("google", "github", "microsoft", "apple", "create", "sign up", "register")):
                        submit_btn = el
                        break
            except Exception:
                pass
            for sel in ([] if submit_btn else [
                "button[type='submit']", "input[type='submit']",
                "button.btn-primary", "button.submit"
            ]):
                try:
                    elems = driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in elems:
                        txt = (el.text or "").lower()
                        if "google" not in txt and el.is_displayed():
                            submit_btn = el
                            break
                    if submit_btn:
                        break
                except Exception:
                    pass

            if not submit_btn:
                for b in driver.find_elements(By.TAG_NAME, "button"):
                    txt = (b.text or "").lower()
                    if any(w in txt for w in ["sign in", "log in", "login"]) and "google" not in txt:
                        submit_btn = b
                        break

            if submit_btn:
                try:
                    submit_btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", submit_btn)
            else:
                pass_input.submit()

            self.log("Credentials submitted. Waiting for authentication response...")

            # DYNAMIC POLLING: Wait up to 10 seconds for login redirect or auth cookies
            start_wait = time.time()
            login_verified = False
            post_login_url = None
            cookies_dict = {}

            while time.time() - start_wait < 10.0:
                time.sleep(0.5)
                curr_url = driver.current_url
                browser_cookies = driver.get_cookies()
                cookies_dict = {c["name"]: c["value"] for c in browser_cookies}

                # Check for explicit error messages
                error_elems = driver.find_elements(By.CSS_SELECTOR, "[role='alert'], .error, .alert-danger, .text-red-500, [class*='error'], [class*='alert']")
                err_text = ""
                for el in error_elems:
                    try:
                        if el.is_displayed() and el.text:
                            err_text = el.text.strip()
                            break
                    except Exception:
                        pass

                if err_text:
                    self.log(f"Login rejected by website: {err_text}")
                    return False, f"Authentication failed: {err_text}", cookies_dict, None, set()

                # If URL changed away from login, or auth token/cookies appeared
                has_auth_cookie = any(k.lower() in [
                    "sb-access-token", "token", "session", "jwt", "auth", "connect.sid", "user", "logged_in"
                ] for k in cookies_dict)
                if (curr_url != login_url and "login" not in curr_url.lower()) or has_auth_cookie:
                    login_verified = True
                    post_login_url = curr_url
                    break

            if not login_verified:
                curr_url = driver.current_url
                if curr_url == login_url or "login" in curr_url.lower():
                    return False, "Submitted credentials but could not confirm successful login. Check email/password.", cookies_dict, None, set()
                post_login_url = curr_url

            # Transfer all cookies to requests.Session
            for c in driver.get_cookies():
                session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
            session.headers["Cookie"] = cookie_header

            # Extract ALL internal links visible on the authenticated landing page directly from browser DOM
            authenticated_links = set()
            try:
                time.sleep(1.0)
                page_src = driver.page_source
                soup = BeautifulSoup(page_src, "html.parser")
                base_dom = get_domain(post_login_url or login_url)
                for a in soup.find_all("a", href=True):
                    href = str(a.get("href") or "").strip()
                    if href and not href.startswith("#") and not href.startswith("javascript:"):
                        full_u = urljoin(post_login_url or login_url, href)
                        clean_u = normalize_url(full_u)
                        if clean_u and is_internal_url(clean_u, base_dom):
                            authenticated_links.add(clean_u)
                self.log(f"Discovered {len(authenticated_links)} authenticated internal links on landing page ({post_login_url})")
            except Exception as e:
                self.log(f"Error extracting post-login links: {e}")

            return True, f"Authentication verified successfully ({len(cookies_dict)} session cookies, {len(authenticated_links)} links)", cookies_dict, post_login_url, authenticated_links

        except ImportError:
            self.log("Selenium is not installed on this server environment.")
            return False, "Browser engine (Selenium) not available on this server. Please use Option A (Session Cookies) in the Authentication tab to crawl protected member pages.", {}, None, set()
        except Exception as e:
            self.log(f"Headless browser login error: {e}")
            return False, f"Browser login failed: {e}. Please use Option A (Session Cookies) from your browser to crawl member pages.", {}, None, set()
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
