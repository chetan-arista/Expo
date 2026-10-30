#!/usr/bin/env python3
"""
ARACHNIX MCP bridge: lets an AI agent on your computer (Claude Desktop, Claude Code, VS Code, Cursor and other MCP
clients) use a ARACHNIX server.

It is a small local MCP server over stdio. Every request it gets (list tools, call a tool, read a resource, get a
prompt) is forwarded to the ARACHNIX server's /mcp endpoint with your MCP token, so the server decides what the agent
may do: MCP on or off, read-only or allowed to start crawls, token expiry and revocation, rate limits. Nothing is
stored on your computer.

Needs Python 3.11 or newer and the official MCP SDK only:   pip install "mcp>=2.3,<3"

Settings (environment variables):
  ARACHNIX_URL         the ARACHNIX server, e.g. http://10.10.6.56:8000 (default http://127.0.0.1:8000)
  ARACHNIX_MCP_TOKEN   an MCP token from ARACHNIX > Connections > AI agents (not the team access key)

Run:
  python arachnix_bridge.py            an MCP client starts it this way and talks to it over stdin / stdout
  python arachnix_bridge.py --check    tests the connection: lists the server's tools and exits (0 = OK)

This file is standalone (no other ARACHNIX code is needed). Messages for people go to stderr; stdout carries only
MCP messages.
"""
import argparse
import asyncio
import contextlib
import os
import re
import sys
from typing import Any, AsyncIterator, Callable, List, Optional

VERSION = "1.0"
DEFAULT_URL = "http://127.0.0.1:8000"
CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 180.0           # a tool may wait up to a minute for a crawl job, plus the server's own work


def _say(text: str) -> None:
    print(f"ARACHNIX bridge: {text}", file=sys.stderr, flush=True)


def endpoint(url: Optional[str]) -> str:
    """The /mcp address of a ARACHNIX server URL (http(s)://host[:port][/mcp])."""
    u = (url or DEFAULT_URL).strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+", u, re.I):
        raise ValueError(f"ARACHNIX_URL must start with http:// or https:// (got {u[:80]!r}).")
    if "@" in u.split("://", 1)[1].split("/", 1)[0]:
        raise ValueError("ARACHNIX_URL must not contain a user name or password.")
    return u if u.endswith("/mcp") else u + "/mcp"


def _leaves(exc: BaseException) -> List[BaseException]:
    """The real errors inside (nested) exception groups."""
    subs = getattr(exc, "exceptions", None)
    if isinstance(subs, (list, tuple)) and subs:
        out: List[BaseException] = []
        for e in subs:
            out += _leaves(e)
        return out
    return [exc]


def plain_error(exc: BaseException, url: str) -> str:
    """A message a person can act on, never a stack trace or the token."""
    try:
        from mcp.shared.exceptions import MCPError
    except ImportError:                                    # pragma: no cover
        MCPError = ()                                      # type: ignore
    leaves = _leaves(exc)
    for e in leaves:
        if MCPError and isinstance(e, MCPError):
            # the SDK's stand-ins for an HTTP error without a JSON-RPC body
            if e.code == -32601 and e.message == "Not Found":
                return (f"{url} has no MCP endpoint (HTTP 404). Check ARACHNIX_URL (the server's address, e.g. "
                        "http://10.10.6.56:8000) and that the server runs ARACHNIX Build 4 or later.")
            if e.message == "Server returned an error response":
                return (f"The server at {url} answered with an HTTP error. Check ARACHNIX_URL and that it is a "
                        "ARACHNIX server (Build 4 or later).")
            return str(e.message or "The ARACHNIX server refused the request.")
    for e in leaves:
        name = type(e).__name__
        text = str(e)
        if name in ("ConnectError", "ConnectTimeout") or isinstance(e, (ConnectionError, OSError)) and "connect" in text.lower():
            return (f"Cannot reach the ARACHNIX server at {url}. Check ARACHNIX_URL and that the server is running "
                    "and reachable from this computer.")
        if "Timeout" in name:
            return f"The ARACHNIX server at {url} did not answer in time. Try again."
        if name == "HTTPStatusError":
            code = getattr(getattr(e, "response", None), "status_code", "")
            if code == 404:
                return (f"{url} has no MCP endpoint. Check ARACHNIX_URL (the server's address, e.g. "
                        "http://10.10.6.56:8000) and that the server runs ARACHNIX Build 4 or later.")
            return f"The ARACHNIX server answered with HTTP {code}."
        if "Redirect" in text and "not followed" in text:
            return f"The ARACHNIX server at {url} redirected the request elsewhere. Use the exact address in ARACHNIX_URL."
    e = leaves[0] if leaves else exc
    return f"The bridge could not talk to ARACHNIX ({type(e).__name__})."


class Forwarder:
    """Opens a short connection to the server for every request, so a revoked token or MCP turned off takes effect
    at the next call and a restarted server needs no restart of the bridge."""

    def __init__(self, url: str, token: str):
        self.url = url
        self._token = token

    def __repr__(self) -> str:                             # never shows the token
        return f"Forwarder({self.url!r})"

    @contextlib.asynccontextmanager
    async def client(self) -> AsyncIterator[Any]:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp_types import Implementation
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {self._token}",
                                           "User-Agent": f"arachnix-bridge/{VERSION}"},
                                  timeout=httpx2.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT))
        async with http:
            async with Client(streamable_http_client(self.url, http_client=http),
                              client_info=Implementation(name="arachnix-bridge", version=VERSION)) as c:
                yield c

    async def run(self, fn: Callable[[Any], Any]) -> Any:
        async with self.client() as c:
            return await fn(c)


async def _all_pages(fetch: Callable[[Optional[str]], Any], attr: str) -> List[Any]:
    items: List[Any] = []
    cursor: Optional[str] = None
    for _page in range(50):
        got = await fetch(cursor)
        items += list(getattr(got, attr) or [])
        cursor = getattr(got, "next_cursor", None)
        if not cursor:
            break
    return items


def build_server(fwd: Forwarder) -> Any:
    """A stdio MCP server whose every handler forwards to the ARACHNIX server."""
    import mcp_types as types
    from mcp.server.lowlevel.server import Server
    from mcp.shared.exceptions import MCPError

    def refused(exc: BaseException) -> MCPError:
        msg = plain_error(exc, fwd.url)
        _say(msg)
        return MCPError(-32001, msg)

    async def list_tools(ctx: Any, params: Any) -> Any:
        try:
            tools = await fwd.run(lambda c: _all_pages(lambda cur: c.list_tools(cursor=cur, cache_mode="refresh"),
                                                       "tools"))
        except Exception as e:
            raise refused(e)
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx: Any, params: Any) -> Any:
        try:
            return await fwd.run(lambda c: c.call_tool(params.name, dict(params.arguments or {})))
        except Exception as e:
            msg = plain_error(e, fwd.url)
            _say(f"{params.name}: {msg}")
            return types.CallToolResult(content=[types.TextContent(text=msg)], is_error=True)

    async def list_resources(ctx: Any, params: Any) -> Any:
        try:
            items = await fwd.run(lambda c: _all_pages(
                lambda cur: c.list_resources(cursor=cur, cache_mode="refresh"), "resources"))
        except Exception as e:
            raise refused(e)
        return types.ListResourcesResult(resources=items)

    async def read_resource(ctx: Any, params: Any) -> Any:
        try:
            return await fwd.run(lambda c: c.read_resource(str(params.uri), cache_mode="refresh"))
        except Exception as e:
            raise refused(e)

    async def list_prompts(ctx: Any, params: Any) -> Any:
        try:
            items = await fwd.run(lambda c: _all_pages(
                lambda cur: c.list_prompts(cursor=cur, cache_mode="refresh"), "prompts"))
        except Exception as e:
            raise refused(e)
        return types.ListPromptsResult(prompts=items)

    async def get_prompt(ctx: Any, params: Any) -> Any:
        try:
            return await fwd.run(lambda c: c.get_prompt(params.name, dict(params.arguments or {})))
        except Exception as e:
            raise refused(e)

    return Server("arachnix", version=VERSION, title="ARACHNIX",
                  instructions="ARACHNIX SEO crawler (through the ARACHNIX bridge). Start with server_status.",
                  on_list_tools=list_tools, on_call_tool=call_tool, on_list_resources=list_resources,
                  on_read_resource=read_resource, on_list_prompts=list_prompts, on_get_prompt=get_prompt)


async def check(fwd: Forwarder) -> int:
    """--check: lists the tools once. 0 when the server answered, 1 otherwise (the reason on stderr)."""
    try:
        tools = await fwd.run(lambda c: _all_pages(lambda cur: c.list_tools(cursor=cur), "tools"))
    except Exception as e:
        _say(plain_error(e, fwd.url))
        return 1
    writes = [t.name for t in tools if not getattr(getattr(t, "annotations", None), "read_only_hint", True)]
    _say(f"connected to {fwd.url}: {len(tools)} tools"
         + (f", including {len(writes)} that change things ({', '.join(writes)})." if writes else " (read-only)."))
    return 0


async def serve(fwd: Forwarder) -> None:
    from mcp.server.stdio import stdio_server
    server = build_server(fwd)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="arachnix_bridge", description="ARACHNIX MCP bridge (stdio <-> ARACHNIX /mcp).")
    parser.add_argument("--check", action="store_true", help="test the connection and exit")
    parser.add_argument("--url", help="the ARACHNIX server (default: ARACHNIX_URL or http://127.0.0.1:8000)")
    parser.add_argument("--version", action="version", version=f"arachnix-bridge {VERSION}")
    args = parser.parse_args(argv)
    try:
        import mcp  # noqa: F401
    except ImportError:
        _say('the MCP SDK is not installed. Run:  pip install "mcp>=2.3,<3"')
        return 2
    try:
        url = endpoint(args.url or os.environ.get("ARACHNIX_URL"))
    except ValueError as e:
        _say(str(e))
        return 2
    token = (os.environ.get("ARACHNIX_MCP_TOKEN") or "").strip()
    if not token:
        if os.environ.get("CRAWLER_ACCESS_KEY"):
            _say("ARACHNIX_MCP_TOKEN is not set. The team access key does not work for AI agents: ask an admin for an "
                 "MCP token (ARACHNIX > Connections > AI agents) and set ARACHNIX_MCP_TOKEN.")
        else:
            _say("ARACHNIX_MCP_TOKEN is not set. Ask an admin for an MCP token (ARACHNIX > Connections > AI agents) "
                 "and set it in the environment of this bridge.")
        return 2
    fwd = Forwarder(url, token)
    if args.check:
        return asyncio.run(check(fwd))
    _say(f"forwarding to {url}")
    try:
        asyncio.run(serve(fwd))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
