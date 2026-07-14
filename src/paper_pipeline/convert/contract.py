"""The converter contract. FROZEN for parallel work — changes require an ADR.

A converter takes one PDF and produces Markdown plus optional figure assets
in a staging directory. It knows nothing about libraries, jobs, or HTTP.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ConversionRequest:
    """One paper's conversion work order.

    ``staging_dir`` is a fresh, empty directory owned by the caller. The
    converter writes ALL output there; the caller validates and atomically
    installs it into the library afterwards.
    """

    pdf_path: Path
    staging_dir: Path
    timeout_seconds: int = 1800


@dataclass(frozen=True)
class ConversionResult:
    """Outcome of one conversion attempt.

    ``ok=True`` requires ``transcription_path`` to exist and be non-empty —
    terminal output alone is never proof of success.
    """

    ok: bool
    backend: str
    backend_version: str
    duration_seconds: float
    transcription_path: Path | None = None  # inside staging_dir
    figure_paths: list[Path] = field(default_factory=list)  # inside staging_dir
    error: str | None = None
    # Free-form diagnostics destined for the paper's .pp/ directory.
    diagnostics: dict[str, str] = field(default_factory=dict)


class Converter(Protocol):
    """Implemented by each conversion backend adapter."""

    name: str

    def convert(self, request: ConversionRequest) -> ConversionResult:
        """Convert one PDF. Must not raise for ordinary failures; return ok=False."""
        ...
