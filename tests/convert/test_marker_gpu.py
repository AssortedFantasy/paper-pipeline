from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import paper_pipeline.convert.marker as marker_adapter
from paper_pipeline.convert.contract import ConversionRequest
from paper_pipeline.convert.marker import MarkerConverter
from paper_pipeline.convert.runner import ConverterSpec, run_conversion


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
            assert kwargs["artifact_dict"] == {"models": "fake"}
            assert kwargs["config"] == {"page_range": [0]}
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
    assert transcription_path.read_text(encoding="utf-8") == (
        "# Converted\n\n![Figure](figures/page/figure.png)\n"
    )
    assert result.figure_paths == [staging / "figures" / "page" / "figure.png"]
    assert result.figure_paths[0].read_bytes() == b"figure bytes"
    assert json.loads(result.diagnostics["marker_metadata"])["page_stats"][0]["page_id"] == 0
    assert result.diagnostics["page_count"] == "1"


def test_adapter_returns_ordinary_failures_without_importing_marker(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()

    result = MarkerConverter().convert(
        ConversionRequest(tmp_path / "missing.pdf", staging, timeout_seconds=30)
    )

    assert not result.ok
    assert result.backend == "marker"
    assert "source PDF does not exist" in (result.error or "")


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
    assert "unsafe image name" in (result.error or "")
    assert not (tmp_path / "escape.png").exists()


@pytest.mark.gpu
def test_marker_gpu_smoke_is_manifest_bounded(tmp_path: Path) -> None:
    """Explicit-only one-page KAN smoke test; never part of the default suite."""
    if importlib.util.find_spec("marker") is None:
        pytest.skip("Marker extra is not installed; run `uv sync --extra marker`")
    torch = pytest.importorskip("torch", reason="PyTorch is required by the Marker extra")
    if not torch.cuda.is_available():
        pytest.skip("CUDA GPU is not available")

    configured_pdf = os.environ.get("PAPER_PIPELINE_MARKER_SMOKE_PDF")
    if not configured_pdf:
        pytest.skip("set PAPER_PIPELINE_MARKER_SMOKE_PDF to the manifest's figures PDF")
    pdf_path = Path(configured_pdf).resolve()
    if not pdf_path.is_file():
        pytest.fail(f"PAPER_PIPELINE_MARKER_SMOKE_PDF does not exist: {pdf_path}")

    manifest_path = Path(__file__).parents[1] / "fixtures" / "corpus" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    document = next(item for item in manifest["documents"] if item["id"] == "figures")
    assert _sha256(pdf_path) == document["sha256"], "smoke PDF does not match the corpus manifest"

    staging = tmp_path / "staging"
    staging.mkdir()
    timeout = min(int(os.environ.get("PAPER_PIPELINE_MARKER_SMOKE_TIMEOUT", "300")), 600)
    result = run_conversion(
        ConverterSpec(
            "paper_pipeline.convert.marker:MarkerConverter",
            {"config": {"page_range": "0", "disable_image_extraction": False}},
        ),
        ConversionRequest(pdf_path, staging, timeout_seconds=timeout),
    )

    assert result.ok, result.error
    assert result.backend_version == "1.10.2"
    assert result.transcription_path is not None
    assert result.transcription_path.read_text(encoding="utf-8").strip()
    assert result.figure_paths, "the manifest's figure page should extract at least one image"
    assert all(path.is_relative_to(staging / "figures") for path in result.figure_paths)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
