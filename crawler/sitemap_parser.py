import gzip
import re
import threading
from urllib.parse import urlparse
from typing import List, Tuple, Set, Optional, Callable
import requests
import urllib3
from .url_utils import normalize_url, is_internal_url

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STANDARD_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/wp-sitemap.xml",
    "/sitemap/sitemap.xml",
    "/sitemap-index.xml"
]

class SitemapParser:
    def __init__(
        self,
        base_url: str,
        base_domain: str,
        user_agent: str,
        timeout: int = 5,
        verify_ssl: bool = False,
        logger: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
        max_sitemaps: int = 8,
        max_urls: int = 1000
    ):
        self.base_url = base_url
        self.base_domain = base_domain
        self.user_agent = user_agent
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.logger = logger or (lambda msg: None)
        self.stop_event = stop_event
        self.max_sitemaps = max_sitemaps
        self.max_urls = max_urls
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})
        self.parsed_sitemaps: Set[str] = set()

    def log(self, message: str):
        self.logger(f"[Sitemap] {message}")

    def discover_sitemap_urls(self) -> List[str]:
        """Find sitemap URLs from robots.txt and standard endpoints."""
        if self.stop_event and self.stop_event.is_set():
            return []

        discovered = []
        parsed = urlparse(self.base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # 1. Check robots.txt
        robots_url = f"{origin}/robots.txt"
        try:
            r = self.session.get(robots_url, timeout=self.timeout, verify=self.verify_ssl)
            if r.status_code == 200:
                self.log(f"Inspecting {robots_url}")
                for line in r.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("sitemap:"):
                        sm_url = line.split(":", 1)[1].strip()
                        if sm_url.startswith("//"):
                            sm_url = f"{parsed.scheme}:{sm_url}"
                        if sm_url and sm_url not in discovered:
                            discovered.append(sm_url)
                            self.log(f"Found sitemap in robots.txt: {sm_url}")
        except Exception as e:
            self.log(f"Could not fetch robots.txt: {e}")

        # 2. Check standard endpoints (only if not found or limited)
        if not discovered:
            for path in STANDARD_SITEMAP_PATHS:
                if self.stop_event and self.stop_event.is_set():
                    break
                test_url = f"{origin}{path}"
                if test_url not in discovered:
                    try:
                        head_res = self.session.head(
                            test_url, timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True
                        )
                        content_type = head_res.headers.get("content-type", "").lower()
                        if head_res.status_code == 200 and ("xml" in content_type or "text" in content_type):
                            discovered.append(test_url)
                            self.log(f"Discovered sitemap at standard path: {test_url}")
                    except Exception:
                        pass

        return discovered[:self.max_sitemaps]

    def parse_all(self) -> List[Tuple[str, str]]:
        """
        Discover and parse sitemaps fast with streaming regex.
        Returns a list of (normalized_page_url, sitemap_source_url).
        """
        sitemap_urls = self.discover_sitemap_urls()
        discovered_urls: List[Tuple[str, str]] = []

        for sm_url in sitemap_urls:
            if self.stop_event and self.stop_event.is_set():
                break
            if len(self.parsed_sitemaps) >= self.max_sitemaps or len(discovered_urls) >= self.max_urls:
                break
            discovered_urls.extend(self._crawl_sitemap_recursive(sm_url, depth=0))

        # Deduplicate while preserving order
        unique_urls = []
        seen = set()
        for page_url, sm_source in discovered_urls:
            if page_url not in seen:
                seen.add(page_url)
                unique_urls.append((page_url, sm_source))
                if len(unique_urls) >= self.max_urls:
                    break

        self.log(f"Total unique internal URLs found via sitemaps: {len(unique_urls)}")
        return unique_urls

    def _crawl_sitemap_recursive(self, sitemap_url: str, depth: int = 0) -> List[Tuple[str, str]]:
        if depth > 2 or sitemap_url in self.parsed_sitemaps or len(self.parsed_sitemaps) >= self.max_sitemaps:
            return []
        if self.stop_event and self.stop_event.is_set():
            return []

        self.parsed_sitemaps.add(sitemap_url)
        results: List[Tuple[str, str]] = []

        try:
            self.log(f"Fetching sitemap: {sitemap_url}")
            resp = self.session.get(sitemap_url, timeout=self.timeout, verify=self.verify_ssl, stream=True)
            if resp.status_code != 200:
                self.log(f"Sitemap {sitemap_url} returned status {resp.status_code}")
                return []

            content_length = resp.headers.get("Content-Length")
            # Don't buffer > 15MB to protect container memory
            if content_length and int(content_length) > 15 * 1024 * 1024:
                raw_bytes = resp.raw.read(5 * 1024 * 1024)
            else:
                raw_bytes = resp.content

            # Handle gzipped sitemaps
            if sitemap_url.endswith(".gz") or raw_bytes[:2] == b"\x1f\x8b":
                try:
                    raw_bytes = gzip.decompress(raw_bytes)
                except Exception as e:
                    self.log(f"Failed to decompress gz sitemap {sitemap_url}: {e}")
                    return []

            xml_text = raw_bytes.decode("utf-8", errors="replace")

            # Fast regex extraction of <loc> tags (takes milliseconds even for 50k URLs)
            loc_matches = re.findall(r"<loc>\s*(https?://[^<\s]+)\s*</loc>", xml_text, re.IGNORECASE)

            # Check if this is a sitemap index (contains <sitemapindex> or <sitemap>)
            header_sample = xml_text[:5000].lower()
            is_index = "<sitemapindex" in header_sample or ("<sitemap>" in header_sample and depth == 0)

            if is_index:
                sub_sitemaps = loc_matches[:self.max_sitemaps]
                self.log(f"Found {len(sub_sitemaps)} sub-sitemaps in {sitemap_url}")
                for sub_sm in sub_sitemaps:
                    if self.stop_event and self.stop_event.is_set():
                        break
                    if len(self.parsed_sitemaps) >= self.max_sitemaps:
                        break
                    results.extend(self._crawl_sitemap_recursive(sub_sm, depth + 1))
                    if len(results) >= self.max_urls:
                        break
            else:
                # Page URLs
                for raw_loc in loc_matches:
                    if self.stop_event and self.stop_event.is_set():
                        break
                    norm_url = normalize_url(raw_loc, self.base_url)
                    if norm_url and is_internal_url(norm_url, self.base_domain):
                        results.append((norm_url, f"sitemap: {sitemap_url}"))
                        if len(results) >= self.max_urls:
                            break

        except Exception as e:
            self.log(f"Error parsing sitemap {sitemap_url}: {e}")

        return results
