"""
Saved crawls (File > Crawls...): every finished crawl kept on disk so it can be opened again, renamed, locked,
duplicated, exported, imported and cleaned up by the retention policy (Settings > Crawl Retention).

Layout under <reports_dir>/crawls/ (reports_dir = env CRAWLER_REPORTS_DIR, else the system temp folder):
  <host>/<ts>/crawl.json.gz   the crawl (FORMAT below)
  <host>/<ts>/meta.json       its meta, so listing never opens crawl.json.gz
  <host>/<ts>/sources.zip     the crawl's View Source pages (the source store's gzip files, read by name)
  <host>/<ts>/evidence/       its screenshots (PDF evidence, pinpointed pictures), when any
  _projects.json              {project: {"locked": bool}}: a locked project protects all its crawls
The id is "<host>/<ts>" with ts the crawl's start time ("20261026T101500Z"), the same id as the crawl's
snapshot (crawler/seo/compare.py); a duplicate or an import of an id that exists gets "-1", "-2"...

  save_crawl(job_or_crawler, reports_dir, name=..., ...) -> meta       after a crawl (or a re-check) finished
  list_crawls(reports_dir, site=None, project=None, q="") -> [meta]   newest first
  crawl_history(reports_dir, site=None) -> [meta]                     snapshots + saved crawls, one row per id
  load_crawl(id, reports_dir) -> LoadedCrawl                           .crawler is a WebsiteCrawler in state "done"
  rename / set_project / lock / lock_project / duplicate / delete
  export_bytes(id) -> (filename, bytes) ; import_bytes(data, filename) -> meta
  apply_retention(settings_or_days, reports_dir) -> {"deleted", "kept_locked", ...}
  storage_info(reports_dir) -> {"dir", "env_set", "warning", "crawls", "bytes_used", "disk_free", ...}

What a saved crawl holds: the CrawlConfig without secrets (no login values, header values, proxy login),
every CrawlResult field (seo PageData, response headers with cookie values masked, list_index...), every
external link, the crawler's own state (sitemaps, post-crawl reports, limits, list-mode positions...: every
plain-data attribute, so new engine attributes are kept without changes here), the validation (re-checks),
the site's dismissals, extra data such as integrations, and the screenshots. Secrets never reach the file:
the login's values (crawler.auth.secret_values) and any value the caller passes are replaced by *** wherever
they appear, Set-Cookie values and secret-looking headers are masked, secret-named keys are dropped.

load_crawl rebuilds a real WebsiteCrawler (status as saved, phase "done") so crawler.site_index(), every tab,
export, report, PDF and re-check works unchanged; the login is not saved, so re-checks of a logged-in crawl run
logged out (LoadedCrawl.notes says so). The registry counts of the crawl at save time are kept ("index_counts")
and LoadedCrawl.count_differences(index) lists any filter whose count differs after loading.

FORMAT (crawl.json.gz): one JSON document written one record per line, so it is read back line by line:
  {"header":{"format","version","meta","config"},
  "results":[
  {result},
  ,{result}
  ],"externals":[
  {external}
  ],"tail":{"state","stats","validation","dismissals","extras","evidence","seo_evidence","index_counts",...}}
"""
import gzip
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

FORMAT = "site-auditor-crawl"
FORMAT_VERSION = 1
FILE_NAME = "crawl.json.gz"
META_NAME = "meta.json"
PROJECTS_NAME = "_projects.json"
NAME_LIMIT = 120
PROJECT_LIMIT = 60
GZIP_LEVEL = 5
# Upper safety cap per site (the retention policy decides first; locked crawls never count): env
# CRAWLER_MAX_CRAWLS, else CRAWLER_MAX_SNAPSHOTS (the snapshot cap), else 20. 0 = no cap.
MAX_CRAWLS_PER_SITE = int(os.environ.get("CRAWLER_MAX_CRAWLS") or os.environ.get("CRAWLER_MAX_SNAPSHOTS") or 20)
MAX_IMPORT_BYTES = int(float(os.environ.get("CRAWLER_MAX_IMPORT_MB") or 500) * 1024 * 1024)   # uploaded file
MAX_UNPACKED_BYTES = MAX_IMPORT_BYTES * 8          # everything inside it, decompressed (zip-bomb guard)
# Scheduled-run export folders kept per task by retention (env CRAWLER_MAX_SCHEDULED_RUNS, 0 = no cap). A cap
# applies even when crawl retention is "never", so an hourly Excel+PDF task cannot fill the disk unbounded.
MAX_SCHEDULED_RUNS = int(os.environ.get("CRAWLER_MAX_SCHEDULED_RUNS") or 100)
RETENTION_CHOICES = ("never", "1", "7", "30", "90", "custom")
CUSTOM_DAYS_MAX = 3650

_TS_RE = r"\d{8}T\d{6}Z(?:-\d{1,3})?"
_ID_RE = re.compile(r"^([a-z0-9._-]{1,200})/(" + _TS_RE + r")$")
_TS_ONLY = re.compile(r"^" + _TS_RE + r"$")
_EVIDENCE_NAME = re.compile(r"^evidence/(?:[A-Za-z0-9._-]{1,200}/){0,3}[A-Za-z0-9._-]{1,200}$")
# Response headers whose value can be a working login (same rule as web/app.py safe_headers)
_SECRET_HEADER = re.compile(r"cookie|authorization|token|secret|session|api-?key|signature|csrf|xsrf|passw", re.I)
_COOKIE_PAIR = re.compile(r"(^|[;,]\s*)([^=;,\s]+)=([^;,]*)")
_COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "samesite", "priority", "partitioned", "secure", "httponly"}
# Keys whose value is a secret wherever they appear in saved state / extras (exact names, lower case)
_SECRET_KEYS = {"password", "passwd", "smtp_password", "proxy_password", "client_secret", "secret", "token",
                "access_token", "refresh_token", "id_token", "api_key", "apikey", "auth_cookies", "session_state",
                "custom_header_value", "authorization"}
# Crawler attributes never written: live machinery (rebuilt by the constructor), one-process caches, secrets
_STATE_SKIP = {
    "config", "results", "results_list", "external_links", "external_links_list",
    "on_result", "on_external_link", "on_log", "on_progress", "session", "http_session", "browser", "auth_handler",
    "robots", "dynamic_extractor", "rate_limiter", "throttle", "source_store", "render_hook", "render_pool",
    "extract_options", "url_rules", "_include_res", "_exclude_res", "_ext_executor", "_ext_futures",
    "_index_cache", "_index_key", "_index_time", "_index_cost", "_semantic_cache", "_session_check",
    "_fixfiles_cache", "saved_crawl",
    "auth_cookies", "session_state", "request_headers", "proxies",
}
_SECRET_ATTR = re.compile(r"passw|secret|token|cookie|api_?key|credential", re.I)

_lock = threading.RLock()
_id_locks: Dict[str, threading.RLock] = {}
# Ids just deleted: a follow-up job (a re-check, Crawl Analysis, the bot check, a connected-data fetch) must
# not quietly recreate the crawl the user removed when it re-saves. Bounded; a re-import clears the entry.
_tombstones: "OrderedDict[str, float]" = OrderedDict()
_TOMBSTONE_MAX = 512


class StoreError(Exception):
    """A saved-crawl operation that cannot be done; the message is shown to the user."""


class _Unsupported(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Paths, ids, small helpers
# ---------------------------------------------------------------------------------------------
def default_reports_dir() -> str:
    return os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()


def crawls_root(reports_dir: Optional[str] = None) -> str:
    return os.path.join(reports_dir or default_reports_dir(), "crawls")


def host_key(url_or_host: str) -> str:
    """Folder-safe site key (the snapshot's): lower-case host and port without a leading "www."."""
    from .seo.compare import host_key as _hk
    return _hk(url_or_host)


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def _parse_iso(text: str) -> float:
    try:
        return datetime.strptime(str(text or ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _path_of(crawl_id: str, reports_dir: Optional[str]) -> Optional[str]:
    """Folder of a crawl id, or None for a malformed id (ids never reach outside the crawls folder)."""
    m = _ID_RE.match(str(crawl_id or "").strip())
    if not m:
        return None
    return os.path.join(crawls_root(reports_dir), m.group(1), m.group(2))


def _require(crawl_id: str, reports_dir: Optional[str]) -> str:
    path = _path_of(crawl_id, reports_dir)
    if not path or not os.path.isfile(os.path.join(path, META_NAME)):
        raise StoreError(f"Saved crawl not found: {crawl_id}")
    return path


def _id_lock(crawl_id: str) -> threading.RLock:
    with _lock:
        return _id_locks.setdefault(crawl_id, threading.RLock())


def _write_json(path: str, data: Any) -> None:
    """Atomic JSON write (temp file + replace)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _folder_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _link_or_copy(src: str, dst: str) -> str:
    """Hard link when possible (same disk: instant, no extra space), else a copy."""
    try:
        os.link(src, dst)
        return dst
    except OSError:
        return shutil.copy2(src, dst)


def _copy_tree(src: str, dst: str, link: bool = True) -> bool:
    """Copies a folder (hard links for files that are only ever replaced, never rewritten in place)."""
    if not src or not os.path.isdir(src):
        return False
    try:
        shutil.copytree(src, dst, copy_function=_link_or_copy if link else shutil.copy2, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.tmp", "*.partial"))
        return True
    except (OSError, shutil.Error):
        return False


def clean_name(text: Any, limit: int = NAME_LIMIT) -> str:
    return " ".join(str(text or "").split())[:limit]


# ---------------------------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------------------------
# A password typed into the start URL ("https://user:pass@host/" - HTTP basic auth, a normal way to crawl a
# protected site) is a login secret: it is masked in the saved crawl, in exports and in debug output.
_URL_USERINFO = re.compile(r"(://[^/?#@:]*:)([^/?#@]*)(@)")


def _url_password(url: Any) -> str:
    """The password embedded in a URL's userinfo (user:password@host), or "" when there is none."""
    m = _URL_USERINFO.search(str(url or ""))
    return m.group(2) if m and m.group(2) else ""


def mask_url_userinfo(url: Any) -> str:
    """`url` with any embedded password replaced by *** ("https://user:***@host/"); the user name and the rest
    of the address are kept so it still reads as the same URL (host, port and path are untouched)."""
    return _URL_USERINFO.sub(lambda m: m.group(1) + "***" + m.group(3) if m.group(2) else m.group(0), str(url or ""))


def _secret_list(crawler, extra: Optional[Iterable[str]]) -> List[str]:
    """Values replaced by *** wherever they appear: the caller's (job.secrets) plus the login's own."""
    found = [str(s) for s in (extra or ()) if s]
    for u in (getattr(crawler, "start_url", "") or "",
              getattr(getattr(crawler, "config", None), "start_url", "") or ""):
        pw = _url_password(u)
        if pw:
            found.append(pw)
    auth = getattr(getattr(crawler, "config", None), "auth", None)
    if auth is not None and getattr(auth, "enabled", False):
        try:
            from .auth import secret_values
            found += secret_values(auth, getattr(crawler, "start_url", "") or "https://example.com/")
        except Exception:
            found += [str(getattr(auth, k, "") or "") for k in ("password", "custom_header_value", "cookies")]
    proxy = getattr(getattr(crawler, "config", None), "proxy", None)
    if isinstance(proxy, dict) and proxy.get("password"):
        found.append(str(proxy["password"]))
    out = set()
    for s in found:
        if len(s) >= 4:
            out.add(s)
            if len(s) >= 20:
                out.add(s[:12])     # an error message quoting the start of a long token
    return sorted(out, key=len, reverse=True)


_JSON_STR = re.compile(r'"(?:[^"\\]|\\.)*"')


def _mask(text: str, secrets: List[str]) -> str:
    """`text` (JSON) with every secret inside a string value replaced by ***. Only string values are touched
    (decoded, masked, encoded again), so a secret such as "true" never breaks the JSON around it."""
    if not secrets:
        return text
    probes = {s: json.dumps(s, ensure_ascii=False)[1:-1] for s in secrets}
    if not any(s in text or e in text for s, e in probes.items()):
        return text

    def sub(m):
        tok = m.group(0)
        if not any(s in tok or e in tok for s, e in probes.items()):
            return tok
        try:
            value = json.loads(tok)
        except ValueError:
            return tok
        new = value
        for s in secrets:
            if s in new:
                new = new.replace(s, "***")
        return tok if new == value else json.dumps(new, ensure_ascii=False)
    return _JSON_STR.sub(sub, text)


def _masked_set_cookie(value: str) -> str:
    found = []

    def sub(m):
        if m.group(2).lower() in _COOKIE_ATTRS:
            return m.group(0)
        found.append(1)
        return f"{m.group(1)}{m.group(2)}=***"
    masked = _COOKIE_PAIR.sub(sub, value)
    return masked if found else "***"


def safe_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Response headers fit to keep on disk: cookie values and token-like header values masked."""
    out: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        v = ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
        name = str(k).lower()
        if name == "set-cookie":
            v = _masked_set_cookie(v)
        elif _SECRET_HEADER.search(name):
            v = "***"
        out[str(k)] = v
    return out


def scrub(obj: Any, depth: int = 0) -> Any:
    """A copy of `obj` with the values of secret-named keys (password, token, api_key...) replaced by ***."""
    if depth > 40:
        return obj
    if isinstance(obj, dict):
        return {k: ("***" if isinstance(k, str) and k.lower() in _SECRET_KEYS and v not in (None, "", {}, [])
                    else scrub(v, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, depth + 1) for v in obj]
    return obj


def public_config(config) -> Dict[str, Any]:
    """CrawlConfig as plain data without secrets: the login keeps only its kind and page, header values and
    the proxy login are left out."""
    import dataclasses
    if config is None:
        return {}
    try:
        data = dataclasses.asdict(config)
    except TypeError:
        data = dict(getattr(config, "__dict__", {}) or {})
    auth = data.get("auth") or {}
    data["auth"] = {"enabled": bool(auth.get("enabled")), "auth_type": auth.get("auth_type") or "none",
                    "login_url": auth.get("login_url") or None}
    data["request_headers"] = [{"name": str(h.get("name") or ""), "value": "***"}
                               for h in data.get("request_headers") or [] if isinstance(h, dict)]
    proxy = data.get("proxy") or {}
    if isinstance(proxy, dict):
        data["proxy"] = {"enabled": bool(proxy.get("enabled")), "host": proxy.get("host") or "",
                         "port": proxy.get("port") or 0, "credentials": bool(proxy.get("username"))}
    if data.get("start_url"):
        data["start_url"] = mask_url_userinfo(data["start_url"])
    if isinstance(data.get("seed_urls"), list):
        data["seed_urls"] = [mask_url_userinfo(u) for u in data["seed_urls"]]
    data.pop("output_file", None)
    return scrub(data)


# ---------------------------------------------------------------------------------------------
# Typed encoding of crawler state (sets, tuples, Counters, deques survive the round trip)
# ---------------------------------------------------------------------------------------------
def _enc(v: Any, depth: int = 0) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if depth > 30:
        raise _Unsupported()
    d = depth + 1
    if isinstance(v, Counter):
        return {"__t": "counter", "v": [[_enc(k, d), _enc(n, d)] for k, n in v.items()]}
    if isinstance(v, defaultdict):
        factory = {list: "list", set: "set", int: "int", dict: "dict"}.get(v.default_factory)
        if factory is None:
            raise _Unsupported()
        return {"__t": "defaultdict", "f": factory, "v": [[_enc(k, d), _enc(x, d)] for k, x in v.items()]}
    if isinstance(v, dict):
        if "__t" not in v and all(isinstance(k, str) for k in v):
            return {k: _enc(x, d) for k, x in v.items()}
        return {"__t": "dict", "v": [[_enc(k, d), _enc(x, d)] for k, x in v.items()]}
    if isinstance(v, list):
        return [_enc(x, d) for x in v]
    if isinstance(v, tuple):
        return {"__t": "tuple", "v": [_enc(x, d) for x in v]}
    if isinstance(v, (set, frozenset)):
        return {"__t": "set", "v": [_enc(x, d) for x in v]}
    if isinstance(v, deque):
        return {"__t": "deque", "v": [_enc(x, d) for x in v], "maxlen": v.maxlen}
    raise _Unsupported()


def _hashable(v: Any) -> Any:
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    return v


def _dec(v: Any) -> Any:
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if not isinstance(v, dict):
        return v
    tag = v.get("__t")
    if tag is None:
        return {k: _dec(x) for k, x in v.items()}
    items = v.get("v") or []
    if tag == "tuple":
        return tuple(_dec(x) for x in items)
    if tag == "set":
        return {_hashable(_dec(x)) for x in items}
    if tag == "counter":
        return Counter({_hashable(_dec(k)): _dec(n) for k, n in items})
    if tag == "deque":
        return deque((_dec(x) for x in items), maxlen=v.get("maxlen"))
    if tag == "dict":
        return {_hashable(_dec(k)): _dec(x) for k, x in items}
    if tag == "defaultdict":
        factory = {"list": list, "set": set, "int": int, "dict": dict}.get(v.get("f"), list)
        out = defaultdict(factory)
        for k, x in items:
            out[_hashable(_dec(k))] = _dec(x)
        return out
    return {k: _dec(x) for k, x in v.items()}


def _json_default(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        try:
            return sorted(o)
        except TypeError:
            return list(o)
    if isinstance(o, deque):
        return list(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return str(o)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def crawler_state(crawler) -> Dict[str, Any]:
    """Every plain-data attribute of the crawler (typed-encoded), without machinery, caches and secrets."""
    out: Dict[str, Any] = {}
    for name, value in list(vars(crawler).items()):
        if name in _STATE_SKIP or name.startswith("__") or _SECRET_ATTR.search(name) or callable(value):
            continue
        try:
            out[name] = _enc(value)
        except (_Unsupported, RecursionError):
            continue
    return out


# ---------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------
def _result_record(r) -> Dict[str, Any]:
    rec = {}
    for f in dc_fields(r):
        rec[f.name] = getattr(r, f.name, None)
    rec["response_headers"] = safe_headers(rec.get("response_headers"))
    seo = rec.get("seo")
    if isinstance(seo, dict) and seo.get("cookies"):
        seo = dict(seo)
        seo["cookies"] = [{k: v for k, v in c.items() if k != "value"} for c in seo["cookies"] if isinstance(c, dict)]
        rec["seo"] = seo
    return rec


def _external_record(e) -> Dict[str, Any]:
    rec = {f.name: getattr(e, f.name, None) for f in dc_fields(e)}
    rec["response_headers"] = safe_headers(rec.get("response_headers"))
    return rec


def _build(cls, rec: Dict[str, Any]):
    """cls(**rec) with only the fields cls has (a crawl saved by another version keeps loading)."""
    names = {f.name for f in dc_fields(cls)}
    obj = cls(**{k: v for k, v in rec.items() if k in names and k not in ("seo", "response_headers")})
    if "seo" in names:
        obj.seo = rec.get("seo")
    if "response_headers" in names:
        obj.response_headers = rec.get("response_headers") or {}
    return obj


def category_counts(results: Iterable[Any]) -> Dict[str, int]:
    """URLs per status category (2xx/3xx/4xx/5xx/blocked/error): category = HTTP status, sums to the total."""
    from .issues import classify_result
    counts = Counter()
    for r in results:
        try:
            counts[classify_result(r)] += 1
        except Exception:
            counts["error"] += 1
    return {k: counts.get(k, 0) for k in ("2xx", "3xx", "4xx", "5xx", "blocked", "error")}


def _index_counts(index) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for tab_id, tr in (getattr(index, "tabs", None) or {}).items():
        out[f"{tab_id}:all"] = len(tr.rows or [])
        for fid, hits in (tr.filter_rows or {}).items():
            out[f"{tab_id}:{fid}"] = len(hits or [])
    return out


# ---------------------------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------------------------
def _default_name(crawler) -> str:
    start = mask_url_userinfo(str(getattr(crawler, "start_url", "") or ""))
    label = re.sub(r"^https?://(www\.)?", "", start).rstrip("/") or "site"
    if getattr(crawler, "mode", "") == "list":
        n = len(getattr(crawler, "_list_index", None) or {})
        return clean_name(f"List of {n:,} URLs ({label.split('/')[0]})")
    return clean_name(label)


def _completeness(crawler, stats: Dict[str, Any]) -> str:
    status = str(getattr(crawler, "status", "") or "")
    if status == "stopped":
        return "stopped"
    cov = stats.get("coverage") if isinstance(stats.get("coverage"), dict) else {}
    if stats.get("limit_reached") or (cov or {}).get("limit_hit"):
        return "partial"
    return "complete"


def _gather(job_or_crawler, kwargs: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """(crawler, follow-up data) from a CrawlJob (web/app.py) or a bare crawler; explicit kwargs win."""
    job = job_or_crawler if hasattr(job_or_crawler, "crawler") and not hasattr(job_or_crawler, "results_list") \
        else None
    crawler = job.crawler if job is not None else job_or_crawler
    info = {
        "secrets": list(getattr(job, "secrets", None) or []),
        "validation": getattr(job, "validation", None),
        "snapshot_id": getattr(job, "snapshot_id", "") or getattr(crawler, "snapshot_id", "") or "",
        "evidence": getattr(job, "evidence", None) or {},
        "evidence_dir": getattr(job, "evidence_dir", "") or "",
        "seo_evidence": getattr(job, "seo_evidence", None) or {},
        "screenshot_note": getattr(job, "screenshot_note", "") or "",
    }
    for k, v in kwargs.items():
        if v is not None:
            info[k] = v
    return crawler, info


def _resave_id(crawler, host: str, started: float, reports_dir: Optional[str]) -> str:
    """The id to (re)save a crawl under. A crawl opened from the saved list - an import or a duplicate among
    them - keeps the id it was opened as (crawler.saved_crawl["id"]), so a re-check, a Crawl Analysis run, a
    connected-data fetch or File > Save updates that saved crawl, not the crawl whose start time it happens to
    share (an import and a duplicate keep the original's start time). A fresh crawl uses its own start time."""
    prev = getattr(crawler, "saved_crawl", None)
    pid = str(prev.get("id") or "") if isinstance(prev, dict) else ""
    if pid and _path_of(pid, reports_dir):          # a well-formed id this crawl already lives under
        return pid
    return f"{host}/{_stamp(started)}"


def _checks_meta(index) -> Dict[str, Any]:
    """What a saved crawl records about its SEO checks: the check modules that ran and any that failed to load
    ("checks_incomplete": its issues list is missing those checks)."""
    try:
        from .seo import registry
        loaded = list(getattr(index, "checks_loaded", None) or registry.loaded_modules())
        failed = dict(getattr(index, "checks_failed", None) or {}) if index is not None else registry.load_failures()
    except Exception as e:
        loaded, failed = [], {"registry": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"checks_loaded": loaded, "checks_failed": failed, "checks_incomplete": bool(failed)}


def save_crawl(job_or_crawler, reports_dir: Optional[str] = None, *, name: Optional[str] = None,
               project: Optional[str] = None, index=None, snapshot_id: Optional[str] = None,
               validation: Optional[dict] = None, dismissals: Optional[List[dict]] = None,
               extras: Optional[Dict[str, Any]] = None, secrets: Optional[List[str]] = None,
               evidence: Optional[dict] = None, evidence_dir: Optional[str] = None,
               seo_evidence: Optional[dict] = None, screenshot_note: Optional[str] = None,
               copy_sources: bool = True, allow_deleted: bool = False) -> Dict[str, Any]:
    """Writes the finished crawl (a CrawlJob or a WebsiteCrawler) and returns its meta. Saving the same crawl
    again (a re-check or a post-crawl check finished) replaces it and keeps its name, project and lock.
    `dismissals` defaults to the site's DismissStore entries; `index` (TabIndex or (site, index)) defaults
    to the crawler's cached index. Raises StoreError when there is nothing to save or the disk write fails.
    A crawl deleted from File > Crawls is never saved again by itself; allow_deleted (File > Save) brings it back."""
    crawler, info = _gather(job_or_crawler, dict(
        secrets=secrets, validation=validation, snapshot_id=snapshot_id, evidence=evidence,
        evidence_dir=evidence_dir, seo_evidence=seo_evidence, screenshot_note=screenshot_note))
    if crawler is None:
        raise StoreError("There is no crawl to save.")
    with getattr(crawler, "_lock", threading.Lock()):
        results = list(getattr(crawler, "results_list", None) or [])
        externals = list(getattr(crawler, "external_links_list", None) or [])
    if not results:
        raise StoreError("The crawl has no URLs to save.")
    if str(getattr(crawler, "status", "")) in ("running", "paused", "idle", "auth_failed"):
        raise StoreError(f"Only a finished crawl can be saved (this one is {getattr(crawler, 'status', '')}).")
    started = float(getattr(crawler, "start_time", 0) or 0) or time.time()
    finished = float(getattr(crawler, "end_time", 0) or 0)
    start_url = str(getattr(crawler, "start_url", "") or "")
    host = host_key(start_url)
    reports_dir = reports_dir or default_reports_dir()
    crawl_id = _resave_id(crawler, host, started, reports_dir)
    secret_values = _secret_list(crawler, info.get("secrets"))

    # the index the dashboard shows (built already: site_index() returns the cache)
    site = None
    if index is None:
        try:
            index = crawler.site_index() if hasattr(crawler, "site_index") else None
        except Exception:
            index = None
    if isinstance(index, tuple) and len(index) == 2:
        site, index = index
    try:
        stats = crawler.get_stats() if hasattr(crawler, "get_stats") else {}
    except Exception:
        stats = {}
    if dismissals is None:
        try:
            from .dismiss import store_for
            dismissals = store_for(reports_dir, start_url).export_entries()
        except Exception:
            dismissals = []
    try:
        from .issues import health_score
        health = health_score(results)
    except Exception:
        health = {"score": stats.get("health_score"), "pages": stats.get("pages_total"), "pages_ok": stats.get("pages_ok")}
    issues = list(getattr(index, "issues", None) or [])
    source_store = getattr(crawler, "source_store", None)

    with _id_lock(crawl_id):
        folder = _path_of(crawl_id, reports_dir)
        if not folder:
            raise StoreError(f"Cannot save a crawl of {start_url!r}.")
        old = _read_json(os.path.join(folder, META_NAME)) or {}
        existing = os.path.isfile(os.path.join(folder, META_NAME))
        if existing and (old.get("locked") or _project_locked(old, _projects(reports_dir))):
            raise StoreError("This crawl is locked, so it cannot be overwritten. "
                             "Unlock it (File > Crawls) or duplicate it first.")
        if not existing and crawl_id in _tombstones:
            if not allow_deleted:
                raise StoreError("This crawl was deleted; it was not saved again.")
            _untombstone(crawl_id)       # File > Save after a delete: the team wants it back
        meta = {
            "id": crawl_id, "name": clean_name(name) if name else (old.get("name") or _default_name(crawler)),
            "project": clean_name(project, PROJECT_LIMIT) if project is not None else (old.get("project") or ""),
            "locked": bool(old.get("locked")), "site": mask_url_userinfo(start_url), "host": host,
            "mode": str(getattr(crawler, "mode", "") or getattr(getattr(crawler, "config", None), "mode", "") or "spider"),
            "crawl_scope": str(getattr(crawler, "crawl_scope", "") or ""),
            "started_at": _iso(started), "finished_at": _iso(finished),
            "duration_s": round(max(0.0, finished - started), 1) if finished else None,
            "saved_at": _iso(time.time()), "created_at": old.get("created_at") or _iso(time.time()),
            "status": str(getattr(crawler, "status", "") or ""), "complete": _completeness(crawler, stats),
            "limit_reached": bool(stats.get("limit_reached")), "auth_used": bool(getattr(crawler, "auth_used", False)),
            "urls": len(results), "externals": len(externals), "categories": category_counts(results),
            "pages": int(health.get("pages") or 0), "pages_ok": int(health.get("pages_ok") or 0),
            "health_score": health.get("score"), "issues": len(issues),
            "issue_urls": sum(int(i.get("urls") or 0) for i in issues),
            "snapshot_id": info.get("snapshot_id") or "", "format": FORMAT, "version": FORMAT_VERSION,
            **_checks_meta(index),
        }
        for k in ("duplicated_from", "imported_from", "imported_at"):
            if old.get(k):
                meta[k] = old[k]
        evidence_src = info.get("evidence_dir") or ""
        seo_ev = info.get("seo_evidence") or {}
        tail = {
            "state": crawler_state(crawler),
            "throttle_summary": _throttle_summary(crawler),
            "source_pages": int(getattr(source_store, "pages", 0) or 0) if source_store is not None else 0,
            "stats": scrub(stats),
            "validation": scrub(info.get("validation")) if info.get("validation") else None,
            "dismissals": list(dismissals or []),
            "extras": scrub(extras or {}),
            "evidence": info.get("evidence") or {},
            "seo_evidence": [[k[0], k[1], v] if isinstance(k, tuple) and len(k) == 2 else [str(k), "", v]
                             for k, v in seo_ev.items()],
            "screenshot_note": info.get("screenshot_note") or "",
            "index_counts": _index_counts(index) if index is not None else {},
            "counts": {"results": len(results), "externals": len(externals)},
        }
        header = {"format": FORMAT, "version": FORMAT_VERSION, "meta": meta,
                  "config": public_config(getattr(crawler, "config", None))}

        resave = os.path.isfile(os.path.join(folder, META_NAME))
        # a first save is written aside and moved in whole; a re-save replaces file by file (each atomically)
        # so the page sources and screenshots it already holds are not copied again
        target = folder if resave else os.path.join(
            os.path.dirname(folder), f".{os.path.basename(folder)}.{os.getpid()}.{threading.get_ident()}.partial")
        try:
            if not resave:
                shutil.rmtree(target, ignore_errors=True)
            os.makedirs(target, exist_ok=True)
            tmp = os.path.join(target, f"{FILE_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
            _write_crawl_file(tmp, header, results, externals, tail, secret_values)
            os.replace(tmp, os.path.join(target, FILE_NAME))
            meta["sources"] = bool(copy_sources and source_store is not None
                                   and _pack_sources(source_store, os.path.join(target, SOURCES_ZIP)))
            if not meta["sources"]:
                _remove_file(os.path.join(target, SOURCES_ZIP))
            meta["evidence"] = _sync_tree(evidence_src, os.path.join(target, "evidence")) or \
                (resave and os.path.isdir(os.path.join(target, "evidence")))
            meta["size_bytes"] = _folder_size(target) + 2048
            _write_json(os.path.join(target, META_NAME), meta)
            if not resave:
                with _lock:
                    os.makedirs(os.path.dirname(folder), exist_ok=True)
                    os.replace(target, folder)
        except OSError as e:
            raise StoreError(f"The crawl could not be saved: {e.strerror or e}") from e
        finally:
            if not resave:
                shutil.rmtree(target, ignore_errors=True)
            for name in os.listdir(folder) if os.path.isdir(folder) else ():
                if name.endswith(".tmp"):
                    _remove_file(os.path.join(folder, name))
    try:
        crawler.saved_crawl = dict(meta)
    except Exception:
        pass
    return meta


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _sync_tree(src: str, dst: str) -> bool:
    """Copies the files of `src` that `dst` lacks or holds in another size (screenshots); True when dst has any."""
    if not src or not os.path.isdir(src) or os.path.normpath(src) == os.path.normpath(dst):
        return bool(src) and os.path.isdir(dst) and any(os.scandir(dst))
    try:
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            out_dir = os.path.normpath(os.path.join(dst, rel))
            for name in files:
                if name.endswith((".tmp", ".partial")):
                    continue
                s, d = os.path.join(root, name), os.path.join(out_dir, name)
                try:
                    if os.path.isfile(d) and os.path.getsize(d) == os.path.getsize(s):
                        continue
                    os.makedirs(out_dir, exist_ok=True)
                    shutil.copy2(s, d + ".tmp")
                    os.replace(d + ".tmp", d)
                except OSError:
                    continue
    except OSError:
        return False
    return os.path.isdir(dst) and any(os.scandir(dst))


# ---------------------------------------------------------------------------------------------
# Page sources (View Source) of a saved crawl: one zip of the source store's gzip files
# ---------------------------------------------------------------------------------------------
SOURCES_ZIP = "sources.zip"
_SOURCE_ENTRY = re.compile(r"^(?:rendered/)?[0-9a-f]{2}/[0-9a-f]{24}\.html\.gz$")
RENDERED_PREFIX = "rendered/"       # the rendered HTML (render every page / Render now) beside the raw HTML


def _digest(url: str) -> str:
    try:
        from .source_store import _digest as digest
        return digest(url)
    except Exception:
        import hashlib
        return hashlib.sha1(url.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _source_files(directory: str, prefix: str = "") -> Dict[str, str]:
    """{zip entry name: file path} of a source store folder (its "rendered" sub-store under RENDERED_PREFIX)."""
    out: Dict[str, str] = {}
    try:
        for sub in os.scandir(directory):
            if sub.is_dir() and sub.name == "rendered" and not prefix:
                out.update(_source_files(sub.path, RENDERED_PREFIX))
                continue
            if not sub.is_dir() or not re.match(r"^[0-9a-f]{2}$", sub.name):
                continue
            for f in os.scandir(sub.path):
                name = f"{prefix}{sub.name}/{f.name}"
                if f.is_file() and _SOURCE_ENTRY.match(name):
                    out[name] = f.path
    except OSError:
        pass
    return out


def _pack_sources(source_store, dest: str) -> bool:
    """Writes the crawl's page sources as one zip (entries already gzip: stored, not compressed again). Kept as
    it is when it already holds the same files. True when the saved crawl has sources."""
    if isinstance(source_store, ZipSourceStore):
        if os.path.normcase(os.path.abspath(source_store.path)) == os.path.normcase(os.path.abspath(dest)):
            return os.path.isfile(dest)
        if not os.path.isfile(source_store.path):
            return False
        with source_store.lock:
            _link_or_copy(source_store.path, dest + ".tmp")
        os.replace(dest + ".tmp", dest)
        return True
    files = _source_files(str(getattr(source_store, "dir", "") or ""))
    if not files:
        return False
    sizes = {n: os.path.getsize(p) for n, p in files.items() if os.path.isfile(p)}
    if os.path.isfile(dest):
        try:
            with zipfile.ZipFile(dest) as zf:
                if {i.filename: i.file_size for i in zf.infolist()} == sizes:
                    return True
        except (OSError, zipfile.BadZipFile):
            pass
    tmp = dest + f".{os.getpid()}.{threading.get_ident()}.tmp"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name in sorted(sizes):
            try:
                zf.write(files[name], name)
            except OSError:
                continue
    os.replace(tmp, dest)
    return True


class ZipSourceStore:
    """Read-only View Source store of a saved crawl (crawler/source_store.SourceStore's reading API). The zip is
    opened per read, so the file is never held open and a re-save can replace it."""

    def __init__(self, path: str, lock: Optional[threading.RLock] = None, prefix: str = ""):
        self.path = path
        self.dir = path
        self.lock = lock or threading.RLock()
        self.prefix = prefix          # RENDERED_PREFIX: the rendered HTML of the saved crawl
        self.errors = 0
        try:
            with zipfile.ZipFile(path) as zf:
                infos = [i for i in zf.infolist() if _SOURCE_ENTRY.match(i.filename)
                         and i.filename.startswith(prefix) and (prefix or not i.filename.startswith(RENDERED_PREFIX))]
            self.pages = len(infos)
            self.bytes_stored = sum(i.file_size for i in infos)
        except (OSError, zipfile.BadZipFile):
            self.pages, self.bytes_stored = 0, 0

    def rendered(self) -> Optional["ZipSourceStore"]:
        """The rendered-HTML store of this saved crawl (None when no page was rendered)."""
        if self.prefix:
            return None
        sub = ZipSourceStore(self.path, self.lock, prefix=RENDERED_PREFIX)
        return sub if sub.pages else None

    def _name(self, url: str) -> str:
        key = _digest(url)
        return f"{self.prefix}{key[:2]}/{key}.html.gz"

    def has(self, url: str) -> bool:
        if not url:
            return False
        try:
            with self.lock, zipfile.ZipFile(self.path) as zf:
                zf.getinfo(self._name(url))
            return True
        except (KeyError, OSError, zipfile.BadZipFile):
            return False

    def get(self, url: str) -> Optional[str]:
        if not url:
            return None
        try:
            with self.lock, zipfile.ZipFile(self.path) as zf:
                data = zf.read(self._name(url))
            return gzip.decompress(data).decode("utf-8", "replace")
        except (KeyError, OSError, EOFError, ValueError, zipfile.BadZipFile):
            return None

    def put(self, url: str, html: str) -> bool:
        return False            # a saved crawl's sources do not change

    def delete(self) -> None:
        return None             # never removes the saved crawl's file

    def summary(self) -> dict:
        return {"pages": self.pages, "bytes": self.bytes_stored, "errors": 0, "dir": self.path, "saved": True}


def _throttle_summary(crawler) -> Dict[str, Any]:
    throttle = getattr(crawler, "throttle", None)
    try:
        return dict(throttle.summary()) if throttle is not None else {}
    except Exception:
        return {}


def _write_crawl_file(path: str, header: dict, results: List[Any], externals: List[Any], tail: dict,
                      secrets: List[str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=GZIP_LEVEL, newline="\n") as fh:
        fh.write(_mask('{"header":' + _dumps(header) + ",", secrets) + "\n")
        fh.write('"results":[\n')
        for i, r in enumerate(results):
            fh.write(("," if i else "") + _mask(_dumps(_result_record(r)), secrets) + "\n")
        fh.write('],"externals":[\n')
        for i, e in enumerate(externals):
            fh.write(("," if i else "") + _mask(_dumps(_external_record(e)), secrets) + "\n")
        fh.write('],"tail":' + _mask(_dumps(tail), secrets) + "}\n")


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------
class _Reader:
    """Line reader over crawl.json.gz with a cap on the decompressed size."""

    def __init__(self, fh, limit: int):
        self.fh, self.limit, self.read = fh, limit, 0

    def line(self) -> str:
        text = self.fh.readline()
        self.read += len(text)
        if self.limit and self.read > self.limit:
            raise StoreError("The crawl file is larger than this server accepts.")
        return text


def _parse_crawl(fh, limit: int = 0) -> Dict[str, Any]:
    """{"header", "results": [records], "externals": [records], "tail"} from an open text stream."""
    rd = _Reader(fh, limit)
    first = rd.line().rstrip("\n")
    if not first.startswith('{"header":') or not first.endswith(","):
        raise StoreError("This is not a ARACHNIX crawl file.")
    try:
        header = json.loads(first[len('{"header":'):-1])
    except ValueError:
        raise StoreError("The crawl file is damaged (header).")
    _check_header(header)
    if rd.line().strip() != '"results":[':
        raise StoreError("The crawl file is damaged (results).")
    out: Dict[str, Any] = {"header": header, "results": [], "externals": [], "tail": {}}
    section = "results"
    while True:
        line = rd.line()
        if not line:
            raise StoreError("The crawl file is incomplete (it ends early).")
        line = line.rstrip("\n")
        if line == '],"externals":[':
            section = "externals"
            continue
        if line.startswith('],"tail":'):
            try:
                out["tail"] = json.loads(line[len('],"tail":'):-1])
            except ValueError:
                raise StoreError("The crawl file is damaged (tail).")
            break
        try:
            rec = json.loads(line[1:] if line.startswith(",") else line)
        except ValueError:
            raise StoreError(f"The crawl file is damaged ({section}).")
        if not isinstance(rec, dict) or not isinstance(rec.get("url"), str):
            raise StoreError(f"The crawl file has an invalid {section[:-1]} record.")
        out[section].append(rec)
    return out


def _check_header(header: Any) -> Dict[str, Any]:
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise StoreError("This is not a ARACHNIX crawl file.")
    try:
        version = int(header.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 1 or version > FORMAT_VERSION:
        raise StoreError(f"This crawl file was written by a newer version (format {version}); update the "
                         "app to open it.")
    meta = header.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("site"), str) or not meta.get("started_at"):
        raise StoreError("The crawl file has no valid description (meta).")
    if not isinstance(header.get("config"), dict):
        raise StoreError("The crawl file has no crawl settings (config).")
    return meta


def read_crawl(crawl_id: str, reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """The parsed crawl file of a saved crawl (records as plain data)."""
    folder = _require(crawl_id, reports_dir)
    path = os.path.join(folder, FILE_NAME)
    try:
        with _id_lock(crawl_id), gzip.open(path, "rt", encoding="utf-8") as fh:
            data = _parse_crawl(fh)
    except (OSError, EOFError, UnicodeDecodeError) as e:
        raise StoreError(f"The saved crawl could not be read: {e}") from e
    _check_header(data["header"])
    return data


@dataclass
class LoadedCrawl:
    """A saved crawl opened again. `crawler` is a WebsiteCrawler in its finished state; the other fields are the
    CrawlJob follow-up data (apply_to_job copies them onto a web/app.py CrawlJob)."""
    id: str
    meta: Dict[str, Any]
    crawler: Any
    base_name: str
    config: Dict[str, Any] = field(default_factory=dict)          # the saved (public) configuration
    validation: Optional[dict] = None
    dismissals: List[dict] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    evidence_dir: str = ""
    seo_evidence: Dict[Tuple[str, str], Any] = field(default_factory=dict)
    screenshot_note: str = ""
    snapshot_id: str = ""
    stats_at_save: Dict[str, Any] = field(default_factory=dict)
    index_counts: Dict[str, int] = field(default_factory=dict)    # "tab:filter" -> count when it was saved
    notes: List[str] = field(default_factory=list)

    def apply_to_job(self, job) -> None:
        """Copies the follow-up data onto a CrawlJob (attributes the job does not have are skipped)."""
        for attr in ("validation", "snapshot_id", "evidence", "evidence_dir", "seo_evidence", "screenshot_note"):
            if hasattr(job, attr) and (attr != "evidence_dir" or self.evidence_dir):
                setattr(job, attr, getattr(self, attr))
        if hasattr(job, "phase"):
            job.phase = "done"
        if self.validation and hasattr(job, "validation_version"):
            job.validation_version += 1

    def count_differences(self, index) -> List[Dict[str, Any]]:
        """Filters whose count differs from the one recorded at save time ([] = the crawl reproduces exactly).
        Filters that did not exist when it was saved (a newer app) are not differences."""
        if isinstance(index, tuple) and len(index) == 2:
            index = index[1]
        now = _index_counts(index)
        return [{"key": k, "saved": self.index_counts[k], "now": n} for k, n in now.items()
                if k in self.index_counts and self.index_counts[k] != n]


class _FrozenThrottle:
    """The fresh HostThrottle of a loaded crawl, whose summary() still reports the original crawl's hosts."""

    def __init__(self, live, saved: Dict[str, Any]):
        self._live, self._saved = live, dict(saved or {})

    def summary(self) -> Dict[str, Any]:
        out = dict(self._saved)
        try:
            out.update(self._live.summary())
        except Exception:
            pass
        return out

    def __getattr__(self, name):
        return getattr(self._live, name)


def _config_from(data: Dict[str, Any], reports_dir: str):
    """CrawlConfig from the saved public config, made safe to rebuild a crawler: no login, no extra header
    values, no proxy (the server's current proxy applies to re-checks), no new View Source folder."""
    import dataclasses
    from .config import AuthConfig, CrawlConfig
    names = {f.name for f in dataclasses.fields(CrawlConfig)}
    kwargs = {k: v for k, v in (data or {}).items() if k in names and k not in ("auth", "request_headers", "proxy")}
    try:
        config = CrawlConfig(**kwargs)
    except TypeError:
        config = CrawlConfig(start_url=str((data or {}).get("start_url") or ""))
    config.auth = AuthConfig()
    config.request_headers = []
    config.store_source = False
    config.save_snapshot = False
    config.reports_dir = reports_dir
    return config


def load_crawl(crawl_id: str, reports_dir: Optional[str] = None, activate_rules: bool = True,
               evidence_to: Optional[str] = None) -> LoadedCrawl:
    """Rebuilds a saved crawl. activate_rules: make its URL Rewriting rules the active ones (the crawl goes on
    screen; one crawl per server), else the rules in force are kept. evidence_to: copy the screenshots to this
    folder (e.g. the app's export folder) instead of reading them from the saved crawl. Raises StoreError."""
    from .engine import CrawlResult, ExternalLink, WebsiteCrawler
    from . import url_utils
    reports_dir = reports_dir or default_reports_dir()
    folder = _require(crawl_id, reports_dir)
    data = read_crawl(crawl_id, reports_dir)
    header, tail = data["header"], data["tail"] or {}
    meta = dict(_read_json(os.path.join(folder, META_NAME)) or header.get("meta") or {})
    meta["id"] = crawl_id
    config = _config_from(header.get("config") or {}, reports_dir)
    previous_rules = url_utils.active_rules()
    try:
        crawler = WebsiteCrawler(config)
    except Exception as e:
        raise StoreError(f"The saved crawl could not be opened: {type(e).__name__}: {e}") from e
    finally:
        if not activate_rules:
            url_utils.set_active_rules(previous_rules)

    for name, enc in (tail.get("state") or {}).items():
        if name in _STATE_SKIP or _SECRET_ATTR.search(name):
            continue
        try:
            setattr(crawler, name, _dec(enc))
        except Exception:
            continue
    results = [_build(CrawlResult, rec) for rec in data["results"]]
    externals = [_build(ExternalLink, rec) for rec in data["externals"]]
    with crawler._lock:
        crawler.results_list = results
        crawler.results = {r.url: r for r in results}
        crawler.external_links_list = externals
        crawler.external_links = {e.url: e for e in externals}
        if hasattr(crawler, "_tally") and isinstance(getattr(crawler, "_tallies", None), Counter):
            crawler._tallies = Counter()
            for r in results:
                crawler._tally(r, 1)
    crawler.status = str((tail.get("state") or {}).get("status") or meta.get("status") or "completed")
    if crawler.status not in ("completed", "stopped"):
        crawler.status = "completed"
    crawler.phase = "done"
    crawler.phase_detail = ""
    crawler.superseded = False
    for ev in ("_crawl_done",):
        event = getattr(crawler, ev, None)
        if event is not None and hasattr(event, "set"):
            event.set()
    crawler._index_cache = None
    crawler._index_key = None
    if getattr(crawler, "throttle", None) is not None:
        crawler.throttle = _FrozenThrottle(crawler.throttle, tail.get("throttle_summary") or {})
    sources = os.path.join(folder, SOURCES_ZIP)
    crawler.source_store = ZipSourceStore(sources, _id_lock(crawl_id)) if os.path.isfile(sources) else None
    crawler.saved_crawl = dict(meta)

    evidence_dir = os.path.join(folder, "evidence")
    if evidence_to and os.path.isdir(evidence_dir):
        if _copy_tree(evidence_dir, evidence_to):
            evidence_dir = evidence_to
    seo_evidence: Dict[Tuple[str, str], Any] = OrderedDict()
    for item in tail.get("seo_evidence") or []:
        if isinstance(item, list) and len(item) == 3:
            seo_evidence[(str(item[0]), str(item[1]))] = item[2]
    notes = []
    if meta.get("auth_used"):
        notes.append("Login details are never saved: re-checks of this crawl run without the login.")
    if not meta.get("sources"):
        notes.append("The page sources were not saved with this crawl, so View Source is empty.")
    started = _parse_iso(meta.get("started_at") or "") or time.time()
    domain = re.sub(r"[^A-Za-z0-9.-]+", "_", meta.get("host") or "site")
    return LoadedCrawl(
        id=crawl_id, meta=meta, crawler=crawler,
        base_name=f"crawl_{domain}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(started))}",
        config=header.get("config") or {}, validation=tail.get("validation"),
        dismissals=[d for d in tail.get("dismissals") or [] if isinstance(d, dict)],
        extras=tail.get("extras") or {}, evidence=tail.get("evidence") or {},
        evidence_dir=evidence_dir if os.path.isdir(evidence_dir) else "",
        seo_evidence=seo_evidence, screenshot_note=str(tail.get("screenshot_note") or ""),
        snapshot_id=str(meta.get("snapshot_id") or ""), stats_at_save=tail.get("stats") or {},
        index_counts={str(k): int(v) for k, v in (tail.get("index_counts") or {}).items()}, notes=notes)


# ---------------------------------------------------------------------------------------------
# Listing and managing
# ---------------------------------------------------------------------------------------------
def _projects(reports_dir: Optional[str]) -> Dict[str, dict]:
    data = _read_json(os.path.join(crawls_root(reports_dir), PROJECTS_NAME))
    return data if isinstance(data, dict) else {}


def _project_locked(meta: dict, projects: Dict[str, dict]) -> bool:
    p = meta.get("project") or ""
    return bool(p and (projects.get(p) or {}).get("locked"))


def _row(meta: dict, projects: Dict[str, dict]) -> dict:
    out = dict(meta)
    out["project_locked"] = _project_locked(meta, projects)
    out["protected"] = bool(meta.get("locked")) or out["project_locked"]
    out["saved"] = True
    out["partial"] = meta.get("complete") in ("partial", "stopped")
    return out


def list_crawls(reports_dir: Optional[str] = None, site: Optional[str] = None, project: Optional[str] = None,
                q: str = "") -> List[dict]:
    """Saved crawls (meta) newest first. site: one site (URL or host); project: one project; q: search in the
    name, site, project and id."""
    root = crawls_root(reports_dir)
    projects = _projects(reports_dir)
    hosts: List[str]
    if site:
        hosts = [host_key(site)]
    else:
        try:
            hosts = [h for h in os.listdir(root) if os.path.isdir(os.path.join(root, h)) and not h.startswith(".")]
        except OSError:
            hosts = []
    out = []
    needle = str(q or "").strip().lower()
    for h in hosts:
        try:
            names = os.listdir(os.path.join(root, h))
        except OSError:
            continue
        for ts in names:
            if not _TS_ONLY.match(ts):
                continue
            meta = _read_json(os.path.join(root, h, ts, META_NAME))
            if not isinstance(meta, dict):
                continue
            meta["id"] = f"{h}/{ts}"
            if project is not None and (meta.get("project") or "") != project:
                continue
            if needle and needle not in " ".join(str(meta.get(k) or "") for k in
                                                 ("name", "site", "project", "id")).lower():
                continue
            out.append(_row(meta, projects))
    out.sort(key=lambda m: (str(m.get("started_at") or ""), m["id"]), reverse=True)
    return out


def get_meta(crawl_id: str, reports_dir: Optional[str] = None) -> Optional[dict]:
    path = _path_of(crawl_id, reports_dir)
    meta = _read_json(os.path.join(path, META_NAME)) if path else None
    if not isinstance(meta, dict):
        return None
    meta["id"] = crawl_id
    return _row(meta, _projects(reports_dir))


def crawl_history(reports_dir: Optional[str] = None, site: Optional[str] = None) -> List[dict]:
    """GET /api/crawls: every snapshot (crawl history used by comparisons) and every saved crawl, one row per
    id, newest first. "saved" says whether the crawl can be opened; "snapshot" whether it can be compared."""
    from .seo.compare import list_snapshots
    rows: "OrderedDict[str, dict]" = OrderedDict()
    for s in list_snapshots(reports_dir, host=site or None):
        row = dict(s, snapshot=True, saved=False, name=s.get("name") or "", locked=False, project="")
        rows[s["id"]] = row
    for m in list_crawls(reports_dir, site=site):
        base = rows.get(m["id"]) or {"snapshot": False}
        merged = dict(base)
        merged.update(m)
        merged["snapshot"] = bool(base.get("snapshot"))
        rows[m["id"]] = merged
    out = list(rows.values())
    out.sort(key=lambda m: (str(m.get("started_at") or ""), str(m.get("rechecked_at") or ""), m["id"]),
             reverse=True)
    return out


def _update_meta(crawl_id: str, reports_dir: Optional[str], change: Callable[[dict], None]) -> dict:
    folder = _require(crawl_id, reports_dir)
    with _id_lock(crawl_id):
        path = os.path.join(folder, META_NAME)
        meta = _read_json(path)
        if not isinstance(meta, dict):
            raise StoreError(f"Saved crawl not found: {crawl_id}")
        change(meta)
        meta["id"] = crawl_id
        _write_json(path, meta)
    return _row(meta, _projects(reports_dir))


def rename(crawl_id: str, name: str, reports_dir: Optional[str] = None) -> dict:
    new = clean_name(name)
    if not new:
        raise StoreError("Enter a name.")
    return _update_meta(crawl_id, reports_dir, lambda m: m.update(name=new))


def set_project(crawl_id: str, project: str, reports_dir: Optional[str] = None) -> dict:
    return _update_meta(crawl_id, reports_dir, lambda m: m.update(project=clean_name(project, PROJECT_LIMIT)))


def lock(crawl_id: str, locked: bool = True, reports_dir: Optional[str] = None) -> dict:
    """A locked crawl is never deleted by the retention policy (and cannot be deleted until unlocked)."""
    return _update_meta(crawl_id, reports_dir, lambda m: m.update(locked=bool(locked)))


def lock_project(project: str, locked: bool = True, reports_dir: Optional[str] = None) -> Dict[str, dict]:
    """Locks a project: all its crawls, also future ones, are protected from retention and deletion."""
    project = clean_name(project, PROJECT_LIMIT)
    if not project:
        raise StoreError("Enter a project name.")
    with _lock:
        data = _projects(reports_dir)
        if locked:
            data[project] = dict(data.get(project) or {}, locked=True)
        else:
            data.pop(project, None)
        _write_json(os.path.join(crawls_root(reports_dir), PROJECTS_NAME), data)
    return data


def projects(reports_dir: Optional[str] = None) -> List[dict]:
    """[{"name", "crawls", "locked"}] for the Crawls dialog's project groups."""
    counts = Counter(m.get("project") or "" for m in list_crawls(reports_dir))
    locked = _projects(reports_dir)
    names = sorted(set(n for n in counts if n) | set(locked))
    return [{"name": n, "crawls": counts.get(n, 0), "locked": bool((locked.get(n) or {}).get("locked"))}
            for n in names]


def _free_id(host: str, ts: str, reports_dir: Optional[str]) -> str:
    base = ts.split("-")[0]
    for i in range(0, 1000):
        cand = f"{host}/{base}" + (f"-{i}" if i else "")
        path = _path_of(cand, reports_dir)
        if path and not os.path.exists(path):
            return cand
    raise StoreError("Too many copies of this crawl.")


def duplicate(crawl_id: str, reports_dir: Optional[str] = None, name: Optional[str] = None) -> dict:
    folder = _require(crawl_id, reports_dir)
    host, ts = crawl_id.split("/", 1)
    with _id_lock(crawl_id), _lock:
        new_id = _free_id(host, ts, reports_dir)
        target = _path_of(new_id, reports_dir)
        staging = target + f".{os.getpid()}.partial"
        try:
            shutil.copytree(folder, staging, copy_function=_link_or_copy)
            meta = _read_json(os.path.join(staging, META_NAME)) or {}
            meta.update(id=new_id, name=clean_name(name) if name else clean_name(f"{meta.get('name') or host} (copy)"),
                        locked=False, duplicated_from=crawl_id, created_at=_iso(time.time()))
            _write_json(os.path.join(staging, META_NAME), meta)
            os.replace(staging, target)
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise StoreError(f"The crawl could not be duplicated: {e.strerror or e}") from e
    _untombstone(new_id)
    _make_snapshot(new_id, reports_dir)          # so the duplicate can be compared like any crawl
    return _row(meta, _projects(reports_dir))


def delete(crawl_id: str, reports_dir: Optional[str] = None, with_snapshot: bool = True,
           force: bool = False) -> bool:
    """Deletes a saved crawl (and, with_snapshot, its comparison snapshot). A locked crawl (or one of a locked
    project) is refused unless force. Returns False when it did not exist."""
    path = _path_of(crawl_id, reports_dir)
    if not path or not os.path.isdir(path):
        return False
    meta = get_meta(crawl_id, reports_dir) or {}
    if meta.get("protected") and not force:
        raise StoreError("This crawl is locked. Unlock it first to delete it.")
    with _id_lock(crawl_id):
        trash = f"{path}.{os.getpid()}.{threading.get_ident()}.del"
        try:
            os.replace(path, trash)
        except OSError as e:
            raise StoreError(f"The crawl could not be deleted: {e.strerror or e}") from e
        shutil.rmtree(trash, ignore_errors=True)
    if with_snapshot:
        _delete_snapshot(crawl_id, reports_dir)
    _remove_empty_host(os.path.dirname(path))
    _tombstone(crawl_id)
    return True


def _delete_snapshot(crawl_id: str, reports_dir: Optional[str]) -> None:
    try:
        from .seo import compare
        for sid in (crawl_id, crawl_id + compare.RECHECK_SUFFIX):
            spath = compare._path_of(sid, reports_dir)
            if spath and os.path.isfile(spath):
                compare._remove(spath)
    except Exception:
        pass


def _tombstone(crawl_id: str) -> None:
    """Remember a just-deleted id so a follow-up re-save (autosave after a re-check / Crawl Analysis / bot
    check / connected-data fetch) does not quietly recreate the crawl the user removed."""
    with _lock:
        _tombstones[crawl_id] = time.time()
        _tombstones.move_to_end(crawl_id)
        while len(_tombstones) > _TOMBSTONE_MAX:
            _tombstones.popitem(last=False)


def _untombstone(crawl_id: str) -> None:
    """A fresh crawl now lives under this id again (a re-import, a duplicate): forget the tombstone."""
    with _lock:
        _tombstones.pop(crawl_id, None)


def _make_snapshot(crawl_id: str, reports_dir: Optional[str]) -> None:
    """Build and save the comparison snapshot of a saved crawl under its own id. store.duplicate and
    store.import_bytes copy the crawl but not its snapshot (snapshots live in a separate tree, and an import
    comes from another server that never shared one), so a loaded copy could not be compared. The saved crawl
    is loaded and turned into a snapshot the same way a live crawl is. Best-effort: never raises."""
    try:
        from .seo import compare
        loaded = load_crawl(crawl_id, reports_dir, activate_rules=False)
        crawler = loaded.crawler
        index = crawler.site_index() if hasattr(crawler, "site_index") else None
        site = None
        if isinstance(index, tuple) and len(index) == 2:
            site, index = index
        if index is not None:
            compare.save_snapshot(crawler, index, reports_dir, site=site, snapshot_id=crawl_id)
    except Exception:
        pass


def _remove_empty_host(folder: str) -> None:
    try:
        if os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------------------------
def _export_name(meta: dict) -> str:
    host = re.sub(r"[^A-Za-z0-9.-]+", "-", meta.get("host") or "site")
    ts = str(meta.get("id") or "").split("/")[-1] or _stamp(time.time())
    return f"site-auditor-crawl_{host}_{ts}.zip"


def export_to(crawl_id: str, fh, reports_dir: Optional[str] = None) -> str:
    """Writes the crawl as a zip (manifest.json, crawl.json.gz, sources.zip, evidence/) into a binary file
    object; returns the suggested file name."""
    folder = _require(crawl_id, reports_dir)
    meta = get_meta(crawl_id, reports_dir) or {}
    manifest = {"format": FORMAT, "version": FORMAT_VERSION, "kind": "crawl", "exported_at": _iso(time.time()),
                "meta": {k: v for k, v in meta.items() if k not in ("protected", "project_locked", "saved")}}
    with _id_lock(crawl_id), zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        zf.write(os.path.join(folder, FILE_NAME), FILE_NAME)
        if os.path.isfile(os.path.join(folder, SOURCES_ZIP)):
            zf.write(os.path.join(folder, SOURCES_ZIP), SOURCES_ZIP)
        base = os.path.join(folder, "evidence")
        for root, _dirs, files in os.walk(base):
            for name in files:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, folder).replace(os.sep, "/"))
    return _export_name(meta)


def export_bytes(crawl_id: str, reports_dir: Optional[str] = None) -> Tuple[str, bytes]:
    buf = io.BytesIO()
    name = export_to(crawl_id, buf, reports_dir)
    return name, buf.getvalue()


def _valid_sources_zip(body: bytes) -> bool:
    """An exported sources.zip holds only source-store entries (it is read by name, never extracted)."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    return bool(infos) and all(_SOURCE_ENTRY.match(i.filename) for i in infos) and         sum(i.file_size for i in infos) <= MAX_UNPACKED_BYTES


def import_bytes(data: bytes, filename: str = "", reports_dir: Optional[str] = None,
                 name: Optional[str] = None, project: Optional[str] = None) -> dict:
    """Adds an exported crawl (the .zip from export_bytes, or a bare crawl.json.gz) to the saved crawls and
    returns its meta. Checks the size, the format and version, and every record; an id that exists already
    gets a new one. Raises StoreError with a plain reason."""
    if not data:
        raise StoreError("The file is empty.")
    if len(data) > MAX_IMPORT_BYTES:
        raise StoreError(f"The file is larger than this server accepts ({MAX_IMPORT_BYTES / 1048576:.1f} MB; "
                         "env CRAWLER_MAX_IMPORT_MB).")
    reports_dir = reports_dir or default_reports_dir()
    crawl_gz: Optional[bytes] = None
    members: List[Tuple[str, bytes]] = []
    if data[:2] == b"\x1f\x8b":
        crawl_gz = data
    elif data[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise StoreError("The file is not a valid zip archive.")
        total = 0
        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                total += info.file_size
                if total > MAX_UNPACKED_BYTES:
                    raise StoreError("The archive unpacks to more than this server accepts.")
                nm = info.filename
                if nm == FILE_NAME:
                    crawl_gz = zf.read(info)
                elif nm == "manifest.json":
                    continue
                elif nm == SOURCES_ZIP:
                    body = zf.read(info)
                    if _valid_sources_zip(body):
                        members.append((nm, body))
                elif _EVIDENCE_NAME.match(nm) and ".." not in nm:
                    members.append((nm, zf.read(info)))
        if crawl_gz is None:
            raise StoreError("The archive has no crawl.json.gz: it is not a ARACHNIX crawl export.")
    else:
        raise StoreError("Unsupported file: upload a crawl exported from ARACHNIX (.zip or .json.gz).")

    try:
        with gzip.open(io.BytesIO(crawl_gz), "rt", encoding="utf-8") as fh:
            parsed = _parse_crawl(fh, limit=MAX_UNPACKED_BYTES)
    except (OSError, EOFError, UnicodeDecodeError) as e:
        raise StoreError(f"The crawl file is damaged: {e}") from e
    meta = dict(_check_header(parsed["header"]))
    counts = (parsed["tail"] or {}).get("counts") or {}
    if counts and (int(counts.get("results", -1)) != len(parsed["results"])
                   or int(counts.get("externals", -1)) != len(parsed["externals"])):
        raise StoreError("The crawl file is incomplete (record counts do not match).")
    if not parsed["results"]:
        raise StoreError("The crawl file has no URLs.")

    host = host_key(meta.get("site") or "")
    ts = _stamp(_parse_iso(meta.get("started_at") or "") or time.time())
    with _lock:
        new_id = _free_id(host, ts, reports_dir)
        target = _path_of(new_id, reports_dir)
        staging = target + f".{os.getpid()}.partial"
        try:
            os.makedirs(staging, exist_ok=True)
            with open(os.path.join(staging, FILE_NAME), "wb") as fh:
                fh.write(crawl_gz)
            for nm, body in members:
                dest = os.path.normpath(os.path.join(staging, *nm.split("/")))
                if not dest.startswith(os.path.normpath(staging) + os.sep):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(body)
            meta.update(id=new_id, host=host, locked=False, imported_at=_iso(time.time()),
                        imported_from=clean_name(os.path.basename(filename or "upload"), 200),
                        sources=any(n == SOURCES_ZIP for n, _ in members),
                        evidence=any(n.startswith("evidence/") for n, _ in members),
                        created_at=_iso(time.time()))
            if name:
                meta["name"] = clean_name(name)
            if project is not None:
                meta["project"] = clean_name(project, PROJECT_LIMIT)
            meta.setdefault("name", meta.get("site") or host)
            meta["size_bytes"] = _folder_size(staging) + 2048
            _write_json(os.path.join(staging, META_NAME), meta)
            os.replace(staging, target)
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise StoreError(f"The crawl could not be imported: {e.strerror or e}") from e
    _untombstone(new_id)
    _make_snapshot(new_id, reports_dir)          # a crawl from another server arrives without a snapshot
    return _row(meta, _projects(reports_dir))


# ---------------------------------------------------------------------------------------------
# Retention and storage
# ---------------------------------------------------------------------------------------------
def retention_days(settings: Any) -> Optional[int]:
    """Days after which crawls are deleted (None = never) from a number, a retention settings dict
    {"delete_after": "never"|"1"|"7"|"30"|"90"|"custom", "custom_days": n} or the whole settings dict."""
    if settings is None or isinstance(settings, bool):
        return None
    if isinstance(settings, (int, float)):
        return int(settings) if settings > 0 else None
    if isinstance(settings, dict):
        page = settings.get("retention") if isinstance(settings.get("retention"), dict) else settings
        choice = str(page.get("delete_after") or "never").strip().lower()
        if choice in ("never", "", "0"):
            return None
        if choice == "custom":
            try:
                days = int(page.get("custom_days") or 0)
            except (TypeError, ValueError):
                return None
            return min(days, CUSTOM_DAYS_MAX) if days > 0 else None
        try:
            days = int(choice.rstrip("d"))
        except ValueError:
            return None
        return days if days > 0 else None
    return None


def apply_retention(settings: Any = None, reports_dir: Optional[str] = None,
                    now: Optional[float] = None, max_per_site: Optional[int] = None,
                    keep_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Deletes saved crawls older than the retention period, then any beyond the per-site safety cap (newest
    kept), and prunes scheduled-run export folders the same way. A crawl's age is the latest of when it
    finished, when its saved entry was first created on this server and when it was imported, so a crawl
    imported from a colleague or duplicated today is not dropped just because the original ran long ago.
    Locked crawls, crawls of locked projects, and the ids in keep_ids (the crawl on screen, passed by the
    caller) are never deleted. Returns {"deleted", "kept_locked", "days", "cap", "scheduled_deleted"}."""
    days = retention_days(settings)
    now = float(now if now is not None else time.time())
    keep = {str(i) for i in (keep_ids or ()) if i}
    _clean_leftovers(reports_dir)
    cap = MAX_CRAWLS_PER_SITE if max_per_site is None else int(max_per_site)
    deleted: List[str] = []
    kept_locked = set()
    rows = list_crawls(reports_dir)
    by_site: Dict[str, List[dict]] = defaultdict(list)
    for m in rows:
        when = max(_parse_iso(m.get("finished_at") or "") or _parse_iso(m.get("started_at") or ""),
                   _parse_iso(m.get("created_at") or ""), _parse_iso(m.get("imported_at") or ""))
        expired = days is not None and when and now - when > days * 86400
        if expired and (m.get("protected") or m["id"] in keep):
            kept_locked.add(m["id"])
            expired = False
        if expired:
            try:
                if delete(m["id"], reports_dir):
                    deleted.append(m["id"])
            except StoreError:
                pass
            continue
        by_site[m.get("host") or ""].append(m)
    if cap > 0:
        for host, items in by_site.items():
            unlocked = [m for m in items if not (m.get("protected") or m["id"] in keep)]  # newest first
            room = max(0, cap - (len(items) - len(unlocked)))
            for m in unlocked[room:]:
                try:
                    if delete(m["id"], reports_dir):
                        deleted.append(m["id"])
                except StoreError:
                    pass
    scheduled_deleted = _prune_scheduled(reports_dir, days, now)
    return {"deleted": deleted, "kept_locked": len(kept_locked), "days": days, "cap": cap,
            "scheduled_deleted": scheduled_deleted}


def _run_time(path: str, name: str) -> float:
    """When a scheduled run happened: parsed from its folder name (local "%Y%m%d_%H%M%S"), else its mtime."""
    try:
        return time.mktime(time.strptime(name[:15], "%Y%m%d_%H%M%S"))
    except (ValueError, OverflowError):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0


def _prune_scheduled(reports_dir: Optional[str], days: Optional[int], now: float) -> int:
    """Deletes scheduled-run export folders (<reports_dir>/scheduled/<task>/<time>/, written by web/app.py's
    _schedule_complete) older than `days` (when set) and, per task, all but the newest MAX_SCHEDULED_RUNS.
    Nothing else cleaned these, so an hourly Excel+PDF task grew the disk without bound. Best-effort."""
    root = os.path.join(reports_dir or default_reports_dir(), "scheduled")
    removed = 0
    try:
        tasks = [os.path.join(root, t) for t in os.listdir(root) if os.path.isdir(os.path.join(root, t))]
    except OSError:
        return 0
    for task_dir in tasks:
        try:
            runs = sorted(r for r in os.listdir(task_dir) if os.path.isdir(os.path.join(task_dir, r)))
        except OSError:
            continue
        drop: List[str] = []
        if MAX_SCHEDULED_RUNS > 0 and len(runs) > MAX_SCHEDULED_RUNS:
            drop = runs[:-MAX_SCHEDULED_RUNS]                 # oldest by name (time-stamped folder names)
        if days is not None:
            for r in runs[-MAX_SCHEDULED_RUNS:] if MAX_SCHEDULED_RUNS > 0 else runs:
                if now - _run_time(os.path.join(task_dir, r), r) > days * 86400:
                    drop.append(r)
        for r in drop:
            shutil.rmtree(os.path.join(task_dir, r), ignore_errors=True)
            removed += 1
        _remove_empty_host(task_dir)
    _remove_empty_host(root)
    return removed


def _clean_leftovers(reports_dir: Optional[str], max_age: float = 3600) -> int:
    """Removes half-written or half-deleted crawl folders a crash left behind (older than max_age seconds)."""
    root = crawls_root(reports_dir)
    removed = 0
    try:
        hosts = [os.path.join(root, h) for h in os.listdir(root) if os.path.isdir(os.path.join(root, h))]
    except OSError:
        return 0
    for host_dir in hosts:
        try:
            names = os.listdir(host_dir)
        except OSError:
            continue
        for name in names:
            path = os.path.join(host_dir, name)
            if not name.endswith((".partial", ".del", ".old")) or not os.path.isdir(path):
                continue
            try:
                if time.time() - os.path.getmtime(path) > max_age:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed


def storage_info(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """Settings > Storage Mode (read-only): where crawls live, how much they use and how much disk is free."""
    base = reports_dir or default_reports_dir()
    env_set = bool(os.environ.get("CRAWLER_REPORTS_DIR"))
    rows = list_crawls(base)
    used = sum(int(m.get("size_bytes") or 0) for m in rows)
    out = {"dir": base, "crawls_dir": crawls_root(base), "env_set": env_set, "crawls": len(rows),
           "locked": sum(1 for m in rows if m.get("protected")), "bytes_used": used,
           "sites": len({m.get("host") for m in rows}), "disk_total": None, "disk_free": None, "disk_used": None,
           "max_per_site": MAX_CRAWLS_PER_SITE, "max_import_bytes": MAX_IMPORT_BYTES, "warning": ""}
    try:
        du = shutil.disk_usage(base)
        out.update(disk_total=du.total, disk_free=du.free, disk_used=du.used)
    except OSError:
        pass
    if not env_set:
        out["warning"] = ("Crawls are kept in the system temp folder. Set CRAWLER_REPORTS_DIR to a lasting folder; "
                          "on hosts such as Render or Vercel the temp folder is wiped on every redeploy.")
    return out
