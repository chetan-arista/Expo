"""
Subdomains: find the hosts of the audited domain, crawl the ones the person ticks, and explain after a crawl which
hosts of the domain ARACHNIX saw and why the ones it did not crawl were left out.

- discover(url, sources, extra_hosts, timeout): candidate hosts of the audited registrable domain (url_utils.
  registrable_domain) from the links of the loaded crawl (else of the home page), the XML sitemaps and robots.txt,
  the public certificate transparency log at crt.sh (one request; wildcard names folded), the Search Console
  property list when it is connected, and hosts typed in. Each candidate is looked up in DNS (an address on a
  private / internal network is never opened) and its home page fetched once (https, http only when https gets no
  answer) for the status and title. At most MAX_HOSTS hosts. start_job / get_job run it in the background.
- extra_host_urls(values, start_url): the home pages a Spider crawl adds as extra start URLs (CrawlConfig.
  extra_hosts); check_extra_hosts drops the ones without DNS or on a private address before the crawl starts.
- subdomain_report(crawler): every host of the domain seen in the crawl (links, resources, redirects, sitemaps),
  in the discovery and in the extra hosts, with the URLs crawled on it, the crawled pages linking to it, whether it
  is in scope and a plain reason (GET /api/crawl/subdomains).

Hosts outside the audited registrable domain are never requested. "www." and the bare name are one site (as the
crawler treats them), so they share a row in the report.
"""
import html as html_lib
import ipaddress
import json
import re
import socket
import threading
import time
import uuid
import weakref
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

from .config import DEFAULT_USER_AGENT
from .net_guard import classify_error, detect_waf, error_message
from .url_utils import (clean_hostname, host_in_domain, is_internal_address, is_internal_url, is_login_url,
                        join_url, redirected_to_login, registrable_domain)

# Sources the person can choose; hosts typed in are always checked
SOURCES = ("links", "sitemaps", "ct", "gsc")
# Where a host was seen, in display order (discovery and report)
SOURCE_ORDER = ("start", "typed", "extra", "links", "home_links", "resources", "redirects", "sitemaps", "robots", "gsc",
                "ct")
# "home_links": a link on the home page of another host the discovery found (not a page of the audited site)
SOURCE_LABELS = {
    "start": "the audited site", "typed": "typed in", "extra": "chosen for the crawl",
    "links": "links on the site's pages", "home_links": "links on the home pages of the other hosts found",
    "resources": "images, scripts or CSS on crawled pages",
    "redirects": "a redirect", "sitemaps": "the XML sitemaps", "robots": "robots.txt",
    "gsc": "Search Console", "ct": "certificate logs",
}
# Priority when more than MAX_HOSTS candidates turn up
_PRIORITY = {s: i for i, s in enumerate(("typed", "links", "home_links", "sitemaps", "robots", "gsc", "ct"))}
MAX_LINKED_BY = 5                   # linking hosts kept per host (the reason names the first one)
MAX_HOSTS = 200
CT_URL = "https://crt.sh/"          # tests point it at a local fixture
CT_TIMEOUT = 20.0
CT_MAX_BYTES = 25 * 1024 * 1024
HTTPS_PORT = 443                    # ports the home-page check connects to (tests point them at local fixtures)
HTTP_PORT = 80
PROBE_TIMEOUT = 8.0
PROBE_WORKERS = 6
PAGE_BYTES = 512 * 1024             # start of a home page read for its title and links
SITEMAP_FILES = 5                   # sitemap files read by a discovery without a loaded crawl
MAX_REDIRECTS = 3                   # the home page of the audited site, followed within the domain
MAX_RUNNING_JOBS = 2
MAX_FINISHED_JOBS = 50              # finished searches kept for polling (the oldest are dropped first)
REPORT_REUSE_SECONDS = 5.0          # a running crawl's subdomain report is rebuilt at most this often
JOB_KEEP_SECONDS = 3600
SCOPE_LABELS = {"subdomain": "Subdomain", "subfolder": "Subfolder", "all_subdomains": "All Subdomains",
                "exact_url": "Exact URL"}
# reason_code of the report, in display order
REASON_CODES = ("crawled", "needs_signin", "robots_blocked", "not_reached", "out_of_scope", "not_linked",
                "no_dns", "private_address")

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_ATTR_URL = re.compile(r"""(?:href|src|action|content)\s*=\s*["']?\s*((?:https?:)?//[^"'\s<>]+)""", re.I)
_CT_NAME = re.compile(r'"(?:name_value|common_name)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_PASSWORD_FIELD = re.compile(r"""<input[^>]+type\s*=\s*["']?password""", re.I)
_APP_ROOT = re.compile(r"""\bid\s*=\s*["']?(?:root|__next|app|__nuxt|q-app)["'\s>]|<app-root[\s>]|\bng-version\s*=""",
                       re.I)


class DiscoveryBusy(RuntimeError):
    """Too many discoveries are running on this server; try again in a moment."""


# ---------------------------------------------------------------------------------------------
# Host names and addresses
# ---------------------------------------------------------------------------------------------
def _is_ip(text: str) -> bool:
    try:
        import ipaddress
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False


def clean_host(value: Any) -> str:
    """A bare host name from what a person types or a log lists: scheme, path, port, user info and a "*." prefix
    removed, lower case, IDN as punycode, no trailing dot. "" when it is not a usable name (an IP address, one
    label, a label with "_" or other characters a web host cannot have)."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = re.split(r"[/?#\s]", text, 1)[0].rsplit("@", 1)[-1]
    if text.startswith("["):
        return ""
    text = text.split(":", 1)[0].strip().rstrip(".")
    while text.startswith("*."):
        text = text[2:]
    if not text.isascii():
        try:
            text = text.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    if len(text) > 253 or "." not in text or _is_ip(text):
        return ""
    if not all(_LABEL.match(label) for label in text.split(".")):
        return ""
    return text


def _url_host(url: str) -> str:
    """Host of a normalized absolute URL, fast (no full parse): lower case, no port, no user info."""
    try:
        rest = url.split("://", 1)[1]
    except (IndexError, AttributeError):
        return ""
    netloc = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1]
    if netloc.startswith("["):
        return netloc.split("]", 1)[0] + "]"
    return netloc.split(":", 1)[0].lower().rstrip(".")


def _system_resolve(host: str) -> List[str]:
    """IP addresses of `host` (IPv4 first); [] when the name does not resolve."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return []
    out: List[str] = []
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]
        if ip not in out:
            out.append(ip)
    out.sort(key=lambda ip: ":" in ip)
    return out


_resolver: Dict[str, Callable[[str], List[str]]] = {"fn": _system_resolve}


def set_resolver(fn: Optional[Callable[[str], List[str]]]) -> Callable[[str], List[str]]:
    """Replaces the DNS lookup used by discovery and the extra-host check (tests map names to 127.0.0.1); None
    restores the system resolver. Returns the previous one."""
    previous = _resolver["fn"]
    _resolver["fn"] = fn or _system_resolve
    return previous


def resolve(host: str) -> List[str]:
    try:
        return list(_resolver["fn"](host) or [])
    except Exception:
        return []


def private_only(addresses: Iterable[str]) -> bool:
    """True when every address is on a private / internal network (and there is at least one)."""
    ips = list(addresses)
    return bool(ips) and all(is_internal_address(ip) for ip in ips)


# Cloud metadata services by name: never opened, whatever they resolve to
_METADATA_NAMES = {"metadata", "metadata.google.internal", "instance-data", "instance-data.ec2.internal"}


def _ip(address: str):
    try:
        ip = ipaddress.ip_address(str(address or "").split("%", 1)[0].strip("[] "))
    except ValueError:
        return None
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped is not None else ip


def loopback_only(addresses: Iterable[str]) -> bool:
    """True when every address is on this machine (127.0.0.0/8, ::1) and there is at least one: a local audit."""
    ips = [_ip(a) for a in addresses]
    return bool(ips) and all(ip is not None and ip.is_loopback for ip in ips)


def never_open(address: str, loopback_ok: bool = False) -> bool:
    """Addresses ARACHNIX never opens for another host, even in an intranet audit (the intranet rule allows private
    networks only): link-local (cloud metadata at 169.254.169.254), unspecified, multicast and reserved addresses,
    and this machine (loopback) unless the audited site is itself on this machine (`loopback_ok`)."""
    ip = _ip(address)
    if ip is None:
        return True
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return True
    return ip.is_loopback and not loopback_ok


def usable_addresses(ips: Iterable[str], allow_private: bool = False, loopback_ok: bool = False) -> List[str]:
    """The addresses of a candidate host ARACHNIX may connect to: public ones, private ones in an intranet audit
    (`allow_private`), never link-local / metadata / unspecified ones, loopback only in a local audit."""
    return [ip for ip in ips if not never_open(ip, loopback_ok) and (allow_private or not is_internal_address(ip))]


def _metadata_name(host: str) -> bool:
    return (host or "").lower().rstrip(".") in _METADATA_NAMES


def _sort_key(host: str) -> Tuple[str, ...]:
    """Groups a domain's hosts by their labels read from the right (example.com, a.example.com, b.a.example.com)."""
    return tuple(reversed(host.split(".")))


def _ordered_sources(sources: Iterable[str]) -> List[str]:
    known = [s for s in SOURCE_ORDER if s in sources]
    return known + sorted(s for s in sources if s not in SOURCE_ORDER)


# ---------------------------------------------------------------------------------------------
# One request, pinned to the checked address
# ---------------------------------------------------------------------------------------------
class _PinnedAdapter(HTTPAdapter):
    """Connects to the IP address that was checked while the request keeps its host name (Host header and TLS
    SNI), so the address ARACHNIX checked is the address it opens: a second DNS answer cannot point elsewhere."""

    def __init__(self, ip: str):
        self.ip = ip
        super().__init__(max_retries=0, pool_connections=1, pool_maxsize=2)

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        host = parts.hostname or ""
        netloc = f"[{self.ip}]" if ":" in self.ip else self.ip
        if parts.port:
            netloc += f":{parts.port}"
        request.headers["Host"] = parts.netloc.rsplit("@", 1)[-1]
        if parts.scheme == "https":
            self.poolmanager.connection_pool_kw["server_hostname"] = host
            self.poolmanager.connection_pool_kw["assert_hostname"] = host if kwargs.get("verify") else False
        request.url = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        return super().send(request, **kwargs)


class _Page:
    __slots__ = ("url", "status", "headers", "text", "location")

    def __init__(self, url: str, status: int, headers: Dict[str, str], text: str, location: str):
        self.url, self.status, self.headers, self.text, self.location = url, status, headers, text, location


def _fetch(url: str, ip: str, *, timeout: float, user_agent: str, proxies: Optional[Dict[str, str]] = None,
           verify: bool = False, headers: Optional[Dict[str, str]] = None, max_bytes: int = PAGE_BYTES) -> _Page:
    """One GET of `url` (redirects not followed) connected to `ip`; the start of an HTML or text body is read.
    Through a proxy the proxy resolves the name (the address was still checked here first)."""
    session = requests.Session()
    try:
        session.headers.update({
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        session.headers.update(headers or {})
        if proxies:
            session.proxies.update(proxies)
        elif not requests.utils.get_environ_proxies(url):
            session.mount("https://", _PinnedAdapter(ip))
            session.mount("http://", _PinnedAdapter(ip))
        resp = session.get(url, timeout=(min(5.0, timeout), timeout), allow_redirects=False, stream=True,
                           verify=verify)
        try:
            low = {k.lower(): v for k, v in resp.headers.items()}
            ctype = low.get("content-type", "").lower()
            text = ""
            if max_bytes and (not ctype or "html" in ctype or "text" in ctype or "xml" in ctype):
                raw = resp.raw.read(max_bytes, decode_content=True) or b""
                text = raw.decode(resp.encoding or "utf-8", errors="replace")
            location = low.get("location", "")
            return _Page(url, resp.status_code, low, text, join_url(url, location) if location else "")
        finally:
            resp.close()
    finally:
        session.close()


def page_title(text: str) -> str:
    m = _TITLE.search(text or "")
    if not m:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(m.group(1))).strip()[:200]


def _hosts_in_text(text: str) -> Set[str]:
    """Hosts of the absolute and protocol-relative URLs in href / src / action / content attributes."""
    out: Set[str] = set()
    for m in _ATTR_URL.finditer(text or ""):
        raw = html_lib.unescape(m.group(1))
        url = ("https:" + raw) if raw.startswith("//") else raw
        host = clean_host(url)
        if host:
            out.add(host)
        if len(out) >= 2000:
            break
    return out


def _short_error(exc: Optional[BaseException]) -> str:
    """A failed request in a few plain words ("connection refused", "no answer in time")."""
    if exc is None:
        return "unable to connect"
    kind = classify_error(exc)
    if kind == "timeout" or isinstance(exc, TimeoutError):
        return "no answer in time"
    return error_message(kind).split(":", 1)[0].strip().lower()


def _status_note(page: _Page, waf: str, scheme: str, https_failed: str) -> str:
    code = page.status
    if waf:
        note = (f"{waf} bot protection answered ({code}). ARACHNIX doesn't get past bot checks: open the host in "
                "your own browser to see it.")
    elif 300 <= code < 400:
        note = f"Redirects to {page.location}" if page.location else f"Redirect ({code}) without a target"
    elif code == 401:
        note = "Asks for a sign-in (401)."
    elif code == 403:
        note = "Access refused (403): it may need a sign-in."
    elif code in (404, 410):
        note = f"The home page is not found ({code})."
    elif code == 429:
        note = "Too many requests (429): the server asked ARACHNIX to slow down."
    elif code >= 500:
        note = f"Server error ({code})."
    elif code >= 400:
        note = f"Error answer ({code})."
    elif is_login_url(page.url):
        note = "Shows a sign-in page."
    elif _PASSWORD_FIELD.search(page.text or ""):
        note = "Shows a sign-in form."
    elif not page_title(page.text) and _APP_ROOT.search(page.text or ""):
        note = "Loads; its HTML has no title because the page is built by JavaScript."
    else:
        note = ""
    if scheme == "http" and https_failed:
        note = (note + " " if note else "") + f"Answers on http only (https: {https_failed})."
    return note


def probe_host(host: str, allow_private: bool = False, timeout: float = PROBE_TIMEOUT, user_agent: str = "",
               proxies: Optional[Dict[str, str]] = None, verify_ssl: bool = False,
               loopback_ok: bool = False) -> Dict[str, Any]:
    """DNS lookup and one GET of https://host/ (http://host/ only when https gets no answer at all).
    {"resolves", "status", "title", "note", "state", "url", "redirect", "links"}; state is "ok" (it answered),
    "no_dns", "private" (only private / internal addresses: never opened), or "no_answer". Link-local / cloud
    metadata addresses are never opened, loopback only when `loopback_ok` (usable_addresses)."""
    out: Dict[str, Any] = {"resolves": None, "status": None, "title": "", "note": "", "state": "not_checked",
                           "url": "", "redirect": "", "links": set()}
    ips = resolve(host)
    if not ips:
        out.update(resolves=False, state="no_dns",
                   note="Doesn't resolve: the name has no DNS record, so there is no site to open.")
        return out
    out["resolves"] = True
    usable = [] if _metadata_name(host) else usable_addresses(ips, allow_private, loopback_ok)
    if not usable:
        out.update(state="private", note="Points to a private / internal network address, so ARACHNIX doesn't "
                                         "open it.")
        return out
    https_failed = ""
    last_exc: Optional[BaseException] = None
    for scheme, port, default in (("https", HTTPS_PORT, 443), ("http", HTTP_PORT, 80)):
        url = f"{scheme}://{host}{'' if port == default else f':{port}'}/"
        try:
            page = _fetch(url, usable[0], timeout=timeout, user_agent=user_agent, proxies=proxies, verify=verify_ssl)
        except Exception as e:
            last_exc = e
            if scheme == "https":
                https_failed = _short_error(e)
            continue
        waf = detect_waf(page.status, page.headers, page.text[:30000])
        links = _hosts_in_text(page.text)
        if page.location:
            links.add(clean_host(page.location))
        links.discard("")
        links.discard(host)
        out.update(status=page.status, url=url, redirect=page.location, state="ok",
                   title=page_title(page.text) if 200 <= page.status < 300 or page.status >= 400 else "",
                   note=_status_note(page, waf, scheme, https_failed), links=links)
        return out
    out.update(state="no_answer", note=f"The name resolves, but no web server answered on https or http "
                                       f"({_short_error(last_exc)}).")
    return out


# ---------------------------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------------------------
def _crawl_domain(crawler) -> str:
    start = getattr(crawler, "start_url", "") or getattr(getattr(crawler, "config", None), "start_url", "") or ""
    return registrable_domain(start)


def _snapshot(crawler) -> Tuple[list, list]:
    """(results, external links) copied under the crawl's lock (a running crawl appends to them)."""
    lock = getattr(crawler, "_lock", None)
    if lock is not None:
        with lock:
            return list(getattr(crawler, "results_list", None) or []), \
                list(getattr(crawler, "external_links_list", None) or [])
    return list(getattr(crawler, "results_list", None) or []), list(getattr(crawler, "external_links_list", None) or [])


def link_hosts(crawler, domain: str, results: Optional[list] = None,
               externals: Optional[list] = None) -> Dict[str, Dict[str, Set[str]]]:
    """{host of the domain: {"links" | "resources" | "redirects": crawled pages referring to it}} from every
    crawled page's links and resources, redirects and the recorded external links."""
    if results is None or externals is None:
        results, externals = _snapshot(crawler)
    memo: Dict[str, str] = {}      # "scheme://netloc" part of a URL -> its host when of the domain, else ""
    out: Dict[str, Dict[str, Set[str]]] = {}

    def note(url: str, kind: str, page: str):
        i = url.find("//")
        if i < 0:
            return
        j = url.find("/", i + 2)
        head = url if j < 0 else url[:j]
        host = memo.get(head)
        if host is None:
            host = _url_host(url)
            host = memo[head] = host if host and not host.startswith("[") and host_in_domain(host, domain) else ""
        if host:
            out.setdefault(host, {}).setdefault(kind, set()).add(page)

    for r in results:
        seo = getattr(r, "seo", None)
        if isinstance(seo, dict):
            for link in seo.get("links") or ():
                if isinstance(link, dict) and link.get("url"):
                    note(link["url"], "links", r.url)
            for res in seo.get("resources") or ():
                if isinstance(res, dict) and res.get("url"):
                    note(res["url"], "resources", r.url)
        if getattr(r, "redirect_destination", None):
            note(r.redirect_destination, "redirects", r.url)
    for e in externals:
        parent = getattr(e, "parent_url", "") or ""
        if parent:
            kind = {"Redirect": "redirects", "Resource": "resources"}.get(getattr(e, "discovery_source", ""), "links")
            note(getattr(e, "url", ""), kind, parent)
    return out


def _crawl_sitemap_hosts(crawler) -> Tuple[Set[str], Set[str]]:
    """(hosts seen in the crawl's sitemaps, hosts named in its robots.txt)."""
    hosts: Set[str] = set(getattr(crawler, "sitemap_hosts", None) or {})
    for url in list(getattr(crawler, "sitemap_index", None) or {})[:200000]:
        hosts.add(_url_host(url))
    for url in list(getattr(crawler, "sitemap_stats", None) or {}) + list(getattr(crawler, "sitemaps_not_read", None)
                                                                         or []):
        hosts.add(_url_host(url))
    robots = set(getattr(crawler, "robots_hosts", None) or ())
    hosts.discard("")
    return hosts, robots


def _ct_hosts(domain: str, timeout: float, user_agent: str, proxies: Optional[Dict[str, str]],
              stop_event: Optional[threading.Event]) -> Set[str]:
    """Names in the certificates logged for *.domain (crt.sh JSON; one request). Raises on failure."""
    url = f"{CT_URL}?q={quote('%.' + domain, safe='')}&output=json"
    deadline = time.monotonic() + timeout
    session = requests.Session()
    try:
        session.headers.update({"User-Agent": user_agent or DEFAULT_USER_AGENT, "Accept": "application/json"})
        if proxies:
            session.proxies.update(proxies)
        resp = session.get(url, timeout=(min(10.0, timeout), timeout), stream=True)
        try:
            if resp.status_code != 200:
                raise RuntimeError(f"crt.sh answered {resp.status_code}")
            chunks: List[bytes] = []
            size = 0
            for chunk in resp.iter_content(64 * 1024):
                chunks.append(chunk)
                size += len(chunk)
                if size >= CT_MAX_BYTES:
                    break   # a very large answer: the names in its first part are used
                if stop_event is not None and stop_event.is_set():
                    raise RuntimeError("stopped")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"crt.sh didn't finish answering within {int(timeout)} s")
        finally:
            resp.close()
    finally:
        session.close()
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if not text.strip().startswith("["):
        raise RuntimeError("crt.sh sent something other than the expected list")
    names: Set[str] = set()
    for m in _CT_NAME.finditer(text):
        try:
            value = json.loads('"' + m.group(1) + '"')
        except ValueError:
            continue
        for name in str(value).split("\n"):
            host = clean_host(name)
            if host and host_in_domain(host, domain):
                names.add(host)
    return names


def _gsc_hosts(domain: str) -> Tuple[str, Set[str], str]:
    """(state, hosts, note) from the Search Console property list: state "done", "not_connected" or "error"."""
    try:
        from . import integrations
    except Exception as e:   # the integrations package is optional
        return "error", set(), f"Search Console isn't available on this server ({type(e).__name__})."
    try:
        conn = integrations.connection("gsc")
    except Exception:
        conn = {}
    try:
        props = integrations.gsc_properties()
    except getattr(integrations, "NotConnected", ()) as e:
        return "not_connected", set(), str(e) or "Not connected - connect under Configuration > API Access."
    except Exception as e:
        reason = str(conn.get("reason") or "") if isinstance(conn, dict) else ""
        return "error", set(), (reason or str(e) or type(e).__name__)[:300]
    hosts: Set[str] = set()
    for p in props or ():
        site = str((p or {}).get("site_url") or "")
        host = clean_host(site.split(":", 1)[1] if site.startswith("sc-domain:") else site)
        if host and host_in_domain(host, domain):
            hosts.add(host)
    return "done", hosts, f"{len(props or ())} propert{'y' if len(props or ()) == 1 else 'ies'} in Search Console."


# ---------------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------------
def _start_url(url: str) -> str:
    text = str(url or "").strip()
    if text and not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "https://" + text
    return text


def discover(url: str, sources: Optional[Iterable[str]] = None, extra_hosts: Optional[Iterable[str]] = None,
             timeout: float = CT_TIMEOUT, crawler=None, user_agent: str = "",
             proxies: Optional[Dict[str, str]] = None, verify_ssl: bool = False,
             stop_event: Optional[threading.Event] = None,
             on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
             allow_private: Optional[bool] = None, max_hosts: int = MAX_HOSTS,
             headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Finds the hosts of `url`'s registrable domain. `sources` is any of SOURCES (default all); `extra_hosts` are
    typed host names; `crawler` is the loaded crawl (used when it is of the same domain: its links and sitemaps
    then need no request). Never raises: a source that fails is named in "errors".

    Returns {"state": "done"|"error", "domain", "start_host", "hosts": [{host, sources, resolves, status, title,
    note, state, url, redirect}], "errors": {source: message}, "sources": {source: {"state", "hosts", "note"}},
    "skipped": [{"value", "reason"}], "found": candidates before the cap, "capped": bool, "finished_at"}.
    allow_private: None = only when the audited site itself is on a private address (an intranet audit)."""
    started = time.time()
    start = _start_url(url)
    try:
        start_host = (urlsplit(start).hostname or "").rstrip(".")
    except ValueError:
        start_host = ""
    domain = registrable_domain(start_host)
    result: Dict[str, Any] = {"state": "done", "domain": domain, "start_host": start_host, "hosts": [],
                              "errors": {}, "sources": {}, "skipped": [], "found": 0, "capped": False,
                              "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
                              "finished_at": "", "seconds": 0.0, "source_labels": dict(SOURCE_LABELS)}
    if not start_host or not domain or _is_ip(start_host) or "." not in domain:
        result["state"] = "error"
        result["errors"]["url"] = "Enter the address of a website by its name (e.g. example.com), not an IP address."
        return result
    start_key = clean_hostname(start_host)
    stop = stop_event or threading.Event()
    chosen = [s for s in SOURCES if sources is None or s in {str(x).strip().lower() for x in sources}]
    start_ips = resolve(start_host)
    if not start_ips:
        result["errors"]["url"] = (f"{start_host} doesn't resolve (no DNS record); only the sources that need no "
                                   "request to it were used.")
    if allow_private is None:
        allow_private = private_only(start_ips)
    # Even an intranet audit never opens link-local / metadata addresses, nor this machine unless the audited site
    # is itself on it (a local audit)
    loopback_ok = loopback_only(start_ips)
    found: Dict[str, Set[str]] = {}

    def progress(message: str, done: int = 0, total: int = 0, hosts: Optional[List[dict]] = None):
        if on_progress is None:
            return
        try:
            on_progress({"message": message, "done": done, "total": total, "domain": domain,
                         "hosts": hosts if hosts is not None else [], "sources": dict(result["sources"]),
                         "errors": dict(result["errors"])})
        except Exception:
            pass

    def add(host: str, source: str) -> bool:
        h = clean_host(host)
        if not h or not host_in_domain(h, domain) or clean_hostname(h) == start_key:
            return False
        found.setdefault(h, set()).add(source)
        return True

    def record(source: str, state: str, hosts: Iterable[str], note: str = ""):
        n = sum(1 for h in set(hosts) if add(h, source))
        entry = result["sources"].setdefault(source, {"state": state, "hosts": 0, "note": note})
        entry["state"], entry["hosts"] = state, entry["hosts"] + n
        if note:
            entry["note"] = note
        if state in ("error", "not_connected"):
            result["errors"][source] = note

    # Typed hosts
    for value in list(extra_hosts or ())[:1000]:
        text = str(value or "").strip()
        if not text:
            continue
        h = clean_host(text)
        if not h:
            result["skipped"].append({"value": text[:200], "reason": "not a host name"})
        elif not host_in_domain(h, domain):
            result["skipped"].append({"value": text[:200], "reason": f"not part of {domain}, so it isn't checked"})
        elif clean_hostname(h) == start_key:
            result["skipped"].append({"value": text[:200], "reason": "the audited site itself"})
        else:
            add(h, "typed")
    if result["skipped"]:
        shown = "; ".join(f"{s['value']}: {s['reason']}" for s in result["skipped"][:5])
        result["errors"]["typed"] = f"{len(result['skipped'])} typed host(s) left out - {shown}"

    # Certificate logs: the slowest source, so it runs alongside the others
    ct_box: Dict[str, Any] = {}
    ct_thread: Optional[threading.Thread] = None
    if "ct" in chosen:
        def ct_job():
            try:
                ct_box["hosts"] = _ct_hosts(domain, timeout, user_agent, proxies, stop)
            except Exception as e:
                ct_box["error"] = e
        ct_thread = threading.Thread(target=ct_job, daemon=True, name="subdomains-ct")
        ct_thread.start()
        progress("Reading the public certificate logs (crt.sh)...")

    same_crawl = crawler is not None and _crawl_domain(crawler) == domain
    fetch_opts = dict(timeout=PROBE_TIMEOUT, user_agent=user_agent, proxies=proxies, verify=verify_ssl,
                      headers=headers)

    def guard(target: str) -> Optional[str]:
        """The address to connect to for `target` (None: refused / no DNS)."""
        host = (urlsplit(target).hostname or "").lower()
        if not host or not host_in_domain(host, domain) or _metadata_name(host):
            return None
        usable = usable_addresses(resolve(host), allow_private, loopback_ok)
        return usable[0] if usable else None

    # Links: the loaded crawl's pages, else the home page (one request, redirects followed within the domain)
    if "links" in chosen and not stop.is_set():
        if same_crawl:
            found_links = link_hosts(crawler, domain)
            pages = len(getattr(crawler, "results_list", None) or [])
            hosts = [h for h, kinds in found_links.items() if kinds.get("links") or kinds.get("redirects")]
            record("links", "done", hosts, f"Read the links of the loaded crawl ({pages:,} URLs).")
            record("links", "done", [h for h, kinds in found_links.items() if kinds.get("resources")])
        else:
            progress("Reading the links of the home page...")
            target, hosts, note, state = start, set(), "", "done"
            try:
                for _ in range(MAX_REDIRECTS + 1):
                    ip = guard(target)
                    if ip is None:
                        state, note = "error", f"{urlsplit(target).hostname} couldn't be opened (no DNS record " \
                                               "or a private address)."
                        break
                    page = _fetch(target, ip, **fetch_opts)
                    hosts |= _hosts_in_text(page.text)
                    if page.location and 300 <= page.status < 400:
                        hosts.add(clean_host(page.location))
                        if host_in_domain(_url_host(page.location), domain):
                            target = page.location
                            continue
                    note = (f"No crawl of {domain} is open, so only the home page's links were read "
                            f"({target}, status {page.status}).")
                    break
                else:
                    note = f"The home page redirected more than {MAX_REDIRECTS} times; its links were not read."
            except Exception as e:
                state, note = "error", f"The home page couldn't be read ({_short_error(e)})."
            record("links", state, hosts, note)

    # XML sitemaps and robots.txt
    if "sitemaps" in chosen and not stop.is_set():
        crawl_hosts, robots = _crawl_sitemap_hosts(crawler) if same_crawl else (set(), set())
        if same_crawl and (crawl_hosts or robots or getattr(crawler, "sitemap_stats", None)):
            record("sitemaps", "done", crawl_hosts, "Read from the loaded crawl's XML sitemaps.")
            record("robots", "done", robots)
        else:
            progress("Reading robots.txt and the XML sitemaps...")
            try:
                from .sitemap_parser import SitemapParser
                parser = SitemapParser(base_url=start, base_domain=start_key, user_agent=user_agent or
                                       DEFAULT_USER_AGENT, timeout=min(10, max(3, int(PROBE_TIMEOUT))),
                                       verify_ssl=verify_ssl, stop_event=stop, max_sitemaps=SITEMAP_FILES,
                                       max_urls=1, index_limit=1, headers=headers, proxies=proxies,
                                       url_ok=lambda u: guard(u) is not None)
                parser.parse_all()
                files = len(parser.sitemap_stats)
                record("sitemaps", "done", list(parser.hosts_seen),
                       f"Read robots.txt and {files} sitemap file{'' if files == 1 else 's'}."
                       if files else "Read robots.txt; no XML sitemap was found.")
                record("robots", "done", parser.robots_hosts)
            except Exception as e:
                record("sitemaps", "error", (), f"The sitemaps couldn't be read: {type(e).__name__}: {e}"[:300])

    # Search Console's property list (only when it is connected)
    if "gsc" in chosen and not stop.is_set():
        progress("Reading the Search Console property list...")
        state, hosts, note = _gsc_hosts(domain)
        record("gsc", state, hosts, note)

    if ct_thread is not None:
        ct_thread.join(timeout + 15)
        if "hosts" in ct_box:
            names = ct_box["hosts"]
            record("ct", "done", names, f"{len(names)} name{'' if len(names) == 1 else 's'} of {domain} in the "
                                        "certificate logs.")
        else:
            exc = ct_box.get("error")
            if exc is None or isinstance(exc, (requests.exceptions.Timeout, TimeoutError)) \
                    or (isinstance(exc, requests.exceptions.RequestException) and classify_error(exc) == "timeout"):
                why = f"crt.sh didn't answer within {int(timeout)} s"
            elif isinstance(exc, requests.exceptions.RequestException):
                why = f"crt.sh couldn't be reached ({_short_error(exc)})"
            else:
                why = str(exc) or type(exc).__name__
            record("ct", "error", (), f"The certificate logs couldn't be read: {why}. Try again later.")

    # Cap, probe, then one more round for hosts the probed home pages link to
    def ranked(hosts: Iterable[str]) -> List[str]:
        return sorted(hosts, key=lambda h: (min(_PRIORITY.get(s, 9) for s in found[h]), _sort_key(h)))

    result["found"] = len(found)
    candidates = ranked(found)[:max(0, int(max_hosts))]
    result["capped"] = len(found) > len(candidates)
    entries: Dict[str, Dict[str, Any]] = {}
    for h in candidates:
        entries[h] = {"host": h, "sources": [], "resolves": None, "status": None, "title": "", "note": "Not checked.",
                      "state": "not_checked", "url": "", "redirect": ""}

    linked_by: Dict[str, Set[str]] = {}     # host -> the probed hosts whose home page links to it

    def public_hosts() -> List[dict]:
        out = []
        for h in sorted(entries, key=_sort_key):
            e = dict(entries[h])
            e["sources"] = _ordered_sources(found.get(h, ()))
            if linked_by.get(h):
                e["linked_by"] = sorted(linked_by[h], key=_sort_key)[:MAX_LINKED_BY]
            out.append(e)
        return out

    def run_round(hosts: List[str], round_no: int):
        total = len(hosts)
        done = 0
        progress(f"Checking {total} host{'' if total == 1 else 's'}...", 0, total, public_hosts())
        if not hosts:
            return
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS, thread_name_prefix="subdomains") as pool:
            futures = {}
            for h in hosts:
                if stop.is_set():
                    break
                futures[pool.submit(probe_host, h, allow_private, PROBE_TIMEOUT, user_agent, proxies,
                                    verify_ssl, loopback_ok)] = h
            for fut in as_completed(futures):
                h = futures[fut]
                try:
                    info = fut.result()
                except Exception as e:
                    info = {"state": "no_answer", "note": f"The check failed: {type(e).__name__}", "links": set()}
                for linked in info.pop("links", set()) or ():
                    # a link on another host's home page: its own source, never "links on the site's pages"
                    if round_no == 1 and add(linked, "home_links"):
                        linked_by.setdefault(clean_host(linked), set()).add(h)
                entries[h].update(info)
                done += 1
                progress(f"Checked {done} of {total} hosts...", done, total, public_hosts())

    run_round(candidates, 1)
    if linked_by:
        result["sources"]["home_links"] = {
            "state": "done", "hosts": len(linked_by),
            "note": "Read the links on the home pages of the hosts found (one more round checks the new ones)."}
    if not stop.is_set():
        room = max(0, int(max_hosts)) - len(entries)
        new = [h for h in ranked(found) if h not in entries]
        result["found"] = len(found)
        if new and room > 0:
            for h in new[:room]:
                entries[h] = {"host": h, "sources": [], "resolves": None, "status": None, "title": "",
                              "note": "Not checked.", "state": "not_checked", "url": "", "redirect": ""}
            run_round(new[:room], 2)
        result["capped"] = result["capped"] or len(new) > room
    if stop.is_set():
        result["errors"]["stopped"] = "Stopped before every host was checked."
    result["hosts"] = public_hosts()
    result["seconds"] = round(time.time() - started, 1)
    result["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _remember(domain, result)
    return result


# ---------------------------------------------------------------------------------------------
# Background jobs and the latest discovery per domain
# ---------------------------------------------------------------------------------------------
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_latest: Dict[str, Dict[str, Any]] = {}     # domain -> {"finished_at", "hosts": {host: entry}}
_MAX_LATEST = 50
_MAX_REMEMBERED_HOSTS = 1000


def _remember(domain: str, result: Dict[str, Any]):
    """Merges a finished discovery into what this server knows of the domain: every host found by any discovery
    is kept (a later run with fewer sources does not forget the others); a host's newest check wins and its
    sources add up."""
    if not domain or result.get("state") != "done":
        return
    with _jobs_lock:
        known = _latest.pop(domain, None) or {"hosts": {}}
        hosts: Dict[str, Dict[str, Any]] = known["hosts"]
        for entry in result.get("hosts") or ():
            host = entry.get("host")
            if not host or (host not in hosts and len(hosts) >= _MAX_REMEMBERED_HOSTS):
                continue
            old = hosts.get(host) or {}
            merged = dict(entry)
            merged["sources"] = _ordered_sources(set(old.get("sources") or ()) | set(entry.get("sources") or ()))
            via = set(old.get("linked_by") or ()) | set(entry.get("linked_by") or ())
            if via:
                merged["linked_by"] = sorted(via, key=_sort_key)[:MAX_LINKED_BY]
            if entry.get("state") == "not_checked" and old.get("state") not in (None, "", "not_checked"):
                merged.update({k: old.get(k) for k in ("resolves", "status", "title", "note", "state", "url",
                                                      "redirect")})
            hosts[host] = merged
        _latest[domain] = {"finished_at": result.get("finished_at") or "", "hosts": hosts}
        while len(_latest) > _MAX_LATEST:
            _latest.pop(next(iter(_latest)))


def last_discovery(domain: str) -> Optional[Dict[str, Any]]:
    """What the discoveries run on this server found for `domain` (a host or URL of it), merged: {"domain",
    "finished_at" (latest run), "hosts": [entry]}; None when none ran. A copy, safe to keep with a crawl."""
    key = registrable_domain(domain) or domain
    with _jobs_lock:
        known = _latest.get(key)
        if known is None:
            return None
        return {"domain": key, "finished_at": known["finished_at"],
                "hosts": [dict(e, sources=list(e.get("sources") or ()), **({"linked_by": list(e["linked_by"])}
                                                                           if e.get("linked_by") else {}))
                          for e in known["hosts"].values()]}


def start_job(url: str, sources: Optional[Iterable[str]] = None, extra_hosts: Optional[Iterable[str]] = None,
              **options: Any) -> str:
    """Runs discover() in a background thread; returns the job id (get_job). The same request while one runs
    returns the running job. Raises DiscoveryBusy when MAX_RUNNING_JOBS discoveries are running."""
    key = (str(url or "").strip().lower(), tuple(sorted(sources or SOURCES)), tuple(sorted(extra_hosts or ())))
    now = time.time()
    with _jobs_lock:
        finished = sorted((job["ended"], j) for j, job in _jobs.items() if job["state"] != "running")
        old = [j for ended, j in finished if now - ended > JOB_KEEP_SECONDS]
        # at most MAX_FINISHED_JOBS finished searches are kept (one more is about to finish): the oldest go first
        old += [j for _, j in finished if j not in old][:max(0, len(finished) - len(old) - MAX_FINISHED_JOBS + 1)]
        for jid in old:
            _jobs.pop(jid, None)
        running = [j for j, job in _jobs.items() if job["state"] == "running"]
        for jid in running:
            if _jobs[jid]["key"] == key:
                return jid
        if len(running) >= MAX_RUNNING_JOBS:
            raise DiscoveryBusy("Another subdomain search is running; try again when it has finished.")
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "key": key, "state": "running", "domain": registrable_domain(_start_url(url)),
               "hosts": [], "errors": {}, "sources": {}, "message": "Starting...", "done": 0, "total": 0,
               "started": now, "ended": 0.0, "stop": threading.Event(), "result": None}
        _jobs[job_id] = job

    def on_progress(snap: Dict[str, Any]):
        with _jobs_lock:
            job.update({k: snap[k] for k in ("message", "done", "total", "domain", "sources", "errors")
                        if k in snap})
            if snap.get("hosts"):
                job["hosts"] = snap["hosts"]

    def run():
        try:
            res = discover(url, sources, extra_hosts, stop_event=job["stop"], on_progress=on_progress, **options)
        except Exception as e:   # discover never raises; a bug must not leave the job "running" for ever
            res = {"state": "error", "domain": job["domain"], "hosts": [], "sources": {},
                   "errors": {"discovery": f"{type(e).__name__}: {e}"[:300]}}
        with _jobs_lock:
            # "result" keeps the summary fields only: the host list is kept once, in "hosts"
            job.update({"state": res.get("state") or "done", "domain": res.get("domain") or job["domain"],
                        "hosts": res.get("hosts") or [], "errors": res.get("errors") or {},
                        "sources": res.get("sources") or {}, "message": "Done.", "ended": time.time(),
                        "result": {k: v for k, v in res.items() if k not in ("hosts", "errors", "sources")}})

    threading.Thread(target=run, daemon=True, name=f"subdomains-{job_id}").start()
    return job_id


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """The contract's GET /api/subdomains/discover/{job_id} body (None for an unknown id): {state, domain, hosts,
    errors, sources, message, done, total, skipped, found, capped, finished_at}."""
    with _jobs_lock:
        job = _jobs.get(str(job_id or ""))
        if job is None:
            return None
        out = {k: job[k] for k in ("state", "domain", "hosts", "errors", "sources", "message", "done", "total")}
        res = job.get("result") or {}
        out.update({k: res.get(k) for k in ("start_host", "skipped", "found", "capped", "finished_at", "seconds")
                    if k in res})
        out["hosts"] = [dict(h) for h in out["hosts"]]
        out["errors"] = dict(out["errors"])
        out["source_labels"] = dict(SOURCE_LABELS)
        out["sources"] = {k: dict(v) for k, v in (out["sources"] or {}).items()}
    return out


def stop_job(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(str(job_id or ""))
    if job is None or job["state"] != "running":
        return False
    job["stop"].set()
    return True


# ---------------------------------------------------------------------------------------------
# Extra hosts of a crawl (CrawlConfig.extra_hosts)
# ---------------------------------------------------------------------------------------------
def extra_host_urls(values: Any, start_url: str) -> Tuple[List[str], Dict[str, str]]:
    """(home page URLs, {value: why left out}) for CrawlConfig.extra_hosts. Each host of the start URL's
    registrable domain becomes <start scheme>://host/; a value written as a URL keeps its scheme and port. The
    start host itself (www or not), other domains, repeats and names that are not hosts are left out (reason
    "same_site", "other_domain", "invalid"); at most MAX_HOSTS."""
    try:
        parts = urlsplit(start_url or "")
    except ValueError:
        return [], {}
    domain = registrable_domain(parts.hostname or "")
    start_key = clean_hostname(parts.hostname or "")
    scheme = parts.scheme if parts.scheme in ("http", "https") else "https"
    if isinstance(values, str):
        values = re.split(r"[\s,]+", values)
    urls: List[str] = []
    keys: Set[str] = set()
    skipped: Dict[str, str] = {}
    for value in list(values or ())[:1000]:
        text = str(value or "").strip()
        if not text:
            continue
        host = clean_host(text)
        if not host:
            skipped[text[:200]] = "invalid"
            continue
        if not domain or not host_in_domain(host, domain):
            skipped[host] = "other_domain"
            continue
        key = clean_hostname(host)
        if key == start_key:
            skipped[host] = "same_site"
            continue
        if key in keys:
            continue
        if len(urls) >= MAX_HOSTS:
            skipped[host] = "too_many"
            continue
        sch, port = scheme, ""
        if "://" in text:
            try:
                p = urlsplit(text)
                sch = p.scheme.lower() if p.scheme.lower() in ("http", "https") else scheme
                port = f":{p.port}" if p.port and p.port != (443 if sch == "https" else 80) else ""
            except ValueError:
                pass
        elif re.match(r"^[^/:]+:\d{1,5}$", text):
            port = ":" + text.rsplit(":", 1)[1]
        keys.add(key)
        urls.append(f"{sch}://{host}{port}/")
    return urls, skipped


def check_extra_hosts(urls: List[str], start_url: str) -> Tuple[List[str], Dict[str, str]]:
    """(home URLs kept, {host: "no_dns" | "private_address"}): names without DNS, and names with any address on a
    private / internal network (the crawl cannot choose which address it connects to), are left out, unless the
    audited site is itself on a private address (an intranet audit). Link-local / cloud metadata addresses are
    always left out, this machine's (loopback) unless the audited site is on it too (never_open)."""
    start_host = urlsplit(start_url or "").hostname or ""
    start_ips = resolve(start_host)
    allow_private = private_only(start_ips)
    loopback_ok = loopback_only(start_ips)
    kept: List[str] = []
    dropped: Dict[str, str] = {}
    hosts = [urlsplit(url).hostname or "" for url in urls]
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(hosts)))) as pool:
        answers = list(pool.map(resolve, hosts))
    for url, host, ips in zip(urls, hosts, answers):
        if not ips:
            dropped[host] = "no_dns"
        elif _metadata_name(host) or any(never_open(ip, loopback_ok) for ip in ips) or (
                not allow_private and any(is_internal_address(ip) for ip in ips)):
            dropped[host] = "private_address"
        else:
            kept.append(url)
    return kept, dropped


# ---------------------------------------------------------------------------------------------
# After a crawl: every host of the domain, crawled or not, with a plain reason
# ---------------------------------------------------------------------------------------------
_report_cache: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_report_lock = threading.Lock()


def _discovery_for(crawler, domain: str, discovery: Optional[dict]) -> Optional[dict]:
    """The discovery passed in, the one the crawl started with (kept with a saved crawl) and what this server
    found since, merged host by host (the newest check of a host wins)."""
    options = [d for d in (discovery, getattr(crawler, "subdomain_discovery", None), last_discovery(domain))
               if isinstance(d, dict) and d.get("domain") == domain]
    if not options:
        return None
    hosts: Dict[str, dict] = {}
    for d in sorted(options, key=lambda d: str(d.get("finished_at") or "")):
        for entry in d.get("hosts") or ():
            if isinstance(entry, dict) and entry.get("host"):
                old = hosts.get(entry["host"]) or {}
                hosts[entry["host"]] = dict(entry, sources=_ordered_sources(
                    set(old.get("sources") or ()) | set(entry.get("sources") or ())))
                via = set(old.get("linked_by") or ()) | set(entry.get("linked_by") or ())
                if via:
                    hosts[entry["host"]]["linked_by"] = sorted(via, key=_sort_key)[:MAX_LINKED_BY]
    return {"domain": domain, "finished_at": max(str(d.get("finished_at") or "") for d in options),
            "hosts": list(hosts.values())}


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


# How a host turned up, worded for the report's reasons ("it was typed in", "found in the certificate logs")
_FOUND_HOW = {"typed": "typed in", "links": "linked from the site's pages",
              "home_links": "linked from the home page of another host it found",
              "resources": "used for images, scripts or CSS on crawled pages", "redirects": "reached by a redirect",
              "sitemaps": "listed in the XML sitemaps", "robots": "named in robots.txt",
              "gsc": "found in Search Console", "ct": "found in the certificate logs"}


def _found_how(sources: List[str], linked_by: Optional[List[str]] = None) -> str:
    """How a host turned up, in words; `linked_by` names the hosts whose home page links to it."""
    via = [h for h in (linked_by or ()) if h]
    if via:
        more = f" (and {_plural(len(via) - 1, 'more host')})" if len(via) > 1 else ""
        home = f"linked from the home page of {via[0]}{more}"
    else:
        home = _FOUND_HOW["home_links"]
    parts = [home if s == "home_links" else (_FOUND_HOW.get(s) or f"found in {SOURCE_LABELS.get(s, s)}")
             for s in sources]
    if not parts:
        return "found by Find subdomains"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _reason(code: str, row: Dict[str, Any], scope: str, mode: str, agent: str, status: str = "") -> str:
    """The plain reason of one host's row; `status` is the crawl's (running, paused, stopped, completed)."""
    linked = row["linked_from"]
    sources = [s for s in row["sources"] if s not in ("start", "extra")]
    how_found = _found_how(sources, row.get("linked_by"))
    if code == "crawled":
        tail = " It was chosen under Find subdomains." if row["extra"] else ""
        n = row.get("bot_check") or 0
        if n:
            # a bot check / firewall answered instead of the page: the status codes stay as sent, the reason says so
            many = "Every one" if n == row["crawled"] else _plural(n, "URL")
            tail += (f" {many} got a bot check instead of the page (status codes kept): sign in with your own "
                     "browser and paste the cookies, or allow ARACHNIX if you own the site.")
        return f"Crawled: {_plural(row['crawled'], 'URL')}.{tail}"
    if code == "needs_signin":
        return ("It asks for a sign-in (401/403 or the login page), so only that answer was recorded. Add a sign-in "
                "under Configuration > Authentication (or paste cookies) to crawl it.")
    if code == "robots_blocked":
        return (f"robots.txt blocks its pages for {agent}. Turn off 'Respect robots.txt' to crawl them "
                "(only on a site you own).")
    if code == "no_dns":
        return "Its name doesn't resolve (no DNS record), so there is no site to crawl."
    if code == "private_address":
        return "It points to a private / internal network address, so ARACHNIX doesn't open it."
    if code == "not_reached" and row["extra"] and not row["crawled"]:
        if status in ("running", "paused", "idle", ""):
            return "Chosen under Find subdomains; not crawled yet (the crawl is still running)."
        why = ("the crawl was stopped before it got there" if status == "stopped" else
               "the crawl reached its limits (Max URLs, depth) before it got there, or the sign-in failed")
        return f"Chosen under Find subdomains, but its home page wasn't crawled: {why}."
    if code == "not_reached":
        return (f"Linked from {_plural(linked, 'crawled page')} and inside the scope, but not crawled: the crawl's "
                "limits (Max URLs, depth), include/exclude rules or a stop left it out.")
    if code == "out_of_scope":
        if mode == "list":
            how = "List mode crawls only the listed URLs."
        else:
            how = (f"it's outside the chosen scope ({SCOPE_LABELS.get(scope, scope)}). Choose All Subdomains, or tick "
                   "it under Find subdomains, to crawl it.")
        if linked:
            lead = f"Linked from {_plural(linked, 'crawled page')}, but "
        else:
            lead = f"It was {how_found}, but "
        return lead + (how[0].lower() + how[1:] if mode == "list" else how)
    return f"No crawled page links to it (it was {how_found}). Tick it under Find subdomains to crawl it."


def subdomain_report(crawler, discovery: Optional[dict] = None) -> Dict[str, Any]:
    """GET /api/crawl/subdomains: {"domain", "start_host", "scope", "mode", "hosts": [{"host", "crawled",
    "linked_from", "in_scope", "reason_code", "reason", "sources", "extra", "home_status", "pages_ok",
    "bot_check" (URLs a bot check / firewall answered instead of the page)}],
    "counts": {reason_code: hosts}, "totals": {"hosts", "crawled_urls", "urls_other_domains"}, "discovery":
    {"ran", "finished_at", "hosts"}, "complete": False while the crawl runs}. "crawled" counts every URL of the
    crawl on the host (pages, files, blocked ones), so the crawled column adds up to the crawl's URLs on the domain
    (totals.crawled_urls; crawled_urls + urls_other_domains = every URL of the crawl); "linked_from" counts the
    crawled pages that link to the host or use its files (its own pages included). www. and the bare name share a
    row. No network request is made; while the crawl runs the report is rebuilt at most every few seconds."""
    config = getattr(crawler, "config", None)
    start_url = getattr(crawler, "start_url", "") or getattr(config, "start_url", "") or ""
    start_host = (urlsplit(start_url).hostname or "").rstrip(".") if start_url else ""
    domain = registrable_domain(start_host)
    scope = str(getattr(crawler, "crawl_scope", "") or getattr(config, "crawl_scope", "") or "subdomain")
    mode = str(getattr(crawler, "mode", "") or getattr(config, "mode", "") or "spider")
    empty = {"domain": domain, "start_host": start_host, "scope": scope, "mode": mode, "hosts": [],
             "counts": {}, "totals": {"hosts": 0, "crawled_urls": 0, "urls_other_domains": 0},
             "discovery": {"ran": False, "finished_at": "", "hosts": 0}, "complete": True,
             "source_labels": dict(SOURCE_LABELS)}
    if not domain:
        return empty
    disc = _discovery_for(crawler, domain, discovery)
    results, externals = _snapshot(crawler)
    key = (len(results), len(externals), getattr(crawler, "status", ""), getattr(crawler, "phase", ""),
           str((disc or {}).get("finished_at") or ""), len(getattr(crawler, "sitemap_hosts", None) or ()))
    running = str(getattr(crawler, "status", "")) in ("running", "paused")
    with _report_lock:
        try:
            cached = _report_cache.get(crawler)
        except TypeError:
            cached = None
    if cached is not None:
        old_key, old_report, made_at, cost = cached
        # unchanged, or a running crawl asked again within a few seconds (a big crawl takes a moment to sum up)
        if old_key == key or (running and time.time() - made_at < max(REPORT_REUSE_SECONDS, 4 * cost)):
            return old_report
    began = time.time()

    rows: Dict[str, Dict[str, Any]] = {}
    names: Dict[str, Counter] = {}

    def row(host: str) -> Dict[str, Any]:
        k = clean_hostname(host)
        names.setdefault(k, Counter())[host] += 1
        r = rows.get(k)
        if r is None:
            r = rows[k] = {"crawled": 0, "pages_ok": 0, "blocked": 0, "dns": 0, "login": 0, "private": 0, "waf": 0,
                           "linked": set(), "sources": set(), "extra": False, "home_status": None,
                           "disc_state": "", "skip": "", "via": set()}
        return r

    row(start_host)["sources"].add("start")
    other_domains = 0
    for res in results:
        host = _url_host(res.url)
        if not host_in_domain(host, domain):
            other_domains += 1
            continue
        r = row(host)
        r["crawled"] += 1
        code = getattr(res, "status_code", None)
        try:
            path = urlsplit(res.url).path or "/"
        except ValueError:
            path = ""
        if path == "/" and r["home_status"] is None:
            r["home_status"] = code
        cause = getattr(res, "root_cause_type", "") or ""
        if cause == "waf":
            r["waf"] += 1
        if getattr(res, "blocked", False):
            r["blocked"] += 1
        elif code is None and cause == "dns":
            r["dns"] += 1
        elif (code in (401, 403) and cause != "waf") or (
                getattr(res, "redirect_destination", None) and redirected_to_login(res.url, res.redirect_destination)) \
                or (code is not None and 200 <= code < 300 and is_login_url(res.url)):
            r["login"] += 1
        elif code is not None and 200 <= code < 300:
            r["pages_ok"] += 1
    for host, kinds in link_hosts(crawler, domain, results, externals).items():
        r = row(host)
        for kind, pages in kinds.items():
            r["linked"] |= pages
            r["sources"].add(kind)
    sitemap_hosts, robots_hosts = _crawl_sitemap_hosts(crawler)
    for host in sitemap_hosts:
        if host_in_domain(host, domain):
            row(host)["sources"].add("sitemaps")
    for host in robots_hosts:
        if host_in_domain(host, domain):
            row(host)["sources"].add("robots")
    for home in getattr(crawler, "extra_hosts", None) or ():
        host = _url_host(home)
        if host_in_domain(host, domain):
            r = row(host)
            r["extra"] = True
            r["sources"].add("extra")
    for host, why in (getattr(crawler, "extra_hosts_skipped", None) or {}).items():
        if why in ("no_dns", "private_address") and host_in_domain(host, domain):
            r = row(host)
            r["extra"], r["skip"] = True, why
            r["sources"].add("extra")
    for entry in (disc or {}).get("hosts") or ():
        host = str(entry.get("host") or "")
        if not host_in_domain(host, domain):
            continue
        r = row(host)
        r["sources"].update(s for s in entry.get("sources") or () if s)
        r["via"].update(str(h) for h in entry.get("linked_by") or () if h)
        r["disc_state"] = str(entry.get("state") or "")

    include_subdomains = bool(getattr(config, "include_subdomains", False)) or scope == "all_subdomains"
    allowed = set(getattr(crawler, "allowed_domains", None) or ())
    base = getattr(crawler, "base_domain", "") or clean_hostname(start_host)
    agent = str(getattr(config, "robots_user_agent", "") or "Googlebot")
    out_rows: List[Dict[str, Any]] = []
    counts: Counter = Counter()
    for k, r in rows.items():
        seen = names.get(k) or Counter({k: 1})
        if clean_hostname(start_host) == k:
            host = start_host
        else:
            host = sorted(seen, key=lambda h: (-seen[h], h.startswith("www."), h))[0]
        in_scope = is_internal_url(f"http://{host}/", base, include_subdomains, allowed or None)
        linked = len(r["linked"])
        if r["crawled"]:
            if not r["pages_ok"] and r["blocked"] == r["crawled"]:
                code = "robots_blocked"
            elif not r["pages_ok"] and r["dns"] == r["crawled"]:
                code = "no_dns"
            elif not r["pages_ok"] and r["login"]:
                code = "needs_signin"
            else:
                code = "crawled"
        elif r["skip"]:
            code = r["skip"]
        elif r["disc_state"] == "no_dns":
            code = "no_dns"
        elif r["disc_state"] == "private":
            code = "private_address"
        elif r["extra"]:
            code = "not_reached"        # chosen under Find subdomains (always in scope), not crawled (yet)
        elif linked or "sitemaps" in r["sources"]:
            code = "not_reached" if (in_scope and mode != "list") else "out_of_scope"
        else:
            code = "not_linked"
        item = {"host": host, "crawled": r["crawled"], "linked_from": linked, "in_scope": bool(in_scope),
                "reason_code": code, "sources": _ordered_sources(r["sources"]), "extra": r["extra"],
                "home_status": r["home_status"], "pages_ok": r["pages_ok"], "bot_check": r["waf"],
                "linked_by": sorted(r["via"], key=_sort_key)[:MAX_LINKED_BY]}
        item["reason"] = _reason(code, item, scope, mode, agent, str(getattr(crawler, "status", "") or ""))
        counts[code] += 1
        out_rows.append(item)
    order = {c: i for i, c in enumerate(REASON_CODES)}
    out_rows.sort(key=lambda x: (x["host"] != start_host, order.get(x["reason_code"], 99), -x["crawled"],
                                 _sort_key(x["host"])))
    report = {"domain": domain, "start_host": start_host, "scope": scope, "mode": mode, "hosts": out_rows,
              "counts": {c: counts[c] for c in REASON_CODES if counts.get(c)},
              "totals": {"hosts": len(out_rows), "crawled_urls": sum(x["crawled"] for x in out_rows),
                         "urls_other_domains": other_domains},
              "discovery": {"ran": disc is not None, "finished_at": str((disc or {}).get("finished_at") or ""),
                            "hosts": len((disc or {}).get("hosts") or ())},
              "complete": not running, "source_labels": dict(SOURCE_LABELS)}
    with _report_lock:
        try:
            _report_cache[crawler] = (key, report, time.time(), time.time() - began)
        except TypeError:
            pass
    return report
