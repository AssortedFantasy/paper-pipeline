"""Library validation: report actionable problems, never auto-destroy data.

Checks include:

- ``library.json`` present, readable, and a supported format version.
- Every ``papers/<citekey>/`` has a valid ``paper.json`` whose citekey
  matches its directory name.
- Citekeys match ``paths.CITEKEY_PATTERN``.
- Declared source PDFs exist (a Git clone legitimately lacks them; report
  as "not reprocessable", not corruption).
- Recorded source/transcription/output hashes match installed files; input-hash
  mismatches are reported as stale dependent artifacts, not corruption.
- No absolute paths in any stored record.
- Indexes reference only papers that still exist (staleness report).

Output is a structured problem report with severity and a suggested action.
Validation never deletes or rewrites paper content.
"""

import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from paper_pipeline.library.paths import (
    FIGURES_DIR,
    INDEXES_DIR,
    OPERATIONAL_DIR,
    PAGES_DIR,
    PAPER_FILE,
    PAPERS_DIR,
    SOURCE_DIR,
    TRANSCRIPTION_FILE,
)
from paper_pipeline.library.storage import (
    Library,
    conversion_is_fresh,
    open_library,
    page_render_is_fresh,
    recipe_is_fresh,
    sha256_file,
    validate_citekey,
)

Severity = Literal["error", "warning", "info"]
_RECIPE_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_INDEX_ENTRY = re.compile(r"^([^:\s][^:]*):")


class ValidationProblem(BaseModel):
    """One actionable issue found without changing the library."""

    severity: Severity
    citekey: str | None = None
    message: str
    action: str


class ValidationReport(BaseModel):
    """Structured, serializable result of a read-only validation pass."""

    problems: list[ValidationProblem] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(problem.severity == "error" for problem in self.problems)


def validate_library(library_or_root: Library | Path) -> ValidationReport:
    """Validate durable library content and derived index references."""
    root = library_or_root.root if isinstance(library_or_root, Library) else library_or_root
    report = ValidationReport()
    try:
        library = open_library(root)
    except (OSError, ValueError) as error:
        _add(
            report,
            "error",
            f"Library metadata is unreadable or unsupported: {error}",
            "Restore a valid library.json or open the library with a compatible version.",
        )
        return report

    papers_root = library.root / PAPERS_DIR
    if not papers_root.is_dir():
        _add(
            report,
            "error",
            "The papers directory is missing.",
            "Restore the papers directory from backup.",
        )
        return report

    existing_citekeys = {entry.name for entry in papers_root.iterdir() if entry.is_dir()}
    for entry in sorted(papers_root.iterdir(), key=lambda path: path.name.casefold()):
        if not entry.is_dir():
            _add(
                report,
                "error",
                f"Unexpected file in papers directory: {entry.name}",
                "Move the file outside papers/ or restore the expected paper directory.",
            )
            continue
        citekey = entry.name
        try:
            validate_citekey(citekey)
            record = library.read_paper(citekey)
        except (OSError, ValueError) as error:
            _add(
                report,
                "error",
                f"Invalid paper record: {error}",
                "Restore or correct paper.json without changing its citekey.",
                citekey,
            )
            continue
        _validate_paper(library, record, report)

    _validate_indexes(library.root / INDEXES_DIR, existing_citekeys, report)
    return report


def _validate_paper(library: Library, record, report: ValidationReport) -> None:
    citekey = record.metadata.citekey
    paper_root = library.root / PAPERS_DIR / citekey
    if record.source_pdf is not None:
        expected_prefix = f"papers/{citekey}/source/"
        if not record.source_pdf.startswith(expected_prefix):
            _add(
                report,
                "error",
                "Declared source PDF is outside the paper's source directory.",
                "Move the source into papers/<citekey>/source/ and update paper.json atomically.",
                citekey,
            )
        source = library.root.joinpath(*Path(record.source_pdf).parts)
        if _has_symlink_component(library.root, source):
            _add(
                report,
                "error",
                "Declared source PDF path contains a symlink.",
                "Replace the symlink with source content stored inside the paper directory.",
                citekey,
            )
        elif not source.is_file():
            _add(
                report,
                "warning",
                "Source PDF is missing; this paper is readable but not reprocessable.",
                "Restore the ignored source PDF before rerunning conversion or PDF recipes.",
                citekey,
            )
        elif record.source_sha256 is None:
            _add(
                report,
                "error",
                "Source PDF has no recorded SHA-256.",
                "Re-import the source through Paper Pipeline.",
                citekey,
            )
        elif sha256_file(source) != record.source_sha256:
            _add(
                report,
                "error",
                "Source PDF hash does not match paper.json.",
                "Restore the recorded source or explicitly accept it as a source replacement.",
                citekey,
            )
    elif record.source_sha256 is not None:
        _add(
            report,
            "error",
            "paper.json records a source hash but no source PDF path.",
            "Restore the source path or re-import the paper.",
            citekey,
        )

    conversion = record.conversion
    transcription = paper_root / TRANSCRIPTION_FILE
    if conversion.transcription_sha256 is not None:
        _check_recorded_artifact(
            library.root,
            transcription,
            conversion.transcription_sha256,
            "transcription",
            citekey,
            report,
        )
        if not conversion_is_fresh(record):
            _add(
                report,
                "warning",
                "Installed transcription is stale relative to the current source PDF.",
                "Rerun conversion.",
                citekey,
            )
    elif transcription.exists():
        _add(
            report,
            "error",
            "transcription.md exists without installed-artifact provenance.",
            "Rerun conversion so the artifact can be validated and recorded atomically.",
            citekey,
        )

    pages_root = paper_root / PAGES_DIR
    declared_pages = set(record.pages.artifacts)
    if declared_pages:
        for stored, expected_hash in sorted(record.pages.artifacts.items()):
            _check_recorded_artifact(
                library.root,
                library.root.joinpath(*PurePosixPath(stored).parts),
                expected_hash,
                "rendered page",
                citekey,
                report,
            )
        if not page_render_is_fresh(record):
            _add(
                report,
                "warning",
                "Rendered pages are stale relative to the current source PDF.",
                "Rerun page rendering.",
                citekey,
            )
        actual_pages = (
            {
                path.relative_to(library.root).as_posix()
                for path in pages_root.rglob("*")
                if path.is_file()
            }
            if pages_root.is_dir() and not pages_root.is_symlink()
            else set()
        )
        unexpected_pages = actual_pages - declared_pages
        if unexpected_pages:
            _add(
                report,
                "error",
                f"Rendered pages contain {len(unexpected_pages)} unrecorded file(s).",
                "Rerun page rendering to install one complete declared page set.",
                citekey,
            )
    elif record.pages.page_count or record.pages.source_sha256 is not None:
        _add(
            report,
            "error",
            "paper.json contains incomplete rendered-page provenance.",
            "Rerun page rendering.",
            citekey,
        )

    recorded_outputs: set[str] = set()
    for recipe_name, recipe in sorted(record.recipes.items()):
        if not _RECIPE_NAME.fullmatch(recipe_name):
            _add(
                report,
                "error",
                f"Invalid recipe record name: {recipe_name!r}.",
                "Restore a recipe name matching the recipe template contract.",
                citekey,
            )
            continue
        output = None
        if recipe.output_artifact is not None:
            recorded_outputs.add(recipe.output_artifact)
            output = library.root.joinpath(*PurePosixPath(recipe.output_artifact).parts)
        if recipe.output_sha256 is not None:
            if output is None:
                _add(
                    report,
                    "error",
                    f"Recipe output {recipe_name!r} has a hash but no artifact path.",
                    f"Rerun recipe {recipe_name!r} to record complete provenance.",
                    citekey,
                )
            else:
                _check_recorded_artifact(
                    library.root,
                    output,
                    recipe.output_sha256,
                    f"recipe output {recipe_name!r}",
                    citekey,
                    report,
                )
            if not recipe_is_fresh(record, recipe_name):
                _add(
                    report,
                    "warning",
                    f"Recipe output {recipe_name!r} is stale relative to its declared input.",
                    f"Rerun recipe {recipe_name!r}.",
                    citekey,
                )
        elif output is not None and output.exists():
            _add(
                report,
                "error",
                f"Recipe output {recipe_name!r} exists without recorded provenance.",
                f"Rerun recipe {recipe_name!r} through Paper Pipeline.",
                citekey,
            )

    allowed_names = {
        PAPER_FILE.casefold(),
        SOURCE_DIR.casefold(),
        TRANSCRIPTION_FILE.casefold(),
        FIGURES_DIR.casefold(),
        PAGES_DIR.casefold(),
        OPERATIONAL_DIR.casefold(),
        *(PurePosixPath(path).name.casefold() for path in recorded_outputs),
    }
    for entry in sorted(paper_root.iterdir(), key=lambda path: path.name.casefold()):
        if entry.name.casefold() in allowed_names:
            continue
        _add(
            report,
            "error",
            f"Unexpected unrecorded entry in paper directory: {entry.name!r}.",
            "Remove the entry or install recipe output through Paper Pipeline.",
            citekey,
        )


def _check_recorded_artifact(
    root: Path,
    path: Path,
    expected_hash: str,
    label: str,
    citekey: str,
    report: ValidationReport,
) -> None:
    if _has_symlink_component(root, path):
        _add(
            report,
            "error",
            f"Recorded {label} path contains a symlink.",
            f"Replace the symlink and rerun the operation that produces the {label}.",
            citekey,
        )
    elif not path.is_file():
        _add(
            report,
            "error",
            f"Recorded {label} is missing from disk.",
            f"Restore the artifact or rerun the operation that produces the {label}.",
            citekey,
        )
    elif sha256_file(path) != expected_hash:
        _add(
            report,
            "error",
            f"Recorded {label} hash does not match the installed file.",
            f"Restore the artifact or rerun the operation that produces the {label}.",
            citekey,
        )


def _has_symlink_component(root: Path, path: Path) -> bool:
    try:
        relative = path.absolute().relative_to(root.resolve())
    except ValueError:
        return True
    current = root.resolve()
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _validate_indexes(
    indexes_root: Path, existing_citekeys: set[str], report: ValidationReport
) -> None:
    if not indexes_root.is_dir():
        return
    stale: set[tuple[str, str]] = set()
    for index in sorted(indexes_root.glob("*.md")):
        try:
            lines = index.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            _add(
                report,
                "warning",
                f"Index {index.name} is unreadable: {error}",
                "Rebuild the indexes.",
            )
            continue
        for line in lines:
            match = _INDEX_ENTRY.match(line)
            if match and match.group(1) not in existing_citekeys:
                stale.add((index.name, match.group(1)))
    for index_name, citekey in sorted(stale):
        _add(
            report,
            "warning",
            f"Index {index_name} references missing paper {citekey!r}.",
            "Rebuild the indexes.",
            citekey,
        )


def _add(
    report: ValidationReport,
    severity: Severity,
    message: str,
    action: str,
    citekey: str | None = None,
) -> None:
    report.problems.append(
        ValidationProblem(
            severity=severity,
            citekey=citekey,
            message=message,
            action=action,
        )
    )
