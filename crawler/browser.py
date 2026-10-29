"""
Headless Chrome for the whole crawler: the password login, the JavaScript renderer, evidence screenshots,
the live login window and later features all start, share and log in Chrome through these helpers.

- new_chrome / close_chrome: one place that starts Chrome (server-friendly flags, an optional cap on
  instances via MAX_CHROME_INSTANCES and a free-memory guard for small containers).
- export_session / import_session: a login as data (full cookies plus localStorage/sessionStorage), so a
  second Chrome or the requests.Session can carry the same login.
- block_dangerous: the browser refuses sign-out URLs and links that change data (admin actions, token links)
  at the network level, so a crawl never logs itself out or edits the site.
- SharedBrowser: the crawl's one logged-in Chrome, handed out with hold()/lease().
- DevToolsTap: a second DevTools connection to a Chrome tab for the events Selenium cannot receive (Chrome
  issues, network failures, navigation reasons), request blocking (robots.txt during rendering) and proxy
  sign-in.
- proxy_server_arg / new_chrome(proxy=...): Chrome goes through the crawl's proxy; its user name and password
  are answered from memory through the tap (never written to disk).
- hide_overlays / restore_overlays: cookie-consent and newsletter pop-ups hidden for a screenshot only.

Ported from the sibling project's browser.py (memory guard, slots) and extended for logged-in crawling.
"""
import atexit
import json
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from .url_utils import get_domain
from .net_guard import HOST_ONLY_COOKIE, HostOnlyCookiePolicy

# Optional cap on headless Chromes running at once in this process (set on the hosted server, where memory
# is small). Unset or 0 = no limit, as on a local machine.
MAX_CHROME_INSTANCES = int(os.environ.get("MAX_CHROME_INSTANCES") or 0)
_slots = threading.BoundedSemaphore(MAX_CHROME_INSTANCES) if MAX_CHROME_INSTANCES > 0 else None
LOW_MEMORY = _slots is not None

# Chrome needs a few hundred MB; below this much free memory it is not started (the container would be killed)
CHROME_MIN_FREE_MB = int(os.environ.get("CHROME_MIN_FREE_MB") or 300)
# Below this much free memory while Chrome runs, the watchdog closes Chrome before the container is killed
CHROME_KILL_FREE_MB = int(os.environ.get("CHROME_KILL_FREE_MB") or 60)

# Requests the crawl's browser never sends: following one would end the logged-in session, or change data on
# the site for the account that is logged in (WordPress admin row actions such as ?action=trash or
# plugins.php?action=deactivate are plain links carrying a _wpnonce token; see url_utils.changes_data_url)
DANGEROUS_URL_PATTERNS = [
    "*logout*", "*signout*", "*sign-out*", "*log-out*", "*/api/auth/signout*",
    "*sign_out*", "*log_out*", "*logoff*", "*log-off*",
    "*_wpnonce=*", "*/wp-admin/post.php*", "*/wp-admin/plugins.php?*", "*/wp-admin/comment.php*",
    "*/wp-admin/update.php*", "*/wp-admin/users.php?*", "*/wp-admin/edit.php?*action=*",
    "*/wp-admin/upload.php?*action=*", "*/wp-admin/themes.php?*action=*", "*/form_key/*", "*form_key=*",
    "*remove_item=*", "*cancel_order=*", "*/cart/clear*", "*/cart/change?*",
]
# Requests blocked while rendering for link discovery (not for screenshots): much of a page's memory
MEDIA_PATTERNS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.avif", "*.svg", "*.ico", "*.bmp",
    "*.mp4", "*.webm", "*.mov", "*.mp3", "*.wav", "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
    "*/_next/image*",
]

_live_drivers: set = set()
_watchdog_started = False
_watchdog_lock = threading.Lock()
_tls = threading.local()
_binary_cache: Optional[str] = None
_binary_note = ""               # why CHROME_BIN was not used (chrome_binary)
_last_start_error = ""          # the last failed Chrome start, cleared by a successful one (chrome_status)


class ChromeBusy(Exception):
    """All Chrome slots are in use."""


class ChromeLowMemory(ChromeBusy):
    """Not enough free memory on the server to start Chrome safely."""


def chrome_error() -> str:
    """Why the last new_chrome() call in this thread returned None ('' after a success)."""
    return getattr(_tls, "error", "")


def _set_error(msg: str):
    _tls.error = msg


def _selenium_installed() -> bool:
    try:
        import selenium  # noqa: F401
        return True
    except ImportError:
        return False


def chrome_binary() -> str:
    """Path of the Chrome/Chromium that new_chrome() starts and chrome_available() checks, or '' when none is
    found. CHROME_BIN wins when that file exists. A CHROME_BIN that points to a missing file is reported
    (chrome_binary_note(), printed once) and the usual install locations are searched instead."""
    global _binary_cache, _binary_note
    if _binary_cache is not None:
        return _binary_cache
    found, note = "", ""
    env = os.environ.get("CHROME_BIN", "").strip()
    if env and os.path.isfile(env):
        found = env
    elif env:
        note = f"CHROME_BIN points to {env}, which does not exist"
    if not found:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
            path = shutil.which(name)
            if path:
                found = path
                break
    if not found:
        candidates = [
            os.path.join(os.environ.get(var, ""), "Google", "Chrome", "Application", "chrome.exe")
            for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if os.environ.get(var)
        ] + ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/chromium"]
        found = next((p for p in candidates if os.path.exists(p)), "")
    if note:
        note += f"; using {found} instead. Fix or remove CHROME_BIN." if found else \
            "; no other Chrome or Chromium was found either. Fix or remove CHROME_BIN."
        print(f"[browser] {note}", flush=True)
    _binary_cache, _binary_note = found, note
    return found


def chrome_binary_note() -> str:
    """'' or why chrome_binary() did not use CHROME_BIN (it points to a missing file)."""
    chrome_binary()
    return _binary_note


def _chrome_off() -> bool:
    return os.environ.get("CRAWLER_NO_CHROME", "").strip().lower() in ("1", "true", "yes")


def chrome_available() -> bool:
    """True when Selenium is installed and a Chrome/Chromium binary exists (CRAWLER_NO_CHROME=1 turns it off)."""
    if _chrome_off():
        return False
    return _selenium_installed() and bool(chrome_binary())


def chrome_status() -> Tuple[bool, str]:
    """(usable, message) for a status page, without starting a browser. The message says why Chrome cannot be
    used, or warns while it can (a CHROME_BIN or CHROMEDRIVER_PATH that points nowhere). A start that failed
    since the last successful one (a missing driver, a crash on start-up) counts as not usable."""
    if _chrome_off():
        return False, "Chrome is turned off on this server (CRAWLER_NO_CHROME=1)."
    if not _selenium_installed():
        return False, "Selenium is not installed on this server (pip install selenium)."
    if not chrome_binary():
        return False, chrome_binary_note() or "No Chrome or Chromium was found on this server (install Chrome, or set CHROME_BIN to its path)."
    driver = os.environ.get("CHROMEDRIVER_PATH", "").strip()
    notes = [chrome_binary_note(),
             f"CHROMEDRIVER_PATH points to {driver}, which does not exist; Selenium Manager looks for a driver instead."
             if driver and not os.path.isfile(driver) else ""]
    with _watchdog_lock:
        failed = _last_start_error
    if failed:
        return False, " ".join([failed] + [n for n in notes if n and n not in failed])
    return True, " ".join(n for n in notes if n)


def memory_headroom_mb() -> Optional[int]:
    """Free memory in this container in MB (cgroup limit minus memory in use, not counting reclaimable file
    cache), or None when there is no container limit to read (a Windows or macOS PC)."""
    for limit_f, usage_f, stat_f, cache_key in (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat", "inactive_file"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.stat", "total_inactive_file"),
    ):
        try:
            with open(limit_f) as f:
                raw = f.read().strip()
            if raw == "max" or int(raw) > 1 << 50:
                continue
            with open(usage_f) as f:
                usage = int(f.read().strip())
            try:
                with open(stat_f) as f:
                    stat = dict(line.split() for line in f if line.strip())
                usage -= int(stat.get(cache_key, 0))
            except (OSError, ValueError):
                pass
            return (int(raw) - usage) // 2**20
        except (OSError, ValueError):
            continue
    return None


def _memory_watchdog():
    """Server only: closes Chrome when free memory gets dangerously low, before the container is killed."""
    while True:
        time.sleep(0.5)
        if not _live_drivers:
            continue
        free = memory_headroom_mb()
        if free is not None and free < CHROME_KILL_FREE_MB:
            for driver in list(_live_drivers):
                print(f"[browser] Only {free} MB free: closing Chrome to keep the server alive", flush=True)
                close_chrome(driver)


def _start_watchdog():
    global _watchdog_started
    with _watchdog_lock:
        if not _watchdog_started and memory_headroom_mb() is not None:
            threading.Thread(target=_memory_watchdog, daemon=True, name="chrome-memory-watchdog").start()
            _watchdog_started = True


_PROXY_HOST = re.compile(r"^[A-Za-z0-9.\-\[\]:_]+$")


def proxy_server_arg(proxy: Optional[Dict[str, Any]]) -> str:
    """The --proxy-server value for the crawl's proxy setting ({"enabled", "host", "port", ...}), or '' when
    the proxy is off or its host is not a plain host name. A host written with a scheme (socks5://, http://)
    keeps it; otherwise Chrome treats it as an HTTP proxy for every scheme."""
    if not isinstance(proxy, dict) or not proxy.get("enabled") or not str(proxy.get("host") or "").strip():
        return ""
    host = str(proxy.get("host")).strip()
    scheme = ""
    if "://" in host:
        scheme, host = host.split("://", 1)
        scheme = scheme.lower()
        if scheme not in ("http", "https", "socks4", "socks5"):
            return ""
    host = host.strip("/")
    if ":" in host and not host.startswith("["):     # "host:port" written in the host box
        host, _, port_txt = host.rpartition(":")
        port = port_txt
    else:
        port = proxy.get("port") or 3128
    try:
        port = int(port)
    except (TypeError, ValueError):
        return ""
    if not host or not _PROXY_HOST.match(host) or not 0 < port < 65536:
        return ""
    return f"{scheme + '://' if scheme else ''}{host}:{port}"


def proxy_credentials(proxy: Optional[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """(user name, password) of an enabled proxy that has a user name, else None."""
    if not proxy_server_arg(proxy) or not str(proxy.get("username") or "").strip():
        return None
    return str(proxy.get("username")).strip(), str(proxy.get("password") or "")


def new_chrome(user_agent: str = "", timeout: int = 30, eager: bool = True, interactive: bool = False,
               width: int = 1366, height: int = 900, wait: float = 0.0, profile_dir: str = "",
               console_log: bool = False, proxy: Optional[Dict[str, Any]] = None, page_cache: bool = True):
    """
    Starts a headless Chrome and returns the Selenium driver, or None (chrome_error() says why).
    CHROME_BIN / CHROMEDRIVER_PATH point to a system Chromium on servers; locally Selenium Manager finds
    Chrome. With a Chrome cap, waits up to `wait` seconds for a free slot. `interactive` is for a browser a
    person drives (the live login window). `profile_dir` keeps cookies, storage and IndexedDB on disk so a
    restarted Chrome stays logged in. `console_log` keeps every console message level for get_log("browser")
    (by default ChromeDriver keeps warnings and errors only). `proxy` is the crawl's proxy setting: Chrome
    connects through it; a proxy user name and password are given by attach_tap(...).set_proxy_auth().
    `page_cache` False turns Chrome's back/forward cache off: a browser that only ever opens the next page (the
    render pool) then frees each page when it moves on, instead of keeping the last ones alive (with everything
    their scripts, and axe-core, left in memory). Close it with close_chrome() so its slot is released.
    """
    global _last_start_error
    _set_error("")
    if not _selenium_installed():
        _set_error("Selenium is not installed on this server (pip install selenium).")
        return None
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--hide-scrollbars")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    if user_agent:
        options.add_argument(f"user-agent={user_agent}")
    options.add_argument(f"--window-size={int(width)},{int(height)}")
    if profile_dir:
        options.add_argument(f"--user-data-dir={profile_dir}")
    proxy_arg = proxy_server_arg(proxy)
    if proxy_arg:
        options.add_argument(f"--proxy-server={proxy_arg}")
    if console_log:
        options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    if interactive:
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
    if LOW_MEMORY:
        # Small server: fewer helper processes, no background work, one renderer, small caches and JS heap
        for arg in ("--disable-extensions", "--disable-background-networking", "--disable-component-update",
                    "--disable-features=Translate,MediaRouter,OptimizationHints,site-per-process,"
                    "IsolateOrigins,BackForwardCache",
                    "--disable-site-isolation-trials", "--renderer-process-limit=1",
                    "--disk-cache-size=1048576", "--disable-software-rasterizer",
                    "--js-flags=--max-old-space-size=256"):
            options.add_argument(arg)
    elif not page_cache:
        # (the low-memory flags above already include it: Chrome reads only the last --disable-features)
        options.add_argument("--disable-features=BackForwardCache")
    if eager:
        options.page_load_strategy = "eager"
    # The Chrome chrome_available() found, never a CHROME_BIN that points to a missing file (Selenium would fail
    # with "Unable to obtain driver"). '' = none found: Selenium Manager looks for one itself.
    binary = chrome_binary()
    if binary:
        options.binary_location = binary

    if LOW_MEMORY:
        free = memory_headroom_mb()
        if free is not None and free < CHROME_MIN_FREE_MB:
            _set_error(f"Only {free} MB of memory free on this server; the browser needs about {CHROME_MIN_FREE_MB} MB.")
            return None
    if _slots:
        got = _slots.acquire(timeout=wait) if wait > 0 else _slots.acquire(blocking=False)
        if not got:
            _set_error("The server's browser is busy with another task.")
            return None
    try:
        driver_path = os.environ.get("CHROMEDRIVER_PATH")
        if driver_path and not os.path.exists(driver_path):
            driver_path = None        # a wrong path: let Selenium Manager find a matching driver
        driver = webdriver.Chrome(options=options, service=Service(driver_path) if driver_path else None)
        driver.set_page_load_timeout(timeout)
    except Exception as e:
        if _slots:
            _slots.release()
        first = (str(e).strip().splitlines() or [type(e).__name__])[0][:200]
        note = chrome_binary_note()
        _set_error(f"Headless Chrome could not start: {first}" + (f" ({note})" if note else ""))
        with _watchdog_lock:
            _last_start_error = chrome_error()
        return None
    with _watchdog_lock:
        _last_start_error = ""
        _live_drivers.add(driver)
    try:
        driver._crawler_console_log = bool(console_log)
        driver._crawler_proxy = proxy_arg
    except Exception:
        pass
    if LOW_MEMORY:
        _start_watchdog()
    return driver


def chrome_alive(driver) -> bool:
    """False once the driver was closed (by its owner or by the memory watchdog)."""
    return driver is not None and driver in _live_drivers


def close_chrome(driver):
    """Quits the driver and frees its slot (once, even if the memory watchdog closed it already)."""
    if driver is None:
        return
    with _watchdog_lock:
        if driver not in _live_drivers:
            return
        _live_drivers.discard(driver)
    tap = getattr(driver, "_crawler_tap", None)
    if tap is not None:
        tap.close()
    try:
        driver.quit()
    except Exception:
        pass
    finally:
        if _slots:
            _slots.release()


# Profile folders made by SharedBrowser (they hold the site's login cookies): removed on close, on a switch to
# a fresh profile and, whatever is left, when the process exits
PROFILE_PREFIX = "crawler-chrome-"
STALE_PROFILE_HOURS = 12      # leftovers of earlier runs (a killed process) older than this are removed
_profile_dirs: set = set()
_profile_lock = threading.Lock()
_stale_swept = False


def remove_profile(path: str, tries: int = 3) -> bool:
    """Deletes a Chrome profile folder (retried briefly: Chrome may still hold its files for a moment after
    quitting). True when it is gone."""
    if not path:
        return True
    for attempt in range(tries):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            with _profile_lock:
                _profile_dirs.discard(path)
            return True
        if attempt + 1 < tries:
            time.sleep(0.5)
    return False


def _sweep_stale_profiles():
    """Once per process: removes crawler-chrome-* folders that earlier runs left in the temp folder (a stopped
    server or a crash) and nobody has touched for STALE_PROFILE_HOURS."""
    global _stale_swept
    with _profile_lock:
        if _stale_swept:
            return
        _stale_swept = True
    try:
        root = tempfile.gettempdir()
        cutoff = time.time() - STALE_PROFILE_HOURS * 3600
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not name.startswith(PROFILE_PREFIX) or not os.path.isdir(path) or path in _profile_dirs:
                continue
            stamps = [os.path.getmtime(p) for p in (path, os.path.join(path, "Local State"),
                                                     os.path.join(path, "Default", "Network", "Cookies"))
                      if os.path.exists(p)]
            if stamps and max(stamps) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def new_profile_dir() -> str:
    """A new, tracked profile folder for Chrome ('' when the temp folder is not writable)."""
    if not _stale_swept:
        threading.Thread(target=_sweep_stale_profiles, daemon=True, name="chrome-profile-sweep").start()
    try:
        path = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
    except Exception:
        return ""
    with _profile_lock:
        _profile_dirs.add(path)
    return path


@atexit.register
def _close_all():
    """No Chrome outlives the process (a crawl's browser waits a minute after the crawl before closing), and
    no profile folder holding login cookies stays on disk after it."""
    for driver in list(_live_drivers):
        close_chrome(driver)
    for path in list(_profile_dirs):
        remove_profile(path)


# ----------------------------------------------------------------------------------------------------
# Sessions: cookies with every attribute plus web storage
# ----------------------------------------------------------------------------------------------------
_SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}


def _origin(url: str) -> str:
    p = urlparse(url or "")
    return f"{p.scheme}://{p.netloc}" if p.scheme in ("http", "https") and p.netloc else ""


def _site_cookie(domain: str, base: str) -> bool:
    """The site's own cookie, a subdomain's or a parent domain's (not third-party analytics or CAPTCHA)."""
    d = (domain or "").lstrip(".").lower()
    if d.startswith("www."):
        d = d[4:]
    return not base or d == base or d.endswith("." + base) or base.endswith("." + d)


# Field names cookie exporters use (DevTools, Selenium, Puppeteer/Playwright, Cookie-Editor, EditThisCookie,
# Cookie Quick Manager...), compared without case, spaces, dashes or underscores
_COOKIE_KEYS = {
    "name": ("name", "key", "nameraw", "cookiename"),
    "value": ("value", "contentraw", "content", "cookievalue"),
    "domain": ("domain", "host", "hostraw", "hostname"),
    "path": ("path", "pathraw"),
    "secure": ("secure", "issecure"),
    "httpOnly": ("httponly", "ishttponly", "httponlyraw"),
    "hostOnly": ("hostonly", "ishostonly"),
    "sameSite": ("samesite", "samesiteraw"),
    "expires": ("expires", "expiry", "expirationdate", "expiresraw", "expire", "expiresutc", "expirestimestamp"),
    "session": ("session", "issession"),
}
_TRUE_WORDS = {"true", "1", "yes", "y", "on", "✓", "✔", "x"}
_DATE_FORMATS = ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d-%b-%Y %H:%M:%S %Z", "%A, %d-%b-%y %H:%M:%S %Z",
                 "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _flag(value: Any) -> bool:
    """A yes/no field as exporters write it (true, "TRUE", 1, a tick mark)."""
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_WORDS
    return bool(value)


def cookie_expiry(value: Any) -> Optional[float]:
    """A cookie expiry as Unix seconds, from seconds, milliseconds, an ISO 8601 date ("2026-11-01T10:00:00.000Z",
    as the DevTools cookie table shows it) or an HTTP date. None for a session cookie (0, -1, "Session", "")."""
    import datetime as _dt
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("session", "-1", "0", "none", "null"):
            return None
        try:
            value = float(text)
        except ValueError:
            stamp = None
            try:
                stamp = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                for fmt in _DATE_FORMATS:
                    try:
                        stamp = _dt.datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue
            if stamp is None:
                return None
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=_dt.timezone.utc)
            return stamp.timestamp()
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    value = float(value)
    if value > 1e14:         # microseconds
        value /= 1e6
    elif value > 1e11:       # milliseconds (JavaScript Date.now())
        value /= 1e3
    return value


def _cookie_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    """`item` with its keys mapped onto the names normalize_cookie reads (first exporter spelling wins)."""
    plain = {re.sub(r"[\s_\-]", "", str(k)).lower(): v for k, v in item.items()}
    out: Dict[str, Any] = {}
    for field, names in _COOKIE_KEYS.items():
        for n in names:
            if n in plain:
                out[field] = plain[n]
                break
    return out


def normalize_cookie(item: Dict[str, Any], default_domain: str = "") -> Optional[Dict[str, Any]]:
    """One cookie in the full format used everywhere here (Chrome DevTools style), from a DevTools,
    Selenium, Playwright, Cookie-Editor or EditThisCookie export (field names compared without case).
    A leading dot in `domain` means a domain cookie; no dot, host-only. The value is kept as given and
    never logged by callers."""
    if not isinstance(item, dict):
        return None
    f = _cookie_fields(item)
    if "name" not in f or "value" not in f or f["name"] is None:
        return None
    domain = str(f.get("domain") or default_domain or "").strip()
    if "://" in domain:                       # Cookie Quick Manager writes "https://.example.com/"
        domain = domain.split("://", 1)[1].split("/", 1)[0]
    host_only = f.get("hostOnly")
    if host_only is not None and not isinstance(host_only, bool):
        host_only = _flag(host_only)
    if host_only is False and domain and not domain.startswith("."):
        domain = "." + domain
    elif host_only is True:
        domain = domain.lstrip(".")
    value = f["value"]
    ck: Dict[str, Any] = {
        "name": str(f["name"]).strip(), "value": "" if value is None else str(value), "domain": domain,
        "path": str(f.get("path") or "/").strip() or "/", "secure": _flag(f.get("secure")),
        "httpOnly": _flag(f.get("httpOnly")),
    }
    same_site = _SAME_SITE.get(str(f.get("sameSite") or "").strip().lower())
    if same_site:
        ck["sameSite"] = same_site
    expires = cookie_expiry(f.get("expires"))
    if expires and not _flag(f.get("session")):
        ck["expires"] = expires
    if ck["name"].startswith(("__Secure-", "__Host-")):
        ck["secure"] = True
    return ck


def _cdp_cookie(ck: Dict[str, Any], site_url: str) -> Dict[str, Any]:
    """Network.setCookie parameters. Host-only cookies are set by URL (no Domain attribute), as
    __Host- cookies require; domain cookies keep their leading-dot domain."""
    scheme = "https" if ck.get("secure") else (urlparse(site_url).scheme or "https")
    params: Dict[str, Any] = {"name": ck["name"], "value": ck["value"], "path": ck.get("path") or "/",
                              "secure": bool(ck.get("secure")), "httpOnly": bool(ck.get("httpOnly"))}
    domain = ck.get("domain") or urlparse(site_url).hostname or ""
    if domain.startswith("."):
        params["domain"] = domain
    else:
        params["url"] = f"{scheme}://{domain}{params['path']}"
    if ck.get("sameSite") in ("Strict", "Lax", "None") and (ck.get("sameSite") != "None" or params["secure"]):
        params["sameSite"] = ck["sameSite"]
    if ck.get("expires"):
        params["expires"] = ck["expires"]
    return params


def _cdp(driver, cmd: str, params: Optional[dict] = None) -> dict:
    return driver.execute_cdp_cmd(cmd, params or {}) or {}


_STORAGE_READ_JS = """
const out = {local: {}, session: {}};
try { for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); out.local[k] = localStorage.getItem(k); } } catch (e) {}
try { for (let i = 0; i < sessionStorage.length; i++) { const k = sessionStorage.key(i); out.session[k] = sessionStorage.getItem(k); } } catch (e) {}
out.origin = location.origin;
return out;
"""


def export_session(driver, site_url: str) -> Dict[str, Any]:
    """The browser's login as data: {"cookies": [full cookie dicts for the site, its subdomains and parent
    domains, HttpOnly ones included], "local": {origin: {k: v}}, "session": {origin: {k: v}}}."""
    state: Dict[str, Any] = {"cookies": [], "local": {}, "session": {}}
    if driver is None:
        return state
    base = get_domain(site_url)
    raw: List[dict] = []
    for cmd in ("Network.getAllCookies", "Storage.getCookies"):
        try:
            raw = _cdp(driver, cmd).get("cookies", [])
            break
        except Exception:
            continue
    else:
        try:
            raw = driver.get_cookies()
        except Exception:
            raw = []
    for c in raw:
        if _site_cookie(c.get("domain", ""), base):
            ck = normalize_cookie(c)
            if ck:
                state["cookies"].append(ck)
    # Storage of the page the browser is on (the app's own origin after login)
    try:
        data = driver.execute_script(_STORAGE_READ_JS) or {}
        origin = data.get("origin") or ""
        if origin.startswith("http") and _site_cookie(urlparse(origin).hostname or "", base):
            if data.get("local"):
                state["local"][origin] = dict(data["local"])
            if data.get("session"):
                state["session"][origin] = dict(data["session"])
    except Exception:
        pass
    # localStorage of the site's other origin (e.g. www vs bare host) when the browser is elsewhere
    site_origin = _origin(site_url)
    if site_origin and site_origin not in state["local"]:
        try:
            entries = _cdp(driver, "DOMStorage.getDOMStorageItems",
                           {"storageId": {"securityOrigin": site_origin, "isLocalStorage": True}}).get("entries", [])
            if entries:
                state["local"][site_origin] = {k: v for k, v in entries}
        except Exception:
            pass
    return state


def _storage_script(state: Dict[str, Any]) -> str:
    """Runs before any page script on every new document: fills in localStorage/sessionStorage keys
    the origin does not have yet (keys the app has since refreshed are left alone)."""
    import json
    payload = json.dumps({"local": state.get("local") or {}, "session": state.get("session") or {}})
    return (
        "(function(){try{const S=" + payload + ";"
        "const fill=(store,items)=>{if(!items)return;for(const k in items){"
        "if(store.getItem(k)===null)store.setItem(k,items[k]);}};"
        "fill(window.localStorage,S.local[location.origin]);"
        "fill(window.sessionStorage,S.session[location.origin]);}catch(e){}})();"
    )


def import_session(driver, state: Optional[Dict[str, Any]], site_url: str = "") -> Dict[str, Any]:
    """Gives `driver` the login in `state` before its next navigation: every cookie through the DevTools
    protocol (keeps domain, path, HttpOnly, Secure, SameSite and expiry; works for __Host- cookies) and
    web storage through a script that runs first on each new document of a matching origin.
    Returns {"cookies": accepted, "failed": [names], "storage_origins": n}."""
    summary: Dict[str, Any] = {"cookies": 0, "failed": [], "storage_origins": 0}
    if driver is None or not state:
        return summary
    try:
        _cdp(driver, "Network.enable")
    except Exception:
        pass
    for raw in state.get("cookies") or []:
        ck = normalize_cookie(raw)
        if not ck:
            continue
        try:
            res = _cdp(driver, "Network.setCookie", _cdp_cookie(ck, site_url))
            if res.get("success") is False:
                summary["failed"].append(ck["name"])
            else:
                summary["cookies"] += 1
        except Exception:
            summary["failed"].append(ck["name"])
    origins = set((state.get("local") or {}).keys()) | set((state.get("session") or {}).keys())
    if origins:
        try:
            old = getattr(driver, "_crawler_storage_script", None)
            if old:
                _cdp(driver, "Page.removeScriptToEvaluateOnNewDocument", {"identifier": old})
            res = _cdp(driver, "Page.addScriptToEvaluateOnNewDocument", {"source": _storage_script(state)})
            driver._crawler_storage_script = res.get("identifier")
            summary["storage_origins"] = len(origins)
        except Exception:
            pass
    return summary


def clear_session(driver):
    """Forgets every cookie and the storage restore script (used before logging in again)."""
    try:
        _cdp(driver, "Network.clearBrowserCookies")
    except Exception:
        pass
    old = getattr(driver, "_crawler_storage_script", None)
    if old:
        try:
            _cdp(driver, "Page.removeScriptToEvaluateOnNewDocument", {"identifier": old})
        except Exception:
            pass
        driver._crawler_storage_script = None


def storage_origins(state: Optional[Dict[str, Any]]) -> int:
    if not state:
        return 0
    return len(set((state.get("local") or {}).keys()) | set((state.get("session") or {}).keys()))


def cookies_to_jar(jar, cookies: Iterable[Dict[str, Any]]) -> int:
    """Puts full cookies into a requests cookie jar with their real domain and path, so the jar picks the
    right ones per request and accepts refreshed values from Set-Cookie (no fixed Cookie header). Expired
    cookies are left out (a browser would not send them either); returns how many went in. A host-only cookie
    (domain without a leading dot) is marked HOST_ONLY_COOKIE: a net_guard.SiteSession sends it to that host only
    (requests' own jar would send an "example.com" one to every subdomain)."""
    from requests.cookies import create_cookie
    n = 0
    host_only = False
    now = time.time()
    for raw in cookies or []:
        ck = normalize_cookie(raw)
        if not ck or not ck["domain"] or (ck.get("expires") and ck["expires"] <= now):
            continue
        domain = ck["domain"]
        if domain.lstrip(".") == "localhost":
            domain = "localhost.local"  # how http.cookiejar names the local host
        rest = {"HttpOnly": None} if ck.get("httpOnly") else {}
        if not domain.startswith("."):
            rest[HOST_ONLY_COOKIE] = None
        jar.set_cookie(create_cookie(ck["name"], ck["value"], domain=domain, path=ck["path"],
                                     secure=ck["secure"], expires=int(ck["expires"]) if ck.get("expires") else None,
                                     rest=rest))
        n += 1
        host_only = host_only or HOST_ONLY_COOKIE in rest
    if host_only and hasattr(jar, "set_policy"):
        jar.set_policy(HostOnlyCookiePolicy())   # a direct read of the jar keeps host-only cookies on their host
    return n


def jar_to_cookies(jar, site_url: str = "") -> List[Dict[str, Any]]:
    """The jar's cookies for the site in the full format (to hand an HTTP login to Chrome)."""
    base = get_domain(site_url) if site_url else ""
    out = []
    for c in jar:
        domain = c.domain or ""
        if domain.lstrip(".") == "localhost.local":
            domain = domain.replace("localhost.local", "localhost")
        if base and not _site_cookie(domain, base):
            continue
        ck = normalize_cookie({
            "name": c.name, "value": c.value or "", "domain": domain, "path": c.path or "/",
            "secure": bool(c.secure), "httpOnly": c.has_nonstandard_attr("HttpOnly"),
            "expires": c.expires or 0,
        })
        if ck:
            out.append(ck)
    return out


# ----------------------------------------------------------------------------------------------------
# Network blocklist (sign-out URLs always; images/fonts while crawling)
# ----------------------------------------------------------------------------------------------------
def _blocklists(driver) -> Dict[str, List[str]]:
    lists = getattr(driver, "_crawler_blocklists", None)
    if lists is None:
        lists = {"danger": [], "media": []}
        try:
            driver._crawler_blocklists = lists
        except Exception:
            pass
    return lists


def _apply_blocklist(driver):
    lists = _blocklists(driver)
    urls = lists["danger"] + lists["media"]
    try:
        _cdp(driver, "Network.enable")
        _cdp(driver, "Network.setBlockedURLs", {"urls": urls})
    except Exception:
        pass


# Chrome's network blocklist stops fetch/XHR/beacons but not page navigations, so every new document also
# gets this guard: it cancels navigations to sign-out URLs and to links that change data (admin screens,
# one-time-token action links, cart changes; see url_utils.changes_data_url) - link clicks, form posts,
# location.href, form.submit()
_NAV_GUARD_JS = r"""(function(){const D=/log[-_]?out|sign[-_]?out|log[-_]?off/i;
const C=/\/wp-admin(\/|$)|\/cart\/(add|change|clear|update|remove)(\.js)?(\/|$)|\/(delete|remove|destroy|trash|deactivate|unsubscribe|cancel|form_key)(\/|$)/i;
const T=/[?&](_?wpnonce|_?nonce|form_key|_?csrf(_token)?|authenticity_token|_token|remove_item|cancel_order|add-to-cart|empty_cart)=/i;
const A=/[?&](action|do|op|task|cmd)=[^&]*(trash|delet|remov|activat|spam|approv|destroy|cancel|disabl|upgrad|updat|install|empty|purge|reset|revok|clear)/i;
const bad=u=>{try{const x=new URL(u,location.href),p=decodeURIComponent(x.pathname);
return D.test(p+x.search)||C.test(p)||T.test(x.search)||A.test(x.search);}catch(e){return false;}};
try{if(window.navigation&&!window.__crawlerGuard){window.__crawlerGuard=1;navigation.addEventListener('navigate',
e=>{if(e.cancelable&&bad(e.destination.url))e.preventDefault();});}}catch(e){}
document.addEventListener('click',e=>{const a=e.target&&e.target.closest&&e.target.closest('a[href]');
if(a&&bad(a.getAttribute('href')||'')){e.preventDefault();e.stopImmediatePropagation();}},true);
document.addEventListener('submit',e=>{const f=e.target;if(f&&bad(f.getAttribute('action')||'')){
e.preventDefault();e.stopImmediatePropagation();}},true);})();"""


def block_dangerous(driver, extra_patterns: Iterable[str] = ()):
    """The browser refuses sign-out URLs and data-changing links: requests (fetch, XHR, beacons) through the
    DevTools network blocklist, and page navigations (links, forms, scripts) through a guard on every new
    document."""
    if driver is None:
        return
    _blocklists(driver)["danger"] = list(DANGEROUS_URL_PATTERNS) + [p for p in extra_patterns if p]
    _apply_blocklist(driver)
    if not getattr(driver, "_crawler_guard_script", None):
        try:
            res = _cdp(driver, "Page.addScriptToEvaluateOnNewDocument", {"source": _NAV_GUARD_JS})
            driver._crawler_guard_script = res.get("identifier") or "set"
            driver.execute_script(_NAV_GUARD_JS)
        except Exception:
            pass


def set_media_blocked(driver, blocked: bool):
    """Images, video and fonts are not needed to find links; screenshots turn them back on."""
    if driver is None:
        return
    _blocklists(driver)["media"] = list(MEDIA_PATTERNS) if blocked else []
    _apply_blocklist(driver)


def media_blocked(driver) -> bool:
    return bool(driver is not None and _blocklists(driver)["media"])


# ----------------------------------------------------------------------------------------------------
# A second DevTools connection: events, request blocking, proxy sign-in
# ----------------------------------------------------------------------------------------------------
class DevToolsTap:
    """
    A second DevTools connection to one Chrome tab (Chrome accepts several clients per tab). Selenium sends
    DevTools commands but never receives their events; this connection receives them on a reader thread.

    listener:    fn(method, params) for every event, called on the reader thread (keep it quick).
    interceptor: set_interceptor(fn): fn(Fetch.requestPaused params) returns None to let the request through,
                 a Fetch error reason ("BlockedByClient") to refuse it, or {"headers": [{"name", "value"}]} to
                 send it with these headers. It runs on a small worker pool and sees every request of the tab,
                 the page loads themselves included (frameId == target_id, resourceType "Document").
    proxy sign-in: set_proxy_auth(user, password) answers the proxy's 407 challenge from memory.
    """

    AUTH_TRIES = 2     # a proxy that keeps asking (wrong password) is refused after this many answers

    def __init__(self, ws, target_id: str):
        self.ws = ws
        self.target_id = target_id
        self.listener: Optional[Callable[[str, dict], None]] = None
        self._interceptor: Optional[Callable[[dict], Optional[str]]] = None
        self._proxy_auth: Optional[Tuple[str, str]] = None
        self._auth_tries: Dict[str, int] = {}
        self._fetch_on = False
        self._next_id = 0
        self._pending: Dict[int, list] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._closed = False
        self._pool: Optional[ThreadPoolExecutor] = None
        self._domains: set = set()
        threading.Thread(target=self._read, daemon=True, name="chrome-devtools-tap").start()

    @classmethod
    def connect(cls, driver) -> Optional["DevToolsTap"]:
        """A tap on the driver's current tab, or None (no DevTools address, websocket-client missing, or the
        connection was refused)."""
        try:
            import websocket
        except ImportError:
            return None
        try:
            addr = ((driver.capabilities or {}).get("goog:chromeOptions") or {}).get("debuggerAddress") or ""
            target = (_cdp(driver, "Target.getTargetInfo").get("targetInfo") or {}).get("targetId") or ""
            if not addr or not target:
                return None
            if addr.startswith("localhost:"):
                addr = "127.0.0.1:" + addr.split(":", 1)[1]   # Windows tries ::1 first and waits ~2 s for it
            ws = websocket.create_connection(f"ws://{addr}/devtools/page/{target}", suppress_origin=True,
                                             enable_multithread=True, timeout=10)
            ws.settimeout(None)
        except Exception:
            return None
        return cls(ws, target)

    @property
    def alive(self) -> bool:
        return not self._closed

    # -- wire ------------------------------------------------------------------------------------------
    def _send(self, method: str, params: Optional[dict] = None) -> int:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})
        with self._send_lock:
            self.ws.send(payload)
        return msg_id

    def post(self, method: str, params: Optional[dict] = None):
        """Sends a command without waiting for its answer (safe on the reader thread)."""
        if self._closed:
            return
        try:
            self._send(method, params)
        except Exception:
            self._closed = True

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict:
        """Sends a command and waits for its result. Raises RuntimeError on an error answer or a closed tap.
        Never call it from a listener (the reader thread would wait for itself)."""
        if self._closed:
            raise RuntimeError("DevTools connection closed")
        done = threading.Event()
        slot: list = [done, None, None]
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            self._pending[msg_id] = slot
        try:
            with self._send_lock:
                self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        except Exception as e:
            with self._lock:
                self._pending.pop(msg_id, None)
            self._closed = True
            raise RuntimeError(f"DevTools connection lost ({type(e).__name__})")
        if not done.wait(timeout):
            with self._lock:
                self._pending.pop(msg_id, None)
            raise RuntimeError(f"{method} timed out")
        if slot[2]:
            raise RuntimeError(str(slot[2])[:200])
        return slot[1] or {}

    def enable(self, *domains: str):
        """Turns on event domains once per connection (Network with small body buffers)."""
        for d in domains:
            if d in self._domains:
                continue
            params = {"maxTotalBufferSize": 1_000_000, "maxResourceBufferSize": 200_000} if d == "Network" else {}
            try:
                self.call(f"{d}.enable", params)
                self._domains.add(d)
            except RuntimeError:
                pass

    def _read(self):
        while not self._closed:
            try:
                raw = self.ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "id" in msg:
                with self._lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot is not None:
                    slot[1], slot[2] = msg.get("result"), (msg.get("error") or {}).get("message")
                    slot[0].set()
                continue
            method, params = msg.get("method") or "", msg.get("params") or {}
            if method == "Fetch.requestPaused":
                self._paused(params)
            elif method == "Fetch.authRequired":
                self._auth(params)
            listener = self.listener
            if listener is not None:
                try:
                    listener(method, params)
                except Exception:
                    pass
        self._closed = True
        with self._lock:
            pending, self._pending = list(self._pending.values()), {}
        for slot in pending:
            slot[2] = "DevTools connection closed"
            slot[0].set()

    # -- request interception ----------------------------------------------------------------------------
    def _update_fetch(self):
        want = self._interceptor is not None or self._proxy_auth is not None
        try:
            if want:
                self.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}],
                                           "handleAuthRequests": self._proxy_auth is not None})
            elif self._fetch_on:
                self.call("Fetch.disable")
            self._fetch_on = want
        except RuntimeError:
            pass

    def set_interceptor(self, fn: Optional[Callable[[dict], Optional[str]]]):
        """Asks `fn` about every request of the tab (see the class docstring); None stops asking."""
        if fn is self._interceptor:
            return
        self._interceptor = fn
        self._update_fetch()

    def set_proxy_auth(self, username: str, password: str):
        """Answers the proxy's sign-in challenge with these credentials (kept in memory only)."""
        self._proxy_auth = (username, password)
        self._update_fetch()

    def _paused(self, params: dict):
        rid = params.get("requestId")
        fn = self._interceptor
        if fn is None:
            self.post("Fetch.continueRequest", {"requestId": rid})
            return
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chrome-tap-decide")
        try:
            self._pool.submit(self._decide, fn, params)
        except RuntimeError:
            self.post("Fetch.continueRequest", {"requestId": rid})

    def _decide(self, fn, params: dict):
        rid = params.get("requestId")
        try:
            reason = fn(params)
        except Exception:
            reason = None
        if isinstance(reason, dict) and reason.get("headers"):
            self.post("Fetch.continueRequest", {"requestId": rid, "headers": reason["headers"]})
        elif isinstance(reason, str) and reason:
            self.post("Fetch.failRequest", {"requestId": rid, "errorReason": reason})
        else:
            self.post("Fetch.continueRequest", {"requestId": rid})

    def _auth(self, params: dict):
        rid = params.get("requestId") or ""
        challenge = params.get("authChallenge") or {}
        creds = self._proxy_auth
        tries = self._auth_tries.get(rid, 0)
        if challenge.get("source") == "Proxy" and creds and tries < self.AUTH_TRIES:
            self._auth_tries[rid] = tries + 1
            response = {"response": "ProvideCredentials", "username": creds[0], "password": creds[1]}
        else:
            response = {"response": "CancelAuth"}
        if len(self._auth_tries) > 5000:
            self._auth_tries.clear()
        self.post("Fetch.continueWithAuth", {"requestId": rid, "authChallengeResponse": response})

    def close(self):
        """Closes the connection (Chrome lets paused requests continue when a client leaves)."""
        if self._closed and self._pool is None:
            return
        self._closed = True
        for step in ("abort", "close"):
            try:
                getattr(self.ws, step)()
            except Exception:
                pass
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None


def attach_tap(driver) -> Optional[DevToolsTap]:
    """The driver's DevToolsTap (connected on first use and kept on the driver; close_chrome closes it), or
    None when Chrome's DevTools address cannot be reached."""
    if driver is None:
        return None
    tap = getattr(driver, "_crawler_tap", None)
    if tap is not None and tap.alive:
        return tap
    tap = DevToolsTap.connect(driver)
    try:
        driver._crawler_tap = tap
    except Exception:
        pass
    return tap


def apply_proxy_auth(driver, proxy: Optional[Dict[str, Any]], logger: Optional[Callable[[str], None]] = None) -> bool:
    """Gives the driver the proxy's user name and password (through its DevToolsTap; nothing is written to
    disk). True when there is nothing to give or it was set up; False (and a log line) when the browser
    cannot sign in to the proxy."""
    creds = proxy_credentials(proxy)
    if not creds or driver is None:
        return True
    tap = attach_tap(driver)
    if tap is None:
        (logger or print)("[Browser] The proxy's user name and password are not used by the browser (its DevTools "
                          "connection is unavailable); browser pages may fail with 407 Proxy Authentication Required.")
        return False
    tap.set_proxy_auth(*creds)
    return True


# ----------------------------------------------------------------------------------------------------
# Cookie-consent and newsletter pop-ups, hidden for a screenshot only
# ----------------------------------------------------------------------------------------------------
# Containers of common consent platforms and sign-up pop-up tools (hidden with display:none). The heuristic in
# _HIDE_OVERLAYS_JS also hides fixed/sticky boxes and modal dialogs that talk about cookies or sign-ups.
OVERLAY_SELECTORS = [
    "#onetrust-consent-sdk", "#onetrust-banner-sdk", ".onetrust-pc-dark-filter", "#CybotCookiebotDialog",
    "#CybotCookiebotDialogBodyUnderlay", "#didomi-host", "#usercentrics-root", "#usercentrics-cmp-ui",
    "#qc-cmp2-container", ".qc-cmp2-container", "#truste-consent-track", ".truste_overlay", ".truste_box_overlay",
    "#consent_blackbar", "#cmplz-cookiebanner-container", ".cmplz-cookiebanner", "#termly-code-snippet-support",
    ".osano-cm-window", ".osano-cm-dialog", "#iubenda-cs-banner", ".iubenda-cs-container",
    "#cookie-law-info-bar", ".cli-modal-backdrop", "#cookie-notice", ".cc-window", ".cc-banner",
    "#cookiescript_injected", "#BorlabsCookieBox", "#moove_gdpr_cookie_info_bar", ".cky-consent-container",
    ".cky-overlay", "#hs-eu-cookie-confirmation", "#shopify-pc__banner", ".fc-consent-root",
    "#sp_message_container", "#gdpr-cookie-message", "#cookieConsent", ".cookie-consent-banner",
    "#attentive_overlay", "#attentive_creative", "#privy-container", ".privy-popup-container",
    "div[class*=kl-private-reset-css][role=dialog]", ".needsclick[role=dialog][aria-modal=true]",
    ".om-holder", ".sumome-react-wysiwyg-popup-container", "#wisepops-root",
]

_HIDE_OVERLAYS_JS = r"""
const SEL = arguments[0], keepSel = arguments[1] || '';
const WORDS = /cookie|consent|gdpr|ccpa|your privacy|privacy (settings|preferences|choices)|newsletter|subscribe|sign ?up|join (our|the)|% ?off|discount|exclusive offer|unlock/i;
if (!document.getElementById('__crawler_overlay_css')) {
  const st = document.createElement('style');
  st.id = '__crawler_overlay_css';
  st.textContent = SEL.join(',') + '{display:none !important;}';
  (document.head || document.documentElement).appendChild(st);
}
let keep = null;
try { keep = keepSel ? document.querySelector(keepSel) : null; } catch (e) {}
let hidden = 0;
const hide = el => {
  if ((keep && el.contains(keep)) || el.hasAttribute('data-crawler-hidden')) return;
  el.setAttribute('data-crawler-hidden', el.getAttribute('style') || '');
  el.style.setProperty('display', 'none', 'important');
  hidden++;
};
const vw = innerWidth, vh = innerHeight;
let scanned = 0;
for (const el of (document.body ? document.body.querySelectorAll('*') : [])) {
  if (++scanned > 5000) break;
  const s = getComputedStyle(el);
  if ((s.position !== 'fixed' && s.position !== 'sticky') || s.display === 'none' || s.visibility === 'hidden') continue;
  const r = el.getBoundingClientRect();
  if (r.width < 40 || r.height < 20) continue;
  if (el.closest('header, nav, [role=banner], [role=navigation]') || el.querySelector('nav, [role=navigation]')) continue;
  const z = parseInt(s.zIndex, 10) || 0, text = (el.innerText || '').slice(0, 2000);
  if (z >= 100 && text.length <= 1500 && WORDS.test(text)) { hide(el); continue; }
  const covers = r.width * r.height >= vw * vh * 0.7;
  if (covers && z >= 100 && text.trim().length < 20 && s.backgroundColor !== 'rgba(0, 0, 0, 0)') hide(el);
}
document.querySelectorAll('[aria-modal=true], [role=dialog], [role=alertdialog], dialog[open]').forEach(el => {
  const r = el.getBoundingClientRect();
  if (r.width && r.height && WORDS.test((el.innerText || '').slice(0, 2000))) hide(el);
});
for (const el of [document.documentElement, document.body]) {
  if (!el) continue;
  const s = getComputedStyle(el);
  if (s.overflow === 'hidden' || s.overflowY === 'hidden') {
    el.setAttribute('data-crawler-overflow', el.style.overflow || '');
    el.style.setProperty('overflow', 'visible', 'important');
  }
}
let matched = 0;
try { matched = document.querySelectorAll(SEL.join(',')).length; } catch (e) {}
return {hidden: hidden, matched: matched};
"""

_RESTORE_OVERLAYS_JS = r"""
const st = document.getElementById('__crawler_overlay_css');
if (st) st.remove();
document.querySelectorAll('[data-crawler-hidden]').forEach(el => {
  const v = el.getAttribute('data-crawler-hidden');
  if (v) el.setAttribute('style', v); else el.removeAttribute('style');
  el.removeAttribute('data-crawler-hidden');
});
document.querySelectorAll('[data-crawler-overflow]').forEach(el => {
  el.style.overflow = el.getAttribute('data-crawler-overflow') || '';
  el.removeAttribute('data-crawler-overflow');
});
"""


def hide_overlays(driver, keep_selector: str = "") -> Dict[str, int]:
    """Hides cookie-consent banners and newsletter/discount pop-ups (and the scroll lock they leave) before a
    screenshot. Nothing is clicked, so no consent is ever given. An element containing `keep_selector` (the
    spotlighted link of an evidence picture) stays visible. Undo with restore_overlays() before reading the
    page again. Returns {"hidden": boxes hidden by the heuristic, "matched": known pop-up containers}."""
    try:
        return driver.execute_script(_HIDE_OVERLAYS_JS, OVERLAY_SELECTORS, keep_selector or "") or {}
    except Exception:
        return {}


def restore_overlays(driver):
    """Puts back what hide_overlays() hid."""
    try:
        driver.execute_script(_RESTORE_OVERLAYS_JS)
    except Exception:
        pass


# ----------------------------------------------------------------------------------------------------
# The crawl's one shared, logged-in browser
# ----------------------------------------------------------------------------------------------------
class SharedBrowser:
    """
    The crawl's one Chrome, shared by the login, the JavaScript renderer, screenshots and later features.
    hold() waits for it; lease() hands it out when free and otherwise starts a short-lived Chrome carrying
    the same session. It keeps its own profile folder, so a restart (to free memory) keeps IndexedDB and
    storage, and import_session() restores the cookies. Callers never quit the driver they are given.
    """

    def __init__(self, user_agent: str = "", timeout: int = 30, site_url: str = "",
                 logger: Optional[Callable[[str], None]] = None, proxy: Optional[Dict[str, Any]] = None):
        self.user_agent = user_agent
        self.timeout = timeout
        self.site_url = site_url
        self.logger = logger or (lambda msg: None)
        self.proxy = proxy if proxy_server_arg(proxy) else None   # the crawl's proxy ({"enabled", "host", ...})
        self.lock = threading.RLock()
        self.driver = None
        self.session_state: Optional[Dict[str, Any]] = None
        self.profile_dir = ""
        self.last_error = ""
        self.starts = 0
        self._idle_timer: Optional[threading.Timer] = None
        self._idle_seconds: Optional[float] = None
        self._last_used = time.time()

    def log(self, msg: str):
        self.logger(f"[Browser] {msg}")

    def log_line(self, line: str):
        """A line that already carries its [Browser] prefix."""
        self.logger(line)

    def alive(self) -> bool:
        return chrome_alive(self.driver)

    def _new_profile(self) -> str:
        return new_profile_dir()

    def start(self, wait: float = 0.0):
        """The running driver, or a newly started one with the session imported (None when Chrome cannot
        start; last_error says why). Call while holding `lock`."""
        with self.lock:
            if self.alive():
                return self.driver
            self.driver = None
            if not chrome_available():
                self.last_error = chrome_error() or "Chrome is not installed on this server."
                return None
            if not self.profile_dir:
                self.profile_dir = self._new_profile()
            driver = new_chrome(self.user_agent, self.timeout, eager=True, wait=wait, profile_dir=self.profile_dir,
                                proxy=self.proxy)
            if driver is None and self.profile_dir and "busy" not in chrome_error() and "memory" not in chrome_error():
                # A profile still locked by a Chrome that is shutting down: start with a fresh one, and remove
                # the old folder (it holds the login cookies; if it is still locked, process exit removes it)
                old, self.profile_dir = self.profile_dir, self._new_profile()
                remove_profile(old, tries=1)
                driver = new_chrome(self.user_agent, self.timeout, eager=True, wait=wait, profile_dir=self.profile_dir,
                                    proxy=self.proxy)
            if driver is None:
                self.last_error = chrome_error()
                return None
            self.starts += 1
            apply_proxy_auth(driver, self.proxy, self.log_line)
            block_dangerous(driver)
            if self.session_state:
                import_session(driver, self.session_state, self.site_url)
            self.driver = driver
            self.last_error = ""
            self._arm_idle()
            return driver

    def adopt(self, driver, session_state: Optional[Dict[str, Any]] = None):
        """Makes an already running (logged-in) driver the shared one."""
        with self.lock:
            if self.driver is not None and self.driver is not driver:
                close_chrome(self.driver)
            self.driver = driver
            block_dangerous(driver)
            if session_state is not None:
                self.session_state = session_state

    def set_session(self, state: Optional[Dict[str, Any]], apply: bool = True):
        """A new login (e.g. after logging in again); imported into the running driver right away."""
        with self.lock:
            self.session_state = state
            if apply and self.alive() and state:
                import_session(self.driver, state, self.site_url)

    def refresh_session(self) -> Optional[Dict[str, Any]]:
        """Re-reads the login from the running driver (tokens the site rotated since the last export)."""
        with self.lock:
            if not self.alive() or not self.session_state:
                return self.session_state
            try:
                fresh = export_session(self.driver, self.site_url)
                if fresh.get("cookies"):
                    # Storage of origins the browser is not on right now stays as exported before
                    for kind in ("local", "session"):
                        merged = dict(self.session_state.get(kind) or {})
                        merged.update(fresh.get(kind) or {})
                        fresh[kind] = merged
                    self.session_state = fresh
            except Exception:
                pass
            return self.session_state

    def restart(self, reason: str = ""):
        """Closes and restarts Chrome with the same profile and session (frees the memory a long-lived
        tab collects)."""
        with self.lock:
            self.refresh_session()
            if self.driver is not None:
                close_chrome(self.driver)
                self.driver = None
            driver = self.start(wait=30)
            if driver is not None and reason:
                self.log(f"Restarted Chrome {reason}.")
            return driver

    @contextmanager
    def hold(self, timeout: float = 120.0, start: bool = True, start_wait: Optional[float] = None) -> Iterator[Any]:
        """Waits for the shared driver (started if needed, waiting up to `start_wait` seconds for a Chrome slot)
        and yields it, or None when Chrome is unavailable or stayed busy for `timeout` seconds."""
        if not self.lock.acquire(timeout=max(0.1, timeout)):
            yield None
            return
        try:
            wait = timeout if start_wait is None else start_wait
            driver = self.start(wait=wait) if start else (self.driver if self.alive() else None)
            yield driver
        finally:
            self._last_used = time.time()
            self.lock.release()

    @contextmanager
    def lease(self, timeout: float = 60.0) -> Iterator[Any]:
        """A logged-in driver for one task: the shared one when free (or once it frees up), else a
        short-lived Chrome with the same session and the sign-out blocklist. None without Chrome."""
        deadline = time.time() + max(0.1, timeout)
        if self.lock.acquire(timeout=min(5.0, timeout)):
            try:
                yield self.start(wait=max(0.1, deadline - time.time()))
            finally:
                self._last_used = time.time()
                self.lock.release()
            return
        temp = new_chrome(self.user_agent, self.timeout, eager=True, proxy=self.proxy) if chrome_available() else None
        if temp is not None:
            try:
                apply_proxy_auth(temp, self.proxy, self.log_line)
                block_dangerous(temp)
                if self.session_state:
                    import_session(temp, self.session_state, self.site_url)
                yield temp
            finally:
                close_chrome(temp)
            return
        with self.hold(timeout=max(0.1, deadline - time.time())) as driver:
            yield driver

    def close_when_idle(self, seconds: float = 60.0):
        """Closes Chrome once nobody has used it for `seconds` (the crawl is over; screenshots and
        re-checks that follow right away still find it logged in). A later start re-arms the timer."""
        self._idle_seconds = max(1.0, float(seconds))
        self._last_used = time.time()
        timer, self._idle_timer = self._idle_timer, None
        if timer is not None:
            timer.cancel()
        self._arm_idle()

    def _arm_idle(self):
        if self._idle_seconds is None or (self._idle_timer is not None and self._idle_timer.is_alive()):
            return
        timer = threading.Timer(self._idle_seconds, self._idle_check)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _idle_check(self):
        self._idle_timer = None
        if not self.alive():
            return
        if self.lock.acquire(blocking=False):
            try:
                if time.time() - self._last_used >= self._idle_seconds - 0.5:
                    self.close()
                    return
            finally:
                self.lock.release()
        self._arm_idle()

    def close(self):
        """Quits Chrome and removes its profile folder. The session stays in session_state."""
        with self.lock:
            timer, self._idle_timer = self._idle_timer, None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
            if self.driver is not None:
                close_chrome(self.driver)
                self.driver = None
            if self.profile_dir:
                remove_profile(self.profile_dir)
                self.profile_dir = ""
