"""Local PDFium page-image renderer.

The native PDFium import stays inside ``render()``, which is invoked only in a
fresh page-render child process.
"""

from __future__ import annotations

import time
from importlib.metadata import PackageNotFoundError, version

from paper_pipeline.pages.contract import PageRenderRequest, PageRenderResult


class PdfiumPageRenderer:
    """Render compact PNG page images without invoking Marker or a remote host."""

    name = "pdfium"

    def render(self, request: PageRenderRequest) -> PageRenderResult:
        started = time.monotonic()
        renderer_version = _pdfium_version()
        try:
            if not request.pdf_path.is_file():
                raise FileNotFoundError(f"source PDF does not exist: {request.pdf_path}")
            if not request.staging_dir.is_dir():
                raise FileNotFoundError(
                    f"page-render staging directory does not exist: {request.staging_dir}"
                )
            if request.dpi <= 0:
                raise ValueError("page-render DPI must be positive")

            import pypdfium2 as pdfium

            pages_dir = request.staging_dir / "pages"
            pages_dir.mkdir()
            page_paths = []
            document = pdfium.PdfDocument(str(request.pdf_path))
            try:
                for index in range(len(document)):
                    page = document[index]
                    try:
                        # PDFium accepts fractional scale despite the package's
                        # overly narrow type annotation.
                        bitmap = page.render(
                            scale=request.dpi / 72  # pyright: ignore[reportArgumentType]
                        )
                        try:
                            destination = pages_dir / f"page{index + 1}.png"
                            bitmap.to_pil().save(destination, format="PNG", optimize=True)
                            page_paths.append(destination)
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
            finally:
                document.close()
            if not page_paths:
                raise ValueError("source PDF has no renderable pages")
            return PageRenderResult(
                ok=True,
                renderer=self.name,
                renderer_version=renderer_version,
                duration_seconds=time.monotonic() - started,
                page_paths=page_paths,
                diagnostics={"page_count": str(len(page_paths)), "dpi": str(request.dpi)},
            )
        except Exception as error:
            return PageRenderResult(
                ok=False,
                renderer=self.name,
                renderer_version=renderer_version,
                duration_seconds=time.monotonic() - started,
                error=f"{type(error).__name__}: {error}",
            )


def _pdfium_version() -> str:
    try:
        return version("pypdfium2")
    except PackageNotFoundError:
        return "unknown"
