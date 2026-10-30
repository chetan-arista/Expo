#!/usr/bin/env python3
"""
A small MCP test client: connects to an MCP server, lists its tools (and resources and prompts) and optionally calls
one tool. Used by the tests and by POST /api/mcp/test ("Test connection"); standalone like bridge.py (only the `mcp`
package), so it also runs on a PC without ARACHNIX.

  await aprobe_http("http://10.10.6.56:8000/mcp", token)                  Streamable HTTP with a bearer token
  await aprobe_stdio("python", ["arachnix_bridge.py"], env={...})            a stdio server such as the bridge
  await aprobe_asgi(mcp_http.app, token, "http://127.0.0.1:8000")           in-process, on the app's own event loop
  probe_http(...), probe_stdio(...)                                         the same from synchronous code

Each returns {"ok", "protocol_version", "server", "tools", "write_tools", "resources", "prompts", "call", "detail"}:
"write_tools" are the tools that do not declare themselves read-only; "call" is {"tool", "ok", "text",
"structured"} when a tool was called; "detail" is a plain sentence. Tokens are never printed or returned.

Command line:
  python client.py http http://127.0.0.1:8000/mcp [--call crawl_status [--args '{}']]   (token: ARACHNIX_MCP_TOKEN)
  python client.py stdio [--call ...] -- python arachnix_bridge.py
"""
import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

CallSpec = Optional[Tuple[str, Dict[str, Any]]]


def _leaves(exc: BaseException) -> List[BaseException]:
    subs = getattr(exc, "exceptions", None)
    if isinstance(subs, (list, tuple)) and subs:
        out: List[BaseException] = []
        for e in subs:
            out += _leaves(e)
        return out
    return [exc]


def plain_error(exc: BaseException) -> str:
    from mcp.shared.exceptions import MCPError
    leaves = _leaves(exc)
    for e in leaves:
        if isinstance(e, MCPError):
            if e.code == -32601 and e.message == "Not Found":
                return "There is no MCP endpoint at that address (HTTP 404)."
            if e.message == "Server returned an error response":
                return "The server answered with an HTTP error (is this an MCP endpoint?)."
            return str(e.message)
    e = leaves[0] if leaves else exc
    name = type(e).__name__
    if name in ("ConnectError", "ConnectTimeout") or isinstance(e, ConnectionError):
        return "Cannot reach the MCP server (connection refused or no route)."
    if "Timeout" in name or isinstance(e, TimeoutError):
        return "The MCP server did not answer in time."
    if isinstance(e, FileNotFoundError):
        return "The command to start the MCP server was not found."
    return f"The MCP connection failed ({name})."


async def _session(client: Any, call: CallSpec) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": True, "protocol_version": client.protocol_version, "server": "", "tools": [],
                           "write_tools": [], "resources": [], "prompts": [], "call": None, "detail": ""}
    info = client.server_info
    if info is not None:
        out["server"] = f"{info.name} {info.version}".strip()
    tools = (await client.list_tools()).tools
    out["tools"] = [t.name for t in tools]
    out["write_tools"] = [t.name for t in tools
                          if getattr(getattr(t, "annotations", None), "read_only_hint", None) is not True]
    caps = client.server_capabilities
    if caps is not None and caps.resources is not None:
        out["resources"] = [str(r.uri) for r in (await client.list_resources()).resources]
    if caps is not None and caps.prompts is not None:
        out["prompts"] = [p.name for p in (await client.list_prompts()).prompts]
    if call:
        name, args = call
        res = await client.call_tool(name, args or {})
        text = "\n".join(getattr(c, "text", "") for c in res.content if getattr(c, "type", "") == "text")
        out["call"] = {"tool": name, "ok": not res.is_error, "text": text, "structured": res.structured_content}
        if res.is_error:
            out["ok"] = False
            out["detail"] = f"{name} failed: {text[:300]}"
    if not out["detail"]:
        out["detail"] = (f"Connected ({out['protocol_version']}): {len(out['tools'])} tools"
                         + (f", {len(out['write_tools'])} of them change things." if out["write_tools"]
                            else ", all read-only."))
    return out


def _failed(exc: BaseException) -> Dict[str, Any]:
    return {"ok": False, "protocol_version": "", "server": "", "tools": [], "write_tools": [], "resources": [],
            "prompts": [], "call": None, "detail": plain_error(exc)}


async def aprobe_http(url: str, token: str = "", call: CallSpec = None, headers: Optional[Dict[str, str]] = None,
                      timeout: float = 30.0, transport: Any = None, mode: str = "auto") -> Dict[str, Any]:
    """Streamable HTTP. `headers` adds request headers (e.g. Origin); `transport` an httpx2 transport (ASGI)."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    kw: Dict[str, Any] = {"headers": hdrs, "timeout": httpx2.Timeout(timeout, read=max(timeout, 120.0))}
    if transport is not None:
        kw["transport"] = transport
    try:
        async with httpx2.AsyncClient(**kw) as http:
            async with Client(streamable_http_client(url, http_client=http), mode=mode) as client:
                return await _session(client, call)
    except Exception as e:
        return _failed(e)


async def aprobe_asgi(app: Any, token: str, base_url: str = "http://127.0.0.1", call: CallSpec = None,
                      path: str = "/mcp", headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The MCP app in-process (no network). Await it on the event loop that runs the app's lifespan. base_url sets
    the Host header, which must be one of the app's allowed hosts."""
    import httpx2
    transport = httpx2.ASGITransport(app=app)
    return await aprobe_http(base_url.rstrip("/") + path, token, call=call, headers=headers, transport=transport)


async def aprobe_stdio(command: str, args: Sequence[str] = (), env: Optional[Dict[str, str]] = None,
                       call: CallSpec = None, cwd: Optional[str] = None, timeout: float = 90.0) -> Dict[str, Any]:
    """A stdio server started as `command args...` with `env` added to a minimal environment."""
    from mcp import Client, StdioServerParameters
    params = StdioServerParameters(command=command, args=list(args), env=dict(env or {}) or None, cwd=cwd)
    try:
        async with asyncio.timeout(timeout):
            async with Client(params) as client:
                return await _session(client, call)
    except Exception as e:
        return _failed(e)


def probe_http(url: str, token: str = "", call: CallSpec = None, **kw: Any) -> Dict[str, Any]:
    return asyncio.run(aprobe_http(url, token, call=call, **kw))


def probe_stdio(command: str, args: Sequence[str] = (), env: Optional[Dict[str, str]] = None,
                call: CallSpec = None, **kw: Any) -> Dict[str, Any]:
    return asyncio.run(aprobe_stdio(command, args, env=env, call=call, **kw))


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="client.py", description="MCP test client: list tools, call one.")
    p.add_argument("transport", choices=["http", "stdio"])
    p.add_argument("url", nargs="?", default="")
    p.add_argument("--call", default="")
    p.add_argument("--args", default="{}")
    p.add_argument("--token-env", default="ARACHNIX_MCP_TOKEN", help="environment variable holding the token")
    a = p.parse_args(argv)
    try:
        call_args = json.loads(a.args or "{}")
    except ValueError:
        print("--args must be JSON", file=sys.stderr)
        return 2
    call = (a.call, call_args) if a.call else None
    if a.transport == "http":
        if not a.url:
            print("Give the MCP URL, e.g. http://127.0.0.1:8000/mcp", file=sys.stderr)
            return 2
        out = probe_http(a.url, os.environ.get(a.token_env, ""), call=call)
    else:
        if not command:
            print("Give the server command after --", file=sys.stderr)
            return 2
        out = probe_stdio(command[0], command[1:], env={k: v for k, v in os.environ.items()
                                                        if k.startswith("ARACHNIX_")}, call=call)
    print(json.dumps(out, indent=1, default=str))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
