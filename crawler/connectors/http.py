"""
Safe outgoing calls for connectors (PLAN4_build.md 10.3 / 10.8).

  check_url(url, allow_private_network=False) -> [IP addresses it may connect to]   or ConnectorError
  safe_request(method, url, *, allow_private_network=False, timeout=(5, 20), max_bytes=2_000_000,
               redirect_hosts=(), secrets=(), **requests_kwargs) -> requests.Response (body read and capped)

Address rules (the helpers of crawler.subdomains, imported): cloud metadata names and link-local / unspecified /
multicast / reserved addresses are always refused; private networks and this machine only with
allow_private_network (a self-hosted LanguageTool, an internal webhook relay). https only, except http to a
private / loopback server that is allowed. Every address the name resolves to must be allowed, and the connection
is pinned to the address that was checked (a second DNS answer cannot point elsewhere).

Redirects are never followed by requests: safe_request follows at most MAX_HOPS hops itself, each checked again.
Same origin (scheme, host, port): followed with the same headers. Another origin: refused ("The service sent us
to another site") unless its host is in redirect_hosts, and then without any custom header, body or secret query
value. Bodies are read up to max_bytes (more -> ConnectorError "too_large") within an overall time budget.

Every network error becomes a ConnectorError built from crawler.net_guard.classify_error's kind and the host only:
never the URL's path or query, never the exception text (which quotes the URL, and so a webhook's secret).
"""
import socket
import threading
import time
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from .base import ConnectorError

MAX_HOPS = 3
USER_AGENT = "ARACHNIX-Connectors/1.0"
REDIRECTS = (301, 302, 303, 307, 308)
SAFE_HEADERS = {"user-agent", "accept", "accept-language"}
SECRET_PARAM_NAMES = {"key", "api_key", "apikey", "token", "access_token", "sig", "signature", "auth", "secret",
                      "password", "code", "apikey"}
KIND_TEXT = {
    "dns": "The name {host} could not be found (DNS lookup failed).",
    "ssl": "The secure connection to {host} failed (TLS certificate or protocol problem).",
    "timeout": "{host} did not answer in time.",
    "refused": "{host} refused the connection (nothing is listening there).",
    "reset": "{host} closed the connection before answering.",
    "too_many_redirects": "{host} redirected too many times.",
    "other": "Could not reach {host}.",
}


def _host_label(parts) -> str:
    host = (parts.hostname or "").lower()
    if not host:
        return "the server"
    port = parts.port
    if port and not ((parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)):
        return f"{host}:{port}"
    return host


def _split(url: str):
    try:
        parts = urlsplit(str(url or "").strip())
        _ = parts.port            # raises ValueError for a bad port
    except ValueError:
        raise ConnectorError("That is not a valid web address.", "invalid")
    return parts


def error_from(exc: BaseException, host: str) -> ConnectorError:
    """A ConnectorError from a requests / urllib3 / socket error: the kind and the host only."""
    try:
        from ..net_guard import classify_error
        kind = classify_error(exc)
    except Exception:
        kind = "other"
    if isinstance(exc, ConnectorError):
        return exc
    return ConnectorError(KIND_TEXT.get(kind, KIND_TEXT["other"]).format(host=host or "the server"), kind, host)


def check_url(url: str, allow_private_network: bool = False) -> List[str]:
    """The resolved IP addresses `url` may connect to (all of them allowed), else ConnectorError."""
    from ..subdomains import _METADATA_NAMES, _ip, never_open, resolve
    from ..url_utils import is_internal_address
    parts = _split(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    label = _host_label(parts)
    if scheme not in ("http", "https") or not host:
        raise ConnectorError("Use a web address that starts with https://.", "invalid", label)
    if parts.username or parts.password:
        raise ConnectorError("Put the sign-in in its own field, not inside the address.", "invalid", label)
    if host in _METADATA_NAMES or host.endswith(".internal") and host.startswith("metadata"):
        raise ConnectorError(f"{label} is a cloud metadata service: ARACHNIX never connects to it.",
                             "refused_address", label)
    ips = [host] if _ip(host) is not None else resolve(host)
    if not ips:
        raise ConnectorError(KIND_TEXT["dns"].format(host=label), "dns", label)
    for ip in ips:
        addr = _ip(ip)
        if addr is not None and addr.is_loopback:
            continue                       # this machine: a private address (::1 counts as reserved in ipaddress)
        if never_open(ip, loopback_ok=True):
            raise ConnectorError(f"{label} points to an address ARACHNIX never connects to (link-local, cloud "
                                 "metadata or reserved).", "refused_address", label)
    internal = [ip for ip in ips if is_internal_address(ip)]
    if internal and not allow_private_network:
        raise ConnectorError(f"{label} points to a private network address. Turn on 'Allow private network' for "
                             "a server inside your own network.", "refused_address", label)
    if scheme == "http":
        if not allow_private_network or len(internal) != len(ips):
            raise ConnectorError("Use an https:// address (http:// is allowed only for a server inside your own "
                                 "network, with 'Allow private network' on).", "invalid", label)
    return ips


def _abort(resp: requests.Response, killed: threading.Event) -> None:
    """Cuts a response's connection from another thread (the overall time budget ran out)."""
    killed.set()
    raw = getattr(resp, "raw", None)
    conn = getattr(raw, "connection", None) or getattr(raw, "_connection", None)
    sock = getattr(conn, "sock", None)
    if sock is None:
        fp = getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None)
        sock = getattr(fp, "_sock", None)
    try:
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        resp.close()
    except Exception:
        pass


def _origin(parts) -> Tuple[str, str, int]:
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


def _strip_secret_query(url: str, secret_values: Iterable[str]) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    values = {str(v) for v in secret_values if v}
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in SECRET_PARAM_NAMES and v not in values]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def _session(url: str, ip: str) -> requests.Session:
    """A session pinned to the checked address (unless the environment routes through a proxy, which resolves
    the name itself; the address was still checked)."""
    from ..subdomains import _PinnedAdapter
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    proxies = requests.utils.get_environ_proxies(url)
    if proxies:
        s.proxies.update(proxies)
    else:
        s.trust_env = False
        s.mount("https://", _PinnedAdapter(ip))
        s.mount("http://", _PinnedAdapter(ip))
    return s


def safe_request(method: str, url: str, *, allow_private_network: bool = False,
                 timeout: Tuple[float, float] = (5, 20), max_bytes: int = 2_000_000,
                 redirect_hosts: Sequence[str] = (), secrets: Sequence[str] = (), **kw) -> requests.Response:
    """One outgoing call under the rules of this module. Returns the response with its body read (any status;
    the caller judges it). Raises ConnectorError (kind + host only)."""
    method = str(method or "GET").upper()
    headers = dict(kw.pop("headers", None) or {})
    kw.pop("allow_redirects", None)
    kw.pop("stream", None)
    if isinstance(timeout, (int, float)):
        timeout = (min(5.0, float(timeout)), float(timeout))
    deadline = time.monotonic() + float(timeout[0]) + float(timeout[1]) * 1.5
    allowed = {h.lower() for h in redirect_hosts or ()}
    first = _split(url)
    origin = _origin(first)
    cur, hops, crossed = str(url), 0, False
    while True:
        parts = _split(cur)
        label = _host_label(parts)
        ips = check_url(cur, allow_private_network)
        session = _session(cur, ips[0])
        try:
            try:
                resp = session.request(method, cur, headers=headers, allow_redirects=False, stream=True,
                                       timeout=timeout, **kw)
            except requests.RequestException as e:
                raise error_from(e, label) from None
            except OSError as e:
                raise error_from(e, label) from None
            if resp.status_code in REDIRECTS and resp.headers.get("Location"):
                location = resp.headers.get("Location") or ""
                resp.close()
                hops += 1
                if hops > MAX_HOPS:
                    raise ConnectorError(KIND_TEXT["too_many_redirects"].format(host=label), "too_many_redirects",
                                         label)
                nxt = urljoin(cur, location)
                nparts = _split(nxt)
                if nparts.scheme not in ("http", "https"):
                    raise ConnectorError(f"{label} sent us to an address that is not a web page.", "redirect", label)
                if _origin(nparts) != origin:
                    nhost = (nparts.hostname or "").lower()
                    if nhost not in allowed:
                        raise ConnectorError(f"The service sent us to another site ({_host_label(nparts)}); "
                                             "ARACHNIX does not follow it.", "redirect", label)
                    # another origin we accept: no custom header, body or secret query value goes there
                    crossed = True
                    origin = _origin(nparts)
                    headers = {k: v for k, v in headers.items() if k.lower() in SAFE_HEADERS}
                    nxt = _strip_secret_query(nxt, secrets)
                    for k in ("data", "json", "files", "params", "auth", "cookies"):
                        kw.pop(k, None)
                    method = "GET"
                elif resp.status_code == 303 or (resp.status_code in (301, 302) and method == "POST"):
                    method = "GET"
                    for k in ("data", "json", "files"):
                        kw.pop(k, None)
                if crossed:
                    kw.pop("params", None)
                cur = nxt
                continue
            # a server that drips bytes keeps each read alive: a watchdog cuts the connection at the deadline
            killed = threading.Event()
            watchdog = threading.Timer(max(0.05, deadline - time.monotonic()), _abort, args=(resp, killed))
            watchdog.daemon = True
            watchdog.start()
            try:
                chunks, total = [], 0
                for chunk in resp.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ConnectorError(f"{label} sent more than {max(1, max_bytes // 1024):,} KB; the answer "
                                             "was not read.", "too_large", label)
                    if time.monotonic() > deadline:
                        raise ConnectorError(KIND_TEXT["timeout"].format(host=label), "timeout", label)
                    chunks.append(chunk)
                if killed.is_set():
                    raise ConnectorError(KIND_TEXT["timeout"].format(host=label), "timeout", label)
            except ConnectorError:
                resp.close()
                raise
            except Exception as e:          # requests / urllib3 / socket errors (also after the watchdog cut it)
                resp.close()
                if killed.is_set():
                    raise ConnectorError(KIND_TEXT["timeout"].format(host=label), "timeout", label) from None
                if isinstance(e, (requests.RequestException, OSError, ValueError, AttributeError)):
                    raise error_from(e, label) from None
                raise
            finally:
                watchdog.cancel()
            resp._content = b"".join(chunks)
            resp._content_consumed = True
            resp.close()
            return resp
        finally:
            session.close()


def short_body(resp: Optional[requests.Response], limit: int = 160) -> str:
    """The start of an error answer as one plain line (callers pass it through the connector's masker)."""
    if resp is None:
        return ""
    try:
        text = resp.text or ""
    except Exception:
        return ""
    text = " ".join(text.split())
    return text[:limit]


def http_error(provider: str, resp: requests.Response, mask=None, detail: str = "") -> str:
    """"<provider> answered <status> <reason>: <short detail>" (masked)."""
    reason = (resp.reason or "").strip() if resp is not None else ""
    msg = f"{provider} answered {resp.status_code}{(' ' + reason) if reason else ''}"
    detail = detail if detail else short_body(resp)
    if detail:
        msg += f": {detail}"
    return mask(msg) if mask is not None else msg
