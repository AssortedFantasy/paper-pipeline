from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path


def _open_browser(url: str) -> None:
    if os.name == "nt":
        try:
            os.startfile(url)
            return
        except OSError:
            pass
    webbrowser.open(url, new=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the Paper Pipeline web GUI.")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=8787, help="Port (default: 8787)")
    parser.add_argument(
        "--workspace", type=Path, default=Path("."), help="Workspace root"
    )
    parser.add_argument(
        "--rdf", type=Path, default=None, help="Path to Zotero RDF export"
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="Papers output directory"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Don't auto-open browser"
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run: uv sync --group gui", file=sys.stderr)
        return 1

    from .app import create_app

    workspace_root = args.workspace.resolve()
    rdf_path = args.rdf.resolve() if args.rdf else None
    papers_dir = args.output.resolve() if args.output else None

    app = create_app(
        workspace_root=workspace_root,
        rdf_path=rdf_path,
        papers_dir=papers_dir,
    )

    if not args.no_browser:
        import threading

        def open_browser():
            import time

            time.sleep(1.2)
            _open_browser(f"http://{args.host}:{args.port}")

        threading.Thread(target=open_browser, daemon=True).start()

    print(f"Paper Pipeline GUI: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
