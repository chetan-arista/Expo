"""
Copy-paste configs that connect an AI agent to ARACHNIX (GET /api/mcp/client-config, the "Connect an AI agent" panel):

  client_config(client, base_url, transport="http", token="", bridge_path="", python="python")
    -> {"client", "transport", "file", "snippet", "steps", "notes", "connection"}

  claude_desktop  claude_desktop_config.json starting the stdio bridge (that file starts local programs only)
  claude_code     `claude mcp add --transport http ...` with the bearer header, or the bridge form
  vscode          .vscode/mcp.json: the token is asked with an `inputs` password prompt, never written in the file
  cursor          the user-level ~/.cursor/mcp.json (never the project's file, which can be committed)
  stdio           any MCP client: the bridge's command line and environment

base_url is the server's address for people (W1 takes it from the allowed-hosts list, LAN address first, never from a
request's Host header). The token stays a placeholder unless the caller passes one that was just created.
"connection" describes the same server for a program (tests, Test connection): {"transport": "http", "url",
"headers"} or {"transport": "stdio", "command", "args", "env"}.
"""
import ipaddress
import json
import re
from typing import Any, Dict, List
from urllib.parse import urlsplit

CLIENTS = ("claude_desktop", "claude_code", "vscode", "cursor", "stdio")
TRANSPORTS = ("http", "stdio")
SERVER_NAME = "arachnix"
TOKEN_PLACEHOLDER = "<paste your MCP token>"
BRIDGE_PLACEHOLDER = "C:\\path\\to\\arachnix_bridge.py"
VSCODE_INPUT = "arachnix-token"
PIP_LINE = 'pip install "mcp>=2.3,<3"'


class SnippetError(ValueError):
    """Unknown client or transport, or a base URL that is not http(s)."""


def _base(base_url: str) -> str:
    u = (base_url or "").strip().rstrip("/")
    if u.endswith("/mcp"):
        u = u[:-4]
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        raise SnippetError("The server address must be http:// or https:// with a host (no user name or password).")
    return u


def _public_https(base: str) -> bool:
    """A public HTTPS address (Claude's cloud connectors could reach it); LAN, localhost and http are not."""
    parts = urlsplit(base)
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    if host in ("localhost",) or "." not in host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return not host.endswith((".local", ".lan", ".internal", ".home"))


def _q(text: str) -> str:
    """A command-line argument, quoted when it has spaces or quotes."""
    if re.search(r'[\s"\'&|<>^]', text):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _bridge_steps(base: str, bridge: str, python: str) -> List[str]:
    return [f"Install Python 3.11 or newer, then run: {PIP_LINE}",
            f"Download the bridge from Connections > AI agents (Download bridge) and save it as {bridge} "
            "(any folder; use its full path below).",
            f"Test it: set ARACHNIX_URL={base} and ARACHNIX_MCP_TOKEN=<your token>, then run: "
            f"{python} {_q(bridge)} --check"]


def client_config(client: str, base_url: str, transport: str = "http", token: str = "", bridge_path: str = "",
                  python: str = "python") -> Dict[str, Any]:
    if client not in CLIENTS:
        raise SnippetError(f"Unknown client. Use one of: {', '.join(CLIENTS)}.")
    if transport not in TRANSPORTS:
        raise SnippetError("Unknown transport. Use http or stdio.")
    base = _base(base_url)
    mcp_url = base + "/mcp"
    tok = (token or "").strip() or TOKEN_PLACEHOLDER
    bridge = (bridge_path or "").strip() or BRIDGE_PLACEHOLDER
    py = (python or "python").strip()
    env = {"ARACHNIX_URL": base, "ARACHNIX_MCP_TOKEN": tok}
    stdio_conn = {"transport": "stdio", "command": py, "args": [bridge], "env": dict(env)}
    http_conn = {"transport": "http", "url": mcp_url, "headers": {"Authorization": f"Bearer {tok}"}}
    notes: List[str] = []
    if token:
        notes.append("This snippet contains your token: keep it private and do not commit it.")

    if client == "claude_desktop":
        if transport == "http":
            notes.append("Claude Desktop's config file starts local programs only, so it connects through the "
                         "ARACHNIX bridge.")
        transport = "stdio"
        snippet = _json({"mcpServers": {SERVER_NAME: {"command": py, "args": [bridge], "env": env}}})
        steps = _bridge_steps(base, bridge, py) + [
            "Open Claude Desktop > Settings > Developer > Edit Config. That opens claude_desktop_config.json "
            "(Windows: %APPDATA%\\Claude\\claude_desktop_config.json; macOS: ~/Library/Application Support/Claude/"
            "claude_desktop_config.json).",
            "Add the \"arachnix\" entry below under \"mcpServers\" (keep any other servers), put in the bridge's full "
            "path and your token, and save.",
            "Quit Claude Desktop completely and open it again; ARACHNIX's tools appear under the tools (search and "
            "tools) menu."]
        if not _public_https(base):
            notes.append(f"Claude's custom connectors (Settings > Connectors) connect from Anthropic's cloud and need "
                         f"a public HTTPS address with sign-in, so they cannot reach {base}. Use this local bridge.")
        return {"client": client, "transport": transport, "file": "claude_desktop_config.json", "snippet": snippet,
                "steps": steps, "notes": notes, "connection": stdio_conn}

    if client == "claude_code":
        if transport == "http":
            snippet = (f"claude mcp add --transport http {SERVER_NAME} {mcp_url} "
                       f"--header {_q('Authorization: Bearer ' + tok)}")
            conn: Dict[str, Any] = http_conn
            steps = ["Run this in a terminal, in the folder of the project where you use Claude Code.",
                     "Keep the default scope (local: only you, this project). Do not add --scope project: that "
                     "writes .mcp.json, which is usually committed, with your token in it.",
                     "Check with: claude mcp list (or /mcp inside Claude Code)."]
        else:
            # the name before the --env values ("--env" takes several; "--" ends them and starts the command)
            snippet = (f"claude mcp add --transport stdio {SERVER_NAME} --env ARACHNIX_URL={base} "
                       f"--env {_q('ARACHNIX_MCP_TOKEN=' + tok)} -- {py} {_q(bridge)}")
            conn = stdio_conn
            steps = _bridge_steps(base, bridge, py) + [
                "Run the command below in a terminal, in your project's folder (default scope: only you).",
                "Check with: claude mcp list (or /mcp inside Claude Code)."]
        return {"client": client, "transport": transport, "file": "", "snippet": snippet, "steps": steps,
                "notes": notes, "connection": conn}

    if client == "vscode":
        inputs = [{"type": "promptString", "id": VSCODE_INPUT, "description": "ARACHNIX MCP token", "password": True}]
        ref = "${input:" + VSCODE_INPUT + "}"
        if transport == "http":
            server = {"type": "http", "url": mcp_url, "headers": {"Authorization": f"Bearer {ref}"}}
            conn = http_conn
        else:
            server = {"type": "stdio", "command": py, "args": [bridge], "env": {"ARACHNIX_URL": base,
                                                                              "ARACHNIX_MCP_TOKEN": ref}}
            conn = stdio_conn
        snippet = _json({"inputs": inputs, "servers": {SERVER_NAME: server}})
        steps = ([] if transport == "http" else _bridge_steps(base, bridge, py)) + [
            "In your project, create .vscode/mcp.json with the content below (or add the \"arachnix\" server and the "
            "input to the file you have).",
            "Click Start above the server in that file. VS Code asks for the token once and keeps it in its secret "
            "storage, so the file never contains it and is safe to commit.",
            "Open Chat, switch to Agent mode and pick ARACHNIX's tools from the tools button."]
        notes = [n for n in notes if "contains your token" not in n]
        return {"client": client, "transport": transport, "file": ".vscode/mcp.json", "snippet": snippet,
                "steps": steps, "notes": notes, "connection": conn}

    if client == "cursor":
        if transport == "http":
            server = {"url": mcp_url, "headers": {"Authorization": f"Bearer {tok}"}}
            conn = http_conn
        else:
            server = {"command": py, "args": [bridge], "env": env}
            conn = stdio_conn
        snippet = _json({"mcpServers": {SERVER_NAME: server}})
        steps = ([] if transport == "http" else _bridge_steps(base, bridge, py)) + [
            "Open your user-level Cursor MCP file: ~/.cursor/mcp.json (Windows: %USERPROFILE%\\.cursor\\mcp.json), "
            "or Cursor Settings > MCP > Add new global MCP server.",
            "Add the \"arachnix\" entry below under \"mcpServers\" and put in your token. Use the user-level file, "
            "never the project's .cursor/mcp.json: that one can be committed with the token in it.",
            "Cursor Settings > MCP should show ARACHNIX with its tools (turn it on if needed)."]
        return {"client": client, "transport": transport, "file": "~/.cursor/mcp.json", "snippet": snippet,
                "steps": steps, "notes": notes, "connection": conn}

    # generic stdio client
    snippet = _json({"command": py, "args": [bridge], "env": env})
    steps = _bridge_steps(base, bridge, py) + [
        "In your MCP client, add a stdio server with this command, arguments and environment.",
        f"Command line (PowerShell): $env:ARACHNIX_URL=\"{base}\"; $env:ARACHNIX_MCP_TOKEN=\"{tok}\"; {py} {_q(bridge)}",
        f"Command line (macOS / Linux): ARACHNIX_URL={base} ARACHNIX_MCP_TOKEN={_q(tok)} {py} {_q(bridge)}"]
    return {"client": client, "transport": "stdio", "file": "", "snippet": snippet, "steps": steps, "notes": notes,
            "connection": stdio_conn}


def all_configs(base_url: str, token: str = "", bridge_path: str = "", python: str = "python") -> List[Dict[str, Any]]:
    """Every client with its default transport (the panel's tabs)."""
    defaults = {"claude_desktop": "stdio", "claude_code": "http", "vscode": "http", "cursor": "http", "stdio": "stdio"}
    return [client_config(c, base_url, defaults[c], token, bridge_path, python) for c in CLIENTS]
