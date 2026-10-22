import time
import re
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Set, Optional, Callable
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter
import urllib3
from bs4 import BeautifulSoup
from urllib3.util.retry import Retry

from .config import CrawlConfig
from .url_utils import (
    normalize_url,
    get_domain,
    clean_hostname,
    is_internal_url,
    is_parsable_html_url,
    is_excluded_path,
)
from .page_parser import PageParser
from .sitemap_parser import SitemapParser
from .dynamic_extractor import DynamicExtractor
from .auth import AuthHandler
from .issues import classify, diagnose

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Client-side route patterns found in JS bundles: router configs / nav arrays and programmatic navigation
BUNDLE_ROUTE_PATTERNS = (
    re.compile(r"""(?:path|to|href|pathname|route|link)["']?\s*:\s*["'`](/[^"'`\s#?]*)["'`]"""),
    re.compile(r"""\.(?:push|replace|prefetch|navigate)\(\s*["'`](/[^"'`\s#?]*)["'`]"""),
)
NEXT_APP_CHUNK = re.compile(r"/_next/static/chunks/app/(.*)/page-[0-9a-f]+\.js$")
BUNDLE_ASSET_EXTENSIONS = ('.js', '.css', '.png', '.jpg', '.jpeg', '.svg', '.json', '.ico', '.woff', '.woff2',
                           '.ttf', '.map', '.wasm', '.webp', '.gif', '.mp4', '.txt', '.xml')
BUNDLE_SKIP_SEGMENTS = ('/api/', '/_next/', '/static/', '/assets/', '/engine.io', '/socket.io', '/oauth/', '/node_modules/')

@dataclass
class CrawlResult:
    url: str
    page_title: str = ""
    parent_url: str = ""
    http_status: str = "Pending"
    status_code: Optional[int] = None
    redirect_destination: Optional[str] = None
    depth: int = 0
    discovery_source: str = "Page Link"
    content_type: str = ""
    response_time_ms: int = 0
    error_message: Optional[str] = None
    # The element on the parent page that links here (see PageParser.element_meta)
    anchor_text: str = ""
    link_tag: str = ""
    link_region: str = ""
    link_selector: str = ""
    link_html: str = ""
    raw_href: str = ""
    server: str = ""

    @property
    def category(self) -> str:
        return classify(self.status_code, self.redirect_destination)

    def to_dict(self) -> dict:
        return {
            "page_title": self.page_title,
            "url": self.url,
            "parent_url": self.parent_url,
            "http_status": self.http_status,
            "status_code": self.status_code,
            "category": self.category,
            "redirect_destination": self.redirect_destination or "-",
            "depth": self.depth,
            "discovery_source": self.discovery_source,
            "content_type": self.content_type or "-",
            "response_time_ms": self.response_time_ms,
            "error_message": self.error_message if (self.error_message and self.error_message != "-") else None,
            "anchor_text": self.anchor_text,
            "link_tag": self.link_tag,
            "link_region": self.link_region,
            "link_selector": self.link_selector,
            "link_html": self.link_html,
            "issue": diagnose(self),
        }

@dataclass
class ExternalLink:
    url: str
    domain: str
    parent_url: str
    anchor_text: str = "-"
    discovery_source: str = "Page Link"
    validation_status: str = "Recorded (Not Followed)"

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "domain": self.domain,
            "parent_url": self.parent_url,
            "anchor_text": self.anchor_text,
            "discovery_source": self.discovery_source,
            "validation_status": self.validation_status
        }

class WebsiteCrawler:
    def __init__(
        self,
        config: CrawlConfig,
        on_result: Optional[Callable[[CrawlResult], None]] = None,
        on_external_link: Optional[Callable[[ExternalLink], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[dict], None]] = None
    ):
        self.config = config
        self.on_result = on_result or (lambda r: None)
        self.on_external_link = on_external_link or (lambda e: None)
        self.on_log = on_log or (lambda msg: None)
        self.on_progress = on_progress or (lambda p: None)

        self.start_url = normalize_url(config.start_url) or config.start_url
        self.base_domain = get_domain(self.start_url)
        self.allowed_domains = {clean_hostname(d) for d in config.allowed_domains if d}
        if self.base_domain:
            self.allowed_domains.add(self.base_domain)
            self.allowed_domains.add(clean_hostname(self.base_domain))

        # Threading & Control
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set() # Unpaused initially

        # Internal URL Queues and Tracker Sets
        # Queue item: (url, parent_url, depth, source)
        self.queue: deque = deque()
        self.enqueued_urls: Set[str] = set()
        self.visited_urls: Set[str] = set()
        self.results: Dict[str, CrawlResult] = {}
        self.results_list: List[CrawlResult] = []
        # url -> element meta of the link that first led to it (anchor text, region, selector...)
        self.link_origin: Dict[str, dict] = {}

        # External Links Collection (NOT crawled, only recorded)
        self.external_links: Dict[str, ExternalLink] = {}
        self.external_links_list: List[ExternalLink] = []

        # Authentication State & Limitations
        self.auth_handler = AuthHandler(
            auth_config=self.config.auth,
            user_agent=self.config.user_agent,
            timeout=self.config.timeout,
            verify_ssl=self.config.verify_ssl,
            logger=self.log
        )
        self.auth_status: str = "Not required (Public)"
        self.auth_used: bool = False
        self.auth_cookies: dict = {}
        self.auth_error: Optional[str] = None
        self.auth_required_count: int = 0
        self.limitations: List[str] = []

        # Metrics
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self.status = "idle" # "idle", "running", "paused", "completed", "stopped"

        # Shared HTTP Session with retries & connection pooling
        self.session = requests.Session()
        retries = Retry(
            total=self.config.max_retries,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            connect=self.config.max_retries,
            read=self.config.max_retries,
            raise_on_status=False
        )
        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=max(20, self.config.concurrency * 2),
            pool_maxsize=max(30, self.config.concurrency * 3)
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive"
        })

        # Dynamic Browser (Headless Chrome) initialized lazily only when needed
        self.dynamic_extractor: Optional[DynamicExtractor] = None
        self._dynamic_init_lock = threading.Lock()
        # JS bundle URLs already scanned for client-side routes (each is fetched once per crawl)
        self._scanned_bundles: Set[str] = set()

    def log(self, message: str):
        self.on_log(f"[{time.strftime('%H:%M:%S')}] {message}")

    def stop(self):
        """Signals the crawler to gracefully stop."""
        self.log("Stop requested by user.")
        self._stop_event.set()
        self._pause_event.set()
        self.status = "stopped"

    def pause(self):
        """Pauses the crawler workers."""
        self.log("Crawling paused.")
        self._pause_event.clear()
        self.status = "paused"

    def resume(self):
        """Resumes the crawler workers."""
        self.log("Crawling resumed.")
        self._pause_event.set()
        self.status = "running"

    def get_stats(self) -> dict:
        with self._lock:
            total_discovered = len(self.enqueued_urls)
            total_crawled = len(self.visited_urls)
            counts = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "error": 0}
            for r in self.results_list:
                counts[r.category] += 1
            status_2xx, status_3xx = counts["2xx"], counts["3xx"]
            status_4xx, status_5xx, status_err = counts["4xx"], counts["5xx"], counts["error"]
            failed_count = status_4xx + status_5xx + status_err

            elapsed = (time.time() - self.start_time) if self.start_time else 0.0
            if self.end_time and self.start_time:
                elapsed = self.end_time - self.start_time

            unique_ext_domains = len(set(e.domain for e in self.external_links.values() if e.domain))

            return {
                "status": self.status,
                "elapsed_seconds": round(elapsed, 1),
                "total_discovered": total_discovered,
                "total_crawled": total_crawled,
                "queue_size": len(self.queue),
                "status_2xx": status_2xx,
                "status_3xx": status_3xx,
                "status_4xx": status_4xx,
                "status_5xx": status_5xx,
                "status_err": status_err,
                "failed_count": failed_count,
                "auth_required_count": self.auth_required_count,
                "total_external": len(self.external_links),
                "unique_external_domains": unique_ext_domains,
                "start_url": self.start_url,
                "auth_used": self.auth_used,
                "auth_status": self.auth_status,
                "auth_error": self.auth_error,
                "auth_prompt_needed": (self.auth_required_count > 0 and not self.auth_used),
                "limitations": list(set(self.limitations))[:10]
            }

    def _enqueue(self, url: str, parent: str, depth: int, source: str, meta: Optional[dict] = None) -> bool:
        """Thread-safe enqueueing of a new URL candidate (Internal only).
        `meta` describes the element on `parent` that links to `url`."""
        with self._lock:
            if not url or url in self.enqueued_urls or url in self.visited_urls:
                return False

            if self.config.max_urls and len(self.enqueued_urls) >= self.config.max_urls:
                return False

            if self.config.max_depth and depth > self.config.max_depth:
                return False

            if not is_internal_url(
                url, self.base_domain,
                include_subdomains=self.config.include_subdomains,
                allowed_domains=self.allowed_domains
            ):
                return False

            if is_excluded_path(url, self.config.exclude_patterns):
                return False

            self.enqueued_urls.add(url)
            if meta:
                self.link_origin[url] = meta
            self.queue.append((url, parent, depth, source))
            return True

    def run(self) -> List[CrawlResult]:
        """Executes the full automatic crawl procedure."""
        self.status = "running"
        self.start_time = time.time()
        self.log(f"Starting automatic crawl for: {self.start_url} (Workers: {self.config.concurrency})")

        # 0. Execute Authentication if configured
        post_login_url = None
        auth_links = set()
        if self.config.auth and self.config.auth.enabled:
            self.log("Authenticating user credentials...")
            auth_ok, auth_msg, auth_cookies, post_login_url, auth_links = self.auth_handler.authenticate(self.session, self.start_url)
            self.auth_used = True
            self.auth_status = auth_msg
            self.auth_cookies = auth_cookies
            if not auth_ok:
                self.auth_error = auth_msg
                self.limitations.append(f"Authentication notice: {auth_msg}")
                self.log(f"Auth notice: {auth_msg}")
            else:
                self.log(f"Authentication active: {auth_msg}")

        # 1. Enqueue Start URL & Authenticated Destinations
        parsed_start = urlparse(self.start_url)
        root_url = f"{parsed_start.scheme}://{parsed_start.netloc}/"

        if post_login_url:
            self._enqueue(post_login_url, parent="Authentication Portal", depth=0, source="Authenticated Landing Page")

        if self.start_url.rstrip('/') != root_url.rstrip('/'):
            self._enqueue(root_url, parent="Domain Root", depth=0, source="Root Domain")

        self._enqueue(self.start_url, parent="Direct Input", depth=0, source="Start URL")

        if auth_links:
            self.log(f"Enqueuing {len(auth_links)} internal pages discovered from authenticated session...")
            for alink in auth_links:
                self._enqueue(alink, parent=post_login_url or self.start_url, depth=1, source="Authenticated Dashboard")

        # 2. Probe robots.txt and XML Sitemaps in background thread (non-blocking)
        sitemap_thread = None
        if self.config.use_sitemaps:
            def probe_sitemaps():
                try:
                    self.log("Probing robots.txt and XML sitemaps in background...")
                    sitemap_parser = SitemapParser(
                        base_url=self.start_url,
                        base_domain=self.base_domain,
                        user_agent=self.config.user_agent,
                        timeout=min(self.config.timeout, 5),
                        verify_ssl=self.config.verify_ssl,
                        logger=self.log,
                        stop_event=self._stop_event,
                        max_sitemaps=8,
                        max_urls=min(self.config.max_urls or 1000, 1000)
                    )
                    sitemap_links = sitemap_parser.parse_all()
                    added_count = 0
                    for sm_url, sm_source in sitemap_links:
                        if self._stop_event.is_set():
                            break
                        if self.config.max_urls and len(self.enqueued_urls) >= self.config.max_urls:
                            break
                        sm_meta = {
                            "anchor_text": "", "tag": "sitemap", "region": "XML sitemap",
                            "selector": "", "html": f"<loc>{sm_url}</loc>"[:240], "raw_href": sm_url,
                        }
                        if self._enqueue(sm_url, parent=sm_source, depth=1, source="XML Sitemap", meta=sm_meta):
                            added_count += 1
                    if added_count > 0:
                        self.log(f"Queued {added_count} unique internal URLs from sitemaps.")
                        try:
                            self.on_progress(self.get_stats())
                        except Exception:
                            pass
                except Exception as e:
                    self.log(f"Sitemap background check completed: {e}")

            sitemap_thread = threading.Thread(target=probe_sitemaps, daemon=True)
            sitemap_thread.start()

        # 3. Multi-threaded worker pool for Breadth-First-Search crawl (Starts IMMEDIATELY)
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
            futures = {}

            while not self._stop_event.is_set():
                self._pause_event.wait() # Check for paused state

                # Assign tasks while queue has items and workers available
                while len(self.queue) > 0 and len(futures) < self.config.concurrency:
                    with self._lock:
                        if not self.queue:
                            break
                        item = self.queue.popleft()

                    future = executor.submit(self._crawl_single_page, item)
                    futures[future] = item

                # If no tasks are running and queue is empty, check if sitemap thread is active
                if not futures and len(self.queue) == 0:
                    if sitemap_thread and sitemap_thread.is_alive():
                        waited = 0
                        while sitemap_thread.is_alive() and len(self.queue) == 0 and waited < 15 and not self._stop_event.is_set():
                            time.sleep(0.2)
                            waited += 1
                        if len(self.queue) > 0:
                            continue
                    break

                # Check for completed tasks without raising TimeoutError
                done_futures = [f for f in futures if f.done()]
                if not done_futures:
                    time.sleep(0.05)
                    continue

                for fut in done_futures:
                    try:
                        result, new_links, new_external = fut.result()
                        if result:
                            with self._lock:
                                self.results[result.url] = result
                                self.results_list.append(result)

                            # Record newly discovered external links (NEVER enqueued or crawled)
                            newly_added_ext = []
                            if new_external and self.config.collect_external_links:
                                with self._lock:
                                    for ext_url, ext_data in new_external.items():
                                        if ext_url not in self.external_links:
                                            ext_obj = ExternalLink(
                                                url=ext_data["url"],
                                                domain=ext_data.get("domain", ""),
                                                parent_url=ext_data.get("parent_url", result.url),
                                                anchor_text=ext_data.get("anchor_text", "-"),
                                                discovery_source=ext_data.get("discovery_source", "Page Link"),
                                                validation_status=ext_data.get("validation_status", "Recorded (Not Followed)")
                                            )
                                            self.external_links[ext_url] = ext_obj
                                            self.external_links_list.append(ext_obj)
                                            newly_added_ext.append(ext_obj)

                            # Enqueue newly discovered internal links FIRST so metrics update immediately
                            current_depth = result.depth
                            for link, link_meta in new_links.items():
                                self._enqueue(
                                    url=link,
                                    parent=result.url,
                                    depth=current_depth + 1,
                                    source="Page Navigation",
                                    meta=link_meta
                                )

                            # Invoke callbacks safely outside the lock to prevent deadlocks
                            for ext_obj in newly_added_ext:
                                try:
                                    self.on_external_link(ext_obj)
                                except Exception:
                                    pass

                            try:
                                self.on_result(result)
                            except Exception:
                                pass

                            try:
                                self.on_progress(self.get_stats())
                            except Exception:
                                pass
                    except Exception as e:
                        self.log(f"Worker exception: {e}")

                    del futures[fut]

                # Polite delay if set
                if self.config.delay > 0:
                    time.sleep(self.config.delay)

        self.end_time = time.time()
        if self.status != "stopped":
            self.status = "completed"

        if self.dynamic_extractor:
            try:
                self.dynamic_extractor.close()
            except Exception:
                pass

        total_time = round(self.end_time - self.start_time, 2)
        ext_count = len(self.external_links)
        self.log(
            f"Crawl finished in {total_time}s! Discovered: {len(self.enqueued_urls)} internal, "
            f"{ext_count} external links (not crawled), Crawled: {len(self.results_list)}"
        )
        self.on_progress(self.get_stats())
        return self.results_list

    def _crawl_single_page(self, item: tuple) -> tuple:
        """
        Processes a single internal URL: fetches HTTP response, handles redirects,
        extracts page title, internal child links, and external outbound links.
        Returns: (result: CrawlResult, new_internal_links: Set[str], new_external_links: Dict[str, dict])
        """
        url, parent_url, depth, source = item

        with self._lock:
            self.visited_urls.add(url)

        start_req = time.time()
        resp_time = 0
        title = ""
        status_code = None
        status_text = "Pending"
        redirect_destination = None
        content_type = ""
        error_msg = None
        server = ""
        new_internal: Dict[str, dict] = {}
        new_external: Dict[str, dict] = {}
        origin = self.link_origin.get(url) or {}
        origin_fields = dict(
            anchor_text=origin.get("anchor_text", ""),
            link_tag=origin.get("tag", ""),
            link_region=origin.get("region", ""),
            link_selector=origin.get("selector", ""),
            link_html=origin.get("html", ""),
            raw_href=origin.get("raw_href", ""),
        )

        max_attempts = 2
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            if self._stop_event.is_set():
                break

            try:
                # Binary/media assets check (fast HEAD)
                if not is_parsable_html_url(url):
                    head_resp = self.session.head(
                        url,
                        timeout=min(self.config.timeout, 5),
                        verify=self.config.verify_ssl,
                        allow_redirects=True
                    )
                    resp_time = int((time.time() - start_req) * 1000)
                    status_code = head_resp.status_code
                    status_text = f"{status_code} {head_resp.reason}"
                    content_type = head_resp.headers.get("content-type", "application/octet-stream")

                    result = CrawlResult(
                        url=url,
                        page_title=f"[Asset] {url.split('/')[-1]}",
                        parent_url=parent_url,
                        http_status=status_text,
                        status_code=status_code,
                        redirect_destination=str(head_resp.url) if str(head_resp.url) != url else None,
                        depth=depth,
                        discovery_source=source,
                        content_type=content_type,
                        response_time_ms=resp_time,
                        server=self._server_label(head_resp.headers),
                        **origin_fields
                    )
                    return result, new_internal, new_external

                # HTML Page Crawling
                resp = self.session.get(
                    url,
                    timeout=self.config.timeout,
                    verify=self.config.verify_ssl,
                    allow_redirects=True
                )
                resp_time = int((time.time() - start_req) * 1000)
                status_code = resp.status_code
                status_text = f"{status_code} {resp.reason}"
                content_type = resp.headers.get("content-type", "")
                server = self._server_label(resp.headers)

                # Check if redirects occurred
                if resp.history:
                    redirect_destination = str(resp.url)
                    initial_status = resp.history[0].status_code
                    status_text = f"{initial_status} -> {status_code}"
                    if depth == 0:
                        dest_domain = get_domain(str(resp.url))
                        if dest_domain:
                            with self._lock:
                                self.allowed_domains.add(clean_hostname(dest_domain))
                                self.allowed_domains.add(dest_domain)

                # Check access limitations (401 Unauthorized, 403 Forbidden, or redirect to login)
                if status_code in (401, 403):
                    with self._lock:
                        self.auth_required_count += 1
                        self.limitations.append(f"HTTP {status_code} Access Restricted on: {url}")
                elif redirect_destination and any(k in redirect_destination.lower() for k in ['/login', '/signin', '/auth', '=login']) and not self.auth_used:
                    with self._lock:
                        self.auth_required_count += 1
                        self.limitations.append(f"Redirected to login portal on: {url} -> {redirect_destination}")

                # Only parse HTML pages (strictly skip JSON APIs, XML feeds, and non-HTML payloads)
                is_html_content = ("html" in content_type.lower()) or (not content_type and is_parsable_html_url(url))
                if 200 <= status_code < 400 and is_html_content and "json" not in content_type.lower() and "xml" not in content_type.lower():
                    # 1. Extract title
                    title = PageParser.extract_title(resp.text)

                    # 2. Extract internal links AND external links with fast HTML parser
                    new_internal, new_external = PageParser.extract_links(
                        html_content=resp.text,
                        current_url=str(resp.url),
                        base_domain=self.base_domain,
                        include_subdomains=self.config.include_subdomains,
                        allowed_domains=self.allowed_domains,
                        exclude_patterns=self.config.exclude_patterns,
                        collect_external=self.config.collect_external_links,
                        known_urls=self.enqueued_urls
                    )

                    # 3. Selective Headless Chrome fallback for client-side JavaScript / SPA rendering:
                    # Trigger when the HTML parser finds few links on an SPA, and on every SPA page of a
                    # logged-in crawl (app menus are usually rendered client-side after the session loads)
                    is_spa = ("id=\"__next\"" in resp.text or "id=\"root\"" in resp.text or "_next/static" in resp.text or "window.__INITIAL_STATE__" in resp.text)
                    html_link_count = len(new_internal)
                    if (html_link_count == 0 and is_spa) or (depth == 0 and html_link_count < 3 and is_spa) or (self.auth_used and is_spa):
                        if self.config.use_dynamic_js:
                            extractor = self._get_dynamic_extractor()
                            if extractor and extractor.driver:
                                self.log(f"Selectively rendering dynamic SPA with Headless Chrome: {url}")
                                dyn_title, _, dyn_internal, dyn_external = extractor.render_and_extract(
                                    url=str(resp.url),
                                    include_subdomains=self.config.include_subdomains,
                                    exclude_patterns=self.config.exclude_patterns,
                                    collect_external=self.config.collect_external_links
                                )
                                if not title and dyn_title:
                                    title = dyn_title
                                for dyn_url, dyn_meta in dyn_internal.items():
                                    new_internal.setdefault(dyn_url, dyn_meta)
                                new_external.update(dyn_external)

                    # 4. SPA Client-Side Route Discovery (React / Vite / Vue / Next.js router bundles).
                    # Always on in SPA mode; automatic for SPA pages that expose few HTML links or sit
                    # behind a login. Each bundle file is scanned once per crawl.
                    if (self.config.enable_spa_mode and (is_spa or len(new_internal) == 0)) or                             (is_spa and (self.auth_used or html_link_count < 5)):
                        spa_routes = self._extract_spa_bundle_routes(resp.text, str(resp.url)) - {url, str(resp.url)}
                        if spa_routes:
                            self.log(f"Discovered {len(spa_routes)} client-side SPA routes from JavaScript bundle: {url}")
                            for route in spa_routes:
                                new_internal.setdefault(route, {
                                    "anchor_text": "", "tag": "bundle", "region": "JavaScript bundle",
                                    "selector": "", "html": "", "raw_href": route,
                                })

                elif status_code >= 400 and "html" in content_type.lower():
                    # Keep the error page's own title (e.g. "Page Not Found") for the issue report
                    title = PageParser.extract_title(resp.text[:200000])

                # Successful fetch, break out of attempt loop
                break

            except requests.exceptions.Timeout:
                if attempt < max_attempts:
                    time.sleep(1.0)
                    continue
                resp_time = int((time.time() - start_req) * 1000)
                status_text = "Connection Timeout"
                error_msg = f"Timed out after {self.config.timeout}s ({max_attempts} attempts)"
            except requests.exceptions.SSLError as e:
                resp_time = int((time.time() - start_req) * 1000)
                status_text = "SSL Certificate Error"
                error_msg = str(e)
                break
            except requests.exceptions.ConnectionError as e:
                if attempt < max_attempts:
                    time.sleep(1.0)
                    continue
                resp_time = int((time.time() - start_req) * 1000)
                status_text = "Connection Failed"
                error_msg = f"Unable to connect: {e}"
            except Exception as e:
                resp_time = int((time.time() - start_req) * 1000)
                status_text = "Request Exception"
                error_msg = str(e)
                break

        if not title:
            if status_code and 200 <= status_code < 400:
                parsed_u = urlparse(url)
                title = f"{parsed_u.path or '/'}"
            elif error_msg:
                title = f"[Failed] {status_text}"
            else:
                title = f"[Status {status_code}]"

        result = CrawlResult(
            url=url,
            page_title=title,
            parent_url=parent_url,
            http_status=status_text,
            status_code=status_code,
            redirect_destination=redirect_destination,
            depth=depth,
            discovery_source=source,
            content_type=content_type,
            response_time_ms=resp_time,
            error_message=error_msg,
            server=server,
            **origin_fields
        )

        return result, new_internal, new_external

    @staticmethod
    def _server_label(headers) -> str:
        """Server/CDN identity used for diagnosis (e.g. 'cloudflare', 'nginx')."""
        server = str(headers.get("server", "") or "")
        if not server and headers.get("cf-ray"):
            server = "cloudflare"
        return server[:60]

    def _get_dynamic_extractor(self) -> Optional[DynamicExtractor]:
        """Starts the shared headless Chrome once (workers may ask for it concurrently)."""
        with self._dynamic_init_lock:
            if self.dynamic_extractor is None:
                parsed = urlparse(self.start_url)
                try:
                    self.dynamic_extractor = DynamicExtractor(
                        base_domain=self.base_domain,
                        user_agent=self.config.user_agent,
                        timeout=self.config.timeout,
                        cookies=self.auth_cookies,
                        logger=self.log,
                        origin=f"{parsed.scheme}://{parsed.netloc}"
                    )
                except Exception as e:
                    self.log(f"Dynamic extractor init exception: {e}")
            return self.dynamic_extractor

    def _extract_spa_bundle_routes(self, html_content: str, current_url: str) -> Set[str]:
        """
        Parses first-party JavaScript bundles referenced in the HTML to discover client-side
        routes that are not plain <a href> links: router configs (path: "/dashboard"), nav
        arrays (href: "/projects"), programmatic navigation (router.push("/ai")) and Next.js
        App Router page chunks (/_next/static/chunks/app/(group)/profile/page-<hash>.js).
        Each bundle URL is fetched at most once per crawl.
        """
        routes: Set[str] = set()
        if not html_content:
            return routes

        try:
            soup = BeautifulSoup(html_content, "html.parser")
            parsed_curr = urlparse(current_url)
            base_origin = f"{parsed_curr.scheme}://{parsed_curr.netloc}"

            def add_route(raw: str):
                if not raw or any(c in raw for c in (":", "[", "$", "{")):
                    return  # dynamic segments (/projects/:id, /[id], template strings)
                lower = raw.lower()
                if any(lower.endswith(ext) or f"{ext}?" in lower for ext in BUNDLE_ASSET_EXTENSIONS):
                    return
                if any(seg in lower for seg in BUNDLE_SKIP_SEGMENTS):
                    return
                clean = raw.rstrip('*').rstrip('/') or '/'
                route_url = normalize_url(clean, base_url=base_origin)
                if route_url and is_internal_url(route_url, self.base_domain, self.config.include_subdomains, self.allowed_domains):
                    if not is_excluded_path(route_url, self.config.exclude_patterns):
                        routes.add(route_url)

            # Only inspect first-party scripts (same domain or relative path)
            for s in soup.find_all("script", src=True):
                src = str(s.get("src") or "").strip()
                if not src:
                    continue
                # Skip external third-party tracking scripts
                if src.startswith("//") or any(tp in src.lower() for tp in ["google", "analytics", "facebook", "sentry", "hotjar", "clarity", "intercom"]):
                    continue
                if src.startswith("http://") or src.startswith("https://"):
                    script_domain = get_domain(src)
                    if script_domain != self.base_domain and script_domain not in self.allowed_domains:
                        continue

                full_js_url = normalize_url(src, base_url=current_url)
                if not full_js_url:
                    continue

                # Next.js App Router: the chunk path itself names the route
                chunk = NEXT_APP_CHUNK.search(urlparse(full_js_url).path)
                if chunk:
                    segments = [seg for seg in chunk.group(1).split("/") if seg and not (seg.startswith("(") or seg.startswith("@"))]
                    add_route("/" + "/".join(segments))

                with self._lock:
                    if full_js_url in self._scanned_bundles:
                        continue
                    self._scanned_bundles.add(full_js_url)

                try:
                    js_resp = self.session.get(full_js_url, timeout=min(self.config.timeout, 8))
                    if js_resp.status_code == 200 and js_resp.text:
                        for pattern in BUNDLE_ROUTE_PATTERNS:
                            for m in pattern.findall(js_resp.text):
                                add_route(m)
                except Exception:
                    pass
        except Exception:
            pass

        return routes

        try:
            soup = BeautifulSoup(html_content, "html.parser")
            parsed_curr = urlparse(current_url)
            base_origin = f"{parsed_curr.scheme}://{parsed_curr.netloc}"

            # Only inspect first-party scripts (same domain or relative path)
            for s in soup.find_all("script", src=True):
                src = str(s.get("src") or "").strip()
                if not src:
                    continue
                # Skip external third-party tracking scripts
                if src.startswith("//") or any(tp in src.lower() for tp in ["google", "analytics", "facebook", "sentry", "hotjar", "clarity", "intercom"]):
                    continue
                if src.startswith("http://") or src.startswith("https://"):
                    script_domain = get_domain(src)
                    if script_domain != self.base_domain and script_domain not in self.allowed_domains:
                        continue

                full_js_url = normalize_url(src, base_url=current_url)
                if not full_js_url:
                    continue

                try:
                    js_resp = self.session.get(full_js_url, timeout=min(self.config.timeout, 8))
                    if js_resp.status_code == 200 and js_resp.text:
                        # Extract route definitions: path: "...", to: "...", href: "..."
                        matches = re.findall(r'(?:path|to|href)\s*:\s*["\'](/[^"\'\s#?]+)["\']', js_resp.text)
                        for m in matches:
                            if any(ext in m.lower() for ext in ['.js', '.css', '.png', '.jpg', '.jpeg', '.svg', '.json', '.ico', '.woff', '.woff2', '.ttf', '.map', '.wasm']):
                                continue
                            clean = m.rstrip('*').rstrip('/')
                            if not clean:
                                clean = '/'
                            if any(api in clean.lower() for api in ['/api/', '/engine.io', '/socket.io', '/oauth/']):
                                continue

                            route_url = normalize_url(clean, base_url=base_origin)
                            if route_url and is_internal_url(route_url, self.base_domain, self.config.include_subdomains, self.allowed_domains):
                                if not is_excluded_path(route_url, self.config.exclude_patterns):
                                    routes.add(route_url)
                except Exception:
                    pass
        except Exception:
            pass

        return routes
