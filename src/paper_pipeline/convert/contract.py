"""The versioned transcription converter contract.

A converter takes one PDF and produces Markdown plus optional extracted
figures in a staging directory. Rendered PDF pages are a separate,
backend-independent processing contract.
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
    # No default here: the value comes from AppConfig.converter_timeout_seconds,
    # which is the single source of the timeout default.
    timeout_seconds: int


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
    figure_paths: list[Path] = field(default_factory=list)  # inside staging_dir/figures
    error: str | None = None
    # Free-form diagnostics destined for the paper's .pp/ directory.
    diagnostics: dict[str, str] = field(default_factory=dict)


class Converter(Protocol):
    """Implemented by each conversion backend adapter."""

    name: str

    def convert(self, request: ConversionRequest) -> ConversionResult:
        """Convert one PDF. Must not raise for ordinary failures; return ok=False."""
        ...
