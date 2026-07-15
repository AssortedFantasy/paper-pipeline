"""Deterministic, atomically installed text indexes for generated libraries."""

from __future__ import annotations

import shutil

from paper_pipeline.library.model import AttemptState, PaperRecord
from paper_pipeline.library.paths import INDEXES_DIR, PAPERS_DIR
from paper_pipeline.library.storage import Library, conversion_is_fresh, recipe_is_fresh

INDEX_FILES = ("titles.md", "authors.md", "summaries.md", "status.md")


def rebuild_indexes(library: Library) -> None:
    """Rebuild all concise indexes from canonical paper records."""
    records, _problems = library.list_papers()
    records.sort(key=lambda record: record.metadata.citekey)
    contents = {
        "titles.md": _lines(records, _title),
        "authors.md": _lines(records, _authors),
        "summaries.md": _lines(records, lambda record: _summary(library, record)),
        "status.md": _lines(records, lambda record: _status(library, record)),
    }

    stage = library.stage_dir()
    try:
        for filename in INDEX_FILES:
            staged = stage / filename
            with staged.open("w", encoding="utf-8", newline="\n") as output:
                output.write(contents[filename])
            library.install_artifact(staged, f"{INDEXES_DIR}/{filename}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _lines(records: list[PaperRecord], value) -> str:
    return "".join(f"{record.metadata.citekey}: {_one_line(value(record))}\n" for record in records)


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _title(record: PaperRecord) -> str:
    return record.metadata.title or "untitled"


def _authors(record: PaperRecord) -> str:
    return "; ".join(record.metadata.authors) or "unknown authors"


def _summary(library: Library, record: PaperRecord) -> str:
    path = library.root / PAPERS_DIR / record.metadata.citekey / "generated" / "summary.md"
    if not path.is_file():
        return "no summary yet"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "no summary yet"
    body = _without_front_matter(text)
    return next((_one_line(line) for line in body.splitlines() if line.strip()), "no summary yet")


def _without_front_matter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return text
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return ""
    return "\n".join(lines[closing + 1 :])


def _status(library: Library, record: PaperRecord) -> str:
    citekey = record.metadata.citekey
    paper_root = library.root / PAPERS_DIR / citekey
    issues: list[str] = []

    source = library.root / record.source_pdf if record.source_pdf else None
    if source is None or not source.is_file():
        issues.append("source missing")

    transcription = paper_root / "transcription.md"
    if record.conversion.transcription_sha256 is None or not transcription.is_file():
        issues.append("transcription missing")
    elif not conversion_is_fresh(record):
        issues.append("transcription stale")
    if (
        record.conversion.last_attempt is not None
        and record.conversion.last_attempt.state == AttemptState.FAILED
    ):
        issues.append("conversion last attempt failed")

    summary = record.recipes.get("summary")
    summary_path = paper_root / "generated" / "summary.md"
    if summary is None or summary.output_sha256 is None or not summary_path.is_file():
        issues.append("summary missing")
    elif not recipe_is_fresh(record, "summary"):
        issues.append("summary stale")
    if (
        summary is not None
        and summary.last_attempt is not None
        and summary.last_attempt.state == AttemptState.FAILED
    ):
        issues.append("summary last attempt failed")

    for name, recipe in sorted(record.recipes.items()):
        if name == "summary":
            continue
        if recipe.last_attempt is not None and recipe.last_attempt.state == AttemptState.FAILED:
            issues.append(f"{name} last attempt failed")
    return "; ".join(issues) or "ready"
