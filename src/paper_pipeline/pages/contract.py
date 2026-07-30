"""Versioned contract for rendering one source PDF into portable page images."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PageRenderRequest:
    """One page-rendering work order over a caller-owned staging directory."""

    pdf_path: Path
    staging_dir: Path
    timeout_seconds: int
    dpi: int = 96


@dataclass(frozen=True)
class PageRenderResult:
    """Outcome of one local page-render attempt."""

    ok: bool
    renderer: str
    renderer_version: str
    duration_seconds: float
    page_paths: list[Path] = field(default_factory=list)  # inside staging_dir/pages
    error: str | None = None
    diagnostics: dict[str, str] = field(default_factory=dict)


class PageRenderer(Protocol):
    """Implemented by a local source-PDF page renderer."""

    name: str

    def render(self, request: PageRenderRequest) -> PageRenderResult:
        """Render one PDF. Ordinary failures are returned as ``ok=False``."""
        ...
