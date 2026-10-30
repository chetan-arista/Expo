"""python -m crawler.mcp: the stdio MCP bridge to a ARACHNIX server (ARACHNIX_URL, ARACHNIX_MCP_TOKEN). See bridge.py."""
import sys

from crawler.mcp.bridge import main

if __name__ == "__main__":
    sys.exit(main())
