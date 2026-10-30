"""
ARACHNIX's tools for AI agents, shared by the MCP server at /mcp (crawler/mcp/server.py), the stdio bridge (which
forwards to that server) and the Ask ARACHNIX assistant.

Every tool calls ARACHNIX's own HTTP API through an ApiClient, so an agent can do what the dashboard's routes allow
and nothing more:
  InProcessApi  one ASGI request into the FastAPI app, no network. The request's client is a marker that is never an
                IP address (so it is never "this PC" and never admin); it carries the per-process internal secret and
                the actor ("mcp:<token name>" | "assistant"). Only the method + path pairs of ROUTES are allowed.
  HttpApi       tests only: a running server over HTTP with the team access key.

call_tool(api, name, args, allow_write, can_write, share_private) never raises. It validates the arguments (JSON
Schema 2020-12), refuses a write tool unless the server allows agent writes and the token may write, refuses the
content of a signed-in crawl unless an admin shares it, and returns {"ok", "data", "error", "truncated"}:
  - keys found_on / parent_url are renamed parent_link at any depth ("Parent link" is the name users see);
  - every filter count carries its state - passed / failed / not_checked / not_connected, or info for a count that
    is not a pass / fail check - and the reason, so a check that did not run is never read as 0 or Passed;
  - long lists are cut to the tool's size cap (truncated, "trimmed", next_offset for paged lists); trimming never
    removes a state, reason, not_checked or not_checked_filters key.
Resources (arachnix://crawl/summary, /issues, /fix-plan) and prompts (audit_site, explain_issue, developer_fix_plan)
are defined here too, so every transport serves the same ones.
"""
import asyncio
import contextvars
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple
from urllib.parse import quote, unquote, urlencode, urlsplit

PRIVATE_MSG = ("This crawl was signed in. Its content is not shared with AI agents unless an admin allows it in "
               "Connections > AI agents.")
WRITE_OFF_MSG = ("AI agents may not change anything on this server. An admin can turn on \"Allow AI agents to start "
                 "crawls\" in Connections > AI agents.")
TOKEN_READ_ONLY_MSG = ("This token is read-only. An admin can create a token that may start crawls in "
                       "Connections > AI agents.")
OTHER_DOMAIN_MSG = ("Finding the subdomains of a domain other than the loaded crawl's needs write access (a token "
                    "that may start crawls, with \"Allow AI agents to start crawls\" on).")
NO_CRAWL_MSG = "No crawl is loaded. Start one with start_crawl, or open a saved crawl with open_saved_crawl."
NOT_FINISHED_MSG = "Wait until the crawl and its reports are finished (crawl_status says finished: true)."
OLD_SERVER_MSG = "This ARACHNIX server does not offer that yet (it may be an older version)."

STATES = ("passed", "failed", "not_checked", "not_connected", "info")
RENAMED_KEYS = {"found_on": "parent_link", "parent_url": "parent_link"}
PROTECTED_KEYS = {"state", "reason", "not_checked", "not_checked_filters", "not_checked_checks", "not_applicable",
                  "counts", "summary", "checks_summary", "filters_state"}

# How an in-process request is marked (W1 reads these: the marker is never admin; the secret is accepted only
# together with the marker; the actor names the caller in the crawl log)
INTERNAL_CLIENT = ("slickup-internal", 0)
INTERNAL_HEADER = "x-slickup-internal"
ACTOR_HEADER = "x-slickup-actor"
ACTOR_STATE = "slickup_actor"
ACTOR_RE = re.compile(r"^[A-Za-z0-9 ._:-]{1,40}$")

MAX_STRING = 2000             # longest string kept in a result (page text never comes back whole)
MAX_ANSWER_BYTES = 50_000_000  # an in-process answer larger than this is refused (no route returns that much JSON)
LINK_NOTE = "Open it within 15 minutes; it works 5 times and needs no access key."

# Method + path pairs the tools may call (InProcessApi refuses anything else). Paths are matched decoded.
ROUTES: Tuple[Tuple[str, str], ...] = (
    ("GET", r"/api/health"),
    ("GET", r"/api/ai/status"),
    ("GET", r"/api/mcp"),
    ("GET", r"/api/crawl/status"),
    ("GET", r"/api/crawl/tabs"),
    ("GET", r"/api/crawl/checks"),
    ("GET", r"/api/crawl/tab/[A-Za-z0-9_\-]{1,80}"),
    ("GET", r"/api/crawl/url"),
    ("GET", r"/api/crawl/issues-overview"),
    ("GET", r"/api/intel/errors"),
    ("GET", r"/api/crawls/saved"),
    ("POST", r"/api/crawl/compare"),
    ("GET", r"/api/crawls/trend"),
    ("POST", r"/api/subdomains/discover"),
    ("GET", r"/api/subdomains/discover/[A-Za-z0-9_\-]{1,80}"),
    ("GET", r"/api/crawl/subdomains"),
    ("POST", r"/api/export/link"),
    ("GET", r"/api/profiles/[^/]{1,100}"),
    ("POST", r"/api/crawl/start"),
    ("POST", r"/api/crawl/stop"),
    ("POST", r"/api/crawls/[^/]{1,200}/[^/]{1,80}/open"),
    ("POST", r"/api/validate/start"),
    ("GET", r"/api/validate/status"),
    ("POST", r"/api/business/start"),
    ("GET", r"/api/business/status"),
)
_ROUTE_RES = [(m, re.compile(p + r"\Z")) for m, p in ROUTES]

# Downloads an export link may point at (W1 whitelists exactly these for POST /api/export/link)
EXPORT_PATHS = {
    "tab": "/api/crawl/export/tab",
    "issues": "/api/crawl/export/issues",
    "report": "/api/crawl/report/{report_id}",
    "bulk": "/api/crawl/export/bulk",
    "excel": "/api/crawl/report/excel",
    "pdf": "/api/crawl/report/pdf",
    "business_pdf": "/api/business/pdf",
    "fix_files": "/api/export/fixes",
}
FIX_FORMATS = ("csv", "yoast", "rankmath", "shopify", "htaccess", "nextjs")


class ToolError(Exception):
    """A plain message for the agent: never a stack trace, a secret or a URL query."""

    def __init__(self, message: str, status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


class ApiClient(Protocol):
    actor: str                                                        # "mcp:<token name>" | "assistant" | "test"

    def get(self, path: str, params: Optional[dict] = None) -> Any: ...   # JSON body; ToolError on 4xx / 5xx

    def post(self, path: str, body: Optional[dict] = None) -> Any: ...


# ---------------------------------------------------------------------------------------------
# API clients
# ---------------------------------------------------------------------------------------------
def _detail_text(status: int, body: Any, raw: bytes = b"") -> str:
    """The plain message of an error answer (FastAPI {"detail": str | [{"loc", "msg"}]}, optional "errors")."""
    msg = ""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            msg = detail
        elif isinstance(detail, list):
            parts = []
            for d in detail[:5]:
                if isinstance(d, dict):
                    loc = [str(x) for x in (d.get("loc") or []) if x not in ("body", "query", "path")]
                    parts.append((loc[-1] + ": " if loc else "") + str(d.get("msg") or ""))
            msg = "; ".join(p for p in parts if p)
        errors = body.get("errors")
        if isinstance(errors, dict) and errors:
            msg = (msg + " " if msg else "") + "; ".join(f"{k}: {v}" for k, v in list(errors.items())[:5])
        if not msg and isinstance(body.get("error"), dict):
            msg = str(body["error"].get("message") or "")
    if not msg:
        msg = {401: "The server refused the request (access key).", 403: "The server refused this action.",
               404: "Not found. " + OLD_SERVER_MSG, 409: "The server is busy with another task.",
               413: "The request is too large.", 429: "Too many requests. Try again shortly."}.get(
            status, f"ARACHNIX answered with an error (HTTP {status}).")
    return msg[:1000]


def _decode(status: int, content_type: str, data: bytes, retry_after: Optional[str] = None) -> Any:
    body = None
    if "json" in (content_type or "").lower() and data:
        try:
            body = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            body = None
    if status >= 400:
        wait = None
        try:
            wait = float(retry_after) if retry_after else None
        except ValueError:
            wait = None
        raise ToolError(_detail_text(status, body, data), status=status, retry_after=wait)
    if body is None:
        if "json" in (content_type or "").lower():
            raise ToolError("ARACHNIX sent an answer that is not valid JSON.", status=status)
        return {"content_type": content_type, "bytes": len(data)}
    return body


def _check_route(method: str, path: str) -> None:
    if not any(m == method and rx.match(path) for m, rx in _ROUTE_RES):
        raise ToolError("That action is not available to AI agents.", status=403)


class HttpApi:
    """A running ARACHNIX server over HTTP with the team access key (tests only; agents use the MCP bridge)."""

    def __init__(self, base_url: str, access_key: str = "", actor: str = "test", timeout: float = 60):
        self.base_url = base_url.rstrip("/")
        self.public_url = self.base_url
        self.access_key = access_key
        self.actor = actor
        self.timeout = timeout

    def _request(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None) -> Any:
        import requests
        headers = {"Accept": "application/json", ACTOR_HEADER: self.actor}
        if self.access_key:
            headers["X-Access-Key"] = self.access_key
        try:
            r = requests.request(method, self.base_url + path, params=params, json=body, headers=headers,
                                 timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as e:
            raise ToolError(f"Cannot reach ARACHNIX ({type(e).__name__}).")
        return _decode(r.status_code, r.headers.get("content-type", ""), r.content, r.headers.get("retry-after"))

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: Optional[dict] = None) -> Any:
        return self._request("POST", path, body=body if body is not None else {})


class InProcessApi:
    """One ASGI request into the FastAPI app per call, no network. Called from a worker thread of the server (the
    MCP server runs tools there), the request runs on the server's own event loop; from any other thread it runs
    on a short-lived loop of that thread. The scope's client is INTERNAL_CLIENT (never an IP, never admin); the
    headers carry the per-process internal secret and the actor, and scope["state"] the actor too. `public_url`
    (the server's address for people, e.g. http://10.10.6.56:8000) turns export links into full URLs."""

    def __init__(self, app: Any, internal_secret: str, actor: str, public_url: str = ""):
        if not ACTOR_RE.match(actor or ""):
            raise ValueError("actor: 1-40 letters, digits, spaces or . _ : -")
        self.app = app
        self.actor = actor
        self.public_url = (public_url or "").rstrip("/")
        self._secret = internal_secret or ""

    def __repr__(self) -> str:      # never shows the secret
        return f"InProcessApi(actor={self.actor!r})"

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: Optional[dict] = None) -> Any:
        return self._request("POST", path, body=body if body is not None else {})

    def _request(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None) -> Any:
        decoded = unquote(path.split("?", 1)[0])
        _check_route(method, decoded)
        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        query = urlencode([(k, v) for k, v in (params or {}).items() if v is not None], doseq=True)
        headers = [(b"host", b"slickup-internal"), (b"accept", b"application/json"),
                   (b"user-agent", b"slickup-tools"),
                   (INTERNAL_HEADER.encode(), self._secret.encode("latin-1", "replace")),
                   (ACTOR_HEADER.encode(), self.actor.encode("latin-1", "replace"))]
        if body is not None:
            headers += [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": decoded, "raw_path": path.encode("latin-1", "replace"),
                 "root_path": "", "query_string": query.encode("latin-1", "replace"), "headers": headers,
                 "client": INTERNAL_CLIENT, "server": INTERNAL_CLIENT,
                 "state": {ACTOR_STATE: self.actor, "slickup_internal": True}}
        status, ctype, data, retry = self._run(scope, payload)
        return _decode(status, ctype, data, retry)

    def _run(self, scope: dict, payload: bytes) -> Tuple[int, str, bytes, Optional[str]]:
        try:
            import anyio.from_thread
            from anyio import NoEventLoopError
        except ImportError:                    # pragma: no cover (anyio comes with Starlette)
            return asyncio.run(self._asgi(scope, payload))
        try:
            return anyio.from_thread.run(self._asgi, scope, payload)
        except NoEventLoopError:
            pass
        try:
            return asyncio.run(self._asgi(scope, payload))
        except RuntimeError as e:
            if "running event loop" in str(e):
                raise ToolError("Internal: ARACHNIX tools must run in a worker thread.")
            raise

    async def _asgi(self, scope: dict, payload: bytes) -> Tuple[int, str, bytes, Optional[str]]:
        done = asyncio.Event()
        sent_body = False
        status, headers, chunks, size = 500, [], [], 0

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status, headers, size
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers") or [])
            elif message["type"] == "http.response.body":
                chunk = message.get("body") or b""
                size += len(chunk)
                if size <= MAX_ANSWER_BYTES:
                    chunks.append(chunk)
                if not message.get("more_body"):
                    done.set()

        try:
            await self.app(scope, receive, send)
        finally:
            done.set()
        if size > MAX_ANSWER_BYTES:
            raise ToolError("ARACHNIX's answer was too large to read.")
        hmap = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers}
        return status, hmap.get("content-type", ""), b"".join(chunks), hmap.get("retry-after")


# ---------------------------------------------------------------------------------------------
# Tool specs and the call context
# ---------------------------------------------------------------------------------------------
@dataclass
class ToolSpec:
    name: str                       # snake_case, stable
    title: str                      # short plain label
    description: str                # 1-2 plain sentences, our own words
    input_schema: Dict[str, Any]    # JSON Schema 2020-12, "additionalProperties": false
    handler: Callable[[ApiClient, Dict[str, Any]], Dict[str, Any]]   # sync; servers run it in a worker thread
    write: bool = False             # changes state: needs allow_write + the token's can_write (MCP), a click (assistant)
    max_chars: int = 60_000         # result cap; lists are paged
    reads_crawl: bool = True        # returns content of the loaded crawl: refused on a signed-in crawl unless shared
    open_world: bool = False        # makes requests to other websites (crawl, subdomain search, business research)


@dataclass
class CallContext:
    """What one call may do; handlers read it with current_call(). The crawl status is fetched once per call."""
    api: Any
    allow_write: bool = False
    can_write: bool = False
    share_private: bool = False
    _status: Optional[dict] = field(default=None, repr=False)
    _settings: Optional[dict] = field(default=None, repr=False)

    @property
    def write_ok(self) -> bool:
        return bool(self.allow_write and self.can_write)

    def status(self) -> dict:
        if self._status is None:
            # cursors past the end: no result rows in the answer, only the counts
            self._status = self.api.get("/api/crawl/status", {"since": 10 ** 9, "ext_since": 10 ** 9}) or {}
        return self._status

    def private(self) -> bool:
        s = self.status()
        if "private" in s:
            return bool(s.get("private"))
        return bool(s.get("auth_used") or (s.get("stats") or {}).get("auth_used"))

    def agent_settings(self) -> dict:
        """GET /api/mcp's switches (allow_private_network ...); {} when the server does not offer it."""
        if self._settings is None:
            try:
                got = self.api.get("/api/mcp")
                self._settings = got if isinstance(got, dict) else {}
            except ToolError:
                self._settings = {}
        return self._settings


_CALL: contextvars.ContextVar[Optional[CallContext]] = contextvars.ContextVar("slickup_mcp_call", default=None)


def current_call(api: Any = None) -> CallContext:
    ctx = _CALL.get()
    if ctx is None or (api is not None and ctx.api is not api):
        ctx = CallContext(api=api)
    return ctx


_AUDIT: Dict[str, Optional[Callable[[Dict[str, Any]], None]]] = {"fn": None}


def set_audit_hook(fn: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """fn(entry) after every tool call and resource read: {"actor", "tool", "args" (secret-free, shortened), "ok",
    "error", "write", "at"}. The server writes it to the crawl log (W1). Errors in fn are ignored."""
    _AUDIT["fn"] = fn


def _audit(entry: Dict[str, Any]) -> None:
    fn = _AUDIT["fn"]
    if fn is None:
        return
    try:
        fn(entry)
    except Exception:
        pass


_USERINFO = re.compile(r"(//)[^/@\s]+@")


def safe_args(args: Any, depth: int = 0) -> Any:
    """Arguments for a log line: user info in URLs masked, strings cut to 200 characters, lists to 10 items."""
    if depth > 5:
        return "..."
    if isinstance(args, dict):
        return {str(k)[:40]: safe_args(v, depth + 1) for k, v in list(args.items())[:20]}
    if isinstance(args, (list, tuple)):
        out = [safe_args(v, depth + 1) for v in list(args)[:10]]
        if len(args) > 10:
            out.append(f"... {len(args) - 10} more")
        return out
    if isinstance(args, str):
        s = _USERINFO.sub(r"\1***@", args)
        return s if len(s) <= 200 else s[:200] + "..."
    return args


# ---------------------------------------------------------------------------------------------
# Normalising and trimming results
# ---------------------------------------------------------------------------------------------
def normalise(value: Any, depth: int = 0) -> Any:
    """found_on / parent_url -> parent_link at any depth (the first non-empty value wins when both exist)."""
    if depth > 60:
        return value
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for k, v in value.items():
            key = RENAMED_KEYS.get(k, k) if isinstance(k, str) else k
            v = normalise(v, depth + 1)
            if key in out and out[key] not in (None, "", [], {}):
                continue
            out[key] = v
        return out
    if isinstance(value, (list, tuple, set)):
        return [normalise(v, depth + 1) for v in value]
    return value


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _short_strings(value: Any, flags: Dict[str, bool], depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            flags["cut"] = True
            return value[:MAX_STRING] + " [cut]"
        return value
    if depth > 60:
        return value
    if isinstance(value, dict):
        return {k: _short_strings(v, flags, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_short_strings(v, flags, depth + 1) for v in value]
    return value


def _lists(value: Any, path: str = "", protected: bool = False, out: Optional[list] = None) -> list:
    """(path, list) of every list that may be shortened (none under a protected key)."""
    out = [] if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            _lists(v, f"{path}.{k}" if path else str(k), protected or k in PROTECTED_KEYS, out)
    elif isinstance(value, list):
        if not protected and value:
            out.append((path, value))
        for i, v in enumerate(value):
            if isinstance(v, (dict, list)):
                _lists(v, f"{path}[{i}]", protected, out)
    return out


def fit(data: Any, max_chars: int) -> Tuple[Any, bool]:
    """(data, truncated): long strings shortened, then the largest lists cut from the end until the JSON fits in
    max_chars. A dict result says what was cut in "trimmed" ({path: {"shown", "total"}}) and, for a paged list
    beside an "offset", where to continue ("next_offset")."""
    flags = {"cut": False}
    data = _short_strings(data, flags)
    truncated = flags["cut"]
    size = len(_dumps(data))
    if size <= max_chars:
        return data, truncated
    trimmed: Dict[str, Dict[str, int]] = {}
    budget = max(max_chars - 400, max_chars // 2)       # room for "trimmed" and "next_offset"
    for _round in range(400):
        if size <= budget:
            break
        candidates = [(len(_dumps(lst)), p, lst) for p, lst in _lists(data)]
        if not candidates:
            break
        _sz, path, lst = max(candidates, key=lambda c: c[0])
        excess = size - budget
        total = trimmed.get(path, {}).get("total", len(lst))
        while lst and excess > 0:
            excess -= len(_dumps(lst[-1])) + 1
            lst.pop()
        trimmed[path] = {"shown": len(lst), "total": total}
        truncated = True
        size = len(_dumps(data))
    if isinstance(data, dict) and trimmed:
        data["trimmed"] = trimmed
        offset = data.get("offset")
        for path, info in trimmed.items():
            if isinstance(offset, int) and "." not in path and "[" not in path and info["shown"] < info["total"]:
                data["next_offset"] = offset + info["shown"]
    note = "The answer was too large to return whole. Ask for fewer items (limit) or one tab at a time."
    if len(_dumps(data)) > max_chars:     # every list is cut: say so, and drop big non-protected values if needed
        data = data if isinstance(data, dict) else {"value": None}
        data["note"] = note
        truncated = True
        if len(_dumps(data)) > max_chars:
            keep = {k: v for k, v in data.items() if k in PROTECTED_KEYS or not isinstance(v, (dict, list, str))
                    or (isinstance(v, str) and len(v) <= 300)}
            keep["note"] = note
            data = keep
    return data, truncated


# ---------------------------------------------------------------------------------------------
# Shared helpers for the handlers
# ---------------------------------------------------------------------------------------------
def _is_not_connected(reason: str) -> bool:
    return str(reason or "").strip().lower().startswith("not connected")


def _state_of(check: Optional[dict], not_checked: str = "") -> Dict[str, Any]:
    """{state, reason (+ not_applicable / dismissed)} of one filter: the check's verdict when the filter is a check,
    else the registry's not-checked reason, else "info" (a count that is not a pass / fail check)."""
    if check is not None:
        status = str(check.get("status") or "")
        reason = str(check.get("reason") or "")
        if check.get("not_connected") or (status == "not_checked" and _is_not_connected(reason)):
            out: Dict[str, Any] = {"state": "not_connected", "reason": reason}
        elif status in ("passed", "failed", "not_checked"):
            out = {"state": status, "reason": reason}
        else:
            out = {"state": "not_checked", "reason": reason or "This check did not report a result."}
        if check.get("not_applicable"):
            out["not_applicable"] = True
        if check.get("dismissed"):
            out["dismissed"] = True
        if check.get("dismissed_urls"):
            out["dismissed_urls"] = int(check.get("dismissed_urls") or 0)
        return out
    if not_checked:
        return {"state": "not_connected" if _is_not_connected(not_checked) else "not_checked", "reason": not_checked}
    return {"state": "info", "reason": ""}


_CHECKS_CACHE: Dict[Any, Tuple[float, dict]] = {}
_CHECKS_LOCK = threading.Lock()
CHECKS_TTL = 20.0


def _checks(api: Any) -> Tuple[Dict[Tuple[str, str], dict], dict, List[dict]]:
    """(by (tab, filter), summary, every check) from GET /api/crawl/checks, reused for CHECKS_TTL seconds while the
    crawl did not change."""
    ctx = current_call(api)
    s = ctx.status()
    key = (id(getattr(api, "app", None) or api), getattr(api, "base_url", ""), s.get("snapshot_id"),
           s.get("results_version"), s.get("phase"), (s.get("stats") or {}).get("total_crawled"))
    now = time.time()
    with _CHECKS_LOCK:
        hit = _CHECKS_CACHE.get(key)
        if hit and now - hit[0] < CHECKS_TTL:
            body = hit[1]
        else:
            body = None
    if body is None:
        body = api.get("/api/crawl/checks") or {}
        with _CHECKS_LOCK:
            if len(_CHECKS_CACHE) > 16:
                _CHECKS_CACHE.clear()
            _CHECKS_CACHE[key] = (now, body)
    checks = [c for c in body.get("checks") or [] if isinstance(c, dict)]
    by = {(str(c.get("tab")), str(c.get("filter"))): c for c in checks}
    return by, dict(body.get("summary") or {}), checks


def _need_crawl(api: Any, finished: bool = False) -> dict:
    s = current_call(api).status()
    phase = str(s.get("phase") or s.get("status") or "idle")
    if phase == "idle":
        raise ToolError(NO_CRAWL_MSG)
    if finished and (phase != "done" or s.get("busy")):
        raise ToolError(NOT_FINISHED_MSG)
    return s


def _registrable(url_or_host: str) -> str:
    try:
        from crawler.url_utils import registrable_domain
        return registrable_domain(url_or_host)
    except Exception:
        host = (urlsplit(url_or_host if "://" in url_or_host else "//" + url_or_host).hostname or "").lower()
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) > 2 else host


def _absolute(api: Any, url: str) -> str:
    base = str(getattr(api, "public_url", "") or getattr(api, "base_url", "") or "").rstrip("/")
    return base + url if base and url.startswith("/") else url


def _export_link(api: Any, path: str, params: Optional[dict] = None, kind: str = "") -> Dict[str, Any]:
    got = api.post("/api/export/link", {"path": path, "params": {k: v for k, v in (params or {}).items()
                                                                 if v not in (None, "")}})
    rel = str((got or {}).get("url") or "")
    if not rel:
        raise ToolError("ARACHNIX did not return a download link.")
    out = {"kind": kind, "url": _absolute(api, rel), "expires_in": (got or {}).get("expires_in"), "note": LINK_NOTE}
    if out["url"] == rel:
        out["note"] += " Add the ARACHNIX server's address in front of the link."
    return out


def _compact(d: dict, keys: Tuple[str, ...]) -> dict:
    return {k: d.get(k) for k in keys if k in d and d.get(k) not in (None, "", [], {})}


def _wait(seconds: float, poll: Callable[[], dict], running: Callable[[dict], bool], step: float = 1.0) -> dict:
    deadline = time.time() + max(0.0, float(seconds))
    got = poll()
    while running(got) and time.time() < deadline:
        time.sleep(min(step, max(0.05, deadline - time.time())))
        got = poll()
    return got


def _refused_address(url: str, allow_private: bool) -> str:
    """Why an agent may not crawl this URL's host ('' when it may): cloud metadata and link-local addresses never;
    private networks and this machine only when the admin allows agents on private networks."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "That is not a valid web address."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Only http:// and https:// web addresses can be crawled."
    if parts.username or parts.password:
        return "Sign-in details are not accepted from AI agents (remove the user name and password from the address)."
    host = parts.hostname.lower().rstrip(".")
    from crawler import subdomains as sd
    if sd._metadata_name(host):
        return f"ARACHNIX never crawls cloud metadata addresses ({host})."
    ips = [host.strip("[]")] if sd._is_ip(host) else sd.resolve(host)
    for ip in ips:
        addr = sd._ip(ip)
        if addr is None:
            continue
        if addr.is_link_local or addr.is_unspecified or addr.is_multicast or addr.is_reserved:
            return f"ARACHNIX never crawls {host}: it points to a link-local or reserved address (such as cloud metadata)."
    if not allow_private:
        if sd.loopback_only(ips) or any(getattr(sd._ip(ip), "is_loopback", False) for ip in ips):
            return (f"{host} is this server itself. An admin can allow AI agents on private networks in "
                    "Connections > AI agents.")
        if any(sd.private_only([ip]) for ip in ips):
            return (f"{host} is on a private network. An admin can allow AI agents on private networks in "
                    "Connections > AI agents.")
    return ""


# ---------------------------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------------------------
def _server_status(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx = current_call(api)
    health = api.get("/api/health") or {}
    try:
        ai = api.get("/api/ai/status") or {}
    except ToolError:
        ai = {}
    agents = ctx.agent_settings()
    allow_write = bool(agents.get("allow_write", ctx.allow_write))
    out: Dict[str, Any] = {
        "server": "ARACHNIX", "status": health.get("status", "ok"),
        "ai": {"enabled": bool(ai.get("enabled")), "provider": ai.get("provider") or "", "model": ai.get("model") or "",
               "note": ai.get("reason") or ("AI is off." if not ai.get("enabled") else "")},
        "agents": {"allow_write": allow_write, "this_token_can_write": bool(ctx.can_write),
                   "write_tools_available": bool(ctx.write_ok),
                   "share_signed_in_crawls": bool(agents.get("share_private_crawls", ctx.share_private)),
                   "allow_private_network": bool(agents.get("allow_private_network", False))},
        "tools": [t.name for t in list_tools(ctx.write_ok)],
    }
    try:
        s = ctx.status()
        out["crawl"] = {"phase": s.get("phase") or "idle", "busy": bool(s.get("busy")), "private": ctx.private()}
    except ToolError as e:
        out["crawl"] = {"state": "not_checked", "reason": e.message}
    return out


CATEGORY_LABELS = {"2xx": "Success (2xx)", "3xx": "Redirect (3xx)", "4xx": "Broken (4xx)", "5xx": "Server error (5xx)",
                   "blocked": "Blocked", "error": "No response"}


def _crawl_status(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx = current_call(api)
    s = ctx.status()
    st = s.get("stats") or {}
    cats = {"2xx": int(st.get("status_2xx") or 0), "3xx": int(st.get("status_3xx") or 0),
            "4xx": int(st.get("status_4xx") or 0), "5xx": int(st.get("status_5xx") or 0),
            "blocked": int(st.get("status_blocked") or 0), "error": int(st.get("status_err") or 0)}
    urls = int(s.get("results_count") or 0)
    phase = str(s.get("phase") or "idle")
    out: Dict[str, Any] = {
        "state": s.get("status") or phase, "phase": phase, "finished": phase == "done" and not s.get("busy"),
        "busy": bool(s.get("busy")), "start_url": st.get("start_url") or "", "urls": urls,
        "crawled": int(st.get("total_crawled") or 0), "queued": int(st.get("queue_size") or 0),
        "discovered": int(st.get("total_discovered") or 0),
        "categories": cats, "category_labels": CATEGORY_LABELS,
        "external_links": int(s.get("external_count") or 0), "elapsed_seconds": st.get("elapsed_seconds"),
        "started_at": st.get("started_at") or "", "health_score": st.get("health_score"),
        "private": ctx.private(), "report_ready": bool(s.get("report_ready")),
    }
    if sum(cats.values()) != urls:
        out["categories_note"] = (f"The status classes add up to {sum(cats.values()):,} of {urls:,} URLs while the "
                                  "crawl is still being counted.")
    if st.get("limit_reached"):
        out["limit_reached"] = True
    if out["finished"]:
        try:      # numbers only (issue names are content): shown for signed-in crawls too
            ov = api.get("/api/crawl/issues-overview") or {}
            by_type: Dict[str, int] = {}
            for i in ov.get("issues") or []:
                by_type[str(i.get("issue_type") or "Other")] = by_type.get(str(i.get("issue_type") or "Other"), 0) + 1
            out["issues"] = {"total": sum(by_type.values()), "by_type": by_type}
        except ToolError as e:
            out["issues"] = {"state": "not_checked", "reason": e.message}
    else:
        out["issues"] = {"state": "not_checked", "reason": "Issues are counted when the crawl has finished."}
    if phase == "idle":
        out["note"] = NO_CRAWL_MSG
    if out["private"] and not ctx.share_private:
        # counts only: the address, logs and notes of a signed-in crawl stay on the server
        out["start_url"] = ""
        out["note"] = PRIVATE_MSG + " Only the counts are shown."
    else:
        if s.get("notes"):
            out["notes"] = list(s.get("notes") or [])[:10]
        if st.get("limitations"):
            out["limitations"] = list(st.get("limitations") or [])[:10]
        if s.get("saved_crawl"):
            out["saved_crawl"] = s.get("saved_crawl")
    return out


def _list_tabs(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    body = api.get("/api/crawl/tabs") or {}
    tabs = [t for t in body.get("tabs") or [] if isinstance(t, dict)]
    want = (args.get("tab") or "").strip()
    if want and not any(t.get("id") == want for t in tabs):
        raise ToolError(f"Unknown tab: {want}. Call list_tabs without a tab to see the tab ids.")
    if "summary" not in body:          # no SEO index yet: counts would read as "none found"
        s = current_call(api).status()
        phase = str(s.get("phase") or "idle")
        return {"phase": phase, "state": "not_checked",
                "reason": NO_CRAWL_MSG if phase == "idle" else "The tabs are counted when the crawl has finished.",
                "tabs": [{"id": t.get("id"), "name": t.get("name")} for t in tabs]}
    by, summary, _all = _checks(api)
    detail = bool(want)
    include_all = detail or bool(args.get("include_passed"))
    first_seen: Dict[str, str] = {}          # a reason already written in full -> the filter that carries it
    out_tabs = []
    for t in tabs:
        if want and t.get("id") != want:
            continue
        tid = str(t.get("id"))
        counts = {s: 0 for s in STATES}
        filters = []
        hidden = 0
        tab_nc = str(t.get("not_checked") or "")
        for f in t.get("filters") or []:
            fid = str(f.get("id"))
            if fid == "all":
                continue
            count = int(f.get("count") or 0)
            st = _state_of(by.get((tid, fid)), str(f.get("not_checked") or ""))
            counts[st["state"]] += 1
            if not include_all and (st["state"] == "passed" or (st["state"] == "info" and count == 0)):
                hidden += 1
                continue
            entry: Dict[str, Any] = {"id": fid, "name": f.get("name"), "count": count}
            entry.update(st)
            if st["state"] in ("not_checked", "not_connected") and st["reason"]:
                # many checks share one reason (every axe rule without rendering): written once in full
                ref = first_seen.get(st["reason"])
                if ref:
                    entry["reason"] = f"Same as {ref}."
                else:
                    first_seen[st["reason"]] = f"{tid}:{fid}"
            if f.get("issue_type") and (detail or st["state"] == "failed"):
                entry["issue_type"] = f.get("issue_type")
                entry["priority"] = f.get("priority")
            if detail:
                if f.get("description"):
                    entry["description"] = _clip(str(f.get("description")), 240)
                if f.get("short_fix") or f.get("fix"):
                    entry["fix"] = _clip(str(f.get("short_fix") or f.get("fix")), 160)
            filters.append(entry)
        item: Dict[str, Any] = {"id": tid, "name": t.get("name"), "total": int(t.get("total") or 0),
                                "counts": {k: v for k, v in counts.items() if v}, "filters": filters}
        if tab_nc:
            item.update(state="not_connected" if _is_not_connected(tab_nc) else "not_checked", reason=tab_nc)
        if hidden:
            item["not_listed"] = (f"{hidden} passed check{'s' if hidden != 1 else ''} or empty information "
                                  "filters (call list_tabs with this tab to see every filter)")
        if detail:
            item["columns"] = [{"id": RENAMED_KEYS.get(c.get("id"), c.get("id")), "label": c.get("label")}
                               for c in t.get("columns") or []]
            if t.get("description"):
                item["description"] = t.get("description")
        out_tabs.append(item)
    return {"phase": body.get("phase"), "issues_total": body.get("issues_total"), "checks_summary": summary,
            "indexable_summary": body.get("summary"), "tabs": out_tabs,
            "states": "passed / failed: the check ran; not_checked / not_connected: it did not run (reason says why, "
                      "never read it as 0; \"Same as tab:filter\" points to the same reason written above); info: a "
                      "count, not a check."}


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _get_tab_rows(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    status = _need_crawl(api)
    tab = args["tab"]
    filters = [str(f) for f in (args.get("filters") or ["all"])] or ["all"]
    limit, offset = int(args.get("limit", 50)), int(args.get("offset", 0))
    sort = str(args.get("sort") or "")
    direction = "desc" if sort.startswith("-") else "asc"
    params = {"filter": ",".join(filters), "match": args.get("match", "any"), "q": args.get("search") or "",
              "sort": sort.lstrip("-"), "dir": direction, "segment": args.get("segment") or "",
              "limit": limit, "offset": offset}
    body = api.get(f"/api/crawl/tab/{quote(tab, safe='')}", {k: v for k, v in params.items() if v not in ("",)})
    by, _summary, _all = _checks(api)
    nc_one = str(body.get("not_checked") or "")
    nc_multi = dict(body.get("not_checked_filters") or {})
    states = []
    for fid in filters:
        nc = nc_multi.get(fid, "") or (nc_one if len(filters) == 1 else "")
        st = _state_of(by.get((tab, fid)), nc)
        states.append(dict({"id": fid}, **st))
    want_cols = [RENAMED_KEYS.get(c, c) for c in (args.get("columns") or [])]
    columns = []
    for c in body.get("columns") or []:
        cid = RENAMED_KEYS.get(c.get("id"), c.get("id"))
        if cid == "address" or (want_cols and cid not in want_cols):
            continue
        columns.append({"id": cid, "label": c.get("label")})
    keep = {c["id"] for c in columns}
    rows = []
    for r in body.get("rows") or []:
        row: Dict[str, Any] = {"url": r.get("url")}
        if r.get("category"):
            row["category"] = r.get("category")
        for k, v in (r.get("cells") or {}).items():
            k = RENAMED_KEYS.get(k, k)
            if k in keep and v not in ("", None):
                row[k] = v
        rows.append(row)
    total = int(body.get("total") or 0)
    out: Dict[str, Any] = {"tab": tab, "filters": [s["id"] for s in states], "filters_state": states,
                           "match": args.get("match", "any"), "total": total,
                           "filter_total": int(body.get("filter_total") or 0), "offset": offset, "limit": limit,
                           "columns": columns, "rows": rows, "blank_cells": "left out of each row"}
    if len(states) == 1:
        out["state"], out["reason"] = states[0]["state"], states[0]["reason"]
    if nc_one:
        out["not_checked"] = nc_one
    if nc_multi:
        out["not_checked_filters"] = nc_multi
    if offset + len(rows) < total:
        out["next_offset"] = offset + len(rows)
    if str(status.get("phase")) != "done":
        out["note"] = "The crawl is still running: these counts are not final."
    return out


DETAIL_SECTIONS = ("summary", "inlinks", "outlinks", "issues", "headers", "images", "resources", "duplicates",
                   "structured_data", "accessibility", "spelling", "grammar", "link_metrics", "carbon", "ai", "pdf")
LIST_CAP = 50


def _get_url_details(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    _need_crawl(api)
    url = str(args["url"]).strip()
    sections = list(dict.fromkeys(args.get("sections") or ["summary", "issues", "inlinks", "outlinks"]))
    d = api.get("/api/crawl/url", {"u": url}) or {}
    result = d.get("result") or {}
    if not result:
        raise ToolError("This URL is not in the loaded crawl. Use get_tab_rows to find the exact address.")
    seo = d.get("seo") or {}
    serp = d.get("serp") or {}
    out: Dict[str, Any] = {"url": result.get("url") or url}
    registered = d.get("details") if isinstance(d.get("details"), dict) else {}
    for sec in sections:
        if sec == "summary":
            dups = d.get("duplicates") or {}
            out["summary"] = {
                "status_code": d.get("status_code", result.get("status_code")), "status": result.get("http_status"),
                "category": d.get("category") or result.get("category"),
                "category_label": CATEGORY_LABELS.get(str(d.get("category") or result.get("category")), ""),
                "content_type": result.get("content_type"), "indexable": d.get("indexable"),
                "indexability_status": d.get("indexability_status"), "title": serp.get("title"),
                "title_pixels": serp.get("title_px"), "meta_description": serp.get("description"),
                "h1": list(seo.get("h1") or [])[:3], "h2_count": len(seo.get("h2") or []),
                "canonical": list(seo.get("canonicals") or [])[:2], "meta_robots": seo.get("meta_robots"),
                "word_count": seo.get("word_count"), "language": seo.get("language"),
                "crawl_depth": d.get("crawl_depth"), "link_score": d.get("link_score"),
                "unique_inlinks": d.get("unique_inlinks"), "outlinks": len(d.get("outlinks") or []),
                "response_time_ms": result.get("response_time_ms"), "size_bytes": seo.get("size_bytes"),
                "redirect_to": result.get("redirect_destination") if result.get("redirect_destination") not in ("-", "") else "",
                "parent_link": result.get("parent_url") or result.get("found_on") or "",
                "discovery_source": result.get("discovery_source"), "segment": d.get("segment"),
                "duplicates": {k: (len(v) if isinstance(v, list) else v) for k, v in dups.items()
                               if k in ("title", "meta_description", "h1", "content", "near")},
            }
        elif sec in ("inlinks", "outlinks", "images", "resources"):
            items = list(d.get(sec) or [])
            out[sec] = {"total": len(items), "items": items[:LIST_CAP]}
            if len(items) > LIST_CAP:
                out[sec]["note"] = f"First {LIST_CAP} of {len(items):,}; export the full list with export_link."
        elif sec == "issues":
            out["issues"] = [_compact(i, ("name", "tab", "filter", "issue_type", "priority", "description", "fix"))
                             for i in d.get("issues") or []]
        elif sec == "headers":
            out["headers"] = d.get("headers") or {}
        elif sec == "duplicates":
            dups = d.get("duplicates") or {}
            out["duplicates"] = {k: (v[:20] if isinstance(v, list) else v) for k, v in dups.items()}
        else:
            value = registered.get(sec, d.get(sec))
            if isinstance(value, dict):
                out[sec] = value
            else:
                out[sec] = {"not_checked": f"No {sec.replace('_', ' ')} data for this URL (it does not apply to it, "
                                           "or the check did not run in this crawl)."}
    return out


GROUP_NAMES = 25          # checks named per not-checked reason in issues_overview


def _issues_overview(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    _need_crawl(api)
    body = api.get("/api/crawl/issues-overview") or {}
    by, summary, checks = _checks(api)
    prio, typ, limit = args.get("priority"), args.get("type"), int(args.get("limit", 50))

    def wanted(row: dict) -> bool:
        return (not prio or row.get("priority") == prio) and (not typ or row.get("issue_type") == typ)

    all_issues = [i for i in body.get("issues") or [] if isinstance(i, dict)]
    issues = []
    for i in all_issues:
        if not wanted(i):
            continue
        st = _state_of(by.get((str(i.get("tab")), str(i.get("filter")))))
        if st["state"] == "info":
            st = {"state": "failed", "reason": f"Flagged on {int(i.get('urls') or 0):,} URLs."}
        row = _compact(i, ("name", "tab", "filter", "issue_type", "priority", "urls", "pct", "short_fix",
                           "dismissed_urls"))
        row.update(st)
        issues.append(row)
    # checks that did not run, grouped by their reason (dozens share one: every axe rule without rendering)
    groups: Dict[Tuple[bool, str, str], Dict[str, Any]] = {}
    for c in checks:
        if c.get("status") != "not_checked" or ((prio or typ) and not wanted(c)):
            continue
        st = _state_of(c)
        key = (bool(c.get("not_applicable")), st["state"], st["reason"])
        g = groups.setdefault(key, {"state": st["state"], "reason": st["reason"], "count": 0, "checks": []})
        g["count"] += 1
        if len(g["checks"]) < GROUP_NAMES:
            g["checks"].append(_compact(c, ("tab", "filter", "name", "priority")))
    for g in groups.values():
        if g["count"] > len(g["checks"]):
            g["more"] = f"{g['count'] - len(g['checks'])} more checks with this reason (list_tabs shows them all)."
    not_checked = sorted((g for (na, _s, _r), g in groups.items() if not na), key=lambda g: -g["count"])
    not_applicable = sorted((g for (na, _s, _r), g in groups.items() if na), key=lambda g: -g["count"])
    for g in not_applicable:
        g["state"] = "not_checked"
    if prio or typ:
        picked = [c for c in checks if wanted(c)]
        summary = {"passed": 0, "failed": 0, "not_checked": 0, "not_applicable": 0, "dismissed": 0}
        for c in picked:
            if c.get("not_applicable"):
                summary["not_applicable"] += 1
            elif c.get("dismissed") and c.get("status") == "failed":
                summary["dismissed"] += 1
            else:
                summary[str(c.get("status"))] = summary.get(str(c.get("status")), 0) + 1
    out: Dict[str, Any] = {"summary": summary, "issues_total": len([i for i in all_issues if wanted(i)]),
                           "issues": issues[:limit], "not_checked": not_checked,
                           "not_checked_total": sum(g["count"] for g in not_checked),
                           "not_applicable": not_applicable,
                           "states": "failed: flagged URLs; not_checked / not_connected: the check did not run "
                                     "(never a pass), grouped by reason; not_applicable: nothing of that kind on "
                                     "the site."}
    if len(issues) > limit:
        out["more"] = f"{len(issues) - limit} more issues: raise limit (max 100)."
    dismissed = [_compact(x, ("name", "tab", "filter", "urls", "dismissed_urls")) for x in body.get("dismissed") or []
                 if isinstance(x, dict)]
    if dismissed:
        out["dismissed"] = dismissed[:50]
    return out


def _fix_plan(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    _need_crawl(api)
    body = api.get("/api/intel/errors") or {}
    groups = [g for g in body.get("groups") or [] if isinstance(g, dict)]
    limit = int(args.get("limit", 15))
    plat = body.get("platform") or {}
    cms = str(plat.get("cms") or "").lower()
    out_groups = []
    for g in groups[:limit]:
        item = _compact(g, ("rank", "title", "category", "where", "urls_count", "issues", "root_cause", "why",
                            "fix_steps", "element", "how_to_verify", "prevent", "effort", "impact", "verified_label",
                            "intentional", "issue_keys"))
        item["sample_urls"] = list(g.get("sample_urls") or [])[:5]
        steps = list(g.get("platform_steps") or [])
        if cms:
            steps = [s for s in steps if str(s).lower().startswith(cms)] or steps
        if steps:
            item["platform_steps"] = steps[:4]
        out_groups.append(item)
    return {"platform": _compact(plat, ("cms", "frontend", "theme", "hosting", "confidence")),
            "summary": body.get("summary") or {}, "ai_used": bool(body.get("ai_used")),
            "groups_total": len(groups), "groups": out_groups}


SAVED_KEYS = ("id", "name", "project", "site", "mode", "started_at", "saved_at", "status", "complete", "urls",
              "categories", "pages", "health_score", "issues", "limit_reached", "locked")


def _list_saved_crawls(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    params = {"site": args.get("site") or ""}
    body = api.get("/api/crawls/saved", {k: v for k, v in params.items() if v}) or {}
    crawls = [c for c in body.get("crawls") or [] if isinstance(c, dict)]
    limit = int(args.get("limit", 20))
    items = []
    for c in crawls[:limit]:
        item = _compact(c, SAVED_KEYS)
        item["signed_in"] = bool(c.get("auth_used"))
        items.append(item)
    out = {"total": len(crawls), "crawls": items, "current": body.get("current") or "",
           "category_labels": CATEGORY_LABELS}
    if len(crawls) > limit:
        out["more"] = f"{len(crawls) - limit} older crawls not listed: raise limit (max 50) or name a site."
    return out


def _changed_issue(i: dict) -> bool:
    return any(int(i.get(k) or 0) for k in ("new_count", "fixed_count", "regressed_count", "not_rechecked_count")) \
        or int(i.get("delta") or 0) != 0


def _changed_row(r: dict) -> bool:
    return any(int(r.get(k) or 0) for k in ("change", "added", "new", "removed", "missing")) \
        or str(r.get("check_current") or "") != str(r.get("check_previous") or "")


def _compare_crawls(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx = current_call(api)
    body = {"a": args["a"], "b": args.get("b") or "current"}
    if args.get("change_detection"):
        body["change_detection"] = list(args["change_detection"])
    got = api.post("/api/crawl/compare", body) or {}
    if not ctx.share_private and ((got.get("a") or {}).get("auth_used") or (got.get("b") or {}).get("auth_used")):
        raise ToolError(PRIVATE_MSG)
    issues = [dict(_compact(i, ("name", "tab", "filter", "issue_type", "priority", "a", "b", "delta", "new_count",
                                "fixed_count", "regressed_count", "not_rechecked_count", "b_check")),
                   new_urls=list(i.get("new_urls") or [])[:10], fixed_urls=list(i.get("fixed_urls") or [])[:10],
                   regressed_urls=list(i.get("regressed_urls") or [])[:10])
              for i in got.get("issues") or [] if isinstance(i, dict) and _changed_issue(i)]
    overview = [r for r in got.get("overview") or [] if isinstance(r, dict) and _changed_row(r)]
    changed = dict(got.get("changed") or {})
    for k, v in list(changed.items()):
        if isinstance(v, list):
            changed[k] = v[:10]
    meta = ("id", "site", "started_at", "urls", "pages", "pages_ok", "issues", "issue_urls", "health_score",
            "limit_reached")
    return {"a": _compact(got.get("a") or {}, meta), "b": _compact(got.get("b") or {}, meta),
            "summary": got.get("summary") or {}, "totals": got.get("totals") or [],
            "changes": got.get("changes") or [], "changed": changed, "issues_changed": issues,
            "issues_unchanged": len([i for i in got.get("issues") or [] if isinstance(i, dict) and not _changed_issue(i)]),
            "filters_changed": overview[:100],
            "filters_unchanged": len([r for r in got.get("overview") or [] if isinstance(r, dict) and not _changed_row(r)])}


def _get_trend(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    params = {"site": args.get("site") or "", "limit": int(args.get("limit", 10))}
    try:
        got = api.get("/api/crawls/trend", {k: v for k, v in params.items() if v not in ("", None)}) or {}
    except ToolError as e:
        if e.status == 404 and "trend" not in e.message.lower():
            raise ToolError("Trends are not available on this ARACHNIX server yet.")
        raise
    return got


HOST_KEYS = ("host", "state", "sources", "resolves", "status", "title", "note", "url", "redirect", "linked_by")


def _find_subdomains(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx = current_call(api)
    if args.get("report"):
        rep = api.get("/api/crawl/subdomains") or {}
        rep.pop("source_labels", None)
        return rep
    loaded = str(((ctx.status().get("stats") or {}).get("start_url")) or "")
    wait = float(args.get("wait_seconds", 30))
    job_id = str(args.get("job_id") or "")

    def poll() -> dict:
        try:
            return api.get(f"/api/subdomains/discover/{quote(job_id, safe='')}") or {}
        except ToolError as e:
            if e.status == 404:
                raise ToolError("This subdomain search has ended or expired. Run find_subdomains again without job_id.")
            raise

    if not job_id:
        target = str(args.get("url") or "").strip() or loaded
        if not target:
            raise ToolError("Give the website address (url), or load a crawl first.")
        if not ctx.write_ok and (not loaded or _registrable(target) != _registrable(loaded)):
            raise ToolError(OTHER_DOMAIN_MSG)
        body: Dict[str, Any] = {"url": target}
        if args.get("sources") is not None:
            body["sources"] = list(args["sources"])
        if args.get("extra_hosts"):
            body["extra_hosts"] = list(args["extra_hosts"])
        try:
            started = api.post("/api/subdomains/discover", body) or {}
        except ToolError as e:
            if e.status == 429:
                raise ToolError("Another subdomain search is running; try again in 10 seconds.", status=429,
                                retry_after=e.retry_after or 10)
            raise
        job_id = str(started.get("job_id") or "")
        if not job_id:
            raise ToolError("ARACHNIX did not start the subdomain search.")
    if args.get("job_id") and not ctx.write_ok:     # a read-only token may only follow searches of its own domain
        first = poll()
        if not loaded or _registrable(str(first.get("domain") or "")) != _registrable(loaded):
            raise ToolError(OTHER_DOMAIN_MSG)
    got = _wait(wait, poll, lambda g: g.get("state") == "running")
    hosts = [_compact(h, HOST_KEYS) for h in got.get("hosts") or [] if isinstance(h, dict)]
    out = {"job_id": job_id, "state": got.get("state"), "domain": got.get("domain"), "message": got.get("message"),
           "done": got.get("done"), "total": got.get("total"), "hosts": hosts, "errors": got.get("errors") or {},
           "sources": got.get("sources") or {}}
    if got.get("state") == "running":
        out["next"] = "Still searching: call find_subdomains again with this job_id for the rest."
    return out


def _export_link_tool(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    kind, fmt = args["kind"], args.get("format", "xlsx")
    s = _need_crawl(api, finished=kind in ("excel", "pdf"))
    if s.get("phase") == "crawling":
        raise ToolError(NOT_FINISHED_MSG)
    if kind == "tab":
        if not args.get("tab"):
            raise ToolError("Name the tab to export (tab).")
        params = {"tab": args["tab"], "filter": ",".join(args.get("filters") or ["all"]),
                  "match": args.get("match", "any"), "format": fmt}
        return _export_link(api, EXPORT_PATHS["tab"], params, kind)
    if kind == "issues":
        return _export_link(api, EXPORT_PATHS["issues"], {"format": fmt}, kind)
    if kind == "report":
        rid = str(args.get("report_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_\-:]{1,80}", rid):
            raise ToolError("Name the report to export (report_id), e.g. crawl_overview.")
        return _export_link(api, EXPORT_PATHS["report"].format(report_id=quote(rid, safe="")), {"format": fmt}, kind)
    if kind == "bulk":
        rid = str(args.get("report_id") or "")
        if not rid:
            raise ToolError("Name the bulk export (report_id), e.g. links_all_inlinks or issue:<tab>:<filter>.")
        return _export_link(api, EXPORT_PATHS["bulk"], {"id": rid, "format": fmt}, kind)
    if kind == "excel":
        return _export_link(api, EXPORT_PATHS["excel"], {"category": "all"}, kind)
    return _export_link(api, EXPORT_PATHS["pdf"], {}, kind)


PROFILE_DROP = {"auth", "request_headers", "force", "handoff_id", "cookies", "cookie", "proxy", "url", "urls", "mode",
                "live_session_id"}


def _start_crawl(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx = current_call(api)
    allow_private = bool(ctx.agent_settings().get("allow_private_network", False))
    url = str(args["url"]).strip()
    mode = args.get("mode", "spider")
    why = _refused_address(url, allow_private)
    if why:
        raise ToolError(why)
    urls = [str(u).strip() for u in args.get("urls") or [] if str(u).strip()]
    if mode == "list":
        if not urls:
            raise ToolError("List mode needs the URLs to crawl (urls).")
        hosts: Dict[str, str] = {}
        for u in urls:
            host = (urlsplit(u).hostname or "").lower()
            if host and host not in hosts:
                hosts[host] = u
            if len(hosts) > 50:
                raise ToolError("List mode from an AI agent takes URLs of at most 50 different hosts.")
        for host, u in hosts.items():
            why = _refused_address(u, allow_private)
            if why:
                raise ToolError(why)
    body: Dict[str, Any] = {}
    profile = str(args.get("profile") or "").strip()
    if profile:
        try:
            prof = api.get(f"/api/profiles/{quote(profile, safe='')}") or {}
        except ToolError as e:
            if e.status == 404:
                raise ToolError(f"No crawl profile named {profile!r}.")
            raise
        body.update({k: v for k, v in (prof.get("options") or {}).items() if str(k).lower() not in PROFILE_DROP})
    # the agent's own limit, else the profile's (never "no limit" from an agent), else 500
    prof_max = body.get("max_urls")
    max_urls = int(args["max_urls"]) if "max_urls" in args else (
        int(prof_max) if isinstance(prof_max, int) and not isinstance(prof_max, bool) and prof_max > 0 else 500)
    body.update(url=url, mode=mode, max_urls=max_urls)
    if "max_depth" in args:
        body["max_depth"] = int(args["max_depth"])
    if "render_all" in args:
        body["render_all"] = bool(args["render_all"])
    if mode == "list":
        body["urls"] = "\n".join(urls)
    body.pop("force", None)
    body.pop("auth", None)
    try:
        got = api.post("/api/crawl/start", body) or {}
    except ToolError as e:
        if e.status == 409 or (e.status == 400 and "already running" in e.message.lower()):
            raise ToolError(f"{e.message} Wait for it to finish (crawl_status) or stop it first (stop_crawl).",
                            status=409)
        raise
    out = _compact(got, ("status", "target", "mode", "crawl_scope", "notes", "list", "extra_hosts"))
    out["max_urls"] = body["max_urls"]
    if profile:
        out["profile"] = profile
    out["next"] = "Call crawl_status every few seconds until finished is true, then issues_overview."
    return out


def _stop_crawl(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    got = api.post("/api/crawl/stop", {}) or {}
    return _compact(got, ("status", "phase", "message", "detail")) or {"status": "stopped"}


def _open_saved_crawl(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx = current_call(api)
    cid = str(args["id"]).strip()
    m = re.fullmatch(r"([^/\s]{1,200})/([^/\s]{1,80})", cid)
    if not m:
        raise ToolError("The id of a saved crawl looks like host/timestamp (see list_saved_crawls).")
    got = api.post(f"/api/crawls/{quote(m.group(1), safe='')}/{quote(m.group(2), safe='')}/open", {}) or {}
    meta = got.get("meta") or {}
    if meta.get("auth_used") and not ctx.share_private:
        return {"status": got.get("status") or "opened", "id": got.get("id") or cid, "signed_in": True,
                "note": PRIVATE_MSG}
    out = {"status": got.get("status") or "opened", "id": got.get("id") or cid,
           "meta": _compact(meta, SAVED_KEYS), "notes": list(got.get("notes") or [])[:10]}
    diffs = list(got.get("count_differences") or [])
    if diffs:
        out["count_differences"] = diffs[:10]
    return out


VERDICT_SAMPLE = 30


def _validate_errors(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    started = ""
    if not args.get("status_only"):
        try:
            started = str((api.post("/api/validate/start", {"scope": "errors"}) or {}).get("job_id") or "")
        except ToolError as e:
            if e.status != 409 or "already running" not in e.message.lower():
                raise

    def poll() -> dict:
        return api.get("/api/validate/status") or {}

    got = _wait(float(args.get("wait_seconds", 20)), poll, lambda g: g.get("state") == "running")
    results = got.get("results") or {}
    sample = []
    hide = current_call(api).private() and not current_call(api).share_private   # signed-in crawl: counts only
    if isinstance(results, dict) and not hide:
        for url, v in list(results.items())[:VERDICT_SAMPLE]:
            sample.append(dict({"url": url}, **(_compact(v, ("verdict", "status_code", "category", "note"))
                                                if isinstance(v, dict) else {"verdict": v})))
    out = {"job_id": got.get("job_id") or started, "state": got.get("state"), "done": got.get("done"),
           "total": got.get("total"), "summary": got.get("summary") or {}, "message": got.get("message") or "",
           "results_sample": sample}
    if hide:
        out["note"] = PRIVATE_MSG + " Only the counts are shown."
    if got.get("state") == "running":
        out["next"] = "Still re-checking: call validate_errors with status_only true in a little while."
    return out


def _generate_report(api: ApiClient, args: Dict[str, Any]) -> Dict[str, Any]:
    kind = args["kind"]
    if kind == "business_pdf":
        s = _need_crawl(api, finished=True)
        try:
            api.post("/api/business/start", {"url": (s.get("stats") or {}).get("start_url") or ""})
        except ToolError as e:
            if e.status != 409:
                raise

        def poll() -> dict:
            return api.get("/api/business/status") or {}

        got = _wait(float(args.get("wait_seconds", 45)), poll, lambda g: g.get("state") == "running", step=2.0)
        if got.get("state") == "running":
            return {"kind": kind, "state": "running", "message": got.get("message") or "",
                    "next": "The business report is still being prepared: call generate_report again in a minute."}
        if got.get("state") != "done":
            raise ToolError(str(got.get("message") or "The business report could not be prepared."))
        return _export_link(api, EXPORT_PATHS["business_pdf"], {}, kind)
    _need_crawl(api, finished=True)
    if kind == "pdf":
        return _export_link(api, EXPORT_PATHS["pdf"], {}, kind)
    if kind == "excel":
        return _export_link(api, EXPORT_PATHS["excel"], {"category": "all"}, kind)
    return _export_link(api, EXPORT_PATHS["fix_files"], {"format": args.get("format", "csv")}, kind)


# ---------------------------------------------------------------------------------------------
# The tool list
# ---------------------------------------------------------------------------------------------
def _obj(props: Dict[str, Any], required: Tuple[str, ...] = ()) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


_ID = {"type": "string", "pattern": r"^[A-Za-z0-9_\-:]{1,80}$"}

TOOLS: List[ToolSpec] = [
    ToolSpec("server_status", "Server status",
             "Shows whether ARACHNIX is up, whether AI is on, and what AI agents may do here (read only, or start "
             "crawls too).", _obj({}), _server_status, reads_crawl=False),
    ToolSpec("crawl_status", "Crawl status",
             "Shows the loaded crawl: running or finished, URLs crawled and queued, counts per HTTP status class "
             "(they add up to the URLs) and the number of issues.", _obj({}), _crawl_status, reads_crawl=False),
    ToolSpec("list_tabs", "List tabs and filters",
             "Lists the audit tabs with each filter's URL count and state (passed, failed, not checked, not "
             "connected, or info). Give a tab id to see every filter of that tab with its description and columns.",
             _obj({"tab": _ID, "include_passed": {"type": "boolean", "default": False}}), _list_tabs,
             max_chars=100_000),
    ToolSpec("get_tab_rows", "Get tab rows",
             "Lists the URLs of a tab, limited to one or more filters, with the tab's columns. Use search to find "
             "URLs, sort by a column id (prefix - for descending) and page with limit and offset.",
             _obj({"tab": _ID,
                   "filters": {"type": "array", "items": _ID, "maxItems": 10},
                   "match": {"type": "string", "enum": ["any", "all"], "default": "any"},
                   "search": {"type": "string", "maxLength": 200},
                   "segment": {"type": "string", "maxLength": 100},
                   "sort": {"type": "string", "maxLength": 81},
                   "columns": {"type": "array", "items": _ID, "maxItems": 30},
                   "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                   "offset": {"type": "integer", "minimum": 0, "default": 0}}, ("tab",)), _get_tab_rows),
    ToolSpec("get_url_details", "URL details",
             "Everything ARACHNIX knows about one crawled URL: status, indexability, title, headings, the page that "
             "links to it (Parent link), inlinks, outlinks, issues and the detail sections you ask for.",
             _obj({"url": {"type": "string", "minLength": 1, "maxLength": 2000},
                   "sections": {"type": "array", "items": {"type": "string", "enum": list(DETAIL_SECTIONS)},
                                "maxItems": len(DETAIL_SECTIONS)}}, ("url",)), _get_url_details),
    ToolSpec("issues_overview", "Issues overview",
             "The issues found, most important first, with URL counts and a short fix, plus every check that could "
             "not run (not checked or not connected) and why.",
             _obj({"priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                   "type": {"type": "string", "enum": ["Issue", "Warning", "Opportunity"]},
                   "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}), _issues_overview),
    ToolSpec("fix_plan", "Fix plan",
             "Groups the issues by root cause in fix-first order, with plain fix steps, where to fix, sample URLs and "
             "how to verify.",
             _obj({"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}}), _fix_plan),
    ToolSpec("list_saved_crawls", "Saved crawls",
             "Lists saved crawls, newest first, with their ids (host/timestamp), URL counts per status class, health "
             "score and issues.",
             _obj({"site": {"type": "string", "maxLength": 300},
                   "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}}), _list_saved_crawls),
    ToolSpec("compare_crawls", "Compare crawls",
             "Compares two crawls (saved crawl ids, or current for the loaded one): new, fixed and regressed issues, "
             "and the filters whose counts changed.",
             _obj({"a": {"type": "string", "minLength": 1, "maxLength": 300},
                   "b": {"type": "string", "maxLength": 300, "default": "current"},
                   "change_detection": {"type": "array", "items": _ID, "maxItems": 20}}, ("a",)), _compare_crawls),
    ToolSpec("get_trend", "Trend",
             "Shows how a site's crawls changed over time: URLs per status class, indexable pages, issues and health "
             "score per crawl.",
             _obj({"site": {"type": "string", "maxLength": 300},
                   "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}}), _get_trend),
    ToolSpec("find_subdomains", "Find subdomains",
             "Finds other hosts of the loaded site's domain (links, sitemaps, certificate logs, Search Console) and "
             "checks their home pages; report true lists the hosts the crawl saw, without new requests.",
             _obj({"url": {"type": "string", "maxLength": 2000},
                   "job_id": {"type": "string", "pattern": r"^[A-Za-z0-9_\-]{1,80}$"},
                   "sources": {"type": "array", "items": {"type": "string", "enum": ["links", "sitemaps", "ct", "gsc"]},
                               "maxItems": 4},
                   "extra_hosts": {"type": "array", "items": {"type": "string", "maxLength": 253}, "maxItems": 20},
                   "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 60, "default": 30},
                   "report": {"type": "boolean", "default": False}}), _find_subdomains, open_world=True),
    ToolSpec("export_link", "Export link",
             "Makes a short-lived download link for a tab, the issues overview, a report, a bulk export, the full "
             "Excel workbook or the PDF report.",
             _obj({"kind": {"type": "string", "enum": ["tab", "issues", "report", "bulk", "excel", "pdf"]},
                   "tab": _ID,
                   "filters": {"type": "array", "items": _ID, "maxItems": 10},
                   "match": {"type": "string", "enum": ["any", "all"], "default": "any"},
                   "report_id": {"type": "string", "pattern": r"^[A-Za-z0-9_\-:]{1,120}$"},
                   "format": {"type": "string", "enum": ["csv", "xlsx"], "default": "xlsx"}}, ("kind",)),
             _export_link_tool),
    ToolSpec("start_crawl", "Start a crawl",
             "Starts a crawl of a public website (Spider mode follows links; List mode crawls the given URLs). "
             "Refused while another crawl runs; sign-in options are not available to agents.",
             _obj({"url": {"type": "string", "minLength": 1, "maxLength": 2000},
                   "mode": {"type": "string", "enum": ["spider", "list"], "default": "spider"},
                   "urls": {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 10000},
                   "max_urls": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 500},
                   "max_depth": {"type": "integer", "minimum": 0, "maximum": 100},
                   "render_all": {"type": "boolean"},
                   "profile": {"type": "string", "minLength": 1, "maxLength": 100}}, ("url",)),
             _start_crawl, write=True, reads_crawl=False, open_world=True),
    ToolSpec("stop_crawl", "Stop the crawl",
             "Stops the running crawl; what was crawled so far is kept and reported.", _obj({}), _stop_crawl,
             write=True, reads_crawl=False),
    ToolSpec("open_saved_crawl", "Open a saved crawl",
             "Opens a saved crawl in place of the loaded one, so every tool works on it. Refused while a crawl runs.",
             _obj({"id": {"type": "string", "minLength": 3, "maxLength": 300}}, ("id",)), _open_saved_crawl,
             write=True, reads_crawl=False),
    ToolSpec("validate_errors", "Re-check errors",
             "Re-checks the broken URLs of the finished crawl live (confirmed, not reproduced, fixed) and reports "
             "the verdicts; status_only shows the progress of a running re-check.",
             _obj({"wait_seconds": {"type": "integer", "minimum": 0, "maximum": 60, "default": 20},
                   "status_only": {"type": "boolean", "default": False}}), _validate_errors,
             write=True, reads_crawl=False, open_world=True),
    ToolSpec("generate_report", "Generate a report",
             "Builds the PDF issue report, the Excel workbook, the business insights PDF or import-ready fix files, "
             "and returns a short-lived download link.",
             _obj({"kind": {"type": "string", "enum": ["pdf", "excel", "business_pdf", "fix_files"]},
                   "format": {"type": "string", "enum": list(FIX_FORMATS), "default": "csv"},
                   "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 60, "default": 45}}, ("kind",)),
             _generate_report, write=True, open_world=True),
]
_BY_NAME = {t.name: t for t in TOOLS}


def list_tools(allow_write: bool) -> List[ToolSpec]:
    """The tools an agent sees: read tools always, write tools only when it may use them."""
    return [t for t in TOOLS if allow_write or not t.write]


def get_tool(name: str) -> Optional[ToolSpec]:
    return _BY_NAME.get(name)


def tool_schemas(allow_write: bool) -> List[Dict[str, Any]]:
    """[{"name", "description", "parameters"}] for function calling (the assistant's chat_tools)."""
    return [{"name": t.name, "description": t.description, "parameters": t.input_schema} for t in list_tools(allow_write)]


def validate_args(spec: ToolSpec, args: Any) -> Dict[str, Any]:
    """The arguments as a dict, checked against the tool's schema; ToolError with a plain message otherwise."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("The arguments must be an object.")
    try:
        from jsonschema import Draft202012Validator
    except ImportError:              # pragma: no cover (comes with mcp)
        return dict(args)
    errors = sorted(Draft202012Validator(spec.input_schema).iter_errors(args), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = ".".join(str(p) for p in e.path)
        if e.validator == "additionalProperties":
            extra = sorted(set(args) - set(spec.input_schema.get("properties") or {}))
            raise ToolError(f"Unknown argument{'s' if len(extra) != 1 else ''}: {', '.join(extra)}.")
        if e.validator == "required":
            missing = [r for r in e.validator_value if r not in (e.instance or {})]
            raise ToolError(f"Missing argument: {', '.join(missing)}.")
        raise ToolError(f"Argument {where or '(arguments)'}: {e.message}"[:300])
    return dict(args)


def requires_write(api: Any, name: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """True when this call changes something or reaches beyond the loaded crawl: a write tool, or find_subdomains
    on another domain (the assistant asks for a click first; MCP needs a write token)."""
    spec = get_tool(name)
    if spec is None:
        return False
    if spec.write:
        return True
    if name == "find_subdomains" and not (args or {}).get("report") and not (args or {}).get("job_id"):
        try:
            loaded = str(((current_call(api).status().get("stats") or {}).get("start_url")) or "")
        except ToolError:
            return True
        target = str((args or {}).get("url") or "").strip() or loaded
        return not loaded or _registrable(target) != _registrable(loaded)
    return False


def _result(ok: bool, data: Any = None, error: str = "", truncated: bool = False) -> Dict[str, Any]:
    return {"ok": ok, "data": data, "error": error, "truncated": truncated}


def call_tool(api: ApiClient, name: str, args: Optional[Dict[str, Any]], allow_write: bool, can_write: bool = True,
              share_private: bool = False) -> Dict[str, Any]:
    """Runs one tool: {"ok", "data", "error", "truncated"}. Never raises."""
    spec = get_tool(name)
    started = time.time()
    actor = str(getattr(api, "actor", "") or "")
    if spec is None:
        res = _result(False, error=f"Unknown tool: {str(name)[:80]}.")
        _audit({"actor": actor, "tool": str(name)[:80], "args": {}, "ok": False, "error": res["error"],
                "write": False, "at": started})
        return res
    ctx = CallContext(api=api, allow_write=bool(allow_write), can_write=bool(can_write),
                      share_private=bool(share_private))
    token = _CALL.set(ctx)
    clean: Dict[str, Any] = args if isinstance(args, dict) else {}
    try:
        clean = validate_args(spec, args)
        if spec.write and not allow_write:
            raise ToolError(WRITE_OFF_MSG, status=403)
        if spec.write and not can_write:
            raise ToolError(TOKEN_READ_ONLY_MSG, status=403)
        if spec.reads_crawl and not share_private and ctx.private():
            raise ToolError(PRIVATE_MSG, status=403)
        data = spec.handler(api, clean)
        data, truncated = fit(normalise(data), spec.max_chars)
        res = _result(True, data, truncated=truncated)
    except ToolError as e:
        res = _result(False, error=e.message)
        if e.retry_after:
            res["retry_after"] = e.retry_after
    except Exception as e:      # a bug must not leak a stack trace or crash the server
        res = _result(False, error=f"ARACHNIX hit an internal error while running {name} ({type(e).__name__}).")
    finally:
        _CALL.reset(token)
    _audit({"actor": actor, "tool": name, "args": safe_args(clean), "ok": res["ok"], "error": res["error"],
            "write": spec.write, "at": started})
    return res


# ---------------------------------------------------------------------------------------------
# Resources and prompts
# ---------------------------------------------------------------------------------------------
@dataclass
class ResourceSpec:
    uri: str
    name: str
    title: str
    description: str
    mime_type: str = "application/json"


RESOURCES: List[ResourceSpec] = [
    ResourceSpec("arachnix://crawl/summary", "crawl_summary", "Crawl summary",
                 "The loaded crawl's status, URL counts per status class and per tab (with check states)."),
    ResourceSpec("arachnix://crawl/issues", "crawl_issues", "Issues",
                 "The issues overview of the loaded crawl, with the checks that did not run."),
    ResourceSpec("arachnix://crawl/fix-plan", "crawl_fix_plan", "Fix plan",
                 "Issues grouped by root cause with plain fix steps."),
]


def read_resource(api: ApiClient, uri: str, share_private: bool = False) -> Dict[str, Any]:
    """{"ok", "data", "error", "truncated"} of one resource (the same rules as the tools)."""
    if uri == "arachnix://crawl/summary":
        status = call_tool(api, "crawl_status", {}, allow_write=False, share_private=share_private)
        if not status["ok"]:
            return status
        tabs = call_tool(api, "list_tabs", {}, allow_write=False, share_private=share_private)
        data = {"crawl": status["data"]}
        if tabs["ok"]:
            t = tabs["data"]
            data["checks_summary"] = t.get("checks_summary")
            data["tabs"] = [_compact(x, ("id", "name", "total", "counts", "state", "reason")) for x in t.get("tabs") or []]
            if t.get("state"):
                data["tabs_state"], data["tabs_reason"] = t.get("state"), t.get("reason")
        else:
            data["tabs"] = {"state": "not_checked", "reason": tabs["error"]}
        return _result(True, data, truncated=bool(status.get("truncated") or tabs.get("truncated")))
    if uri == "arachnix://crawl/issues":
        return call_tool(api, "issues_overview", {"limit": 100}, allow_write=False, share_private=share_private)
    if uri == "arachnix://crawl/fix-plan":
        return call_tool(api, "fix_plan", {"limit": 20}, allow_write=False, share_private=share_private)
    return _result(False, error=f"Unknown resource: {str(uri)[:200]}")


@dataclass
class PromptSpec:
    name: str
    title: str
    description: str
    arguments: List[Tuple[str, str, bool]]          # (name, description, required)
    render: Callable[[Dict[str, str]], str]


def _audit_prompt(a: Dict[str, str]) -> str:
    n = a.get("max_urls") or "500"
    return (f"Audit the website {a['url']} with ARACHNIX.\n"
            f"1. Call start_crawl with url {a['url']} and max_urls {n}. If start_crawl is not available or refused, "
            "say so and work with the crawl that is loaded (crawl_status).\n"
            "2. Call crawl_status every few seconds until finished is true.\n"
            "3. Read issues_overview and fix_plan.\n"
            "4. Write a short report for a website owner: the most important problems first, each with the number "
            "of URLs and one plain fix. Keep the numbers as ARACHNIX gives them (the status classes add up to the "
            "URLs crawled; a 404 is Broken 4xx). Call the page that links to a URL its Parent link. List the checks "
            "that were not checked or not connected with their reason, and never call them passed.\n"
            "5. End with a download link for the issues overview from export_link (kind issues).")


def _explain_prompt(a: Dict[str, str]) -> str:
    return (f"Explain the ARACHNIX finding {a['tab']} / {a['filter']} in plain language.\n"
            f"Call get_tab_rows with tab {a['tab']}, filters [{a['filter']}] and limit 10, then get_url_details on "
            "one or two of the URLs. Explain what is wrong, why it matters for search engines and visitors, and how "
            "to fix it, with the number of URLs affected. If the filter's state is not_checked or not_connected, "
            "explain why it did not run and what would make it run, instead of guessing a result. Call the page "
            "that links to a URL its Parent link.")


def _devplan_prompt(a: Dict[str, str]) -> str:
    n = a.get("limit") or "10"
    return (f"Write a fix plan for developers from ARACHNIX. Call fix_plan with limit {n}.\n"
            "Make one ticket per root cause, in the order given: a clear title, where to change it (template, "
            "setting or URLs), the steps, the sample URLs and how to verify the fix (re-crawl or validate_errors). "
            "Keep the URL counts exactly as ARACHNIX reports them, and mention any check that was not checked.")


PROMPTS: List[PromptSpec] = [
    PromptSpec("audit_site", "Audit a website",
               "Crawl a website with ARACHNIX and write a short, prioritised SEO report.",
               [("url", "The website to audit, e.g. https://www.example.com/", True),
                ("max_urls", "Most URLs to crawl (default 500)", False)], _audit_prompt),
    PromptSpec("explain_issue", "Explain an issue",
               "Explain one ARACHNIX filter (tab and filter id) in plain language with examples from the crawl.",
               [("tab", "Tab id, e.g. response_codes", True), ("filter", "Filter id, e.g. internal_client_error", True)],
               _explain_prompt),
    PromptSpec("developer_fix_plan", "Developer fix plan",
               "Turn ARACHNIX's fix plan into developer tickets.",
               [("limit", "How many root causes (default 10)", False)], _devplan_prompt),
]
_PROMPTS = {p.name: p for p in PROMPTS}


def get_prompt(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """{"description", "messages": [{"role": "user", "text"}]}; ToolError for an unknown prompt or a missing
    argument."""
    p = _PROMPTS.get(name)
    if p is None:
        raise ToolError(f"Unknown prompt: {str(name)[:80]}.")
    given = {str(k): str(v)[:500] for k, v in (args or {}).items() if v not in (None, "")}
    missing = [a for a, _d, req in p.arguments if req and not given.get(a)]
    if missing:
        raise ToolError(f"Missing argument: {', '.join(missing)}.")
    known = {a for a, _d, _r in p.arguments}
    extra = sorted(set(given) - known)
    if extra:
        raise ToolError(f"Unknown argument: {', '.join(extra)}.")
    if "max_urls" in given and not given["max_urls"].isdigit():
        raise ToolError("max_urls must be a whole number.")
    if "limit" in given and not given["limit"].isdigit():
        raise ToolError("limit must be a whole number.")
    return {"description": p.description, "messages": [{"role": "user", "text": p.render(given)}]}
