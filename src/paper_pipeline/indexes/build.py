"""Deterministic, atomically installed text indexes for generated libraries."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from paper_pipeline.library.model import PaperRecord
from paper_pipeline.library.paths import INDEXES_DIR
from paper_pipeline.library.storage import (
    Library,
    sha256_file,
)

INDEX_FILES = ("titles.md", "authors.md", "years.md", "venues.md", "summaries.md")


def rebuild_indexes(library: Library) -> None:
    """Rebuild all concise indexes from canonical paper records."""
    records, _problems = library.list_papers()
    records.sort(key=lambda record: record.metadata.citekey)
    contents = {
        "titles.md": _lines(records, _title),
        "authors.md": _lines(records, _authors),
        "years.md": _lines(records, _year),
        "venues.md": _lines(records, _venue),
        "summaries.md": _lines(records, lambda record: _summary(library, record)),
    }

    stage = library.stage_dir()
    try:
        for filename in INDEX_FILES:
            staged = stage / filename
            with staged.open("w", encoding="utf-8", newline="\n") as output:
                output.write(contents[filename])
            library.install_artifact(staged, f"{INDEXES_DIR}/{filename}")
        _remove_unsupported_indexes(library)
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


def _year(record: PaperRecord) -> str:
    return str(record.metadata.year) if record.metadata.year is not None else "unknown year"


def _venue(record: PaperRecord) -> str:
    return record.metadata.venue or "unknown venue"


def _summary(library: Library, record: PaperRecord) -> str:
    summary = record.recipes.get("summary")
    if summary is None or summary.output_artifact is None or summary.output_sha256 is None:
        return "no summary yet"
    path = library.root.joinpath(*PurePosixPath(summary.output_artifact).parts)
    if not _is_safe_file(library.root, path) or sha256_file(path) != summary.output_sha256:
        return "no summary yet"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "no summary yet"
    return next((_one_line(line) for line in text.splitlines() if line.strip()), "no summary yet")


def _remove_unsupported_indexes(library: Library) -> None:
    """Remove derived Markdown indexes that are not part of the current set."""
    indexes = library.root / INDEXES_DIR
    supported = set(INDEX_FILES)
    for path in indexes.glob("*.md"):
        if path.name not in supported:
            path.unlink()


def _is_safe_file(root: Path, path: Path) -> bool:
    try:
        relative = path.absolute().relative_to(root.resolve())
    except ValueError:
        return False
    current = root.resolve()
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return path.is_file()
