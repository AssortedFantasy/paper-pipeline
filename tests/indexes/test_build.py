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


def _index_entries(root: Path, name: str) -> list[tuple[str, str]]:
    lines = (root / "indexes" / name).read_text(encoding="utf-8").splitlines()
    entries = []
    for line in lines:
        citekey, value = line.split(": ", 1)
        entries.append((citekey, value))
    return entries


def test_rebuild_produces_deterministic_portable_metadata_indexes(
    library_root: Path,
) -> None:
    library = create_library(library_root)
    library.write_paper(_record("Zulu2025", "  A   later title ", ["Zed Author"], 2025, "Venue  Z"))
    library.write_paper(
        _record("Alpha2024", "First\nTitle", ["Ada One", "Bob Two"], 2024, "Venue A")
    )
    library.write_paper(_record("Missing2023", "", []))

    rebuild_indexes(library)
    first = _index_bytes(library_root)
    rebuild_indexes(library)

    assert _index_bytes(library_root) == first
    assert all(b"\r" not in content and content.endswith(b"\n") for content in first.values())

    expected = {
        "titles.md": {
            "Alpha2024": "First Title",
            "Missing2023": "untitled",
            "Zulu2025": "A later title",
        },
        "authors.md": {
            "Alpha2024": "Ada One; Bob Two",
            "Missing2023": "unknown authors",
            "Zulu2025": "Zed Author",
        },
        "years.md": {
            "Alpha2024": "2024",
            "Missing2023": "unknown year",
            "Zulu2025": "2025",
        },
        "venues.md": {
            "Alpha2024": "Venue A",
            "Missing2023": "unknown venue",
            "Zulu2025": "Venue Z",
        },
    }
    for name, values in expected.items():
        entries = _index_entries(library_root, name)
        assert [citekey for citekey, _value in entries] == sorted(values)
        assert dict(entries) == values


def test_summaries_require_declared_matching_provenance(library_root: Path) -> None:
    library = create_library(library_root)
    trusted = _record("Trusted2024", "Trusted")
    mismatch = _record("Mismatch2024", "Mismatch")
    unrecorded = _record("Unrecorded2024", "Unrecorded")
    for record in (trusted, mismatch, unrecorded):
        library.write_paper(record)

    trusted_summary = library_root / "papers" / "Trusted2024" / "summary.md"
    trusted_summary.write_text("\n  One   sentence TLDR.  \nMore detail.\n", encoding="utf-8")
    trusted.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Trusted2024/transcription.md",
        input_sha256="input-hash",
        output_artifact="papers/Trusted2024/summary.md",
        output_sha256=sha256_file(trusted_summary),
    )
    library.write_paper(trusted)

    mismatched_summary = library_root / "papers" / "Mismatch2024" / "summary.md"
    mismatched_summary.write_text("stale summary", encoding="utf-8")
    mismatch.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Mismatch2024/transcription.md",
        input_sha256="input-hash",
        output_artifact="papers/Mismatch2024/summary.md",
        output_sha256="not-the-installed-content",
    )
    library.write_paper(mismatch)
    (library_root / "papers" / "Unrecorded2024" / "summary.md").write_text(
        "undeclared summary", encoding="utf-8"
    )

    rebuild_indexes(library)

    assert _index_entries(library_root, "summaries.md") == [
        ("Mismatch2024", "no summary yet"),
        ("Trusted2024", "One sentence TLDR."),
        ("Unrecorded2024", "no summary yet"),
    ]


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

    assert _index_entries(library_root, "summaries.md") == [("Alpha2024", "no summary yet")]


def test_rebuild_removes_deleted_entries_and_unsupported_indexes(library_root: Path) -> None:
    library = create_library(library_root)
    library.write_paper(_record("Deleted2024", "Gone"))
    library.write_paper(_record("Retained2024", "Still here"))
    rebuild_indexes(library)

    obsolete = library_root / "indexes" / "status.md"
    obsolete.write_text("old status index\n", encoding="utf-8")
    unsupported = library_root / "indexes" / "custom.md"
    unsupported.write_text("not part of the supported index set\n", encoding="utf-8")
    shutil.rmtree(library_root / "papers" / "Deleted2024")

    rebuild_indexes(library)

    assert all(
        [citekey for citekey, _value in _index_entries(library_root, name)] == ["Retained2024"]
        for name in INDEX_FILES
    )
    assert not obsolete.exists()
    assert not unsupported.exists()
