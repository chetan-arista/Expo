"""
Scheduling (File > Scheduling): crawls that start on their own at a set time, once or repeating, then produce
their exports, email the team and compare with the previous crawl.

Tasks live in <reports_dir>/schedules.json and each run in <reports_dir>/schedules_history.json (newest
HISTORY_MAX kept). A task:
  id, name, project, enabled
  first_run      ISO 8601 date and time ("2026-10-27T09:00:00+05:30"; without an offset = the server's local
                 time, daylight saving included)
  interval       once | hourly | daily | weekly | monthly, "every" N of them (every 2 weeks...)
  start          {"mode": "spider" | "list", "url", "urls": [...], "profile": name, "options": {crawl options},
                  "login": {"type": "none" | "cookies" | "header", "env": "ENV_VAR", "header_name"}}
                 A login is never stored: "env" names a server environment variable holding the cookie string or
                 header value, read only when the crawl starts.
  exports        {"items": [report / bulk export ids], "format": "xlsx" | "csv", "skip_empty", "excel", "pdf",
                  "save_crawl", "sitemap"}
  notify         {"emails": [...], "attach": bool, "on_failure": bool}
  auto_compare   compare with the previous crawl of the site (in the email and the history)
  wait_minutes   how long a due task waits while another crawl runs before that run is skipped (0-1440)
  next_run, last_run, last_status   kept by the scheduler
"valid" / "problems" say whether the task can run now (profile exists, login variable set...).

The scheduler only decides WHEN; web/app.py supplies how:
  Scheduler(start_fn, on_complete=None, reports_dir=None, busy_fn=None, clock=time.time, tick_seconds=30)
  start_fn(task, request) -> run_ref (str)   starts the crawl (request = CrawlRequest fields built by
                                             build_request); raise SchedulerBusy when a crawl is running
  busy_fn() -> bool                          a crawl is running / paused / analysing (one crawl per server)
  busy_detail() -> str                       optional: why the server is busy (Run now says it)
  scheduler.crawl_finished(run_ref, status, error="", info=None)   the app calls this when that crawl ends
  on_complete(task, run) -> dict             then runs in a thread (exports, email, comparison); its result
                                             ({"exports", "emailed", "compare", "notes"}) is kept in the run
  scheduler.start() / stop()                 the tick thread (every tick_seconds); tick() runs one pass
  list() / get / add / edit / delete / duplicate / set_enabled / run_now / history / export_json / import_json
One task starts per tick at most; while a crawl runs, due tasks wait (and are skipped after wait_minutes,
recorded in the history). Runs missed while the server was off are not repeated: the task runs once and its
next run is the next future slot (the history says how many were missed).
"""
import calendar
import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

TASKS_FILE = "schedules.json"
HISTORY_FILE = "schedules_history.json"
VERSION = 1
INTERVALS = ("once", "hourly", "daily", "weekly", "monthly")
HISTORY_MAX = 1000
MAX_TASKS = 200
MAX_LIST_URLS = 10000
NAME_LIMIT = 80
TICK_SECONDS = 30
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,100}$")
# A scheduled login reads its secret from a server variable named for that purpose only (SITE_SESSION_COOKIE,
# SITE_TOKEN...): the server's own keys and passwords are never sent to a website (web/app.py checks it too)
LOGIN_ENV_PREFIX = "SITE_"
LOGIN_ENV_RULE = (f"For safety a scheduled login can only use a server variable whose name starts with "
                  f"{LOGIN_ENV_PREFIX} (for example SITE_SESSION_COOKIE). Other server variables, such as "
                  "API keys and passwords, are never sent to a website.")
URL_PASSWORD_RULE = ("Leave the user name and password out of the address: they would be stored with the task. "
                     "For a password-protected site choose the login Header, name Authorization, with a SITE_ "
                     "variable that holds the value.")
_URL_PASSWORD = re.compile(r"^https?://[^/?#]*@", re.I)


def login_env_allowed(name: Any) -> bool:
    """Whether a scheduled login may read this server variable: SITE_<something> only."""
    name = str(name or "").strip().upper()
    return name.startswith(LOGIN_ENV_PREFIX) and len(name) > len(LOGIN_ENV_PREFIX)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}$")
_EXPORT_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")

DEFAULT_TASK: Dict[str, Any] = {
    "name": "", "project": "", "enabled": True, "first_run": "", "interval": "once", "every": 1,
    "start": {"mode": "spider", "url": "", "urls": [], "profile": "", "options": {},
              "login": {"type": "none", "env": "", "header_name": ""}},
    "exports": {"items": [], "format": "xlsx", "skip_empty": True, "excel": True, "pdf": False, "save_crawl": True,
                "sitemap": False},
    "notify": {"emails": [], "attach": False, "on_failure": True},
    "auto_compare": True, "wait_minutes": 60,
}


# Every Run now refusal because the server is busy ends with this (web/app.py answers it with 409, not 400)
BUSY_SUFFIX = "Wait until it has finished."


class SchedulerBusy(Exception):
    """start_fn: a crawl is running, the task has to wait."""


# ---------------------------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------------------------
def parse_when(text: Any) -> Optional[datetime]:
    """A datetime from ISO 8601 text ("Z" and offsets accepted; naive = server local time) or epoch seconds."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    s = str(text or "").strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s.replace(" ", "T", 1) if "T" not in s and " " in s else s)
    except ValueError:
        return None


def _ts(dt: datetime) -> float:
    return dt.timestamp()     # naive = local time (DST rules applied by the OS)


def _add_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    year, month = dt.year + m // 12, m % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_run_after(task: Dict[str, Any], after: float) -> Optional[float]:
    """Epoch seconds of the task's first slot strictly after `after` (None: a one-off task that is past)."""
    first = parse_when(task.get("first_run"))
    if first is None:
        return None
    interval = task.get("interval") or "once"
    every = max(1, int(task.get("every") or 1))
    if _ts(first) > after:
        return _ts(first)
    if interval == "once":
        return None
    if interval == "monthly":
        months = (datetime.fromtimestamp(after).year - first.year) * 12 + \
            (datetime.fromtimestamp(after).month - first.month)
        k = max(0, months // every - 1)
        for _ in range(48):
            cand = _add_months(first, k * every)
            if _ts(cand) > after:
                return _ts(cand)
            k += 1
        return None
    step = {"hourly": timedelta(hours=every), "daily": timedelta(days=every),
            "weekly": timedelta(weeks=every)}.get(interval)
    if step is None:
        return None
    n = int((after - _ts(first)) // step.total_seconds())
    cand = first + step * max(0, n - 1)
    for _ in range(5):              # local-time slots shift by up to an hour across DST: step forward
        if _ts(cand) > after:
            return _ts(cand)
        cand = cand + step
    while _ts(cand) <= after:
        cand = cand + step
    return _ts(cand)


def _iso(ts: Optional[float]) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(ts: Optional[float]) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="minutes")


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------
def _merge(defaults: Any, given: Any) -> Any:
    if isinstance(defaults, dict) and not defaults:          # free-form section (crawl options)
        return copy.deepcopy(given) if isinstance(given, dict) else {}
    if isinstance(defaults, dict):
        src = given if isinstance(given, dict) else {}
        return {k: _merge(v, src.get(k)) if k in src else copy.deepcopy(v) for k, v in defaults.items()}
    return copy.deepcopy(defaults) if given is None else given


def _normalize_url(raw: Any) -> str:
    text = str(raw or "").strip()
    if text and not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    return text


def _valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/?#]+\.[^\s/?#]+|^https?://(localhost|\d{1,3}(\.\d{1,3}){3})(:\d+)?",
                         url, re.I)) and len(url) <= 2000


def validate_task(task: Any, profile_exists: Optional[Callable[[str], bool]] = None
                  ) -> Tuple[Dict[str, Any], Dict[str, str], List[str]]:
    """(clean task, {field: error}, problems). Errors stop a task from being saved; problems (a profile that
    no longer exists, a login variable not set on this server) are saved but mark the task "not valid"."""
    from .settings_store import parse_emails, strip_secrets, validate_profile
    t = _merge(DEFAULT_TASK, task if isinstance(task, dict) else {})
    e: Dict[str, str] = {}
    problems: List[str] = []
    t["name"] = " ".join(str(t.get("name") or "").split())[:NAME_LIMIT]
    if not t["name"]:
        e["name"] = "Enter a task name."
    t["project"] = " ".join(str(t.get("project") or "").split())[:60]
    t["enabled"] = bool(t.get("enabled"))
    when = parse_when(t.get("first_run"))
    if when is None:
        e["first_run"] = "Enter the date and time of the first run."
    t["first_run"] = when.isoformat(timespec="seconds") if when else ""
    interval = str(t.get("interval") or "once").lower()
    if interval not in INTERVALS:
        e["interval"] = "Choose Once, Hourly, Daily, Weekly or Monthly."
    t["interval"] = interval
    try:
        t["every"] = int(t.get("every") or 1)
        if not 1 <= t["every"] <= 1000:
            raise ValueError
    except (TypeError, ValueError):
        e["every"] = "Repeat every 1 to 1,000."
        t["every"] = 1
    try:
        t["wait_minutes"] = int(t.get("wait_minutes") if t.get("wait_minutes") is not None else 60)
        if not 0 <= t["wait_minutes"] <= 1440:
            raise ValueError
    except (TypeError, ValueError):
        e["wait_minutes"] = "Wait 0 to 1,440 minutes."
        t["wait_minutes"] = 60

    st = t["start"]
    st["mode"] = "list" if str(st.get("mode") or "").lower() == "list" else "spider"
    if st["mode"] == "spider":
        st["url"] = _normalize_url(st.get("url"))
        if not _valid_url(st["url"]):
            e["start.url"] = "Enter the address to crawl (https://...)."
        elif _URL_PASSWORD.match(st["url"]):
            e["start.url"] = URL_PASSWORD_RULE
        st["urls"] = []
    else:
        raw = st.get("urls")
        lines = raw.splitlines() if isinstance(raw, str) else [str(u) for u in raw or []]
        urls = list(dict.fromkeys(_normalize_url(u) for u in lines if str(u).strip()))
        bad = [u for u in urls if not _valid_url(u)]
        if not urls:
            e["start.urls"] = "Add the URLs to crawl in List mode."
        elif any(_URL_PASSWORD.match(u) for u in urls):
            e["start.urls"] = URL_PASSWORD_RULE
        elif bad:
            e["start.urls"] = f"{len(bad)} line(s) are not web addresses, e.g. {bad[0][:80]}"
        elif len(urls) > MAX_LIST_URLS:
            e["start.urls"] = f"At most {MAX_LIST_URLS:,} URLs."
        st["urls"] = urls[:MAX_LIST_URLS]
        st["url"] = urls[0] if urls else ""
    st["profile"] = " ".join(str(st.get("profile") or "").split())[:60]
    if st["profile"] and profile_exists is not None and not profile_exists(st["profile"]):
        problems.append(f"The profile '{st['profile']}' does not exist.")
    opts = st.get("options") if isinstance(st.get("options"), dict) else {}
    opts = strip_secrets({k: v for k, v in opts.items() if k not in ("url", "urls", "mode", "auth", "force")})
    for k, msg in validate_profile(opts).items():
        e[f"start.options.{k}"] = msg
    st["options"] = opts
    login = st.get("login") if isinstance(st.get("login"), dict) else {}
    ltype = str(login.get("type") or "none").lower()
    ltype = ltype if ltype in ("none", "cookies", "header") else "none"
    env = str(login.get("env") or "").strip()
    hname = str(login.get("header_name") or "").strip()
    if ltype != "none":
        if not _ENV_NAME.match(env):
            e["start.login.env"] = (f"Name the server environment variable that holds the login; its name starts "
                                    f"with {LOGIN_ENV_PREFIX} (e.g. SITE_COOKIES).")
        elif not login_env_allowed(env):
            e["start.login.env"] = LOGIN_ENV_RULE     # never says whether that variable exists
        elif not os.environ.get(env):
            problems.append(f"The environment variable {env} is not set on this server.")
        if ltype == "header" and not _HEADER_NAME.match(hname):
            e["start.login.header_name"] = "Enter the header name (e.g. Authorization)."
    st["login"] = {"type": ltype, "env": env if ltype != "none" else "", "header_name": hname if ltype == "header" else ""}

    ex = t["exports"]
    items = [str(i).strip() for i in ex.get("items") or [] if str(i).strip()]
    bad = [i for i in items if not _EXPORT_ID.match(i)]
    if bad:
        e["exports.items"] = "Unknown export: " + bad[0][:60]
    ex["items"] = list(dict.fromkeys(items))[:100]
    ex["format"] = "csv" if str(ex.get("format") or "").lower() == "csv" else "xlsx"
    for k in ("skip_empty", "excel", "pdf", "save_crawl", "sitemap"):
        ex[k] = bool(ex.get(k))

    nt = t["notify"]
    ok, bad = parse_emails(nt.get("emails"))
    if bad:
        e["notify.emails"] = "Not valid email addresses: " + ", ".join(bad[:5])
    nt["emails"] = ok[:50]
    nt["attach"] = bool(nt.get("attach"))
    nt["on_failure"] = bool(nt.get("on_failure"))
    t["auto_compare"] = bool(t.get("auto_compare"))
    for k in ("id", "next_run", "last_run", "last_status", "created_at", "updated_at"):
        if isinstance(task, dict) and k in task:
            t[k] = task[k]
    return t, e, problems


# ---------------------------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------------------------
class Scheduler:
    """The task list, its history and the tick thread. Thread-safe."""

    def __init__(self, start_fn: Callable[[Dict[str, Any], Dict[str, Any]], str],
                 on_complete: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Optional[dict]]] = None,
                 reports_dir: Optional[str] = None, busy_fn: Optional[Callable[[], bool]] = None,
                 clock: Optional[Callable[[], float]] = None, tick_seconds: float = TICK_SECONDS,
                 profile_exists: Optional[Callable[[str], bool]] = None,
                 logger: Optional[Callable[[str], None]] = None,
                 busy_detail: Optional[Callable[[], str]] = None):
        self.start_fn, self.on_complete, self.busy_fn = start_fn, on_complete, busy_fn
        self.busy_detail = busy_detail
        self.reports_dir = reports_dir or os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()
        self.clock = clock or time.time
        self.tick_seconds = max(1.0, float(tick_seconds))
        self.log = logger or (lambda msg: None)
        if profile_exists is None:
            from .settings_store import profile_exists as _pe
            profile_exists = lambda name: _pe(name, self.reports_dir)   # noqa: E731
        self.profile_exists = profile_exists
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._waiting: Dict[str, float] = {}       # task id -> when it became due while a crawl ran
        self._post_threads: List[threading.Thread] = []
        self._mark_interrupted()

    # -- files ----------------------------------------------------------------------------------
    @property
    def tasks_path(self) -> str:
        return os.path.join(self.reports_dir, TASKS_FILE)

    @property
    def history_path(self) -> str:
        return os.path.join(self.reports_dir, HISTORY_FILE)

    def _read(self, path: str, key: str) -> List[dict]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get(key) if isinstance(data, dict) else None
            return [x for x in items or [] if isinstance(x, dict)]
        except (OSError, ValueError):
            return []

    def _write(self, path: str, key: str, items: List[dict]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": VERSION, key: items}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def _tasks(self) -> List[dict]:
        return self._read(self.tasks_path, "tasks")

    def _save_tasks(self, tasks: List[dict]) -> None:
        self._write(self.tasks_path, "tasks", tasks)

    def _runs(self) -> List[dict]:
        return self._read(self.history_path, "runs")

    def _save_runs(self, runs: List[dict]) -> None:
        self._write(self.history_path, "runs", runs[-HISTORY_MAX:])

    def _mark_interrupted(self) -> None:
        """Runs still "started" from before a restart never report back: say so instead of leaving them open."""
        with self._lock:
            runs = self._runs()
            changed = False
            for r in runs:
                if r.get("status") in ("started", "waiting"):
                    r["status"] = "interrupted"
                    r["note"] = (r.get("note") or "") + " The server restarted before this run finished."
                    r["finished_at"] = r.get("finished_at") or _iso(self.clock())
                    changed = True
            if changed:
                self._save_runs(runs)

    # -- task list --------------------------------------------------------------------------------
    def _public(self, t: dict) -> dict:
        out = copy.deepcopy(t)
        _clean, errors, problems = validate_task(t, self.profile_exists)
        out["problems"] = list(errors.values()) + problems
        if t.get("enabled") and not t.get("next_run") and not t.get("last_run"):
            out["problems"].append("The first run is in the past and the task does not repeat: set a new date.")
        out["valid"] = not out["problems"]
        out["next_run_local"] = _local(self._next_ts(t))
        out["waiting_since"] = _iso(self._waiting.get(t.get("id", "")))
        return out

    @staticmethod
    def _next_ts(t: dict) -> Optional[float]:
        nr = parse_when(t.get("next_run")) if t.get("next_run") else None
        return nr.timestamp() if nr else None

    def list(self, q: str = "") -> List[dict]:
        with self._lock:
            tasks = [self._public(t) for t in self._tasks()]
        needle = str(q or "").strip().lower()
        if needle:
            tasks = [t for t in tasks if needle in " ".join([t.get("name", ""), t.get("project", ""),
                                                             t["start"].get("url", "")]).lower()]
        tasks.sort(key=lambda t: (self._next_ts(t) is None, self._next_ts(t) or 0, t.get("name", "").lower()))
        return tasks

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            t = next((t for t in self._tasks() if t.get("id") == task_id), None)
            return self._public(t) if t else None

    def _schedule(self, t: dict, after: Optional[float] = None) -> None:
        nxt = next_run_after(t, self.clock() - 1 if after is None else after) if t.get("enabled") else None
        t["next_run"] = _iso(nxt) if nxt else ""

    def add(self, task: dict) -> Tuple[Optional[dict], Dict[str, str]]:
        clean, errors, _problems = validate_task(task, self.profile_exists)
        if errors:
            return None, errors
        with self._lock:
            tasks = self._tasks()
            if len(tasks) >= MAX_TASKS:
                return None, {"name": f"At most {MAX_TASKS} scheduled tasks."}
            now = _iso(self.clock())
            clean.update(id=uuid.uuid4().hex[:12], created_at=now, updated_at=now, last_run="", last_status="")
            self._schedule(clean)
            tasks.append(clean)
            self._save_tasks(tasks)
        return self._public(clean), {}

    def edit(self, task_id: str, task: dict) -> Tuple[Optional[dict], Dict[str, str]]:
        with self._lock:
            tasks = self._tasks()
            old = next((t for t in tasks if t.get("id") == task_id), None)
            if old is None:
                return None, {"id": "Task not found."}
            merged = copy.deepcopy(old)
            for k, v in (task or {}).items():
                if k not in DEFAULT_TASK:
                    continue
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = dict(merged[k], **v)       # a partial section keeps the rest of the old one
                else:
                    merged[k] = v
            clean, errors, _problems = validate_task(merged, self.profile_exists)
            if errors:
                return None, errors
            clean.update(id=task_id, created_at=old.get("created_at", ""), updated_at=_iso(self.clock()),
                         last_run=old.get("last_run", ""), last_status=old.get("last_status", ""))
            self._schedule(clean)
            tasks[tasks.index(old)] = clean
            self._waiting.pop(task_id, None)
            self._save_tasks(tasks)
        return self._public(clean), {}

    def delete(self, task_id: str) -> bool:
        with self._lock:
            tasks = self._tasks()
            kept = [t for t in tasks if t.get("id") != task_id]
            if len(kept) == len(tasks):
                return False
            self._waiting.pop(task_id, None)
            self._save_tasks(kept)
        return True

    def duplicate(self, task_id: str) -> Optional[dict]:
        with self._lock:
            t = next((t for t in self._tasks() if t.get("id") == task_id), None)
            if t is None:
                return None
            copy_ = {k: v for k, v in t.items() if k in DEFAULT_TASK}
            copy_["name"] = (t.get("name", "") + " (copy)")[:NAME_LIMIT]
            copy_["enabled"] = False                   # a copy never doubles the crawls until switched on
            out, _errors = self.add(copy_)
            return out

    def set_enabled(self, task_id: str, enabled: bool) -> Optional[dict]:
        out, errors = self.edit(task_id, {"enabled": bool(enabled)})
        return out

    # -- history ----------------------------------------------------------------------------------
    def history(self, task_id: Optional[str] = None, limit: int = 200) -> List[dict]:
        with self._lock:
            runs = self._runs()
        if task_id:
            runs = [r for r in runs if r.get("task_id") == task_id]
        return list(reversed(runs))[:max(1, int(limit or 200))]

    def _add_run(self, t: dict, status: str, scheduled_for: float, **extra) -> dict:
        run = {"id": uuid.uuid4().hex[:12], "task_id": t.get("id", ""), "task_name": t.get("name", ""),
               "scheduled_for": _iso(scheduled_for), "started_at": "", "finished_at": "", "status": status,
               "run_ref": "", "note": "", "error": ""}
        run.update(extra)
        runs = self._runs()
        runs.append(run)
        self._save_runs(runs)
        return run

    def _update_run(self, run_id: str, **changes) -> Optional[dict]:
        with self._lock:
            runs = self._runs()
            run = next((r for r in runs if r.get("id") == run_id), None)
            if run is None:
                return None
            run.update(changes)
            self._save_runs(runs)
            return dict(run)

    # -- running ----------------------------------------------------------------------------------
    def build_request(self, task: dict) -> Dict[str, Any]:
        """CrawlRequest fields for a run: the profile's options, the task's own options over them, the start
        URL or list, and the login read from its environment variable now (never stored)."""
        from .settings_store import load_profile, strip_secrets
        st = task.get("start") or {}
        req: Dict[str, Any] = {}
        if st.get("profile"):
            prof = load_profile(st["profile"], self.reports_dir)
            if prof:
                req.update(prof.get("options") or {})
        req.update(strip_secrets(st.get("options") or {}))
        req["mode"] = st.get("mode") or "spider"
        if req["mode"] == "list":
            req["urls"] = list(st.get("urls") or [])
            req["url"] = req["urls"][0] if req["urls"] else ""
        else:
            req["url"] = st.get("url") or ""
            req.pop("urls", None)
        login = st.get("login") or {}
        env = str(login.get("env") or "")
        secret = os.environ.get(env) if login.get("type") in ("cookies", "header") and login_env_allowed(env) \
            else None
        if secret:
            if login["type"] == "cookies":
                req["auth"] = {"enabled": True, "auth_type": "cookies", "cookies": secret}
            else:
                req["auth"] = {"enabled": True, "auth_type": "header", "custom_header_name": login.get("header_name"),
                               "custom_header_value": secret}
        else:
            req.pop("auth", None)
        req["force"] = False
        return req

    def _busy(self) -> bool:
        try:
            return bool(self.busy_fn()) if self.busy_fn else False
        except Exception:
            return False

    def _busy_reason(self) -> str:
        """Why Run now has to wait: the app's own words (a crawl, Crawl Analysis, a task building its exports)."""
        try:
            why = str(self.busy_detail() or "").strip() if self.busy_detail else ""
        except Exception:
            why = ""
        return (why + " " if why else "A crawl is running. ") + BUSY_SUFFIX

    def tick(self) -> List[str]:
        """One pass: starts the first due task when no crawl runs (others wait); returns the ids started."""
        started: List[str] = []
        with self._lock:
            now = self.clock()
            tasks = self._tasks()
            due = [t for t in tasks if t.get("enabled") and self._next_ts(t) is not None and self._next_ts(t) <= now]
            due.sort(key=lambda t: self._next_ts(t) or 0)
            dirty = False
            for t in due:
                slot = self._next_ts(t) or now
                tid = t.get("id", "")
                _clean, errors, problems = validate_task(t, self.profile_exists)
                if errors or problems:      # a task that cannot run is skipped at once, busy or not
                    self._add_run(t, "skipped", slot, finished_at=_iso(now),
                                  note="Not valid: " + "; ".join(list(errors.values()) + problems))
                    self._finish_slot(t, now, "skipped")
                    dirty = True
                    continue
                if started or self._busy():
                    since = self._waiting.setdefault(tid, now)
                    if now - since >= int(t.get("wait_minutes", 60)) * 60:
                        self._add_run(t, "skipped", slot, finished_at=_iso(now),
                                      note=f"Skipped: another crawl was running for {int((now - since) // 60)} "
                                           "minute(s) after this run was due.")
                        self._finish_slot(t, now, "skipped")
                        dirty = True
                    continue
                self._waiting.pop(tid, None)
                missed = self._missed(t, slot, now)
                run = self._add_run(t, "started", slot, started_at=_iso(now),
                                    note=f"{missed} earlier run(s) were missed while the server was off." if missed
                                    else "")
                try:
                    ref = self.start_fn(copy.deepcopy(t), self.build_request(t))
                except SchedulerBusy:
                    self._remove_run(run["id"])
                    self._waiting.setdefault(tid, now)
                    continue
                except Exception as e:
                    self._update_run(run["id"], status="failed", finished_at=_iso(now),
                                     error=f"{type(e).__name__}: {str(e)[:300]}")
                    self._finish_slot(t, now, "failed")
                    dirty = True
                    self.log(f"[Scheduler] Task '{t.get('name')}' could not start: {e}")
                    continue
                self._update_run(run["id"], run_ref=str(ref or run["id"]))
                self._finish_slot(t, now, "started")
                dirty = True
                started.append(tid)
                self.log(f"[Scheduler] Started task '{t.get('name')}'.")
            if dirty:
                self._save_tasks(tasks)
        return started

    def _remove_run(self, run_id: str) -> None:
        runs = [r for r in self._runs() if r.get("id") != run_id]
        self._save_runs(runs)

    def _missed(self, t: dict, slot: float, now: float) -> int:
        n, cur = 0, slot
        for _ in range(10000):
            nxt = next_run_after(t, cur)
            if nxt is None or nxt > now:
                break
            n, cur = n + 1, nxt
        return n

    def _finish_slot(self, t: dict, now: float, status: str) -> None:
        """The due slot is used up: the next run is the next future slot (a one-off task is switched off)."""
        t["last_run"] = _iso(now)
        t["last_status"] = status
        nxt = next_run_after(t, now)
        t["next_run"] = _iso(nxt) if nxt else ""
        if nxt is None and t.get("interval") == "once":
            t["enabled"] = False
        self._waiting.pop(t.get("id", ""), None)

    def run_now(self, task_id: str) -> Tuple[Optional[str], str]:
        """Starts a task at once (its schedule is unchanged). Returns (run_ref, message); a refusal because the
        server is busy ends with BUSY_SUFFIX."""
        with self._lock:
            t = next((x for x in self._tasks() if x.get("id") == task_id), None)
            if t is None:
                return None, "Task not found."
            if self._busy():
                return None, self._busy_reason()
            _clean, errors, problems = validate_task(t, self.profile_exists)
            if errors or problems:
                return None, "Not valid: " + "; ".join(list(errors.values()) + problems)
            now = self.clock()
            run = self._add_run(t, "started", now, started_at=_iso(now), note="Started by hand.")
            try:
                ref = self.start_fn(copy.deepcopy(t), self.build_request(t))
            except SchedulerBusy:
                self._remove_run(run["id"])
                return None, self._busy_reason()
            except Exception as e:
                self._update_run(run["id"], status="failed", finished_at=_iso(now), error=str(e)[:300])
                return None, f"The crawl could not start: {e}"
            self._update_run(run["id"], run_ref=str(ref or run["id"]))
            return str(ref or run["id"]), "Started."

    def crawl_finished(self, run_ref: str, status: str = "completed", error: str = "",
                       info: Optional[dict] = None, wait: bool = False) -> Optional[dict]:
        """The crawl a task started has ended (web/app.py calls this). Records it and runs on_complete in a
        thread (wait=True: in this thread). Returns the run, or None when the crawl was not a scheduled one."""
        with self._lock:
            run = next((r for r in reversed(self._runs()) if r.get("run_ref") == run_ref
                        and r.get("status") == "started"), None)
            if run is None:
                return None
            task = next((t for t in self._tasks() if t.get("id") == run.get("task_id")), None)
            run = self._update_run(run["id"], status=status if status in ("completed", "stopped", "failed",
                                                                            "auth_failed") else "completed",
                                   finished_at=_iso(self.clock()), error=str(error or "")[:500],
                                   info=info or {}) or run
            if task is not None:
                tasks = self._tasks()
                for x in tasks:
                    if x.get("id") == task.get("id"):
                        x["last_status"] = run["status"]
                self._save_tasks(tasks)
        if self.on_complete is None or task is None:
            return run

        def post():
            try:
                result = self.on_complete(copy.deepcopy(task), copy.deepcopy(run)) or {}
                self._update_run(run["id"], post="done", **{k: result[k] for k in
                                                            ("exports", "emailed", "compare", "notes", "files")
                                                            if k in result})
            except Exception as e:
                self._update_run(run["id"], post="failed", post_error=f"{type(e).__name__}: {str(e)[:300]}")
                self.log(f"[Scheduler] After-crawl steps of '{task.get('name')}' failed: {e}")
        if wait:
            post()
        else:
            th = threading.Thread(target=post, name="schedule-post", daemon=True)
            th.start()
            self._post_threads = [x for x in self._post_threads if x.is_alive()] + [th]
        return self._update_run(run["id"]) or run

    # -- import / export --------------------------------------------------------------------------
    def export_json(self, ids: Optional[List[str]] = None) -> bytes:
        with self._lock:
            tasks = [{k: v for k, v in t.items() if k in DEFAULT_TASK or k == "id"} for t in self._tasks()
                     if not ids or t.get("id") in ids]
        return json.dumps({"app": "site-auditor", "kind": "schedules", "version": VERSION, "tasks": tasks},
                          ensure_ascii=False, indent=1).encode("utf-8")

    def import_json(self, data: Any) -> Dict[str, Any]:
        """Adds the tasks of an exported file (bytes, text or dict). Every task is validated; imported tasks
        start switched off when they were invalid. Returns {"imported", "errors": [{"task", "errors"}]}."""
        try:
            if isinstance(data, (bytes, bytearray)):
                data = data.decode("utf-8-sig")
            if isinstance(data, str):
                data = json.loads(data)
        except (UnicodeDecodeError, ValueError):
            return {"imported": 0, "errors": [{"task": "", "errors": {"file": "The file is not valid JSON."}}]}
        items = data.get("tasks") if isinstance(data, dict) else data if isinstance(data, list) else None
        if not isinstance(items, list) or (isinstance(data, dict) and data.get("kind") not in (None, "schedules")):
            return {"imported": 0, "errors": [{"task": "", "errors": {"file": "The file holds no scheduled tasks."}}]}
        imported, errs = 0, []
        for item in items[:MAX_TASKS]:
            src = {k: v for k, v in (item or {}).items() if k in DEFAULT_TASK} if isinstance(item, dict) else {}
            out, errors = self.add(src)
            if errors:
                errs.append({"task": str(src.get("name") or "")[:80], "errors": errors})
            else:
                imported += 1
        return {"imported": imported, "errors": errs}

    # -- thread ---------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception as e:   # the scheduler must keep running whatever one task does
                self.log(f"[Scheduler] tick failed: {type(e).__name__}: {e}")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
