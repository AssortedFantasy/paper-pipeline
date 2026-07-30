from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter

from paper_pipeline.ingest.plan import ImportPlan, PlannedImport
from paper_pipeline.library.model import PaperMetadata
from paper_pipeline.library.storage import Library, sha256_file
from paper_pipeline.services.import_ops import apply_import
from paper_pipeline.services.library_catalog import refresh_catalog
from paper_pipeline.services.paper_browse import browse_papers
from paper_pipeline.services.pdf_info import LARGE_DOCUMENT_PAGE_THRESHOLD, pdf_page_count
from paper_pipeline.services.runtime import RuntimeRegistry


def _pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


async def _import_pdf(runtime, path: Path, citekey: str) -> None:  # type: ignore[no-untyped-def]
    planned = PlannedImport(
        metadata=PaperMetadata(citekey=citekey, title=f"Title for {citekey}"),
        attachment_path=path,
        attachment_sha256=sha256_file(path),
        expected_source_sha256=None,
    )
    await apply_import(runtime, ImportPlan(additions=[planned]))


def test_pdf_page_count_handles_valid_and_unreadable_sources(tmp_path: Path) -> None:
    assert pdf_page_count(_pdf(tmp_path / "paper.pdf", 7)) == 7

    unreadable = tmp_path / "unreadable.pdf"
    unreadable.write_bytes(b"not a pdf")
    assert pdf_page_count(unreadable) is None


async def test_large_documents_are_flagged_and_not_preselected(tmp_path: Path) -> None:
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


async def test_sort_and_filter_query_catalog_without_rescanning(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    runtime = RuntimeRegistry().create(tmp_path / "library")
    await _import_pdf(runtime, _pdf(tmp_path / "paper.pdf", 3), "Smith2026")

    def unexpected_scan(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("interactive browse rescanned the library")

    monkeypatch.setattr(Library, "list_papers", unexpected_scan)
    monkeypatch.setattr(
        "paper_pipeline.services.library_catalog.pdf_page_count",
        unexpected_scan,
    )

    first = await browse_papers(runtime, sort="year", direction="desc")
    second = await browse_papers(runtime, query="smith", sort="citekey")

    assert [row.record.metadata.citekey for row in first.rows] == ["Smith2026"]
    assert [row.record.metadata.citekey for row in second.rows] == ["Smith2026"]
    assert first.generation == second.generation


async def test_explicit_refresh_rebuilds_external_pdf_facts(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runtime = RuntimeRegistry().create(tmp_path / "library")
    source = _pdf(tmp_path / "paper.pdf", 3)
    await _import_pdf(runtime, source, "Smith2026")
    source_pdf = runtime.catalog.snapshot().papers[0].record.source_pdf
    assert source_pdf is not None
    installed = runtime.root / source_pdf
    _pdf(installed, LARGE_DOCUMENT_PAGE_THRESHOLD)
    monkeypatch.setattr(
        "paper_pipeline.services.library_catalog._STALE_CHECK_INTERVAL_SECONDS",
        0,
    )

    before = await browse_papers(runtime)
    assert before.rows[0].page_count == 3
    assert runtime.catalog.is_stale()

    refreshed = await refresh_catalog(runtime)
    after = await browse_papers(runtime)

    assert refreshed.generation > before.generation
    assert not runtime.catalog.is_stale()
    assert after.rows[0].page_count == LARGE_DOCUMENT_PAGE_THRESHOLD
    assert after.rows[0].is_large_document


async def test_disposable_cache_avoids_reopening_unchanged_pdf(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "library"
    first = RuntimeRegistry().create(root)
    await _import_pdf(first, _pdf(tmp_path / "paper.pdf", 8), "Smith2026")

    def unexpected_pdf_read(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("unchanged PDF was reopened")

    monkeypatch.setattr(
        "paper_pipeline.services.library_catalog.pdf_page_count",
        unexpected_pdf_read,
    )
    reopened = RuntimeRegistry().open(root)

    assert reopened.catalog.snapshot().papers[0].page_count == 8
