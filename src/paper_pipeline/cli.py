"""Command-line entry point.

Thin argument parsing over application services — no business logic here.
Subcommands are implemented alongside their services (see PLAN.md):

- ``serve``     : run the web dashboard (WP-4.1)
- ``doctor``    : environment/health checks with actionable errors (WP-0.2)
- ``validate``  : validate a library (WP-3.1)
- ``reindex``   : rebuild indexes, AGENTS.md, .gitignore (WP-3.1)
"""

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper-pipeline",
        description="Build portable, agent-searchable paper libraries from Zotero exports.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="run the web dashboard")
    subparsers.add_parser("doctor", help="check environment health")
    subparsers.add_parser("validate", help="validate a library")
    subparsers.add_parser("reindex", help="rebuild library indexes")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    print(f"'{args.command}' is not implemented yet; see PLAN.md.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
