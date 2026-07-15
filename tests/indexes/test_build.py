"""Tests for deterministic, rebuildable text indexes."""

import shutil
from pathlib import Path

from paper_pipeline.indexes.build import INDEX_FILES, rebuild_indexes
from paper_pipeline.library.model import (
    ConversionRecord,
    PaperMetadata,
    PaperRecord,
    RecipeRecord,
)
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import create_library, sha256_file


def _record(citekey: str, title: str, authors: list[str] | None = None) -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(
            citekey=citekey,
            title=title,
            authors=authors or [],
        ),
    )


def _index_bytes(root: Path) -> dict[str, bytes]:
    return {name: (root / "indexes" / name).read_bytes() for name in INDEX_FILES}


def test_rebuild_is_sorted_lf_only_and_deterministic(library_root: Path) -> None:
    library = create_library(library_root)
    library.write_paper(_record("Zulu2025", "  A   later title ", ["Zed Author"]))
    library.write_paper(_record("Alpha2024", "First\nTitle", ["Ada One", "Bob Two"]))

    rebuild_indexes(library)
    first = _index_bytes(library_root)
    rebuild_indexes(library)

    assert _index_bytes(library_root) == first
    assert first["titles.md"] == b"Alpha2024: First Title\nZulu2025: A later title\n"
    assert first["authors.md"] == b"Alpha2024: Ada One; Bob Two\nZulu2025: Zed Author\n"
    assert all(b"\r\n" not in content for content in first.values())


def test_summary_uses_first_body_line_after_front_matter(library_root: Path) -> None:
    library = create_library(library_root)
    record = _record("Alpha2024", "First")
    library.write_paper(record)
    generated = library_root / "papers" / "Alpha2024" / "generated"
    generated.mkdir()
    (generated / "summary.md").write_text(
        "---\nrecipe: summary\n---\n\nOne sentence TLDR.\nMore detail.\n",
        encoding="utf-8",
    )

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


def test_status_derives_missing_stale_and_ready_states(library_root: Path) -> None:
    library = create_library(library_root)
    missing = _record("Missing2024", "Missing")
    library.write_paper(missing)

    stale = _record("Stale2024", "Stale")
    stale.source_pdf = "papers/Stale2024/source/paper.pdf"
    stale.source_sha256 = "current-source"
    stale.conversion = ConversionRecord(
        source_sha256="old-source",
        transcription_sha256="transcription-hash",
    )
    stale.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Stale2024/transcription.md",
        input_sha256="old-transcription",
        output_sha256="summary-hash",
    )
    stale_root = library_root / "papers" / "Stale2024"
    (stale_root / "source").mkdir(parents=True)
    (stale_root / "source" / "paper.pdf").write_bytes(b"source")
    (stale_root / "transcription.md").write_text("text", encoding="utf-8")
    (stale_root / "generated").mkdir()
    (stale_root / "generated" / "summary.md").write_text("summary", encoding="utf-8")
    library.write_paper(stale)

    ready = _record("Ready2024", "Ready")
    ready.source_pdf = "papers/Ready2024/source/paper.pdf"
    ready_root = library_root / "papers" / "Ready2024"
    (ready_root / "source").mkdir(parents=True)
    source = ready_root / "source" / "paper.pdf"
    source.write_bytes(b"source")
    ready.source_sha256 = sha256_file(source)
    transcription = ready_root / "transcription.md"
    transcription.write_text("text", encoding="utf-8")
    ready.conversion = ConversionRecord(
        source_sha256=ready.source_sha256,
        transcription_sha256=sha256_file(transcription),
    )
    (ready_root / "generated").mkdir()
    summary = ready_root / "generated" / "summary.md"
    summary.write_text("summary", encoding="utf-8")
    ready.recipes["summary"] = RecipeRecord(
        input_artifact="papers/Ready2024/transcription.md",
        input_sha256=ready.conversion.transcription_sha256,
        output_sha256=sha256_file(summary),
    )
    library.write_paper(ready)

    rebuild_indexes(library)

    status = (library_root / "indexes" / "status.md").read_text(encoding="utf-8")
    assert "Missing2024: source missing; transcription missing; summary missing\n" in status
    assert "Stale2024: transcription stale; summary stale\n" in status
    assert "Ready2024: ready\n" in status


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
