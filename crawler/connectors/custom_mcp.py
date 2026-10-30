"""
Custom MCP servers (kind "mcp"): a remote MCP server (Streamable HTTP) whose tools the Ask ARACHNIX assistant may use.
Several can be added, each "custom_mcp:<slug>" (POST /api/connectors/custom-mcp or the template's connect).

  create(name, url, header_name="", header_value="", allow_private_network=False) -> Status   (admin; W2)
  list_tools(cid, refresh=False) -> [{name, title, description, input_schema, enabled, run_without_asking, host,
                                      help, changed, read_only_hint}]
  set_tools(cid, enabled, run_without_asking) -> list_tools(cid)                              (admin; W2)
  call_tool(cid, name, args, timeout=20) -> {"ok", "data", "error", "truncated"}             (the assistant, N10)
  enabled_tools() -> [{connector, name, host, description, input_schema, run_without_asking}] (the assistant)

Provider document (read 2026-10-28): the MCP specification, Streamable HTTP transport and tools
(https://modelcontextprotocol.io/specification, revisions 2025-06-18 and 2026-07-28), through the official Python SDK
(mcp 2.3.0, MIT): its Client with streamable_http_client over an httpx2 client we build ourselves.

Settings: the server address (a secret: hosted MCP addresses often carry a key in their path, so only its host is
ever shown), an optional auth header (name + value; the value is a secret, e.g. "Bearer <token>") and 'Allow private
network'. Safety:
- check_url before every request (cloud metadata and link-local always refused, private / loopback only when
  allowed) and each connection pinned to the address that was checked; redirects are not followed by httpx2 (the
  SDK follows same-origin ones itself, each checked again here) and a request to any other origin is refused;
- 20 s timeouts (connect, read, overall), answers capped at 1 MB, at most MAX_TOOLS tools listed;
- the admin enables tools one by one and marks each "Run without asking" or not (default: the assistant asks first);
  a tool whose description or arguments change after it was enabled is switched off until it is enabled again;
- tool descriptions and tool output are untrusted data: output comes back wrapped in a <connector_output> tag
  (like ai.wrap_page) and capped; messages carry the host only (masked: never the address path or the header).
"""
import asyncio
import copy
import hashlib
import json
import logging
import re
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .base import Connector, ConnectorError, Field, Masker, Status, TestResult, now_iso
from .registry import register

BASE_ID = "custom_mcp"
TIMEOUT = 20.0                 # connect / read / write, and the whole listing
MAX_CALL_TIMEOUT = 60.0
MAX_ANSWER = 1_000_000         # bytes of any one HTTP answer
MAX_RESULT_CHARS = 1_000_000   # characters of a tool result handed back
MAX_TOOLS = 100
MAX_PAGES = 5
MAX_DESCRIPTION = 600
MAX_SCHEMA_BYTES = 16_000
MAX_ARGS_BYTES = 100_000
MAX_SESSIONS = 4               # MCP sessions open at once (keeps memory and threads modest)
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.\-/]{1,128}$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
FORBIDDEN_HEADERS = {"host", "content-length", "content-type", "transfer-encoding", "connection", "upgrade",
                     "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id", "te", "trailer",
                     "keep-alive", "proxy-authorization", "expect", "cookie2"}
OUTPUT_TAG = "connector_output"
_TAGS = re.compile(r"(?i)<\s*/?\s*(connector_output|crawled_page|tool_result)")
_sessions = threading.BoundedSemaphore(MAX_SESSIONS)
CHANGED_NOTE = ("Its description or arguments changed since it was enabled, so it was switched off: check it and "
                "enable it again.")
# httpx2 logs every request URL at INFO and the SDK logs redirect targets: a filter on those loggers masks the
# secret server addresses and header values of the custom servers in use
SCRUBBED_LOGGERS = ("httpx2", "httpcore2.connection", "httpcore2.http11", "httpcore2.proxy", "mcp.client.client",
                    "mcp.client.streamable_http", "mcp.client.session", "mcp.client._probe",
                    "mcp.shared._httpx_utils")
_secret_lock = threading.Lock()
_secret_urls: Dict[str, None] = {}
_secret_values: Dict[str, None] = {}


class _ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        with _secret_lock:
            urls, values = list(_secret_urls), list(_secret_values)
        if not urls and not values:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = Masker(values, urls)(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


_SCRUB = _ScrubFilter()
for _name in SCRUBBED_LOGGERS:
    logging.getLogger(_name).addFilter(_SCRUB)


def _remember_secrets(url: str, values: List[str]) -> None:
    with _secret_lock:
        for d, items in ((_secret_urls, [url]), (_secret_values, values)):
            for v in items:
                if v:
                    d.pop(v, None)
                    d[v] = None
            while len(d) > 50:
                d.pop(next(iter(d)))


class CustomMcp(Connector):
    id = BASE_ID
    name = "Custom MCP server"
    kind = "mcp"
    icon = "mcp"
    multi = True
    max_instances = 10
    sends = ["tool_arguments"]
    description = ("Let Ask ARACHNIX use the tools of another MCP server (Streamable HTTP). You choose which tools it "
                   "may use; each one asks before it runs unless you mark it 'Run without asking'.")
    docs = ("https://modelcontextprotocol.io/specification (Streamable HTTP + tools; read 2026-10-28) through the "
            "official Python SDK mcp 2.3.0")
    fields = [
        Field("name", "Name", "text", required=True, help="A name for this server, e.g. 'Company wiki'."),
        Field("url", "Server address", "url", required=True, secret=True,
              help="The server's Streamable HTTP address, e.g. https://mcp.example.com/mcp. Only its host is shown "
                   "again (addresses often carry a key)."),
        Field("header_name", "Auth header name", "text", default="Authorization",
              help="The request header that carries the key, usually Authorization."),
        Field("header_value", "Auth header value", "password", secret=True,
              help="e.g. 'Bearer <token>'. Leave empty when the server needs no key."),
        Field("allow_private_network", "Allow private network", "bool", default=False,
              help="Allow a server inside your own network (http:// is then allowed too)."),
    ]

    # --- settings ---------------------------------------------------------------------------------
    def check(self, config: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, str]:
        from .http import check_url
        errors: Dict[str, str] = {}
        hname = str(config.get("header_name") or "").strip()
        if secrets.get("header_value") and not hname:
            errors["header_name"] = "Give the header name (usually Authorization)."
        if hname and (not HEADER_NAME.match(hname) or hname.lower() in FORBIDDEN_HEADERS):
            errors["header_name"] = "Use a plain header name such as Authorization or X-API-Key."
        url = secrets.get("url") or ""
        try:
            check_url(url, bool(config.get("allow_private_network")) or self.allow_private_default)
        except ConnectorError as e:
            errors["url"] = str(e)
        return errors

    def connect(self, config: Dict[str, Any], secrets: Dict[str, Any]) -> Status:
        if self.is_template():
            return super().connect(config, secrets)
        before = self.secret("url")
        st = super().connect(config, secrets)
        if before and self.secret("url") != before:
            # another server: what was listed and enabled for the old one does not apply
            self._set_meta(tools=None, enabled_tools=None, run_without_asking=None, fingerprints=None)
        return st

    def host(self) -> str:
        try:
            return (urlsplit(self.secret("url")).hostname or "").lower()
        except ValueError:
            return ""

    def host_label(self) -> str:
        try:
            p = urlsplit(self.secret("url"))
            host = (p.hostname or "").lower()
            if p.port and p.port not in (80, 443):
                return f"{host}:{p.port}"
            return host
        except ValueError:
            return ""

    def headers(self) -> Dict[str, str]:
        name = str(self.config().get("header_name") or "").strip()
        value = self.secret("header_value")
        return {name: value} if name and value else {}

    def masker(self) -> Masker:
        held = self._store().secrets(self.id)
        values = [held.get("header_value") or ""]
        hv = held.get("header_value") or ""
        if " " in hv:                      # "Bearer abc...": the token alone too
            values.append(hv.split(" ", 1)[1])
        return Masker([v for v in values if v], [held.get("url") or ""])

    def public_config(self) -> Dict[str, Any]:
        out = super().public_config()
        meta = self.meta()
        out["server_host"] = self.host_label()
        out["tools_listed"] = len((meta.get("tools") or {}).get("listed") or [])
        out["tools_enabled"] = len(meta.get("enabled_tools") or [])
        out["tools_listed_at"] = (meta.get("tools") or {}).get("listed_at") or ""
        out["server"] = (meta.get("tools") or {}).get("server") or ""
        return out

    # --- the MCP session ---------------------------------------------------------------------------
    def _session_run(self, work, timeout: float) -> Any:
        """Runs `work(client)` (async) inside one MCP session with the guarded transport. Raises ConnectorError
        (plain, masked messages)."""
        url = self.secret("url")
        if not url:
            raise ConnectorError("The server address is not set.", "invalid")
        from .http import check_url
        label = self.host_label() or "the server"
        check_url(url, self.allow_private())          # refuses before any connection is opened
        mask = self.masker()
        hv = self.secret("header_value")
        _remember_secrets(url, [hv, hv.split(" ", 1)[1] if " " in hv else ""])
        guard = _Guard(url, self.allow_private(), label)

        async def main():
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client
            transport = _GuardedTransport(guard)
            hdrs = dict(self.headers())
            hdrs["User-Agent"] = "ARACHNIX-Connectors/1.0"
            async with httpx2.AsyncClient(transport=transport, headers=hdrs, follow_redirects=False,
                                          timeout=httpx2.Timeout(TIMEOUT), trust_env=False) as http:
                with _deadline(timeout):
                    async with Client(streamable_http_client(url, http_client=http), mode="auto",
                                      read_timeout_seconds=timeout,
                                      client_info=_client_info()) as client:
                        return await work(client)

        if not _sessions.acquire(timeout=timeout):
            raise ConnectorError("Too many custom MCP calls are running; try again in a moment.", "other", label)
        try:
            return _run_async(main, timeout + 10)
        except ConnectorError as e:
            raise ConnectorError(mask(str(e)), e.kind, label) from None
        except BaseException as e:      # SDK / httpx2 / anyio errors (also grouped): kind + host only
            raise _plain_error(e, guard, label, mask) from None
        finally:
            _sessions.release()

    def fetch_tools(self) -> Dict[str, Any]:
        """Lists the server's tools live: {"tools": [...], "server", "protocol", "dropped"}."""
        async def work(client):
            out, cursor, dropped = [], None, 0
            for _ in range(MAX_PAGES):
                res = await client.list_tools(cursor=cursor) if cursor else await client.list_tools()
                for t in res.tools:
                    item = _tool_entry(t)
                    if item is None:
                        dropped += 1
                    elif len(out) < MAX_TOOLS:
                        out.append(item)
                    else:
                        dropped += 1
                cursor = getattr(res, "next_cursor", None)
                if not cursor:
                    break
            info = client.server_info
            server = f"{getattr(info, 'name', '') or ''} {getattr(info, 'version', '') or ''}".strip() if info else ""
            return {"tools": out, "server": _clip(server, 80), "protocol": str(client.protocol_version or ""),
                    "dropped": dropped}
        return self._session_run(work, TIMEOUT)

    def refresh_tools(self) -> Dict[str, Any]:
        """Lists the tools live and stores the listing. An enabled tool whose description or arguments changed is
        switched off (it must be enabled again)."""
        got = self.fetch_tools()
        meta = self.meta()
        fps = dict(meta.get("fingerprints") or {})
        enabled = [n for n in (meta.get("enabled_tools") or [])]
        rwa = [n for n in (meta.get("run_without_asking") or [])]
        by_name = {t["name"]: t for t in got["tools"]}
        changed: List[str] = []
        for n in list(enabled):
            t = by_name.get(n)
            if t is None:
                enabled.remove(n)
                rwa = [x for x in rwa if x != n]
                continue
            if fps.get(n) and fps[n] != t["fingerprint"]:
                enabled.remove(n)
                rwa = [x for x in rwa if x != n]
                changed.append(n)
        self._set_meta(tools={"listed": got["tools"], "listed_at": now_iso(), "server": got["server"],
                              "protocol": got["protocol"], "dropped": got["dropped"],
                              "changed": sorted(set(changed) | set((meta.get("tools") or {}).get("changed") or [])
                                                - set(enabled))},
                       enabled_tools=enabled, run_without_asking=rwa,
                       fingerprints={n: fps[n] for n in enabled if n in fps})
        return got

    def _test(self) -> TestResult:
        got = self.refresh_tools()
        n = len(got["tools"])
        en = len(self.meta().get("enabled_tools") or [])
        extra = f" ({got['dropped']} more not listed)" if got.get("dropped") else ""
        return TestResult(True, f"The MCP server at {self.host_label()} answered"
                                f"{(' (' + got['server'] + ')') if got['server'] else ''}: {n} tool"
                                f"{'s' if n != 1 else ''}{extra}, {en} enabled.")

    # --- tools ------------------------------------------------------------------------------------------
    def tool_list(self) -> List[Dict[str, Any]]:
        meta = self.meta()
        listing = (meta.get("tools") or {}).get("listed") or []
        enabled = set(meta.get("enabled_tools") or [])
        rwa = set(meta.get("run_without_asking") or [])
        changed = set((meta.get("tools") or {}).get("changed") or [])
        host = self.host_label()
        out = []
        for t in listing:
            d = {k: copy.deepcopy(t.get(k)) for k in ("name", "title", "description", "input_schema",
                                                       "read_only_hint", "destructive_hint")}
            d["enabled"] = t["name"] in enabled
            d["run_without_asking"] = t["name"] in rwa and t["name"] in enabled
            d["host"] = host
            d["help"] = f"When the assistant uses it, its arguments are sent to {host}."
            d["changed"] = t["name"] in changed and t["name"] not in enabled
            if d["changed"]:
                d["note"] = CHANGED_NOTE
            out.append(d)
        return out

    def set_tools(self, enabled: Any, run_without_asking: Any) -> List[Dict[str, Any]]:
        listing = {t["name"]: t for t in ((self.meta().get("tools") or {}).get("listed") or [])}
        if not listing:
            raise ConnectorError("List the server's tools first (Test).", "invalid")
        en = _names(enabled)
        rwa = _names(run_without_asking)
        unknown = [n for n in en + rwa if n not in listing]
        if unknown:
            raise ConnectorError(f"The server does not list a tool named {unknown[0][:60]!r}; list the tools again.",
                                 "invalid", errors={"enabled": "Unknown tool."})
        not_enabled = [n for n in rwa if n not in en]
        if not_enabled:
            raise ConnectorError(f"Enable {not_enabled[0][:60]!r} before marking it 'Run without asking'.", "invalid",
                                 errors={"run_without_asking": "Enable the tool first."})
        changed = [n for n in ((self.meta().get("tools") or {}).get("changed") or []) if n not in en]
        tools_meta = dict(self.meta().get("tools") or {}, changed=changed)
        self._set_meta(enabled_tools=en, run_without_asking=rwa, tools=tools_meta,
                       fingerprints={n: listing[n]["fingerprint"] for n in en})
        return self.tool_list()

    def call(self, name: str, args: Optional[Dict[str, Any]], timeout: float = TIMEOUT) -> Dict[str, Any]:
        """Calls one enabled tool: {"ok", "data" (wrapped as untrusted), "error", "truncated", "host", "chars"}."""
        host = self.host_label()
        if not self.is_set_up():
            return _result(False, error="This custom MCP server is not connected.", host=host)
        meta = self.meta()
        listing = {t["name"]: t for t in ((meta.get("tools") or {}).get("listed") or [])}
        if name not in set(meta.get("enabled_tools") or []) or name not in listing:
            return _result(False, error=f"The tool {str(name)[:60]!r} is not enabled for {self.name}. An admin can "
                                        "enable it in Connections.", host=host)
        args = args if isinstance(args, dict) else ({} if args is None else None)
        if args is None:
            return _result(False, error="The arguments must be an object.", host=host)
        try:
            raw = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            return _result(False, error="The arguments are not plain JSON.", host=host)
        if len(raw.encode("utf-8")) > MAX_ARGS_BYTES:
            return _result(False, error="The arguments are too large.", host=host)
        problem = _schema_problem(listing[name].get("input_schema"), args)
        if problem:
            return _result(False, error=problem, host=host)
        timeout = max(1.0, min(float(timeout or TIMEOUT), MAX_CALL_TIMEOUT))

        async def work(client):
            return await client.call_tool(name, args, read_timeout_seconds=timeout)

        try:
            res = self._session_run(work, timeout)
        except ConnectorError as e:
            self.record_delivery(False, str(e))
            return _result(False, error=str(e), host=host)
        text, truncated = _result_text(res)
        text = self.masker()(text)       # the server must not be able to echo our key into the assistant
        data = wrap_output(self.id, host, name, text)
        if getattr(res, "is_error", False):
            return _result(False, data=data, error=f"The tool {name[:60]!r} on {host} reported an error (see its "
                                                   "output).", truncated=truncated, host=host, chars=len(text))
        return _result(True, data=data, truncated=truncated, host=host, chars=len(text))


# ---------------------------------------------------------------------------------------------
# Module functions (the contract of PLAN4_build.md 10.2)
# ---------------------------------------------------------------------------------------------
def _get(cid: str) -> CustomMcp:
    from . import registry
    c = registry.get(cid)
    if not isinstance(c, CustomMcp) or c.is_template():
        raise ConnectorError("No such custom MCP server.", "invalid")
    return c


def create(name: str, url: str, header_name: str = "", header_value: str = "",
           allow_private_network: bool = False) -> Status:
    """POST /api/connectors/custom-mcp: a new instance (its id is in Status.config["id"])."""
    from . import registry
    tmpl = registry.get(BASE_ID)
    config = {"name": name, "header_name": header_name or ("Authorization" if header_value else ""),
              "allow_private_network": bool(allow_private_network)}
    secrets = {"url": url, "header_value": header_value or ""}
    return tmpl.connect(config, secrets)


def list_tools(cid: str, refresh: bool = False) -> List[Dict[str, Any]]:
    """The tools of one server with their enabled / run_without_asking state. refresh=True (or no listing yet)
    asks the server (ConnectorError when it cannot be reached)."""
    c = _get(cid)
    if refresh or not (c.meta().get("tools") or {}).get("listed"):
        if not c.is_set_up():
            return []
        c.refresh_tools()
    return c.tool_list()


def set_tools(cid: str, enabled: Any, run_without_asking: Any) -> List[Dict[str, Any]]:
    """PUT /api/connectors/{id}/tools {enabled: [names], run_without_asking: [names]}."""
    return _get(cid).set_tools(enabled, run_without_asking)


def call_tool(cid: str, name: str, args: Optional[Dict[str, Any]], timeout: float = TIMEOUT) -> Dict[str, Any]:
    """Calls one enabled tool. Never raises: {"ok", "data", "error", "truncated"} (+ "host", "chars")."""
    try:
        c = _get(cid)
    except ConnectorError as e:
        return _result(False, error=str(e))
    try:
        return c.call(name, args, timeout)
    except Exception as e:          # a bug must not leak an address or a key
        return _result(False, error=f"The custom tool call failed unexpectedly ({type(e).__name__}).",
                       host=c.host_label())


def servers() -> List[CustomMcp]:
    from . import registry
    return [c for c in registry.instances(BASE_ID) if isinstance(c, CustomMcp)]


def enabled_tools() -> List[Dict[str, Any]]:
    """Every enabled tool of every connected custom server (for the assistant)."""
    out = []
    for c in servers():
        try:
            if c.status().state not in ("connected", "error"):
                continue
            for t in c.tool_list():
                if t["enabled"]:
                    out.append({"connector": c.id, "server_name": c.name, "name": t["name"], "host": t["host"],
                                "title": t.get("title") or "", "description": t.get("description") or "",
                                "input_schema": t.get("input_schema") or {"type": "object"},
                                "run_without_asking": bool(t["run_without_asking"])})
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def _client_info():
    try:
        from mcp_types import Implementation
        return Implementation(name="ARACHNIX", version="4")
    except Exception:
        return None


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= n else s[:n - 3] + "..."


def _names(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ConnectorError("Give a list of tool names.", "invalid")
    out: List[str] = []
    for v in value:
        s = str(v or "").strip()
        if s and s not in out:
            out.append(s[:128])
    return out[:MAX_TOOLS]


def _clean_schema(schema: Any) -> Tuple[Dict[str, Any], bool]:
    """The tool's input schema as a JSON object schema (capped; a schema that is too large or not an object
    schema becomes {"type": "object"})."""
    if not isinstance(schema, dict):
        return {"type": "object"}, True
    try:
        raw = json.dumps(schema, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"type": "object"}, True
    if len(raw.encode("utf-8")) > MAX_SCHEMA_BYTES:
        return {"type": "object"}, True
    out = json.loads(raw)
    if out.get("type") not in (None, "object"):
        return {"type": "object"}, True
    out["type"] = "object"
    return out, False


def _tool_entry(t: Any) -> Optional[Dict[str, Any]]:
    name = str(getattr(t, "name", "") or "")
    if not TOOL_NAME.match(name):
        return None
    schema, trimmed = _clean_schema(getattr(t, "input_schema", None) or getattr(t, "inputSchema", None))
    ann = getattr(t, "annotations", None)
    entry = {"name": name, "title": _clip(getattr(t, "title", "") or getattr(ann, "title", "") or "", 120),
             "description": _clip(getattr(t, "description", "") or "", MAX_DESCRIPTION),
             "input_schema": schema, "schema_trimmed": trimmed,
             "read_only_hint": getattr(ann, "read_only_hint", None) if ann is not None else None,
             "destructive_hint": getattr(ann, "destructive_hint", None) if ann is not None else None}
    entry["fingerprint"] = hashlib.sha256(json.dumps([entry["description"], schema], sort_keys=True,
                                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
    return entry


def _schema_problem(schema: Any, args: Dict[str, Any]) -> str:
    """A plain message when the arguments do not match the tool's schema ("" when they do, or when the server's
    schema itself cannot be used for checking)."""
    if not isinstance(schema, dict):
        return ""
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        return ""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return ""
    except Exception:
        return ""
    try:
        errors = sorted(Draft202012Validator(schema).iter_errors(args), key=lambda e: list(e.path))
    except Exception:
        return ""
    if not errors:
        return ""
    e = errors[0]
    where = ".".join(str(p) for p in e.path)
    return _clip(f"Argument {where or '(arguments)'} does not match the tool: {e.message}", 300)


def wrap_output(cid: str, host: str, tool: str, text: str) -> str:
    """Tool output as clearly delimited, untrusted DATA (the output cannot close the tag)."""
    text = _TAGS.sub("[tag removed]", str(text or ""))
    attr = lambda s: re.sub(r"[^A-Za-z0-9_.:/\-]", "_", str(s or ""))[:140]    # noqa: E731
    return (f'<{OUTPUT_TAG} source="{attr(cid)}" host="{attr(host)}" tool="{attr(tool)}">\n{text}\n'
            f"</{OUTPUT_TAG}>")


def _result_text(res: Any) -> Tuple[str, bool]:
    """The text of a CallToolResult (text parts; text resources; else the structured content as JSON), capped."""
    parts: List[str] = []
    for c in getattr(res, "content", None) or []:
        kind = getattr(c, "type", "")
        if kind == "text":
            parts.append(str(getattr(c, "text", "") or ""))
        elif kind == "resource":
            r = getattr(c, "resource", None)
            if getattr(r, "text", None) is not None:
                parts.append(str(r.text))
            else:
                parts.append("[binary resource omitted]")
        elif kind == "resource_link":
            parts.append(f"[link: {getattr(c, 'uri', '')}]")
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} omitted]")
    structured = getattr(res, "structured_content", None)
    if not parts and structured is not None:
        try:
            parts.append(json.dumps(structured, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            parts.append(str(structured))
    text = "\n".join(parts)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + "\n[...truncated]", True
    return text, False


def _result(ok: bool, data: Any = None, error: str = "", truncated: bool = False, host: str = "",
            chars: int = 0) -> Dict[str, Any]:
    return {"ok": ok, "data": data, "error": error, "truncated": truncated, "host": host, "chars": chars}


def _run_async(factory, timeout: float) -> Any:
    """Runs the coroutine `factory()` to the end from synchronous code (in a worker thread when this thread
    already runs an event loop)."""
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if not in_loop:
        return asyncio.run(factory())
    box: Dict[str, Any] = {}

    def runner():
        try:
            box["value"] = asyncio.run(factory())
        except BaseException as e:      # handed back to the caller's thread
            box["error"] = e

    t = threading.Thread(target=runner, name="custom-mcp", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise ConnectorError("The MCP server did not answer in time.", "timeout")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class _deadline:
    """An overall time limit for the session (anyio cancel scope)."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._scope = None

    def __enter__(self):
        import anyio
        self._scope = anyio.fail_after(self.seconds)
        return self._scope.__enter__()

    def __exit__(self, *exc):
        return self._scope.__exit__(*exc)


class _Guard:
    """What the transport enforces, and what went wrong (for a plain message)."""

    def __init__(self, url: str, allow_private: bool, label: str):
        p = urlsplit(url)
        self.scheme = p.scheme.lower()
        self.host = (p.hostname or "").lower()
        self.port = p.port or (443 if self.scheme == "https" else 80)
        self.allow_private = allow_private
        self.label = label
        self.problem: Optional[ConnectorError] = None
        self.status: int = 0


def _proxy_for(scheme: str) -> str:
    try:
        return urllib.request.getproxies().get(scheme, "") or ""
    except Exception:
        return ""


def _make_transport():
    import httpx2

    class GuardedTransport(httpx2.AsyncBaseTransport):
        """Checks every request's address (check_url), refuses other origins, pins the connection to the checked
        address and caps each answer at MAX_ANSWER bytes."""

        def __init__(self, guard: _Guard):
            self.guard = guard
            proxy = _proxy_for(guard.scheme)
            self.pinned = not proxy
            self.inner = httpx2.AsyncHTTPTransport(retries=0, proxy=proxy or None, trust_env=False)

        async def handle_async_request(self, request):
            import anyio
            from .http import check_url
            g = self.guard
            u = request.url
            port = u.port or (443 if u.scheme == "https" else 80)
            if (u.scheme, (u.host or "").lower(), port) != (g.scheme, g.host, g.port):
                g.problem = ConnectorError("The MCP server sent us to another site; ARACHNIX does not follow it.",
                                           "redirect", g.label)
                raise httpx2.ConnectError("refused: another origin", request=request)
            try:
                ips = await anyio.to_thread.run_sync(check_url, str(u), g.allow_private)
            except ConnectorError as e:
                g.problem = e
                raise httpx2.ConnectError("refused address", request=request)
            send = request
            if self.pinned:
                ip = ips[0]
                ext = dict(request.extensions)
                if u.scheme == "https":
                    ext["sni_hostname"] = u.host
                send = httpx2.Request(request.method, u.copy_with(host=ip), headers=request.headers,
                                      stream=request.stream, extensions=ext)
            resp = await self.inner.handle_async_request(send)
            g.status = resp.status_code
            return httpx2.Response(status_code=resp.status_code, headers=resp.headers,
                                   stream=_capped(resp.stream, g), extensions=resp.extensions, request=request)

        async def aclose(self):
            await self.inner.aclose()

    return GuardedTransport


def _capped(stream, guard: _Guard):
    import httpx2

    class Capped(httpx2.AsyncByteStream):
        async def __aiter__(self):
            total = 0
            async for chunk in stream:
                total += len(chunk)
                if total > MAX_ANSWER:
                    guard.problem = ConnectorError(f"{guard.label} sent more than {MAX_ANSWER // 1000:,} KB in one "
                                                   "answer; it was not read.", "too_large", guard.label)
                    raise httpx2.ReadError("answer too large")
                yield chunk

        async def aclose(self):
            await stream.aclose()

    return Capped()


_TRANSPORT_CLASS: Dict[str, Any] = {}


def _GuardedTransport(guard: _Guard):
    cls = _TRANSPORT_CLASS.get("cls")
    if cls is None:
        cls = _TRANSPORT_CLASS["cls"] = _make_transport()
    return cls(guard)


def _leaves(exc: BaseException) -> List[BaseException]:
    subs = getattr(exc, "exceptions", None)
    if isinstance(subs, (list, tuple)) and subs:
        out: List[BaseException] = []
        for e in subs:
            out += _leaves(e)
        return out
    return [exc]


def _plain_error(exc: BaseException, guard: _Guard, label: str, mask) -> ConnectorError:
    """A ConnectorError from an SDK / httpx2 / anyio error: the kind and the host (plus, for an error the MCP server
    answered itself, its short message, masked)."""
    if guard.problem is not None:
        return ConnectorError(mask(str(guard.problem)), guard.problem.kind, label)
    leaves = _leaves(exc)
    for e in leaves:
        if isinstance(e, ConnectorError):
            return ConnectorError(mask(str(e)), e.kind, label)
    try:
        from mcp.shared.exceptions import MCPError
    except Exception:          # pragma: no cover
        MCPError = ()          # type: ignore
    for e in leaves:
        if MCPError and isinstance(e, MCPError):
            msg = str(getattr(e, "message", "") or "")
            if guard.status in (401, 403):
                return ConnectorError(f"{label} refused the sign-in (HTTP {guard.status}): check the auth header.",
                                      "http", label, status=guard.status)
            if guard.status == 404:
                return ConnectorError(f"There is no MCP endpoint at that address on {label} (HTTP 404).", "http",
                                      label, status=404)
            if guard.status >= 400:
                return ConnectorError(f"{label} answered HTTP {guard.status}.", "http", label, status=guard.status)
            if "location" in msg.lower() or "redirect" in msg.lower():
                return ConnectorError("The MCP server sent us to another address; ARACHNIX does not follow it.",
                                      "redirect", label)
            return ConnectorError(f"The MCP server at {label} answered with an error: {_clip(mask(msg), 200)}",
                                  "http", label)
    e = leaves[0] if leaves else exc
    name = type(e).__name__
    if isinstance(e, TimeoutError) or "Timeout" in name or name in ("TooSlowError", "CancelledError"):
        return ConnectorError(f"{label} did not answer in time.", "timeout", label)
    if guard.status in (401, 403):
        return ConnectorError(f"{label} refused the sign-in (HTTP {guard.status}): check the auth header.", "http",
                              label, status=guard.status)
    if guard.status == 404:
        return ConnectorError(f"There is no MCP endpoint at that address on {label} (HTTP 404).", "http", label,
                              status=404)
    if guard.status >= 400:
        return ConnectorError(f"{label} answered HTTP {guard.status}.", "http", label, status=guard.status)
    if name in ("ConnectError", "ConnectTimeout") or isinstance(e, ConnectionError):
        text = str(e).lower()
        if "ssl" in text or "certificate" in text:
            return ConnectorError(f"The secure connection to {label} failed (TLS certificate or protocol problem).",
                                  "ssl", label)
        return ConnectorError(f"Could not connect to {label} (refused, or nothing is listening there).", "refused",
                              label)
    if name in ("ValidationError",):
        return ConnectorError(f"{label} sent an answer that is not valid MCP.", "other", label)
    return ConnectorError(f"The MCP connection to {label} failed ({name}).", "other", label)


register(CustomMcp())
