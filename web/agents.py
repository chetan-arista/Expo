"""
AI agents on the ARACHNIX server (Build 4 W1): the MCP endpoint at /mcp, its settings and tokens, single-purpose
export links, the crawl log of agent calls and the rules for crawls an agent starts.

web/app.py imports this module at its end and calls install(app); app.py's AccessKeyMiddleware, settings_editable,
_require_admin and the crawl-start route call the helpers below.

In-process requests (crawler/mcp/tools.py InProcessApi, used by the MCP tools and Ask ARACHNIX) carry the scope client
INTERNAL_CLIENT (never an IP address: never "this PC", never admin), the per-process secret INTERNAL_SECRET in the
x-slickup-internal header and the actor ("mcp:<token name>" | "assistant") in the scope state. The secret is accepted
only together with the marker; from the network the header is a wrong key (401 on a keyed server).

Endpoints (PLAN4_build.md 10.4, MCP group): GET / PUT /api/mcp, POST /api/mcp/tokens, DELETE /api/mcp/tokens/{id},
GET /api/mcp/client-config, GET /api/mcp/bridge, POST /api/mcp/test, POST /api/export/link, GET /api/export/link/{id}.
"""
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from crawler.mcp import server as mcp_server
from crawler.mcp import snippets as mcp_snippets
from crawler.mcp import tokens as mcp_tokens
from crawler.mcp import tools as mcp_tools

INTERNAL_SECRET = secrets.token_urlsafe(32)          # new at every server start, never written anywhere
INTERNAL_CLIENT = mcp_tools.INTERNAL_CLIENT
NO_STORE = {"Cache-Control": "no-store"}
EXPORT_LINK_TTL = 15 * 60
EXPORT_LINK_USES = 5
EXPORT_LINK_RE = re.compile(r"^/api/export/link/([A-Za-z0-9_\-]{20,80})$")
LOG_FOLD_S = 10.0
AGENT_HOST_MSG = ("AI agents may not crawl {host}: it points to {why}. An admin can allow agents on private "
                  "networks in Connections > AI agents (cloud-metadata and link-local addresses are never allowed).")

router = APIRouter()


def _app_module():
    import web.app as A
    return A


def reports_dir() -> Optional[str]:
    return getattr(_app_module(), "REPORTS_DIR", None)


def port() -> int:
    for name in ("CRAWLER_PORT", "PORT"):
        try:
            value = int(os.environ.get(name) or 0)
            if value:
                return value
        except ValueError:
            continue
    return 8000


def public_url() -> str:
    urls = mcp_server.public_urls(port())
    return urls[0] if urls else f"http://localhost:{port()}"


# ---------------------------------------------------------------------------------------------
# In-process requests and the actor
# ---------------------------------------------------------------------------------------------
def _header(scope: dict, name: bytes) -> str:
    for k, v in scope.get("headers") or ():
        if k.lower() == name:
            return v.decode("latin-1", "replace")
    return ""


def internal_state(scope: dict) -> str:
    """'internal' for a genuine in-process request, 'forged' for a request that claims to be one (the marker
    client without the secret, or the secret header from the network), '' for an ordinary request."""
    hdr = _header(scope, mcp_tools.INTERNAL_HEADER.encode())
    client = tuple(scope.get("client") or ())
    marker = client == tuple(INTERNAL_CLIENT)
    if marker and hdr and hmac.compare_digest(hdr, INTERNAL_SECRET) and \
            (scope.get("state") or {}).get("slickup_internal") is True:
        return "internal"
    if marker or hdr:
        return "forged"
    return ""


def actor(request: Request) -> str:
    """The agent behind an in-process request ("mcp:<token name>" | "assistant"), '' for everyone else."""
    if internal_state(request.scope) != "internal":
        return ""
    name = str((request.scope.get("state") or {}).get(mcp_tools.ACTOR_STATE) or "")
    return name if mcp_tools.ACTOR_RE.match(name) else "agent"


def is_agent(request: Request) -> bool:
    return bool(actor(request))


# ---------------------------------------------------------------------------------------------
# Same-origin rule (PLAN4_build.md 10.8)
# ---------------------------------------------------------------------------------------------
EXTENSION_PATHS = ("/api/extension/session",)


def same_origin_ok(request: Request) -> bool:
    """A state-changing request passes when it has no Origin (curl, a script), comes from the server's own
    origin, or from an origin listed in CRAWLER_CORS_ORIGINS (never '*'). chrome-extension:// origins pass only on
    the browser extension's routes. GET / HEAD / OPTIONS always pass."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if not origin or origin == "null" and internal_state(request.scope) == "internal":
        return True
    if origin.lower().startswith(("chrome-extension://", "moz-extension://")):
        return request.url.path.startswith(EXTENSION_PATHS)
    host = (request.headers.get("host") or "").strip().lower()
    if host and origin.lower() == f"{request.url.scheme}://{host}":
        return True
    allowed = [o.strip().rstrip("/").lower() for o in (os.environ.get("CRAWLER_CORS_ORIGINS") or "").split(",")
               if o.strip() and o.strip() != "*"]
    return origin.lower() in allowed


def require_same_origin(request: Request) -> None:
    if not same_origin_ok(request):
        raise HTTPException(status_code=403, detail="This change was sent from another website, so it was refused. "
                                                    "Make it in the ARACHNIX dashboard itself.")


# ---------------------------------------------------------------------------------------------
# The MCP endpoint
# ---------------------------------------------------------------------------------------------
_mcp: Optional[mcp_server.McpHttp] = None


def _get_api(actor_name: str):
    return mcp_tools.InProcessApi(_app_module().app, INTERNAL_SECRET, actor_name, public_url())


def mcp_settings() -> Dict[str, bool]:
    return mcp_tokens.load_settings(reports_dir())


def mcp_app() -> mcp_server.McpHttp:
    global _mcp
    if _mcp is None:
        _mcp = mcp_server.create_mcp_app(
            _get_api, mcp_settings, lambda token: mcp_tokens.verify_token(token, reports_dir()),
            lambda: mcp_server.default_allowed_hosts(port()))
    return _mcp


# ---------------------------------------------------------------------------------------------
# Crawl log of agent calls (identical read calls within 10 s fold into one line with a count)
# ---------------------------------------------------------------------------------------------
class _AgentLog:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending: Optional[Dict[str, Any]] = None
        self._timer: Optional[threading.Timer] = None

    @staticmethod
    def _emit(line: str) -> None:
        job = getattr(getattr(_app_module(), "session", None), "job", None)
        if job is not None:
            try:
                job.log(line)
                return
            except Exception:
                pass
        logging.getLogger("uvicorn.error").info(line)

    @staticmethod
    def _line(entry: Dict[str, Any], count: int = 1) -> str:
        who = str(entry.get("actor") or "agent")[:40]
        tool = str(entry.get("tool") or "?")[:60]
        args = entry.get("args") or {}
        try:
            text = json.dumps(args, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = "{}"
        if len(text) > 160:
            text = text[:157] + "..."
        what = f"{tool}({text})" if args else tool
        if count > 1:
            what += f" x{count}"
        outcome = "ok" if entry.get("ok") else "refused: " + " ".join(str(entry.get("error") or "").split())[:140]
        return f"AI agent {who}: {what} - {outcome}"

    def _flush_locked(self) -> Optional[str]:
        p, self._pending = self._pending, None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        return self._line(p["entry"], p["count"]) if p else None

    def _flush_later(self) -> None:
        with self._lock:
            line = self._flush_locked()
        if line:
            self._emit(line)

    def flush(self) -> None:
        self._flush_later()

    def __call__(self, entry: Dict[str, Any]) -> None:
        key = json.dumps([entry.get("actor"), entry.get("tool"), entry.get("args"), bool(entry.get("ok")),
                          entry.get("error")], default=str, sort_keys=True)
        now = time.time()
        out: List[str] = []
        with self._lock:
            p = self._pending
            if p and p["key"] == key and not entry.get("write") and now - p["at"] < LOG_FOLD_S:
                p["count"] += 1
                return
            line = self._flush_locked()
            if line:
                out.append(line)
            if entry.get("write"):
                out.append(self._line(entry))
            else:
                self._pending = {"key": key, "entry": dict(entry), "count": 1, "at": now}
                self._timer = threading.Timer(LOG_FOLD_S, self._flush_later)
                self._timer.daemon = True
                self._timer.start()
        for line in out:
            self._emit(line)


agent_log = _AgentLog()


# ---------------------------------------------------------------------------------------------
# Crawls started by agents
# ---------------------------------------------------------------------------------------------
def _host_refusal(url: str, allow_private: bool) -> str:
    from crawler.address_guard import METADATA_NAMES, resolve
    from crawler.subdomains import never_open
    from crawler.url_utils import is_internal_address
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return "That is not a web address."
    if host in METADATA_NAMES:
        return AGENT_HOST_MSG.format(host=host, why="a cloud-metadata service")
    try:
        ips = resolve(host, parts.port or (443 if parts.scheme == "https" else 80))
    except (OSError, UnicodeError):
        return ""                                  # unknown name: the crawl itself reports the DNS error
    for ip in ips:
        if never_open(ip, loopback_ok=allow_private):
            return AGENT_HOST_MSG.format(host=host, why="a link-local, reserved or cloud-metadata address")
        if not allow_private and is_internal_address(ip):
            return AGENT_HOST_MSG.format(host=host, why="a private network address or this server")
    return ""


def agent_crawl_rules(req: Any, request: Request) -> Tuple[str, List[str]]:
    """For a crawl an AI agent starts: refuses metadata / link-local start hosts always and private / loopback ones
    unless the admin allows agents on private networks (HTTP 400 with the reason); never replaces a running crawl
    (force off); Chrome rendering off unless private networks are allowed (a page's scripts could reach the
    server's own network from inside the browser). Returns (block_addresses mode, notes)."""
    settings = mcp_settings()
    allow_private = bool(settings.get("allow_private_network"))
    urls = [getattr(req, "url", "") or ""]
    if (getattr(req, "mode", "") or "").strip().lower() == "list":
        urls = [u for u in str(getattr(req, "urls", "") or "").splitlines() if u.strip()][:50]
    for u in urls:
        u = u.strip()
        if u and "://" not in u:
            u = "https://" + u
        why = _host_refusal(u, allow_private)
        if why:
            raise HTTPException(status_code=400, detail=why)
    notes: List[str] = []
    try:
        req.force = False
    except Exception:
        pass
    if not allow_private:
        for field in ("render_all", "use_dynamic_js"):
            if getattr(req, field, False):
                try:
                    setattr(req, field, False)
                except Exception:
                    continue
        notes.append(f"Started by the AI agent {actor(request)}: JavaScript rendering in Chrome is off for crawls "
                     "agents start (an admin can allow agents on private networks).")
    return ("metadata" if allow_private else "private"), notes


# ---------------------------------------------------------------------------------------------
# Export links (single-purpose download addresses for agents and tickets; never ?key=)
# ---------------------------------------------------------------------------------------------
_links: Dict[str, Dict[str, Any]] = {}
_links_lock = threading.Lock()


def _export_path(path: str) -> Optional[str]:
    path = (path or "").split("?", 1)[0]
    for allowed in mcp_tools.EXPORT_PATHS.values():
        pattern = "^" + re.escape(allowed).replace(re.escape("{report_id}"), r"[A-Za-z0-9_\-]{1,80}") + "$"
        if re.match(pattern, path):
            return path
    return None


def export_link_ok(path: str, method: str) -> bool:
    """AccessKeyMiddleware lets a GET of a valid, unused-up export link through without the key."""
    if method not in ("GET", "HEAD"):
        return False
    m = EXPORT_LINK_RE.match(path or "")
    if not m:
        return False
    with _links_lock:
        rec = _links.get(m.group(1))
        return bool(rec and rec["expires"] > time.time() and rec["uses"] < EXPORT_LINK_USES)


@router.post("/api/export/link")
async def create_export_link(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    path = _export_path(str(body.get("path") or ""))
    if path is None:
        raise HTTPException(status_code=400, detail="That download cannot be shared as a link. Allowed: "
                                                    + ", ".join(sorted(mcp_tools.EXPORT_PATHS)) + ".")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    params = {str(k)[:40]: str(v)[:500] for k, v in list(params.items())[:20]
              if str(k).lower() not in ("key", "session_id", "live_session_id")}
    link_id = secrets.token_urlsafe(32)
    now = time.time()
    with _links_lock:
        for k in [k for k, v in _links.items() if v["expires"] <= now]:
            _links.pop(k, None)
        while len(_links) >= 500:
            _links.pop(next(iter(_links)))
        _links[link_id] = {"path": path, "params": params, "expires": now + EXPORT_LINK_TTL, "uses": 0}
    return JSONResponse({"url": f"/api/export/link/{link_id}", "expires_in": EXPORT_LINK_TTL,
                         "uses": EXPORT_LINK_USES}, headers=NO_STORE)


async def _forward(path: str, params: Dict[str, str]) -> Tuple[int, List[Tuple[bytes, bytes]], bytes]:
    """One in-process GET of an export route (marker client, internal secret; never admin)."""
    app = _app_module().app
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": urlencode(params).encode(), "client": INTERNAL_CLIENT, "server": INTERNAL_CLIENT,
             "headers": [(b"host", b"slickup-internal"), (mcp_tools.INTERNAL_HEADER.encode(), INTERNAL_SECRET.encode()),
                         (mcp_tools.ACTOR_HEADER.encode(), b"export-link")],
             "state": {mcp_tools.ACTOR_STATE: "export-link", "slickup_internal": True}}
    status, headers, chunks = 500, [], []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status, headers = message["status"], list(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body") or b"")
    await app(scope, receive, send)
    return status, headers, b"".join(chunks)


@router.get("/api/export/link/{link_id}")
async def open_export_link(link_id: str):
    now = time.time()
    with _links_lock:
        rec = _links.get(link_id)
        if not rec or rec["expires"] <= now or rec["uses"] >= EXPORT_LINK_USES:
            raise HTTPException(status_code=410, detail="This download link has expired or was used up. Ask for a "
                                                        "new one.")
        rec["uses"] += 1
        path, params = rec["path"], dict(rec["params"])
    status, headers, body = await _forward(path, params)
    keep = [(k, v) for k, v in headers if k.lower() in (b"content-type", b"content-disposition",
                                                         b"x-content-type-options")]
    resp = Response(content=body, status_code=status)
    for k, v in keep:
        resp.headers[k.decode("latin-1")] = v.decode("latin-1")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------------------------
# MCP management endpoints
# ---------------------------------------------------------------------------------------------
def _admin(request: Request) -> None:
    _app_module()._require_admin(request)


def _mcp_answer(request: Optional[Request] = None) -> Dict[str, Any]:
    out = mcp_tokens.summary(reports_dir())
    out["url"] = public_url() + "/mcp"
    out["urls"] = [u + "/mcp" for u in mcp_server.public_urls(port())]
    out["tools"] = [{"name": t.name, "write": bool(t.write)} for t in mcp_tools.list_tools(True)]
    out["running"] = _mcp is not None and _mcp.running
    if request is not None:
        ok, why = _app_module().settings_editable(request)
        out["editable"], out["why"] = ok, why
    return out


@router.get("/api/mcp")
async def mcp_status(request: Request):
    return JSONResponse(_mcp_answer(request), headers=NO_STORE)


@router.put("/api/mcp")
async def mcp_update(request: Request):
    _admin(request)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Send the switches as a JSON object.")
    changes = {k: v for k, v in body.items() if k in mcp_tokens.SETTINGS}
    try:
        mcp_tokens.save_settings(changes, reports_dir())
    except mcp_tokens.TokenError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(_mcp_answer(request), headers=NO_STORE)


@router.post("/api/mcp/tokens")
async def mcp_token_create(request: Request):
    _admin(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    days = body.get("expires_days", mcp_tokens.DEFAULT_EXPIRES_DAYS)
    try:
        days = int(days) if days not in (None, "") else mcp_tokens.DEFAULT_EXPIRES_DAYS
        made = mcp_tokens.create_token(body.get("name"), can_write=bool(body.get("can_write")),
                                       expires_days=days or None, reports_dir=reports_dir())
    except (ValueError, mcp_tokens.TokenError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(made, headers=NO_STORE)


@router.delete("/api/mcp/tokens/{token_id}")
async def mcp_token_revoke(token_id: str, request: Request):
    _admin(request)
    if not mcp_tokens.revoke_token(token_id, reports_dir()):
        raise HTTPException(status_code=404, detail="No such token.")
    return {"ok": True}


@router.get("/api/mcp/client-config")
async def mcp_client_config(client: str = "claude_code", transport: str = "http"):
    try:
        cfg = mcp_snippets.client_config(client, public_url(), transport=transport,
                                         bridge_path="arachnix_bridge.py")
    except mcp_snippets.SnippetError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(cfg, headers=NO_STORE)


@router.get("/api/mcp/bridge")
async def mcp_bridge():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crawler", "mcp", "bridge.py")
    with open(path, "rb") as f:
        data = f.read()
    return Response(content=data, media_type="text/x-python",
                    headers={"Content-Disposition": 'attachment; filename="arachnix_bridge.py"'})


@router.post("/api/mcp/test")
async def mcp_test(request: Request):
    """Lists the tools through the MCP endpoint itself, in-process (never the request's Host). Without a token, a
    temporary read-only token is made for the test and revoked right after."""
    _admin(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = str((body or {}).get("token") or "").strip() if isinstance(body, dict) else ""
    temp_id = None
    if not mcp_settings().get("enabled"):
        return {"ok": False, "detail": "MCP is turned off: turn it on first, then test.", "tools": [],
                "write_tools": [], "protocol_version": None}
    if not token:
        made = mcp_tokens.create_token("connection test", can_write=False, expires_days=1, reports_dir=reports_dir())
        token, temp_id = made["token"], made["id"]
    try:
        from crawler.mcp.client import aprobe_asgi
        res = await aprobe_asgi(mcp_app().app, token, f"http://127.0.0.1:{port()}")
    finally:
        if temp_id:
            mcp_tokens.revoke_token(temp_id, reports_dir())
    keep = ("ok", "protocol_version", "server", "tools", "write_tools", "detail")
    return JSONResponse({k: res.get(k) for k in keep}, headers=NO_STORE)


# ---------------------------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------------------------
def install(app) -> None:
    """Adds the routes and /mcp to the FastAPI app and the audit hook to the tools (once)."""
    from starlette.routing import Route
    if getattr(app.state, "agents_installed", False):
        return
    app.include_router(router)
    app.router.routes.insert(0, Route("/mcp", endpoint=mcp_app().app, methods=["GET", "POST", "DELETE"]))
    mcp_tools.set_audit_hook(agent_log)
    app.state.agents_installed = True
