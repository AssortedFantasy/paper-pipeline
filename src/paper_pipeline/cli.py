"""Command-line entry point.

Thin argument parsing over application services — no business logic here.
Subcommands are implemented alongside their services:

- ``serve``     : run the web dashboard (WP-4.1)
- ``doctor``    : environment/health checks with actionable errors (WP-0.2)
- ``validate``  : validate a library (WP-3.1)
- ``reindex``   : rebuild indexes, AGENTS.md, .gitignore (WP-3.1)
"""

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from paper_pipeline import __version__
from paper_pipeline.config import AppConfig, load_config
from paper_pipeline.services.library_ops import (
    open_library,
    rebuild_indexes,
    validate_library,
)


def _target_is_writable(target: Path) -> bool:
    """Return whether *target*, or its nearest existing parent, is writable."""
    candidate = target.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)


def _run_doctor(target: Path | None, config: AppConfig | None = None) -> int:
    """Print safe, actionable environment diagnostics."""
    config = config or load_config()
    python_ok = sys.version_info >= (3, 12)
    print(
        f"Python: {sys.version.split()[0]} "
        f"({'ok' if python_ok else 'unsupported; Python 3.12+ is required'})"
    )
    print(f"Paper Pipeline: {__version__}")

    marker_available = importlib.util.find_spec("marker") is not None
    if marker_available:
        print("Marker extra: available")
    else:
        print("Marker extra: not installed (optional; run 'uv sync --extra marker' for conversion)")

    print(
        "LLM credentials: configured"
        if config.llm_api_key
        else "LLM credentials: not configured (optional; set PAPER_PIPELINE_LLM_API_KEY)"
    )
    print(
        f"LLM model: configured ({config.llm_model})"
        if config.llm_model
        else "LLM model: not configured (optional; set PAPER_PIPELINE_LLM_MODEL)"
    )

    target_ok = True
    if target is not None:
        target_ok = _target_is_writable(target)
        if target_ok:
            print(f"Target directory: writable ({target.expanduser()})")
        else:
            print(
                f"Target directory: not writable ({target.expanduser()}); "
                "choose an existing writable directory or adjust permissions"
            )
    return 0 if python_ok and target_ok else 1


def _run_validate(path: Path) -> int:
    """Validate one library and print actionable findings."""
    try:
        runtime = open_library(path)
        report = asyncio.run(validate_library(runtime))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Could not validate library {path}: {error}", file=sys.stderr)
        return 2

    if not report.problems:
        print(f"Library is valid: {runtime.root}")
        return 0
    for problem in report.problems:
        citekey = f" [{problem.citekey}]" if problem.citekey else ""
        print(f"{problem.severity.upper()}{citekey}: {problem.message}")
        print(f"  Action: {problem.action}")
    return 0 if report.ok else 1


def _run_reindex(path: Path) -> int:
    """Rebuild all derived navigation and guidance files."""
    try:
        runtime = open_library(path)
        asyncio.run(rebuild_indexes(runtime))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Could not reindex library {path}: {error}", file=sys.stderr)
        return 2
    print(f"Rebuilt indexes, AGENTS.md, and .gitignore: {runtime.root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper-pipeline",
        description="Build portable, agent-searchable paper libraries from Zotero exports.",
    )
    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser("serve", help="run the web dashboard")
    serve_parser.add_argument("--host", default="127.0.0.1", help="bind host")
    serve_parser.add_argument("--port", type=int, default=8000, help="bind port")
    doctor_parser = subparsers.add_parser("doctor", help="check environment health")
    doctor_parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        help="optional library parent directory to check for writability",
    )
    validate_parser = subparsers.add_parser("validate", help="validate a library")
    validate_parser.add_argument("library", type=Path, help="library directory")
    reindex_parser = subparsers.add_parser("reindex", help="rebuild library indexes")
    reindex_parser.add_argument("library", type=Path, help="library directory")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return _run_doctor(args.target)
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "paper_pipeline.web.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
        )
        return 0
    if args.command == "validate":
        return _run_validate(args.library)
    if args.command == "reindex":
        return _run_reindex(args.library)
    print(f"'{args.command}' is not implemented yet.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
