"""Marker conversion backend adapter.

Uses the representative corpus findings and pinned optional dependencies to
translate ``ConversionRequest`` into a Marker invocation and
normalizes Marker's output (markdown file, extracted images, metadata JSON)
into a ``ConversionResult``. All Marker-specific flags and quirks live here.

Requires the ``marker`` extra; must only be imported in the conversion child
process.
"""

from __future__ import annotations

import json
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any

from paper_pipeline.convert.contract import ConversionRequest, ConversionResult


class MarkerConverter:
    """Adapt Marker 1.10.x's Python API to the converter contract."""

    name = "marker"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = {
            "output_format": "markdown",
            "use_llm": False,
            **(config or {}),
        }

    def convert(self, request: ConversionRequest) -> ConversionResult:
        """Convert one PDF, returning ordinary Marker failures as data."""
        started = time.monotonic()
        backend_version = _marker_version()
        try:
            if not request.pdf_path.is_file():
                raise FileNotFoundError(f"source PDF does not exist: {request.pdf_path}")
            if not request.staging_dir.is_dir():
                raise FileNotFoundError(
                    f"conversion staging directory does not exist: {request.staging_dir}"
                )

            # Heavy imports deliberately stay inside convert(), which the runner calls
            # only in its freshly spawned conversion child process.
            from marker.config.parser import ConfigParser  # pyright: ignore[reportMissingImports]
            from marker.converters.pdf import PdfConverter  # pyright: ignore[reportMissingImports]
            from marker.models import create_model_dict  # pyright: ignore[reportMissingImports]
            from marker.output import text_from_rendered  # pyright: ignore[reportMissingImports]

            imports_finished = time.monotonic()
            parser = ConfigParser(dict(self.config))
            converter = PdfConverter(
                artifact_dict=create_model_dict(),
                processor_list=parser.get_processors(),
                renderer=parser.get_renderer(),
                llm_service=parser.get_llm_service(),
                config=parser.generate_config_dict(),
            )
            models_finished = time.monotonic()
            rendered = converter(str(request.pdf_path))
            conversion_finished = time.monotonic()
            markdown, extension, images = text_from_rendered(rendered)
            if extension != "md":
                raise ValueError(f"Marker returned unexpected output format: {extension}")
            if not markdown.strip():
                raise ValueError("Marker returned empty Markdown")

            markdown, figure_paths = _save_figures(markdown, images, request.staging_dir)
            transcription_path = request.staging_dir / "transcription.md"
            transcription_path.write_text(markdown.rstrip() + "\n", encoding="utf-8", newline="\n")
            serialization_finished = time.monotonic()
            metadata = getattr(rendered, "metadata", {})
            diagnostics = {
                "marker_metadata": json.dumps(
                    metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                "timing_import_seconds": _duration(started, imports_finished),
                "timing_model_load_seconds": _duration(imports_finished, models_finished),
                "timing_conversion_seconds": _duration(models_finished, conversion_finished),
                "timing_serialization_seconds": _duration(
                    conversion_finished, serialization_finished
                ),
            }
            page_count = getattr(converter, "page_count", None)
            if page_count is not None:
                diagnostics["page_count"] = str(page_count)
            return ConversionResult(
                ok=True,
                backend=self.name,
                backend_version=backend_version,
                duration_seconds=time.monotonic() - started,
                transcription_path=transcription_path,
                figure_paths=figure_paths,
                diagnostics=diagnostics,
            )
        except Exception as error:
            return ConversionResult(
                ok=False,
                backend=self.name,
                backend_version=backend_version,
                duration_seconds=time.monotonic() - started,
                error=f"{type(error).__name__}: {error}",
            )


def _marker_version() -> str:
    try:
        return version("marker-pdf")
    except PackageNotFoundError:
        return "unknown"


def _save_figures(
    markdown: str, images: dict[str, Any], staging_dir: Path
) -> tuple[str, list[Path]]:
    if not images:
        return markdown, []

    figures_dir = staging_dir / "figures"
    figures_dir.mkdir()
    figure_paths: list[Path] = []
    seen_names: set[str] = set()
    for marker_name, image in sorted(images.items()):
        relative_name = _safe_figure_name(marker_name)
        casefolded_name = relative_name.as_posix().casefold()
        if casefolded_name in seen_names:
            raise ValueError(f"Marker returned colliding image names: {marker_name!r}")
        seen_names.add(casefolded_name)

        destination = figures_dir.joinpath(*relative_name.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
        figure_paths.append(destination)
        library_reference = f"figures/{relative_name.as_posix()}"
        markdown = markdown.replace(f"]({marker_name})", f"]({library_reference})")
        markdown = markdown.replace(f'src="{marker_name}"', f'src="{library_reference}"')
        markdown = markdown.replace(f"src='{marker_name}'", f"src='{library_reference}'")
    return markdown, figure_paths


def _duration(started: float, finished: float) -> str:
    return f"{finished - started:.3f}"


def _safe_figure_name(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ValueError(f"Marker returned an unsafe image name: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise ValueError(f"Marker returned an unsafe image name: {value!r}")
    return path
