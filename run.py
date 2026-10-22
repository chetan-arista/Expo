import sys
import os
import argparse
import uvicorn

def main():
    parser = argparse.ArgumentParser(description="Universal Website Crawler & Excel Exporter")
    parser.add_argument("--cli", action="store_true", help="Run in Command Line (CLI) mode instead of Web UI")
    parser.add_argument("--url", help="Target URL for CLI mode (e.g. https://aristasystems.in/)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the Web UI server (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind the Web UI server (default: 8000)")
    
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
        print(f"\n=======================================================")
        print(f"  Universal Website Crawler Web UI Running!")
        print(f"  Open your browser at: http://{known.host}:{known.port}")
        print(f"  Press Ctrl+C to stop the server.")
        print(f"=======================================================\n")
        uvicorn.run("web.app:app", host=known.host, port=known.port, reload=False)

if __name__ == "__main__":
    main()
