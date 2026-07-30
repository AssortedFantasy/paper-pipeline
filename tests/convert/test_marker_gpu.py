from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import paper_pipeline.convert.marker as marker_adapter
from paper_pipeline.convert.contract import ConversionRequest
from paper_pipeline.convert.marker import MarkerConverter


class FakeImage:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def save(self, path: Path) -> None:
        path.write_bytes(self.content)


def _install_fake_marker(monkeypatch: pytest.MonkeyPatch, *, unsafe_image: bool = False) -> None:
    marker_package = ModuleType("marker")
    marker_package.__path__ = []  # type: ignore[attr-defined]
    config_package = ModuleType("marker.config")
    config_package.__path__ = []  # type: ignore[attr-defined]
    converters_package = ModuleType("marker.converters")
    converters_package.__path__ = []  # type: ignore[attr-defined]

    parser_module = ModuleType("marker.config.parser")

    class FakeConfigParser:
        def __init__(self, options: dict[str, Any]) -> None:
            self.options = options

        def generate_config_dict(self) -> dict[str, Any]:
            return {"page_range": [0]}

        def get_processors(self) -> None:
            return None

        def get_renderer(self) -> str:
            return "marker.renderers.markdown.MarkdownRenderer"

        def get_llm_service(self) -> None:
            return None

    parser_module.ConfigParser = FakeConfigParser  # type: ignore[attr-defined]

    pdf_module = ModuleType("marker.converters.pdf")

    class FakePdfConverter:
        def __init__(self, **kwargs: Any) -> None:
            assert {
                "artifact_dict",
                "processor_list",
                "renderer",
                "llm_service",
                "config",
            } <= kwargs.keys()
            self.page_count = 1

        def __call__(self, path: str) -> SimpleNamespace:
            assert Path(path).is_file()
            return SimpleNamespace(metadata={"page_stats": [{"page_id": 0}]})

    pdf_module.PdfConverter = FakePdfConverter  # type: ignore[attr-defined]

    models_module = ModuleType("marker.models")
    models_module.create_model_dict = lambda: {"models": "fake"}  # type: ignore[attr-defined]
    output_module = ModuleType("marker.output")
    image_name = "../escape.png" if unsafe_image else "page/figure.png"
    output_module.text_from_rendered = lambda _rendered: (  # type: ignore[attr-defined]
        f"# Converted\n\n![Figure]({image_name})",
        "md",
        {image_name: FakeImage(b"figure bytes")},
    )

    for name, module in {
        "marker": marker_package,
        "marker.config": config_package,
        "marker.config.parser": parser_module,
        "marker.converters": converters_package,
        "marker.converters.pdf": pdf_module,
        "marker.models": models_module,
        "marker.output": output_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    def render_pages(_pdf_path: Path, staging_dir: Path) -> list[Path]:
        pages = staging_dir / "pages"
        pages.mkdir()
        page = pages / "page1.png"
        page.write_bytes(b"page bytes")
        return [page]

    monkeypatch.setattr(marker_adapter, "_render_pages", render_pages)


def test_adapter_normalizes_mocked_marker_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_marker(monkeypatch)
    monkeypatch.setattr(marker_adapter, "version", lambda _distribution: "1.10.2")
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF synthetic")
    staging = tmp_path / "staging"
    staging.mkdir()

    result = MarkerConverter(config={"page_range": "0"}).convert(
        ConversionRequest(source, staging, timeout_seconds=30)
    )

    assert result.ok
    assert result.backend == "marker"
    assert result.backend_version == "1.10.2"
    transcription_path = result.transcription_path
    assert transcription_path is not None
    assert transcription_path == staging / "transcription.md"
    assert transcription_path.stat().st_size > 0
    markdown = transcription_path.read_text(encoding="utf-8")
    assert result.figure_paths
    assert all(
        path.is_file() and path.is_relative_to(staging / "figures") for path in result.figure_paths
    )
    assert all(path.relative_to(staging).as_posix() in markdown for path in result.figure_paths)
    assert result.page_paths
    assert all(
        path.is_file() and path.is_relative_to(staging / "pages") for path in result.page_paths
    )

    metadata = json.loads(result.diagnostics["marker_metadata"])
    assert isinstance(metadata, dict)
    timings = {
        name: value for name, value in result.diagnostics.items() if name.startswith("timing_")
    }
    assert timings
    assert all(float(value) >= 0 for value in timings.values())


def test_adapter_returns_ordinary_failures_without_importing_marker(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()

    result = MarkerConverter().convert(
        ConversionRequest(tmp_path / "missing.pdf", staging, timeout_seconds=30)
    )

    assert not result.ok
    assert result.backend == "marker"
    assert result.error
    assert "source" in result.error.lower() and "exist" in result.error.lower()
    assert result.transcription_path is None


def test_adapter_rejects_unsafe_marker_image_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_marker(monkeypatch, unsafe_image=True)
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF synthetic")
    staging = tmp_path / "staging"
    staging.mkdir()

    result = MarkerConverter().convert(ConversionRequest(source, staging, timeout_seconds=30))

    assert not result.ok
    assert result.error
    assert "unsafe" in result.error.lower()
    assert result.transcription_path is None
    assert result.figure_paths == []
    assert not (tmp_path / "escape.png").exists()
