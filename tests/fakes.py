"""Fake implementations of external contracts for the fast test suite.

Expanded by the work packages that need them:

- ``FakeConverter``: implements ``convert.contract.Converter``. Writes a tiny
  deterministic transcription and optional figure files into the staging
  directory. Configurable to fail, hang (for timeout tests), or crash.
- ``FakeLLMProvider``: implements ``recipes.provider.LLMProvider``. Returns
  canned text; records calls so scheduler tests can assert per-paper
  sequencing. Configurable to fail or delay.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from paper_pipeline.convert.contract import ConversionRequest, ConversionResult

FakeConverterMode = Literal["success", "failure", "crash", "hang", "empty"]


@dataclass
class FakeConverter:
    """Deterministic converter double usable from spawned child processes."""

    name: str = field(default="fake", init=False)

    mode: FakeConverterMode = "success"
    figure_count: int = 0
    hang_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.figure_count < 0:
            raise ValueError("figure_count must not be negative")

    def convert(self, request: ConversionRequest) -> ConversionResult:
        """Produce the configured outcome without reading the input PDF."""
        started = time.perf_counter()

        if self.mode == "crash":
            raise RuntimeError("fake converter crash")
        if self.mode == "hang":
            delay = self.hang_seconds
            time.sleep(delay if delay is not None else request.timeout_seconds + 1.0)
        if self.mode == "failure":
            return self._result(
                started=started,
                ok=False,
                error="fake converter failure",
            )

        transcription_path = request.staging_dir / "transcription.md"
        if self.mode == "empty":
            transcription_path.write_text("", encoding="utf-8")
            return self._result(
                started=started,
                ok=False,
                transcription_path=transcription_path,
                error="fake converter produced an empty transcription",
            )

        transcription_path.write_text(
            "# Fake transcription\n\nDeterministic converter output.\n",
            encoding="utf-8",
        )
        figure_paths = self._write_figures(request.staging_dir)
        return self._result(
            started=started,
            ok=True,
            transcription_path=transcription_path,
            figure_paths=figure_paths,
        )

    def _write_figures(self, staging_dir: Path) -> list[Path]:
        if self.figure_count == 0:
            return []

        figures_dir = staging_dir / "figures"
        figures_dir.mkdir()
        paths = [figures_dir / f"figure-{index}.png" for index in range(1, self.figure_count + 1)]
        for index, path in enumerate(paths, start=1):
            path.write_bytes(f"fake figure {index}\n".encode())
        return paths

    @staticmethod
    def _result(
        *,
        started: float,
        ok: bool,
        transcription_path: Path | None = None,
        figure_paths: list[Path] | None = None,
        error: str | None = None,
    ) -> ConversionResult:
        return ConversionResult(
            ok=ok,
            backend="fake",
            backend_version="1.0",
            duration_seconds=time.perf_counter() - started,
            transcription_path=transcription_path,
            figure_paths=figure_paths or [],
            error=error,
        )
