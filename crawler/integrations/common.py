"""
Shared plumbing for the optional Google integrations (Search Console, GA4, PageSpeed Insights):
endpoints (every base URL can be overridden by an environment variable, so tests run against local fake
servers), date ranges, HTTP calls with retry / back-off / quota handling, Screaming Frog-style URL matching,
and background jobs with progress and stop.

Nothing here logs or returns a token or an API key: request URLs that carry a key are never put in an error
message (mask_key), and errors carry Google's own message only.
"""
import os
import random
import re
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests

# ---------------------------------------------------------------------------------------------
# Endpoints (env overrides for tests)
# ---------------------------------------------------------------------------------------------
ENDPOINTS: Dict[str, Tuple[str, str]] = {
    "oauth_auth": ("GOOGLE_OAUTH_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth"),
    "oauth_token": ("GOOGLE_OAUTH_TOKEN_URL", "https://oauth2.googleapis.com/token"),
    "oauth_revoke": ("GOOGLE_OAUTH_REVOKE_URL", "https://oauth2.googleapis.com/revoke"),
    "userinfo": ("GOOGLE_USERINFO_URL", "https://openidconnect.googleapis.com/v1/userinfo"),
    "gsc": ("GSC_API_BASE", "https://www.googleapis.com/webmasters/v3"),
    "gsc_inspect": ("GSC_INSPECT_API_BASE", "https://searchconsole.googleapis.com/v1"),
    "ga4_admin": ("GA4_ADMIN_API_BASE", "https://analyticsadmin.googleapis.com/v1beta"),
    "ga4_data": ("GA4_DATA_API_BASE", "https://analyticsdata.googleapis.com/v1beta"),
    "psi": ("PSI_API_BASE", "https://www.googleapis.com/pagespeedonline/v5"),
    "sheets": ("GOOGLE_SHEETS_API_BASE", "https://sheets.googleapis.com/v4"),
    "drive": ("GOOGLE_DRIVE_API_BASE", "https://www.googleapis.com/drive/v3"),
}


def endpoint_overridden(name: str) -> bool:
    """The base URL of `name` comes from its environment variable (a test fake or a relay set by the admin)."""
    return bool(os.environ.get(ENDPOINTS[name][0]))

USER_AGENT = "SiteAuditor-Integrations/1.0"
TIMEOUT = 60                      # seconds per API call (PageSpeed runs Lighthouse: up to ~60 s)
MAX_RETRIES = 5                   # retries of 429 / 5xx / network errors (exponential back-off)
MAX_BACKOFF = 64.0                # seconds


def endpoint(name: str) -> str:
    env, default = ENDPOINTS[name]
    return (os.environ.get(env) or default).rstrip("/")


def backoff_base() -> float:
    """First back-off delay in seconds (env INTEGRATIONS_BACKOFF_BASE; tests use a few milliseconds)."""
    try:
        return max(0.0, float(os.environ.get("INTEGRATIONS_BACKOFF_BASE") or 1.0))
    except ValueError:
        return 1.0


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_KEY_IN_URL = re.compile(r"([?&](?:key|access_token|token)=)[^&#]*", re.I)


def mask_key(text: str) -> str:
    """Hide ?key=... / access_token=... values in a URL or message."""
    return _KEY_IN_URL.sub(r"\1***", str(text or ""))


# ---------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------
class IntegrationError(Exception):
    """An API call that failed; the message is plain language and safe to show."""

    def __init__(self, message: str, status: int = 0, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


class NotConnected(IntegrationError):
    pass


class AuthExpired(IntegrationError):
    """The stored Google sign-in no longer works (revoked or expired): connect again."""


class QuotaExceeded(IntegrationError):
    """A daily quota is used up: stop cleanly, the rest can run tomorrow."""


class Stopped(IntegrationError):
    pass


def google_error(resp: Optional[requests.Response]) -> Tuple[int, str, str]:
    """(status, message, reason) from a Google error answer ({"error": {...}} or the OAuth form
    {"error": "invalid_grant", "error_description": ...})."""
    if resp is None:
        return 0, "No answer", ""
    status = resp.status_code
    message, reason = "", ""
    try:
        data = resp.json()
    except Exception:
        data = None
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = str(err.get("message") or "")
        reason = str(err.get("status") or "")
        for e in err.get("errors") or []:
            if isinstance(e, dict) and e.get("reason"):
                reason = reason or str(e["reason"])
        for d in err.get("details") or []:
            if isinstance(d, dict):
                meta = d.get("metadata") or {}
                if isinstance(meta, dict) and meta.get("quota_limit"):
                    reason = f"{reason}|{meta.get('quota_limit')}|{meta.get('quota_limit_value', '')}"
    elif isinstance(err, str):
        reason = err
        message = str((data or {}).get("error_description") or err)
    if not message:
        message = (resp.text or resp.reason or "").strip()[:300]
    return status, mask_key(message)[:500], reason


def is_daily_quota(status: int, message: str, reason: str) -> bool:
    """A 429 that will not clear by waiting a minute (a per-day limit, or a limit of 0 such as Google's
    shared keyless PageSpeed quota)."""
    text = f"{message} {reason}".lower()
    if status != 429 and "resource_exhausted" not in text:
        return False
    return ("per day" in text or "perday" in text or "/d/" in text or "daily" in text
            or text.rstrip().endswith("|0") or "|0 " in text)


# ---------------------------------------------------------------------------------------------
# HTTP with back-off
# ---------------------------------------------------------------------------------------------
def new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _sleep(seconds: float, stop: Optional[threading.Event]) -> None:
    if seconds <= 0:
        return
    if stop is not None:
        if stop.wait(seconds):
            raise Stopped("Stopped.")
    else:
        time.sleep(seconds)


def _retry_after(resp: Optional[requests.Response]) -> Optional[float]:
    if resp is None:
        return None
    value = (resp.headers.get("Retry-After") or "").strip()
    if value.isdigit():
        return min(MAX_BACKOFF * 2, float(value))
    return None


def call(session: requests.Session, method: str, url: str, *, stop: Optional[threading.Event] = None,
         retries: int = MAX_RETRIES, timeout: float = TIMEOUT, on_wait: Optional[Callable[[str], None]] = None,
         **kwargs) -> requests.Response:
    """One API call with retries: 429 (per-minute limits), 500/502/503/504 and network errors wait and try
    again (Retry-After honoured, else exponential back-off with jitter). A per-day quota raises QuotaExceeded
    at once; other 4xx answers are returned to the caller. Stop (the event) raises Stopped."""
    attempt = 0
    while True:
        if stop is not None and stop.is_set():
            raise Stopped("Stopped.")
        resp: Optional[requests.Response] = None
        error: Optional[Exception] = None
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as e:
            error = e
        if resp is not None and resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        if resp is not None and resp.status_code == 429:
            status, message, reason = google_error(resp)
            if is_daily_quota(status, message, reason):
                raise QuotaExceeded(message or "Daily quota reached.", status, reason)
        if attempt >= retries:
            if resp is not None:
                return resp
            raise IntegrationError(f"Could not reach Google: {type(error).__name__}", 0, "network")
        delay = _retry_after(resp)
        if delay is None:
            delay = min(MAX_BACKOFF, backoff_base() * (2 ** attempt)) * (0.75 + random.random() * 0.5)
        if on_wait is not None:
            what = f"HTTP {resp.status_code}" if resp is not None else "network error"
            on_wait(f"Google answered {what}; waiting {delay:.0f} s before trying again")
        _sleep(delay, stop)
        attempt += 1


class RateLimiter:
    """At most `per_minute` calls per rolling minute, shared by threads (Google's per-minute limits:
    PageSpeed 240, URL Inspection 600)."""

    def __init__(self, per_minute: int):
        self.per_minute = max(1, int(per_minute or 1))
        self._times: List[float] = []
        self._lock = threading.Lock()

    def wait(self, stop: Optional[threading.Event] = None) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < 60.0]
                if len(self._times) < self.per_minute:
                    self._times.append(now)
                    return
                delay = 60.0 - (now - self._times[0]) + 0.01
            _sleep(min(delay, 5.0), stop)


# ---------------------------------------------------------------------------------------------
# Date ranges (Screaming Frog "Past 30 days" = yesterday back 30 days, inclusive)
# ---------------------------------------------------------------------------------------------
RANGE_DAYS = {"7d": 7, "28d": 28, "30d": 30, "90d": 90, "180d": 180, "365d": 365, "16m": 486}
DATE_RANGES = tuple(RANGE_DAYS) + ("custom",)


def date_range(kind: str = "30d", start: str = "", end: str = "", today: Optional[date] = None) -> Tuple[str, str]:
    """(start, end) as YYYY-MM-DD. Presets end yesterday; "custom" uses start/end as given (validated)."""
    today = today or date.today()
    if kind == "custom":
        try:
            s = date.fromisoformat(str(start))
            e = date.fromisoformat(str(end))
        except ValueError:
            raise IntegrationError("Enter the start and end dates as YYYY-MM-DD.")
        if s > e:
            raise IntegrationError("The end date is before the start date.")
        return s.isoformat(), e.isoformat()
    days = RANGE_DAYS.get(kind or "30d", 30)
    e = today - timedelta(days=1)
    s = e - timedelta(days=days - 1)
    return s.isoformat(), e.isoformat()


# ---------------------------------------------------------------------------------------------
# URL matching (Screaming Frog "General": trailing slash, upper/lower case; protocol; www as an option)
# ---------------------------------------------------------------------------------------------
@dataclass
class MatchOptions:
    trailing_slash: bool = True       # /a and /a/ are the same page
    case: bool = False                # /A and /a are the same page (off by default, as in Screaming Frog)
    protocol: bool = True             # http:// and https:// are the same page
    www: bool = False                 # www.example.com and example.com are the same host

    @classmethod
    def from_options(cls, o: Dict[str, Any]) -> "MatchOptions":
        return cls(trailing_slash=bool(o.get("match_trailing_slash", True)), case=bool(o.get("match_case", False)),
                   protocol=bool(o.get("match_protocol", True)), www=bool(o.get("match_www", False)))


def normalize(url: str) -> str:
    """The crawler's own normalisation (fragments, tracking parameters, parameter order), so API URLs
    compare with crawled ones; the URL as given when it cannot be normalised."""
    try:
        from ..url_utils import normalize_url
        return normalize_url(url) or url
    except Exception:
        return url


def match_key(url: str, opts: MatchOptions) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = "" if opts.protocol else parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if opts.www and host.startswith("www."):
        host = host[4:]
    port = parts.port
    if port and not ((parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    if opts.trailing_slash and len(path) > 1:
        path = path.rstrip("/") or "/"
    query = parts.query
    if opts.case:
        path, query = path.lower(), query.lower()
    return urlunsplit((scheme, host, path, query, ""))


@dataclass
class JoinResult:
    matched: Dict[str, Tuple[str, Any]] = field(default_factory=dict)   # crawl url -> (api url, payload)
    unmatched: List[Tuple[str, Any]] = field(default_factory=list)       # (api url, payload) found by no crawl URL


def join(rows: Iterable[Tuple[str, float, Any]], crawl_urls: Iterable[str], opts: MatchOptions) -> JoinResult:
    """Attach API rows (api_url, rank, payload) to crawled URLs. An exact URL match wins; otherwise the
    match key (MatchOptions) decides. When several API rows match one crawled URL the one with the highest
    rank (clicks, sessions) is kept: values are never summed, as in Screaming Frog."""
    crawl = list(crawl_urls)
    exact = set(crawl)
    by_key: Dict[str, str] = {}
    for u in crawl:
        by_key.setdefault(match_key(u, opts), u)
    out = JoinResult()
    best: Dict[str, Tuple[bool, float]] = {}
    for api_url, rank, payload in rows:
        norm = normalize(api_url)
        target = norm if norm in exact else by_key.get(match_key(norm, opts))
        if target is None:
            out.unmatched.append((norm, payload))
            continue
        score = (norm == target, float(rank or 0))
        if target not in best or score > best[target]:
            best[target] = score
            out.matched[target] = (norm, payload)
    return out


def origin_of(url: str) -> str:
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
    except ValueError:
        return ""


# ---------------------------------------------------------------------------------------------
# Jobs (one per source at a time; progress + stop)
# ---------------------------------------------------------------------------------------------
@dataclass
class Job:
    source: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"          # running | done | partial | stopped | error
    phase: str = "starting"
    done: int = 0
    total: int = 0
    message: str = ""
    errors: List[str] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)
    finished_at: str = ""
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _owner: Any = field(default=None, repr=False)   # the crawl this fetch serves (so a replaced crawl's job stops)

    def set_owner(self, owner: Any) -> None:
        """Remember which crawl this fetch belongs to, without keeping that crawl alive on its own."""
        if owner is None:
            self._owner = None
            return
        try:
            self._owner = weakref.ref(owner)
        except TypeError:          # not weak-referenceable: keep a plain reference (the running thread holds it anyway)
            self._owner = lambda o=owner: o

    @property
    def owner(self) -> Any:
        return self._owner() if self._owner is not None else None

    def owned_by(self, crawler: Any) -> bool:
        """True when this fetch serves `crawler` (same object). A collected or unset owner is never "owned"."""
        return crawler is not None and self.owner is crawler

    def progress(self, phase: str = "", done: Optional[int] = None, total: Optional[int] = None,
                 message: Optional[str] = None) -> None:
        if phase:
            self.phase = phase
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total
        if message is not None:
            self.message = mask_key(message)

    def error(self, message: str) -> None:
        if len(self.errors) < 50:
            self.errors.append(mask_key(message)[:300])

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def public(self) -> Dict[str, Any]:
        pct = round(self.done / self.total * 100, 1) if self.total else (100.0 if self.status != "running" else 0.0)
        return {"id": self.id, "source": self.source, "status": self.status, "phase": self.phase, "done": self.done,
                "total": self.total, "percent": pct, "message": self.message, "errors": list(self.errors[-10:]),
                "error_count": len(self.errors), "started_at": self.started_at, "finished_at": self.finished_at,
                "running": self.status == "running"}


class JobManager:
    """Runs at most one job per source in a background thread."""

    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, source: str, work: Callable[[Job], None], owner: Any = None) -> Job:
        with self._lock:
            current = self._jobs.get(source)
            if current is not None and current.status == "running":
                if owner is not None and not current.owned_by(owner):
                    # The running fetch belongs to a crawl that was replaced (reset / new crawl / opened a
                    # saved crawl): stop it and take over, rather than blocking this crawl's fetch.
                    current.stop_event.set()
                else:
                    raise IntegrationError(f"A {source.upper()} fetch is already running; stop it first or wait.")
            job = Job(source=source)
            job.set_owner(owner)
            self._jobs[source] = job

        def run():
            try:
                work(job)
                if job.status == "running":
                    job.status = "stopped" if job.stopped else ("partial" if job.errors else "done")
            except Stopped:
                job.status = "stopped"
                job.message = "Stopped. The data fetched so far is kept."
            except IntegrationError as e:
                job.status = "error"
                job.error(str(e))
                job.message = str(e)
            except Exception as e:          # never let a job thread die silently
                job.status = "error"
                job.error(f"{type(e).__name__}: {e}")
                job.message = "The fetch failed unexpectedly; see the errors."
            finally:
                job.finished_at = now_iso()

        threading.Thread(target=run, name=f"integration-{source}", daemon=True).start()
        return job

    def stop(self, source: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(source)
        if job is not None and job.status == "running":
            job.stop_event.set()
        return job

    def get(self, source: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(source)

    def running(self, source: str) -> bool:
        job = self.get(source)
        return job is not None and job.status == "running"

    def running_for(self, source: str, owner: Any) -> bool:
        """True only when a fetch of this source is running for `owner` (not for a replaced crawl)."""
        job = self.get(source)
        return job is not None and job.status == "running" and job.owned_by(owner)

    def stop_all(self, except_owner: Any = None) -> List[str]:
        """Stop every running fetch (keep those serving `except_owner`). The data fetched so far is kept on
        the crawl it belongs to. Returns the sources stopped."""
        with self._lock:
            jobs = list(self._jobs.items())
        stopped = []
        for source, job in jobs:
            if job.status != "running":
                continue
            if except_owner is not None and job.owned_by(except_owner):
                continue
            job.stop_event.set()
            stopped.append(source)
        return stopped

    def stop_for(self, owner: Any) -> List[str]:
        """Stop every running fetch tied to `owner` (that crawl was replaced or closed). Returns the sources."""
        if owner is None:
            return []
        with self._lock:
            jobs = list(self._jobs.items())
        stopped = []
        for source, job in jobs:
            if job.status == "running" and job.owned_by(owner):
                job.stop_event.set()
                stopped.append(source)
        return stopped

    def all(self, owner: Any = None) -> Dict[str, Dict[str, Any]]:
        """Public view of every source's job. With `owner`, a job that belongs to a different (replaced) crawl
        is hidden, so the crawl on screen never sees another crawl's fetch (running or already stopped). Jobs
        with no owner are always shown."""
        with self._lock:
            jobs = list(self._jobs.items())
        out = {}
        for s, j in jobs:
            if owner is not None and j.owner is not None and not j.owned_by(owner):
                continue
            out[s] = j.public()
        return out

    def wait(self, source: str, timeout: float = 60.0) -> Optional[Job]:
        """Block until the source's job finished (tests, CLI)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = self.get(source)
            if job is None or job.status != "running":
                return job
            time.sleep(0.05)
        return self.get(source)


JOBS = JobManager()
