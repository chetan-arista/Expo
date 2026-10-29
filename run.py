import os
import sys
import argparse
import socket
import uvicorn


def _lan_address() -> str:
    """This computer's address on the local network ('' when it cannot be found); no packet is sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def main():
    parser = argparse.ArgumentParser(description="ARACHNIX: website crawler and SEO audit (web dashboard or command line)")
    parser.add_argument("--cli", action="store_true", help="Run in Command Line (CLI) mode instead of Web UI")
    parser.add_argument("--url", help="Target URL for CLI mode (e.g. https://aristasystems.in/)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the Web UI server (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind the Web UI server (default: 8000)")
    # Optional HTTPS (DEPLOY.md, "HTTPS on the office network"): Chrome and Edge block or warn about every file
    # downloaded over plain HTTP from another computer's address (http://10.x...). Without these: plain HTTP.
    parser.add_argument("--ssl-certfile", default=os.environ.get("CRAWLER_SSL_CERTFILE") or None,
                        help="HTTPS certificate (PEM), e.g. made with mkcert (or env CRAWLER_SSL_CERTFILE)")
    parser.add_argument("--ssl-keyfile", default=os.environ.get("CRAWLER_SSL_KEYFILE") or None,
                        help="the certificate's private key (PEM) (or env CRAWLER_SSL_KEYFILE)")
    
    # Catch any additional CLI args
    known, remaining = parser.parse_known_args()

    if known.cli or known.url:
        # Forward to CLI runner
        from cli import main as cli_main
        # If url provided through --url, construct argv for cli.py
        if known.url and known.url not in remaining:
            remaining.insert(0, known.url)
        sys.argv = [sys.argv[0]] + remaining
        cli_main()
    else:
        # Launch FastAPI Web UI
        # Every SEO check module must load first: a broken one would silently drop its checks from every crawl
        ssl = {}
        if known.ssl_certfile or known.ssl_keyfile:
            for what, path in (("--ssl-certfile", known.ssl_certfile), ("--ssl-keyfile", known.ssl_keyfile)):
                if not path or not os.path.isfile(path):
                    parser.error(f"{what}: give both --ssl-certfile and --ssl-keyfile (file not found: {path or 'missing'})")
            ssl = {"ssl_certfile": known.ssl_certfile, "ssl_keyfile": known.ssl_keyfile}
        scheme = "https" if ssl else "http"
        from cli import require_all_checks
        require_all_checks("ARACHNIX server")
        print("\n=======================================================")
        print("  ARACHNIX is running!")
        print(f"  Open your browser at: {scheme}://{known.host}:{known.port}")
        if known.host in ("0.0.0.0", "::"):
            lan = _lan_address()
            print(f"  On this computer:     {scheme}://localhost:{known.port}")
            if lan:
                print(f"  From the network:     {scheme}://{lan}:{known.port}")
        if not ssl and known.host not in ("127.0.0.1", "localhost", "::1"):
            print("  Plain HTTP: Chrome and Edge on other computers block or warn about downloads (PDF, Excel).")
            print("  Fix: DEPLOY.md, 'HTTPS on the office network' (--ssl-certfile / --ssl-keyfile).")
        print("  Press Ctrl+C to stop the server.")
        print("=======================================================\n")
        os.environ["CRAWLER_PORT"] = str(known.port)   # the MCP client snippets and /api/mcp/test use it
        uvicorn.run("web.app:app", host=known.host, port=known.port, reload=False, **ssl)

if __name__ == "__main__":
    main()
