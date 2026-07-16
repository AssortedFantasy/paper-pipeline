"""Tests for deterministic, rebuildable text indexes."""

import shutil
from pathlib import Path

import pytest

from paper_pipeline.indexes.build import INDEX_FILES, rebuild_indexes
from paper_pipeline.library.model import (
    PaperMetadata,
    PaperRecord,
    RecipeRecord,
)
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import create_library, sha256_file


def _record(
    citekey: str,
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    venue: str | None = None,
) -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(
            citekey=citekey,
            title=title,
            authors=authors or [],
            year=year,
            venue=venue,
        ),
    )


def _index_bytes(root: Path) -> dict[str, bytes]:
    return {name: (root / "indexes" / name).read_bytes() for name in INDEX_FILES}


def test_rebuild_is_sorted_lf_only_and_deterministic(library_root: Path) -> None:
    library = create_library(library_root)
    library.write_paper(_record("Zulu2025", "  A   later title ", ["Zed Author"], 2025, "Venue  Z"))
    library.write_paper(
        _record("Alpha2024", "First\nTitle", ["Ada One", "Bob Two"], 2024, "Venue A")
    )

    rebuild_indexes(library)
    first = _index_bytes(library_root)
    rebuild_indexes(library)

    assert _index_bytes(library_root) == first
    assert first["titles.md"] == b"Alpha2024: First Title\nZulu2025: A later title\n"
    assert first["authors.md"] == b"Alpha2024: Ada One; Bob Two\nZulu2025: Zed Author\n"
    assert first["years.md"] == b"Alpha2024: 2024\nZulu2025: 2025\n"
    assert first["venues.md"] == b"Alpha2024: Venue A\nZulu2025: Venue Z\n"
    assert all(b"\r\n" not in content for content in first.values())


def test_indexes_use_explicit_placeholders_for_missing_metadata(library_root: Path) -> None:
    library = create_library(library_root)
    library.write_paper(_record("Alpha2024", "", []))

    rebuild_indexes(library)

    assert (library_root / "indexes" / "titles.md").read_text() == "Alpha2024: untitled\n"
    assert (library_root / "indexes" / "authors.md").read_text() == ("Alpha2024: unknown authors\n")
    assert (library_root / "indexes" / "years.md").read_text() == "Alpha2024: unknown year\n"
    assert (library_root / "indexes" / "venues.md").read_text() == ("Alpha2024: unknown venue\n")


def test_summary_uses_first_output_line(library_root: Path) -> None:
    library = create_library(library_root)
    record = _record("Alpha2024", "First")
    library.write_paper(record)
    summary = library_root / "papers" / "Alpha2024" / "summary.md"
    summary.write_text(
        "One sentence TLDR.\nMore detail.\n",
        encoding="utf-8",
    )
    record.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Alpha2024/transcription.md",
        input_sha256="input-hash",
        output_artifact="papers/Alpha2024/summary.md",
        output_sha256=sha256_file(summary),
    )
    library.write_paper(record)

    rebuild_indexes(library)

    assert (library_root / "indexes" / "summaries.md").read_text(encoding="utf-8") == (
        "Alpha2024: One sentence TLDR.\n"
    )


def test_summaries_distinguish_missing_summary(library_root: Path) -> None:
    library = create_library(library_root)
    library.write_paper(_record("Alpha2024", "First"))

    rebuild_indexes(library)

    assert (library_root / "indexes" / "summaries.md").read_text(encoding="utf-8") == (
        "Alpha2024: no summary yet\n"
    )


def test_rebuild_removes_unsupported_derived_indexes(library_root: Path) -> None:
    library = create_library(library_root)
    obsolete = library_root / "indexes" / "status.md"
    obsolete.write_text("old status index\n", encoding="utf-8")
    unsupported = library_root / "indexes" / "custom.md"
    unsupported.write_text("not part of the supported index set\n", encoding="utf-8")

    rebuild_indexes(library)

    assert not obsolete.exists()
    assert not unsupported.exists()


def test_unrecorded_or_hash_mismatched_summary_is_not_indexed(library_root: Path) -> None:
    library = create_library(library_root)
    record = _record("Alpha2024", "First")
    library.write_paper(record)
    summary = library_root / "papers" / "Alpha2024" / "summary.md"
    summary.write_text("unrecorded summary", encoding="utf-8")

    rebuild_indexes(library)
    summaries = library_root / "indexes" / "summaries.md"
    assert summaries.read_text(encoding="utf-8") == "Alpha2024: no summary yet\n"

    record.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Alpha2024/transcription.md",
        input_sha256="input",
        output_artifact="papers/Alpha2024/summary.md",
        output_sha256="wrong-hash",
    )
    library.write_paper(record)
    rebuild_indexes(library)
    assert summaries.read_text(encoding="utf-8") == "Alpha2024: no summary yet\n"


def test_symlinked_summary_is_not_indexed(library_root: Path, tmp_path: Path) -> None:
    library = create_library(library_root)
    record = _record("Alpha2024", "First")
    generated = library_root / "papers" / "Alpha2024"
    outside = tmp_path / "outside-summary.md"
    outside.write_text("outside summary", encoding="utf-8")
    summary = generated / "summary.md"
    try:
        summary.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")
    record.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Alpha2024/transcription.md",
        input_sha256="input",
        output_artifact="papers/Alpha2024/summary.md",
        output_sha256=sha256_file(outside),
    )
    library.write_paper(record)

    rebuild_indexes(library)

    assert (library_root / "indexes" / "summaries.md").read_text(encoding="utf-8") == (
        "Alpha2024: no summary yet\n"
    )


def test_rebuild_drops_entries_for_deleted_paper_directory(library_root: Path) -> None:
    library = create_library(library_root)
    library.write_paper(_record("Deleted2024", "Gone"))
    rebuild_indexes(library)
    shutil.rmtree(library_root / "papers" / "Deleted2024")

    rebuild_indexes(library)

    assert all(content == b"" for content in _index_bytes(library_root).values())


def test_empty_library_produces_valid_empty_indexes(library_root: Path) -> None:
    library = create_library(library_root)

    rebuild_indexes(library)

    assert all(content == b"" for content in _index_bytes(library_root).values())
