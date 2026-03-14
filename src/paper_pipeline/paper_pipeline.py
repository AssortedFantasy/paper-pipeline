from __future__ import annotations

import argparse
from pathlib import Path

from .formatting import format_meta, format_placeholder, write_text
from .locking import acquire_run_lock, release_run_lock
from .models import PaperRecord
from .nougat_setup import doctor_report
from .rdf_parser import load_records
from .state import mark_completed, mark_failed, mark_running
from .steps.registry import get_step

DEFAULT_ALLOWED_TYPES = {"conferencePaper", "journalArticle", "preprint"}


def build_command(args: argparse.Namespace) -> int:
    workspace_root = args.workspace.resolve()
    allowed_types = set(args.allowed_type)
    records = load_records(args.rdf, workspace_root, allowed_types)

    for record in records:
        paper_dir = args.output / record.citation_key
        write_text(paper_dir / "meta.md", format_meta(record), args.overwrite)
        write_text(
            paper_dir / "transcribed.md", format_placeholder(record), args.overwrite
        )

    with_pdf = sum(
        1 for record in records if record.local_pdf and record.local_pdf.exists()
    )
    print(f"built={len(records)}")
    print(f"with_pdf={with_pdf}")
    print(f"output={args.output}")
    return 0


def select_records(
    records: list[PaperRecord], citekeys: set[str], require_existing_pdf: bool
) -> list[PaperRecord]:
    selected = records
    if citekeys:
        selected = [record for record in selected if record.citation_key in citekeys]
    if require_existing_pdf:
        selected = [
            record
            for record in selected
            if record.local_pdf and record.local_pdf.exists()
        ]
    return selected


def run_nougat_command(args: argparse.Namespace) -> int:
    workspace_root = args.workspace.resolve()
    lock_path = None if args.dry_run else acquire_run_lock(workspace_root)
    allowed_types = set(args.allowed_type)
    try:
        records = load_records(args.rdf, workspace_root, allowed_types)
        requested_citekeys = {value.strip() for value in args.citekey if value.strip()}
        selected = select_records(
            records, requested_citekeys, require_existing_pdf=True
        )

        if args.limit is not None:
            selected = selected[: args.limit]

        if not selected:
            print("No matching PDFs found.")
            return 1

        step = get_step("nougat")
        config = {
            "workspace_root": str(workspace_root),
            "max_size_mb": args.max_size_mb,
            "max_pages": args.max_pages,
            "page_chunk_size": args.page_chunk_size,
            "page_timeout_seconds": args.page_timeout_seconds,
            "nougat_cmd": args.nougat_cmd,
            "model": args.model,
            "batchsize": args.batchsize,
            "dry_run": args.dry_run,
            "no_skipping": args.no_skipping,
            "recompute": args.recompute,
        }

        processed = 0
        skipped = 0
        for record in selected:
            assert record.local_pdf is not None
            paper_dir = args.output / record.citation_key
            if not args.dry_run:
                mark_running(paper_dir, record.citation_key)

            result = step.run(record, paper_dir, config, on_log=print)
            if result.success:
                if not args.dry_run:
                    mark_completed(paper_dir, record.citation_key)
                processed += 1
                continue

            if not args.dry_run:
                mark_failed(
                    paper_dir, record.citation_key, result.error or "unknown error"
                )
            print(f"skip {record.citation_key}: {result.error or 'unknown error'}")
            skipped += 1

        print(f"processed={processed}")
        print(f"skipped={skipped}")
        return 0
    finally:
        if lock_path is not None:
            release_run_lock(lock_path)


def doctor_command(args: argparse.Namespace) -> int:
    for line in doctor_report(args.workspace.resolve()):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build paper metadata and run Nougat over Zotero RDF exports."
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root containing the RDF file and files/ directory.",
    )
    parser.add_argument(
        "--rdf",
        type=Path,
        default=None,
        help="Path to the Zotero RDF export. If omitted, uses the first .rdf file found in the workspace root.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("papers"),
        help="Output directory for paper folders.",
    )
    parser.add_argument(
        "--allowed-type",
        action="append",
        default=sorted(DEFAULT_ALLOWED_TYPES),
        help="Item type to include. Repeat to add more. Defaults to conferencePaper, journalArticle, preprint.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build",
        help="Generate papers/<citekey>/meta.md and placeholder transcribed.md files.",
    )
    build.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing meta.md and transcribed.md files.",
    )
    build.set_defaults(handler=build_command)

    doctor = subparsers.add_parser(
        "doctor",
        help="Inspect the local runtime and explain whether Nougat is ready to run.",
    )
    doctor.set_defaults(handler=doctor_command)

    run_nougat = subparsers.add_parser(
        "run-nougat",
        help="Run Nougat for selected PDFs and write transcribed.md outputs.",
    )
    run_nougat.add_argument(
        "--citekey",
        action="append",
        default=[],
        help="Limit processing to a citation key. Repeat as needed.",
    )
    run_nougat.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit how many papers to process after filtering.",
    )
    run_nougat.add_argument(
        "--max-size-mb",
        type=float,
        default=40.0,
        help="Skip PDFs larger than this size in MB.",
    )
    run_nougat.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Skip PDFs with more than this many pages.",
    )
    run_nougat.add_argument(
        "--page-chunk-size",
        type=int,
        default=0,
        help="Run Nougat in page chunks of this size. The safer default is 0, which processes one whole PDF in one subprocess.",
    )
    run_nougat.add_argument(
        "--page-timeout-seconds",
        type=int,
        default=1800,
        help="Timeout for each Nougat subprocess. Applies per page chunk when chunking is enabled.",
    )
    run_nougat.add_argument(
        "--nougat-cmd",
        default=None,
        help="Nougat executable name or full path. Defaults to the workspace .venv install when present.",
    )
    run_nougat.add_argument("--model", default="0.1.0-small", help="Nougat model tag.")
    run_nougat.add_argument(
        "--batchsize", type=int, default=2, help="Nougat batch size (default: 2)."
    )
    run_nougat.add_argument(
        "--dry-run", action="store_true", help="Print commands without running Nougat."
    )
    run_nougat.add_argument(
        "--no-skipping", action="store_true", help="Pass --no-skipping to Nougat."
    )
    run_nougat.add_argument(
        "--recompute", action="store_true", help="Pass --recompute to Nougat."
    )
    run_nougat.set_defaults(handler=run_nougat_command)

    return parser


def _resolve_rdf(rdf_arg: Path | None, workspace: Path) -> Path:
    """Resolve the RDF path: use the explicit argument, or find the first .rdf in workspace."""
    if rdf_arg is not None:
        return rdf_arg.resolve()
    candidates = sorted(workspace.resolve().glob("*.rdf"))
    if not candidates:
        raise SystemExit(
            f"No .rdf file found in {workspace.resolve()}. "
            "Pass --rdf explicitly or place a Zotero RDF export in the workspace root."
        )
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        print(f"Multiple .rdf files found ({names}); using {candidates[0].name}")
    return candidates[0].resolve()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.rdf = _resolve_rdf(args.rdf, args.workspace)
    args.output = args.output.resolve()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
