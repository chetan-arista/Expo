import re
import threading
import zlib
from urllib.parse import urljoin, urlparse
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
import requests
import urllib3
from .url_utils import normalize_url, is_internal_url
from .net_guard import SiteSession

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STANDARD_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/wp-sitemap.xml",
    "/sitemap/sitemap.xml",
    "/sitemap-index.xml"
]

_URL_ENTRY = re.compile(r"<(?:[\w-]+:)?url(?:\s[^>]*)?>(.*?)</(?:[\w-]+:)?url\s*>", re.I | re.S)
_ENTRY_LOC = re.compile(r"<(?:[\w-]+:)?loc>\s*(https?://[^<\s]+)\s*</(?:[\w-]+:)?loc>", re.I)
_ENTRY_LASTMOD = re.compile(r"<(?:[\w-]+:)?lastmod>\s*([^<]{1,64}?)\s*</(?:[\w-]+:)?lastmod>", re.I)
# <xhtml:link rel="alternate" hreflang="de" href="..."/> inside a <url> entry (any prefix, any attribute order)
_ENTRY_LINK = re.compile(r"<(?:[\w-]+:)?link\b([^>]*)>", re.I)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
MAX_HOSTS_SEEN = 1000   # distinct hosts remembered from <loc> entries and robots.txt (subdomain report)
MAX_SITEMAP_HREFLANG = 100_000   # hreflang alternates kept from sitemaps per crawl (memory); past it: capped


def url_host(url: str) -> str:
    """Lower-case host of an absolute http(s) URL without port or user info ("" when there is none)."""
    try:
        return (urlparse(str(url or "").strip()).hostname or "").rstrip(".")
    except ValueError:
        return ""


BUFFER_LIMIT_BYTES = 15 * 1024 * 1024   # a sitemap whose Content-Length is above this is never held in memory whole
PARSE_LIMIT_BYTES = 5 * 1024 * 1024     # ...its first 5MB (uncompressed) are parsed, the rest is only counted
MAX_SITEMAP_BYTES = 50 * 1024 * 1024    # sitemaps.org's limit: a gzipped file is unpacked up to this, the rest counted
STREAM_CHUNK = 256 * 1024
# One entry of a sitemap: <url> in a urlset, <sitemap> in a sitemap index (namespace prefix and attributes allowed;
# <urlset> and <sitemapindex> do not match)
_ENTRY_TAG = {False: re.compile(r"<(?:[\w-]+:)?url[\s>]", re.I), True: re.compile(r"<(?:[\w-]+:)?sitemap[\s>]", re.I)}
_ENTRY_TAG_BYTES = {k: re.compile(v.pattern.encode(), re.I) for k, v in _ENTRY_TAG.items()}
_TAG_TAIL = 64   # bytes kept between two streamed chunks so an entry tag split across them is still counted


class _BodyStream:
    """A big sitemap read in parts (HTTP compression undone; a gzipped file unzipped as it streams): the first
    part is parsed, the rest is counted (bytes and entries) and dropped, so memory stays flat."""

    def __init__(self, resp=None):
        self.resp = resp        # None: the whole body is in memory already (only unzipping is left)
        self.gzip = None        # zlib decompressor when the file itself is gzipped

    def read(self, n: int) -> bytes:
        if self.resp is None:
            return b""
        return self.resp.raw.read(n, decode_content=True) or b""

    def unzip(self, data: bytes, limit: int) -> bytes:
        """The first `limit` bytes of the gzipped file that `data` starts (several gzip members joined, as gzip
        allows); input not unpacked yet waits in self.gzip (unconsumed_tail / unused_data) for count_rest."""
        self.gzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
        parts: List[bytes] = []
        size = 0
        while data and size < limit:
            piece = self.gzip.decompress(data, limit - size)
            parts.append(piece)
            size += len(piece)
            if not self.gzip.eof or not self.gzip.unused_data.strip(b"\x00"):   # zero padding may follow
                break
            data = self.gzip.unused_data
            self.gzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
        return b"".join(parts)

    def count_rest(self, carry: bytes, is_index: bool,
                   stop_event: Optional[threading.Event] = None) -> Tuple[int, int, bool]:
        """(uncompressed bytes, entries, read to the end) for the part of the file after what was parsed;
        `carry` is the parsed part's last bytes (an entry tag split at the boundary is counted once)."""
        pattern = _ENTRY_TAG_BYTES[is_index]
        total = entries = 0

        def feed(data: bytes):
            nonlocal carry, total, entries
            total += len(data)
            window = carry + data
            entries += sum(1 for m in pattern.finditer(window) if m.end() > len(carry))
            carry = window[-_TAG_TAIL:]

        try:
            if self.gzip is None:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        return total, entries, False
                    chunk = self.read(STREAM_CHUNK)
                    if not chunk:
                        return total, entries, True
                    feed(chunk)
            data = self.gzip.unconsumed_tail
            full = True    # the parsed part may have stopped at the output limit: its held-back output first
            while True:
                if stop_event is not None and stop_event.is_set():
                    return total, entries, False
                if self.gzip.eof:   # end of one gzip member; another may follow (gzip allows several)
                    data = self.gzip.unused_data or self.read(STREAM_CHUNK)
                    if not data.strip(b"\x00"):
                        return total, entries, True
                    self.gzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
                elif not data and not full:   # all input used and no output held back
                    data = self.read(STREAM_CHUNK)
                    if not data:
                        return total, entries, False     # the file ended inside the gzip stream
                out = self.gzip.decompress(data, STREAM_CHUNK)
                full = len(out) >= STREAM_CHUNK
                data = self.gzip.unconsumed_tail
                if out:
                    feed(out)
        except Exception:
            return total, entries, False


def url_lastmods(xml_text: str) -> Dict[str, str]:
    """{<loc> as written: <lastmod> as written} for each <url> entry of a sitemap that has both."""
    out: Dict[str, str] = {}
    for entry in _URL_ENTRY.finditer(xml_text):
        body = entry.group(1)
        loc = _ENTRY_LOC.search(body)
        mod = _ENTRY_LASTMOD.search(body)
        if loc and mod:
            out.setdefault(loc.group(1), mod.group(1).strip())
    return out


def url_alternates(xml_text: str) -> Dict[str, List[Tuple[str, str]]]:
    """{<loc> as written: [(hreflang code lowercased, href as written)]} for each <url> entry of a sitemap that lists
    <xhtml:link rel="alternate" hreflang="..." href="..."> alternates (Google's sitemap method of hreflang)."""
    out: Dict[str, List[Tuple[str, str]]] = {}
    for entry in _URL_ENTRY.finditer(xml_text):
        body = entry.group(1)
        if "hreflang" not in body.lower():
            continue
        loc = _ENTRY_LOC.search(body)
        if not loc:
            continue
        alts: List[Tuple[str, str]] = []
        for link in _ENTRY_LINK.finditer(body):
            attrs = {k.lower(): (a if a else b) for k, a, b in _ATTR.findall(link.group(1))}
            if "alternate" not in (attrs.get("rel") or "").lower().split():
                continue
            lang = (attrs.get("hreflang") or "").strip().lower()
            href = (attrs.get("href") or "").strip().replace("&amp;", "&")
            if lang and href and (lang, href) not in alts:
                alts.append((lang, href))
        if alts:
            out.setdefault(loc.group(1), alts)
    return out


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
        max_urls: int = 1000,
        index_limit: int = 100000,
        include_subdomains: bool = False,
        headers: Optional[Dict[str, str]] = None,
        proxies: Optional[Dict[str, str]] = None,
        allowed_domains: Optional[Iterable[str]] = None,
        url_ok: Optional[Callable[[str], bool]] = None,
        on_site: Optional[Callable[[str], bool]] = None,
        extra_sitemaps: Optional[Iterable[str]] = None,
        robots_text: Optional[str] = None
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
        self.include_subdomains = include_subdomains
        # Hosts that count as the crawled site besides base_domain (the crawl's allowed set: extra subdomains
        # chosen under Find subdomains, hosts the start URL redirects to); a copy, read from this thread only
        self.allowed_domains: Set[str] = {str(d).lower() for d in (allowed_domains or ()) if d}
        # Optional guard asked before each request (robots.txt, sitemap files): False skips the URL. Subdomain
        # discovery uses it to stay on the audited domain and off private network addresses
        self.url_ok = url_ok
        self.session = SiteSession()
        self.session.headers.update({"User-Agent": self.user_agent})
        # The crawl's custom HTTP headers go only to the crawled site: robots.txt can name sitemaps on any host,
        # and a sitemap can redirect off the site, so each request and redirect hop is checked (`on_site`, else
        # the crawl's internal hosts). Then the proxy.
        self.session.set_site_headers(headers or {}, on_site or self._on_site)
        if proxies:
            self.session.proxies.update(proxies)
        self.parsed_sitemaps: Set[str] = set()
        # Sitemap addresses given in the crawl settings (read first, whatever robots.txt says) and a robots.txt text
        # to read the Sitemap lines from instead of fetching the live file (a custom robots.txt)
        self.extra_sitemaps: List[str] = [str(u).strip() for u in (extra_sitemaps or ()) if str(u or "").strip()]
        self.robots_text = robots_text
        # Every page URL listed in a parsed sitemap -> the sitemaps that list it. Unlike the list
        # parse_all returns (capped at max_urls for queueing), this keeps up to index_limit URLs.
        self.index_limit = index_limit
        self.url_sources: Dict[str, List[str]] = {}
        # Set when the index was full: {"limit", "file" (the sitemap it stopped in), "left_out" (internal page URLs
        # listed after that; a URL listed in two files counts twice)}; URLs past it are not known to the crawl
        self.index_full: Optional[Dict[str, object]] = None
        # hreflang alternates of indexed page URLs: url -> [{"lang", "url"}] (normalized), at most MAX_SITEMAP_HREFLANG
        self.url_hreflang: Dict[str, List[Dict[str, str]]] = {}
        self.hreflang_capped = False
        self._hreflang_kept = 0
        # <lastmod> as written (trimmed) per listed page URL, first sitemap that gives one wins
        self.url_lastmod: Dict[str, str] = {}
        # Per sitemap file read: {"urls": <loc> entries (URLs, or sitemaps for an index), "size_bytes":
        # uncompressed size, "index": bool} (Sitemaps tab: over 50,000 URLs / over 50MB)
        self.sitemap_stats: Dict[str, dict] = {}
        # Sitemap files listed (robots.txt, sitemap indexes) but not read: at most max_sitemaps files are read
        self.not_read: List[str] = []
        # Every host seen in the sitemaps -> page URLs listed for it (counted before the internal-URL filter, so
        # other subdomains of the site show up; a host holding only sitemap files counts 0). Hosts named in
        # robots.txt (Sitemap: and Host: lines) are kept in robots_hosts. At most MAX_HOSTS_SEEN each.
        self.hosts_seen: Dict[str, int] = {}
        self.robots_hosts: Set[str] = set()

    def _on_site(self, url: str) -> bool:
        """Whether `url` is on the crawled site (base domain or the allowed set): its custom headers go there only."""
        return is_internal_url(url, self.base_domain, self.include_subdomains, self.allowed_domains)

    def _see_host(self, url: str, pages: int = 0):
        host = url_host(url)
        if host and (host in self.hosts_seen or len(self.hosts_seen) < MAX_HOSTS_SEEN):
            self.hosts_seen[host] = self.hosts_seen.get(host, 0) + pages

    def _allowed(self, url: str) -> bool:
        """The url_ok guard's answer (True when there is no guard); a refused URL is logged, never fetched."""
        if self.url_ok is None:
            return True
        try:
            ok = bool(self.url_ok(url))
        except Exception:
            ok = False
        if not ok:
            self.log(f"Skipped {url}: outside the site or on a private network address.")
        return ok

    def log(self, message: str):
        self.logger(f"[Sitemap] {message}")

    def _not_read(self, sitemaps: List[str], listed_in: str):
        """Remembers sitemap files left unread because max_sitemaps files were read already."""
        left = [s for s in sitemaps if s not in self.parsed_sitemaps and s not in self.not_read]
        if left:
            self.not_read.extend(left)
            self.log(f"{len(left)} more sitemap file(s) listed in {listed_in} were not read: at most "
                     f"{self.max_sitemaps} sitemap files are read per crawl.")

    def _robots_lines(self, robots_url: str) -> Optional[List[str]]:
        """The lines of the robots.txt to read Sitemap / Host lines from: the custom text when one was given, else
        the live file (None when it could not be read)."""
        if self.robots_text is not None:
            self.log("Reading the Sitemap lines of the custom robots.txt (the live file is not used).")
            return self.robots_text.splitlines()
        if not self._allowed(robots_url):
            return None
        try:
            r = self.session.get(robots_url, timeout=self.timeout, verify=self.verify_ssl)
        except Exception as e:
            self.log(f"Could not fetch robots.txt: {e}")
            return None
        if r.status_code != 200:
            return None
        self.log(f"Inspecting {robots_url}")
        return r.text.splitlines()

    def discover_sitemap_urls(self) -> List[str]:
        """Find sitemap URLs: the addresses given in the settings first, then robots.txt's Sitemap lines, and the
        standard endpoints when robots.txt names none."""
        if self.stop_event and self.stop_event.is_set():
            return []

        discovered: List[str] = []
        for sm_url in self.extra_sitemaps:
            if sm_url.lower().startswith(("http://", "https://")) and sm_url not in discovered:
                discovered.append(sm_url)
                self._see_host(sm_url)
                self.log(f"Sitemap given in the crawl settings: {sm_url}")
        given = len(discovered)
        named = 0       # Sitemap lines in robots.txt
        parsed = urlparse(self.base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # 1. Check robots.txt
        robots_url = f"{origin}/robots.txt"
        if self.robots_text is None and not self._allowed(robots_url):
            self._not_read(discovered[self.max_sitemaps:], "the crawl settings")
            return discovered[:self.max_sitemaps]
        try:
            lines = self._robots_lines(robots_url)
            if lines is not None:
                for line in lines:
                    line = line.strip()
                    if line.lower().startswith("host:"):
                        # Yandex's Host directive names the site's main host
                        named = line.split(":", 1)[1].split("#", 1)[0].strip()
                        host = url_host(named if "://" in named else "http://" + named)
                        if host and len(self.robots_hosts) < MAX_HOSTS_SEEN:
                            self.robots_hosts.add(host)
                    if line.lower().startswith("sitemap:"):
                        # "#" starts a comment (RFC 9309); "//host/..." and "/path" are read from robots.txt
                        sm_url = line.split(":", 1)[1].split("#", 1)[0].strip()
                        if sm_url and not sm_url.lower().startswith(("http://", "https://")):
                            sm_url = urljoin(robots_url, sm_url)
                        named += 1 if sm_url else 0
                        if sm_url and sm_url not in discovered:
                            discovered.append(sm_url)
                            self._see_host(sm_url)
                            host = url_host(sm_url)
                            if host and len(self.robots_hosts) < MAX_HOSTS_SEEN:
                                self.robots_hosts.add(host)
                            self.log(f"Found sitemap in robots.txt: {sm_url}")
        except Exception as e:
            self.log(f"Could not read robots.txt: {e}")

        # 2. Check standard endpoints (only when robots.txt names no sitemap)
        if not named:
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
                            self._see_host(test_url)
                            self.log(f"Discovered sitemap at standard path: {test_url}")
                    except Exception:
                        pass

        self._not_read(discovered[self.max_sitemaps:], "the crawl settings and robots.txt" if given else "robots.txt")
        return discovered[:self.max_sitemaps]

    def parse_all(self) -> List[Tuple[str, str]]:
        """
        Discover and parse sitemaps fast with streaming regex.
        Returns a list of (normalized_page_url, sitemap_source_url).
        """
        sitemap_urls = self.discover_sitemap_urls()
        discovered_urls: List[Tuple[str, str]] = []

        for i, sm_url in enumerate(sitemap_urls):
            if self.stop_event and self.stop_event.is_set():
                break
            if len(self.parsed_sitemaps) >= self.max_sitemaps:
                self._not_read(sitemap_urls[i:], "robots.txt")
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

    def _index_left_out(self, sitemap_url: str):
        """Counts a page URL the full index could not hold (the first one names the file and is logged)."""
        if self.index_full is None:
            self.index_full = {"limit": self.index_limit, "file": sitemap_url, "left_out": 0}
            self.log(f"The sitemap URL index is full ({self.index_limit:,} URLs) at {sitemap_url}: URLs listed after "
                     "that are counted but not kept, so they are Not checked in the sitemap comparisons.")
        self.index_full["left_out"] = int(self.index_full.get("left_out") or 0) + 1

    def _keep_alternates(self, page_url: str, alts: List[Tuple[str, str]]):
        """Keeps a page's sitemap hreflang alternates (normalized against the page) up to MAX_SITEMAP_HREFLANG."""
        kept: List[Dict[str, str]] = []
        for lang, href in alts:
            url = normalize_url(href, page_url) or ""
            if not url:
                continue
            if self._hreflang_kept >= MAX_SITEMAP_HREFLANG:
                if not self.hreflang_capped:
                    self.hreflang_capped = True
                    self.log(f"More than {MAX_SITEMAP_HREFLANG:,} hreflang alternates in the sitemaps: the rest are "
                             "not kept, so the pages they belong to are not checked for sitemap hreflang.")
                break
            kept.append({"lang": lang, "url": url})
            self._hreflang_kept += 1
        if kept:
            self.url_hreflang[page_url] = kept

    def _crawl_sitemap_recursive(self, sitemap_url: str, depth: int = 0) -> List[Tuple[str, str]]:
        if depth > 2 or sitemap_url in self.parsed_sitemaps or len(self.parsed_sitemaps) >= self.max_sitemaps:
            return []
        if self.stop_event and self.stop_event.is_set():
            return []

        self.parsed_sitemaps.add(sitemap_url)
        results: List[Tuple[str, str]] = []
        self._see_host(sitemap_url)
        if not self._allowed(sitemap_url):
            return []

        try:
            self.log(f"Fetching sitemap: {sitemap_url}")
            resp = self.session.get(sitemap_url, timeout=self.timeout, verify=self.verify_ssl, stream=True)
            if resp.status_code != 200:
                self.log(f"Sitemap {sitemap_url} returned status {resp.status_code}")
                return []

            content_length = resp.headers.get("Content-Length")
            # Don't buffer > 15MB to protect container memory: the first 5MB are parsed, the rest is only
            # counted (size and <loc> entries) as it streams past
            big = bool(content_length and content_length.strip().isdigit() and
                       int(content_length) > BUFFER_LIMIT_BYTES)
            if big:
                stream = _BodyStream(resp)
                raw_bytes = stream.read(PARSE_LIMIT_BYTES)
            else:
                stream = None
                raw_bytes = resp.content

            # Gzipped sitemaps: the bytes decide, not the name (a .gz sent with Content-Encoding: gzip arrives
            # already unpacked, and some .gz files are plain XML). Unpacked up to a limit and the rest only
            # counted, so a small download that unpacks to gigabytes cannot fill the server's memory
            if raw_bytes[:2] == b"\x1f\x8b":
                try:
                    limit = PARSE_LIMIT_BYTES if stream is not None else MAX_SITEMAP_BYTES
                    stream = stream or _BodyStream()
                    raw_bytes = stream.unzip(raw_bytes, limit)
                except Exception as e:
                    self.log(f"Failed to decompress gz sitemap {sitemap_url}: {e}")
                    return []

            xml_text = raw_bytes.decode("utf-8", errors="replace")

            # Fast regex extraction of <loc> tags (takes milliseconds even for 50k URLs)
            loc_matches = re.findall(r"<loc>\s*(https?://[^<\s]+)\s*</loc>", xml_text, re.IGNORECASE)
            lastmods = url_lastmods(xml_text) if "lastmod" in xml_text[:200000].lower() else {}

            # Check if this is a sitemap index (contains <sitemapindex> or <sitemap>)
            header_sample = xml_text[:5000].lower()
            is_index = "<sitemapindex" in header_sample or ("<sitemap>" in header_sample and depth == 0)

            # The file's uncompressed size and entry count (a big file: counted to its end, never kept whole)
            size: Optional[int] = len(raw_bytes)
            entries = len(_ENTRY_TAG[is_index].findall(xml_text))
            if stream is not None:
                more_bytes, more_entries, complete = stream.count_rest(raw_bytes[-_TAG_TAIL:], is_index,
                                                                       self.stop_event)
                resp.close()
                entries += more_entries
                if more_entries:
                    self.log(f"Sitemap {sitemap_url} is {'at least ' if not complete else ''}"
                             f"{(size + more_bytes) / 1048576:.1f}MB: only the URLs in its first "
                             f"{size / 1048576:.1f}MB were read; {more_entries:,} more entries were counted.")
                if complete:
                    size += more_bytes
                else:   # entries stay a lower bound; an unknown size is never reported as small
                    size = None
                    self.log(f"Sitemap {sitemap_url} could not be read to its end, so its size is not known.")
            self.sitemap_stats[sitemap_url] = {"urls": entries, "size_bytes": size, "index": is_index}

            if is_index:
                self.log(f"Found {len(loc_matches)} sub-sitemaps in {sitemap_url}")
                for sub_sm in loc_matches[:MAX_HOSTS_SEEN]:
                    self._see_host(sub_sm)
                for i, sub_sm in enumerate(loc_matches):
                    if self.stop_event and self.stop_event.is_set():
                        break
                    if len(self.parsed_sitemaps) >= self.max_sitemaps:
                        self._not_read(loc_matches[i:], sitemap_url)
                        break
                    results.extend(self._crawl_sitemap_recursive(sub_sm, depth + 1))
            else:
                # Page URLs: every entry's host is counted (the index stops at index_limit URLs; past it the URLs
                # are only counted, index_full)
                for raw_loc in loc_matches:
                    self._see_host(raw_loc, 1)
                alternates = url_alternates(xml_text) if "hreflang" in xml_text.lower() else {}
                source = f"sitemap: {sitemap_url}"
                for raw_loc in loc_matches:
                    if self.stop_event and self.stop_event.is_set():
                        break
                    norm_url = normalize_url(raw_loc.replace("&amp;", "&"), self.base_url)
                    if norm_url and is_internal_url(norm_url, self.base_domain, self.include_subdomains,
                                                    self.allowed_domains or None):
                        sources = self.url_sources.get(norm_url)
                        if sources is None:
                            if len(self.url_sources) >= self.index_limit:
                                self._index_left_out(sitemap_url)
                                continue
                            self.url_sources[norm_url] = [sitemap_url]
                        elif sitemap_url not in sources:
                            sources.append(sitemap_url)
                        lastmod = lastmods.get(raw_loc)
                        if lastmod and norm_url not in self.url_lastmod:
                            self.url_lastmod[norm_url] = lastmod
                        alts = alternates.get(raw_loc)
                        if alts and norm_url not in self.url_hreflang:
                            self._keep_alternates(norm_url, alts)
                        if len(results) < self.max_urls:
                            results.append((norm_url, source))

        except Exception as e:
            self.log(f"Error parsing sitemap {sitemap_url}: {e}")

        return results
