"""Lightweight source-PDF inspection used by application services."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

LARGE_DOCUMENT_PAGE_THRESHOLD = 100


def pdf_page_count(path: Path) -> int | None:
    """Return the page count, or ``None`` when the source cannot be inspected."""
    try:
        return len(PdfReader(path, strict=False).pages)
    except (OSError, PyPdfError, ValueError):
        return None
