"""Hardware-independent integration test for the local PDFium renderer."""

from pathlib import Path

from pypdf import PdfWriter

from paper_pipeline.pages.contract import PageRenderRequest
from paper_pipeline.pages.pdfium import PdfiumPageRenderer


def test_pdfium_renders_source_pages_without_marker(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    writer.add_blank_page(width=300, height=200)
    with source.open("wb") as output:
        writer.write(output)
    staging = tmp_path / "staging"
    staging.mkdir()

    result = PdfiumPageRenderer().render(
        PageRenderRequest(source, staging, timeout_seconds=30, dpi=96)
    )

    assert result.ok
    assert [path.name for path in result.page_paths] == ["page1.png", "page2.png"]
    assert all(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for path in result.page_paths)
