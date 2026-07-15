from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter

from paper_pipeline.ingest.plan import ImportPlan, PlannedImport
from paper_pipeline.library.model import PaperMetadata
from paper_pipeline.library.storage import sha256_file
from paper_pipeline.services.import_ops import apply_import
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
