"""
MCP (Model Context Protocol) for ARACHNIX: AI agents (Claude Desktop, Claude Code, VS Code, Cursor...) operate ARACHNIX.

  tools.py     the tools, resources and prompts, shared by the /mcp endpoint and the Ask ARACHNIX assistant
  server.py    create_mcp_app(): the Streamable HTTP endpoint mounted at /mcp (token, Host / Origin checks, limits)
  tokens.py    MCP tokens (hashed) and the switches of Connections > AI agents
  bridge.py    a standalone stdio server forwarding to a ARACHNIX server's /mcp (python -m crawler.mcp)
  client.py    a small test client (list tools, call one; HTTP or stdio), standalone like the bridge
  snippets.py  copy-paste configs for Claude Desktop, Claude Code, VS Code and Cursor

Nothing is imported here, so the bridge and the client start without the crawler's modules.
"""
