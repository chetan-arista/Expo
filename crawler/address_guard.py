"""
Refused addresses for a crawl (P24, CrawlConfig.block_addresses):

  ''         no guard (the default; a crawl from the dashboard on the office PC can audit an intranet site)
  'metadata' never connect to cloud metadata / link-local, unspecified, multicast or reserved addresses, nor to
             this machine unless the audited site is itself on it (Render, crawls started by AI agents when the
             admin allows private networks)
  'private'  'metadata' plus private networks (10/8, 172.16/12, 192.168/16, fc00::/7, carrier-grade NAT) and
             loopback (crawls started by AI agents, MCP)

guard_session(session, mode, loopback_ok) mounts GuardedAdapter on a requests session (it keeps the session's
retry and pool settings). Every address the host name resolves to must be allowed: the request is refused before
it goes out (BlockedAddress, a requests ConnectionError, no retry), and each new connection resolves the name
again and connects to an address that passed the same test, so a second DNS answer cannot point elsewhere. With a
proxy the target name is checked before the request (the proxy makes the connection). Refused rows are
"No Response" with error_type 'blocked_address' and the plain reason, counted like any other row.
"""
import socket
from typing import List, Optional
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError

MODES = ("", "metadata", "private")
ERROR_KIND = "blocked_address"
METADATA_NAMES = {"metadata.google.internal", "metadata", "metadata.azure.com", "instance-data",
                  "instance-data.ec2.internal"}


class BlockedAddress(requests.exceptions.ConnectionError):
    """A request this crawl may not make; str() is the plain reason."""
    error_kind = ERROR_KIND


class _BlockedConnect(NewConnectionError):
    error_kind = ERROR_KIND


def _refused_ip(ip: str, mode: str, loopback_ok: bool) -> str:
    from .subdomains import never_open
    from .url_utils import is_internal_address
    loopback = ip.startswith("127.") or ip == "::1"
    if never_open(ip, loopback_ok=loopback_ok):
        return "this server itself" if loopback else "a cloud-metadata, link-local or reserved address"
    if loopback and loopback_ok:
        return ""                          # the audited site itself runs on this machine
    if mode == "private" and is_internal_address(ip):
        return "a private network address"
    return ""


def resolve(host: str, port: int) -> List[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: List[str] = []
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]
        if ip not in out:
            out.append(ip)
    return out


def check_host(host: str, port: int, mode: str, loopback_ok: bool = False) -> str:
    """'' when every address of `host` may be opened under `mode`, else the plain reason it is refused. A name
    that does not resolve is left to the request itself (it fails as a DNS error)."""
    if not mode:
        return ""
    host = (host or "").strip("[]").rstrip(".").lower()
    if host in METADATA_NAMES:
        return f"Not opened: {host} is a cloud-metadata address, which this crawl may not reach."
    try:
        ips = resolve(host, port)
    except OSError:
        return ""
    for ip in ips:
        why = _refused_ip(ip, mode, loopback_ok)
        if why:
            return f"Not opened: {host} points to {why} ({ip}), which this crawl may not reach."
    return ""


def _guarded(cls):
    class Guarded(cls):
        guard_mode = ""
        guard_loopback_ok = False

        def _new_conn(self):
            host = self._dns_host
            try:
                ips = resolve(host, self.port)
            except OSError:
                return super()._new_conn()
            allowed = [ip for ip in ips if not _refused_ip(ip, self.guard_mode, self.guard_loopback_ok)]
            if len(allowed) != len(ips) or not allowed:
                raise _BlockedConnect(self, f"Not opened: {host} points to an address this crawl may not reach.")
            saved = self._dns_host
            self._dns_host = allowed[0]           # connect to the address that was checked (SNI keeps the name)
            try:
                return super()._new_conn()
            finally:
                self._dns_host = saved
    Guarded.__name__ = "Guarded" + cls.__name__
    return Guarded


class GuardedAdapter(HTTPAdapter):
    def __init__(self, mode: str = "private", loopback_ok: bool = False, base: Optional[HTTPAdapter] = None):
        self.guard_mode = mode if mode in MODES else "private"
        self.guard_loopback_ok = loopback_ok
        kw = {}
        if base is not None:
            kw = {"max_retries": base.max_retries, "pool_connections": getattr(base, "_pool_connections", 10),
                  "pool_maxsize": getattr(base, "_pool_maxsize", 10), "pool_block": getattr(base, "_pool_block", False)}
        super().__init__(**kw)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        mode, loop = self.guard_mode, self.guard_loopback_ok

        def pool(base_pool, base_conn):
            conn = _guarded(base_conn)
            conn.guard_mode, conn.guard_loopback_ok = mode, loop

            class Pool(base_pool):
                ConnectionCls = conn
            return Pool
        self.poolmanager.pool_classes_by_scheme = {"http": pool(HTTPConnectionPool, HTTPConnection),
                                                   "https": pool(HTTPSConnectionPool, HTTPSConnection)}

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        why = check_host(parts.hostname or "", port, self.guard_mode, self.guard_loopback_ok)
        if why:
            raise BlockedAddress(why, request=request)
        return super().send(request, **kwargs)


def guard_session(session: requests.Session, mode: str, loopback_ok: bool = False) -> requests.Session:
    """Mounts GuardedAdapter for http and https on `session` (a no-op for mode ''); keeps the retry and pool
    settings of the adapter that was there."""
    mode = (mode or "").strip().lower()
    if not mode or mode not in MODES:
        return session
    for prefix in ("https://", "http://"):
        base = session.adapters.get(prefix)
        session.mount(prefix, GuardedAdapter(mode, loopback_ok, base if isinstance(base, HTTPAdapter) else None))
    return session


def is_blocked(exc: BaseException) -> bool:
    cur: Optional[BaseException] = exc
    seen = 0
    while cur is not None and seen < 10:
        if getattr(cur, "error_kind", "") == ERROR_KIND:
            return True
        nxt = cur.args[0] if cur.args and isinstance(cur.args[0], BaseException) else None
        cur = getattr(cur, "reason", None) or nxt or cur.__cause__ or cur.__context__
        seen += 1
    return False
