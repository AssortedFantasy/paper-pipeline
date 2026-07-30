from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from paper_pipeline.ingest.plan import ImportPlan, PlannedImport
from paper_pipeline.library.model import PaperMetadata
from paper_pipeline.library.storage import Library, sha256_file
from paper_pipeline.services.import_ops import apply_import
from paper_pipeline.services.library_catalog import refresh_catalog
from paper_pipeline.services.paper_browse import browse_papers
from paper_pipeline.services.pdf_info import (
    LARGE_DOCUMENT_PAGE_THRESHOLD,
    pdf_page_count,
)
from paper_pipeline.services.runtime import LibraryRuntime, RuntimeRegistry


def _pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


async def _import_pdf(
    runtime: LibraryRuntime,
    path: Path,
    citekey: str,
    *,
    title: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
) -> None:
    planned = PlannedImport(
        metadata=PaperMetadata(
            citekey=citekey,
            title=title or f"Title for {citekey}",
            authors=authors or [],
            year=year,
        ),
        attachment_path=path,
        attachment_sha256=sha256_file(path),
        expected_source_sha256=None,
    )
    report = await apply_import(runtime, ImportPlan(additions=[planned]))
    assert report.ok


def test_pdf_page_count_handles_valid_and_unreadable_sources(
    tmp_path: Path,
) -> None:
    assert pdf_page_count(_pdf(tmp_path / "paper.pdf", 7)) == 7

    unreadable = tmp_path / "unreadable.pdf"
    unreadable.write_bytes(b"not a pdf")
    assert pdf_page_count(unreadable) is None


async def test_large_documents_are_flagged_and_not_preselected(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    await _import_pdf(runtime, _pdf(tmp_path / "article.pdf", 12), "Article2026")
    await _import_pdf(
        runtime,
        _pdf(tmp_path / "book.pdf", LARGE_DOCUMENT_PAGE_THRESHOLD),
        "Book2026",
    )

    page = await browse_papers(runtime, select_pending_conversion=True)
    rows = {row.record.metadata.citekey: row for row in page.rows}

    assert rows["Article2026"].page_count == 12
    assert rows["Article2026"].selected
    assert not rows["Article2026"].is_large_document
    assert rows["Book2026"].page_count == LARGE_DOCUMENT_PAGE_THRESHOLD
    assert rows["Book2026"].is_large_document
    assert not rows["Book2026"].selected


async def test_sort_and_filter_use_catalog_without_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    for citekey, title, authors, year in (
        ("Older2020", "Zeta methods", ["Ada Smith"], 2020),
        ("Newer2025", "Alpha results", ["Ben Smith"], 2025),
        ("Other2024", "Middle study", ["Cara Jones"], 2024),
    ):
        await _import_pdf(
            runtime,
            _pdf(tmp_path / f"{citekey}.pdf", 1),
            citekey,
            title=title,
            authors=authors,
            year=year,
        )

    def unexpected_scan(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("interactive browse rescanned durable library data")

    monkeypatch.setattr(Library, "list_papers", unexpected_scan)
    monkeypatch.setattr(
        "paper_pipeline.services.library_catalog.pdf_page_count",
        unexpected_scan,
    )

    filtered = await browse_papers(
        runtime,
        query="smith",
        sort="year",
        direction="desc",
    )
    sorted_page = await browse_papers(runtime, sort="title")

    assert [row.record.metadata.citekey for row in filtered.rows] == [
        "Newer2025",
        "Older2020",
    ]
    assert [row.record.metadata.citekey for row in sorted_page.rows] == [
        "Newer2025",
        "Other2024",
        "Older2020",
    ]


async def test_disposable_pdf_cache_is_reused_then_invalidated_by_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "library"
    first = RuntimeRegistry().create(root)
    await _import_pdf(
        first,
        _pdf(tmp_path / "paper.pdf", 3),
        "Smith2026",
    )

    real_page_count = pdf_page_count
    inspected_paths: list[Path] = []

    def recording_page_count(path: Path) -> int | None:
        inspected_paths.append(path)
        return real_page_count(path)

    monkeypatch.setattr(
        "paper_pipeline.services.library_catalog.pdf_page_count",
        recording_page_count,
    )
    reopened = RuntimeRegistry().open(root)
    before = await browse_papers(reopened)

    assert before.rows[0].page_count == 3
    assert inspected_paths == []

    source_pdf = before.rows[0].record.source_pdf
    assert source_pdf is not None
    installed = reopened.root / source_pdf
    _pdf(installed, LARGE_DOCUMENT_PAGE_THRESHOLD)
    monkeypatch.setattr(
        "paper_pipeline.services.library_catalog._STALE_CHECK_INTERVAL_SECONDS",
        0,
    )

    assert reopened.catalog.is_stale()
    refreshed = await refresh_catalog(reopened)
    after = await browse_papers(reopened)

    assert inspected_paths
    assert refreshed.generation > before.generation
    assert not reopened.catalog.is_stale()
    assert after.rows[0].page_count == LARGE_DOCUMENT_PAGE_THRESHOLD
    assert after.rows[0].is_large_document
