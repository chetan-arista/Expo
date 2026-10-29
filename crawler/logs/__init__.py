"""
Server log file analysis (P22): which pages Googlebot, Bingbot and AI bots really request.

  analyse(path, crawler, verify=True, stop=None, progress=None) -> stored entry (integrations source "logs")

The upload (W11: streamed by web/v2_misc.py to a temp file, at most 200 MB, deleted after parsing) is read line by line
(gzip detected by its magic bytes), so a big file never sits in memory. Formats: Apache / Nginx Combined and Common,
W3C extended / IIS (#Fields: header). Bots are told apart by their user agent (the robots_audit bot list); the
search bots that publish their reverse DNS (Googlebot, Bingbot, Applebot) are verified for at most VERIFY_MAX_IPS
addresses: a hit from an address whose reverse name is not theirs is "unverified" (a fake bot). Raw lines and IP
addresses are never kept: only counts per URL and bot, the last hit and the status codes.

The entry: {"source": "logs", "connected": True, "status": "done", "fetched_at", "lines", "parsed", "bot_hits",
"formats", "results": {url: {"hits": {bot label: n}, "verified": n, "unverified": n, "statuses": {code: n},
"last_hit": iso}}, "orphans": [{"url", "hits", "bots", "statuses"}] (bot-requested URLs the crawl never found),
"bots": {label: {"hits", "urls", "kind"}}, "reason": ""}.
"""
import gzip
import io
import re
import socket
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

MAX_BYTES = 200 * 1024 * 1024
VERIFY_MAX_IPS = 200
MAX_ORPHANS = 5000
_COMBINED = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" '
                       r'(?P<status>\d{3}) \S+(?: "(?P<ref>[^"]*)" "(?P<ua>[^"]*)")?')
VERIFY_DOMAINS = {"Googlebot": (".googlebot.com", ".google.com", ".googleusercontent.com"),
                  "Bingbot": (".search.msn.com",), "Applebot": (".applebot.apple.com",)}


def _bots():
    from ..seo.robots_audit import BOTS
    out = []
    for b in BOTS:
        if b.token in ("google-extended", "applebot-extended"):
            continue                          # robots.txt tokens only: they never appear in a user agent
        out.append((b.label.lower(), b.label, b.kind))
    out.sort(key=lambda x: -len(x[0]))        # "claude-searchbot" before "claudebot"...
    return out


def bot_of(ua: str, bots) -> Optional[Tuple[str, str]]:
    low = (ua or "").lower()
    for token, label, kind in bots:
        if token in low:
            return label, kind
    return None


def _open(path: str):
    with open(path, "rb") as f:
        magic = f.read(2)
    raw = gzip.open(path, "rb") if magic == b"\x1f\x8b" else open(path, "rb")
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


def _time(text: str) -> str:
    for fmt in ("%d/%b/%Y:%H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return ""


def parse_lines(lines, stop: Optional[threading.Event] = None):
    """Yields (ip, iso time, method, path, status, user agent, format) for every request line it can read."""
    fields: Optional[List[str]] = None
    for line in lines:
        if stop is not None and stop.is_set():
            return
        line = line.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("#"):
            if line.lower().startswith("#fields:"):
                fields = line[8:].split()
            continue
        if fields:
            parts = line.split(" ")
            if len(parts) < len(fields):
                continue
            rec = dict(zip(fields, parts))
            path = rec.get("cs-uri-stem", "")
            q = rec.get("cs-uri-query", "-")
            if q and q != "-":
                path += "?" + q
            ua = rec.get("cs(User-Agent)", "").replace("+", " ")
            when = _time(f"{rec.get('date', '')} {rec.get('time', '')}".strip())
            try:
                status = int(rec.get("sc-status", "0"))
            except ValueError:
                continue
            yield rec.get("c-ip", ""), when, rec.get("cs-method", "GET"), path, status, ua, "w3c"
            continue
        m = _COMBINED.match(line)
        if m:
            yield (m.group("ip"), _time(m.group("time")), m.group("method"), m.group("path"), int(m.group("status")),
                   m.group("ua") or "", "combined" if m.group("ua") is not None else "common")


def _reverse_ok(ip: str, label: str) -> bool:
    """Reverse DNS of the address ends in the bot's domain and that name resolves back to the address."""
    try:
        name = socket.gethostbyaddr(ip)[0].lower()
        if not name.endswith(VERIFY_DOMAINS[label]):
            return False
        return ip in {i[4][0] for i in socket.getaddrinfo(name, None)}
    except (OSError, UnicodeError):
        return False


def verify_ips(pairs: Dict[Tuple[str, str], int], limit: int = VERIFY_MAX_IPS) -> Dict[Tuple[str, str], Optional[bool]]:
    """{(ip, bot label): True | False | None (not checked: over the limit or DNS too slow)}, busiest addresses first."""
    todo = [k for k, _ in sorted(pairs.items(), key=lambda x: -x[1])][:limit]
    out: Dict[Tuple[str, str], Optional[bool]] = {k: None for k in pairs}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_reverse_ok, ip, label): (ip, label) for ip, label in todo}
        for f, key in futs.items():
            try:
                out[key] = f.result(timeout=5)
            except Exception:
                out[key] = None
    return out


def analyse(path: str, crawler, verify: bool = True, stop: Optional[threading.Event] = None,
            progress: Optional[Callable[[int], None]] = None) -> Dict[str, Any]:
    """Reads the log at `path` and returns the stored entry (see the module docstring). Never keeps an IP address
    beyond this call."""
    start = str(getattr(crawler, "start_url", "") or "")
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(start)) if start else ""
    try:
        from ..url_utils import normalize_url
    except Exception:          # pragma: no cover
        normalize_url = None
    bots = _bots()
    per_url: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"hits": Counter(), "statuses": Counter(), "last_hit": "",
                                                             "ip_hits": Counter()})
    ip_pairs: Counter = Counter()
    lines = parsed = bot_hits = 0
    formats: Counter = Counter()
    with _open(path) as fh:
        counting = (setattr(fh, "_n", 0) or fh)
        for ip, when, method, req, status, ua, fmt in parse_lines(_counted(counting, lambda n: progress(n)
                                                                            if progress else None), stop):
            parsed += 1
            formats[fmt] += 1
            hit = bot_of(ua, bots)
            if hit is None or method not in ("GET", "HEAD"):
                continue
            label, _kind = hit
            bot_hits += 1
            url = req if req.startswith(("http://", "https://")) else origin + (req if req.startswith("/") else "/" + req)
            if normalize_url is not None:
                url = normalize_url(url) or url
            rec = per_url[url]
            rec["hits"][label] += 1
            rec["statuses"][str(status)] += 1
            if when > rec["last_hit"]:
                rec["last_hit"] = when
            if label in VERIFY_DOMAINS and ip:
                rec["ip_hits"][(ip, label)] += 1
                ip_pairs[(ip, label)] += 1
        lines = getattr(counting, "_n", parsed)
    checks = verify_ips(ip_pairs) if verify and ip_pairs else {}
    crawled = set((getattr(crawler, "results", None) or {}).keys())
    results: Dict[str, Dict[str, Any]] = {}
    orphans: List[Dict[str, Any]] = []
    bots_summary: Dict[str, Dict[str, Any]] = {}
    kinds = {label: kind for _t, label, kind in bots}
    for url, rec in per_url.items():
        verified = sum(n for (ip, label), n in rec["ip_hits"].items() if checks.get((ip, label)) is True)
        unverified = sum(n for (ip, label), n in rec["ip_hits"].items() if checks.get((ip, label)) is False)
        out = {"hits": dict(rec["hits"]), "statuses": dict(rec["statuses"]), "last_hit": rec["last_hit"],
               "verified": verified, "unverified": unverified}
        for label, n in rec["hits"].items():
            b = bots_summary.setdefault(label, {"hits": 0, "urls": 0, "kind": kinds.get(label, "")})
            b["hits"] += n
            b["urls"] += 1
        if url in crawled:
            results[url] = out
        elif len(orphans) < MAX_ORPHANS:
            orphans.append({"url": url, "hits": sum(rec["hits"].values()), "bots": sorted(rec["hits"]),
                            "statuses": dict(rec["statuses"]), "last_hit": rec["last_hit"]})
    orphans.sort(key=lambda o: -o["hits"])
    reason = "" if parsed else "No request line of this file could be read (Combined, Common or W3C / IIS format)."
    return {"source": "logs", "connected": True, "status": "done" if parsed else "error", "lines": lines,
            "parsed": parsed, "bot_hits": bot_hits, "formats": dict(formats), "results": results, "orphans": orphans,
            "bots": bots_summary, "verified_ips": sum(1 for v in checks.values() if v is True),
            "unverified_ips": sum(1 for v in checks.values() if v is False),
            "not_verified_ips": sum(1 for v in checks.values() if v is None), "reason": reason}


def _counted(fh, on_line: Callable[[int], Any]):
    n = 0
    for line in fh:
        n += 1
        if n % 20000 == 0:
            on_line(n)
        yield line
    fh._n = n
