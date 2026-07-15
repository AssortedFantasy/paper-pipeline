"""Explicit-only structural golden runs over the reference PDF corpus."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, model_validator

from paper_pipeline.convert.contract import ConversionRequest
from paper_pipeline.convert.runner import ConverterSpec, run_conversion

CORPUS_ROOT = Path(__file__).parent / "fixtures" / "corpus"
MANIFEST_PATH = CORPUS_ROOT / "manifest.json"
EXPECTATIONS_PATH = CORPUS_ROOT / "golden_expectations.json"
MARKER_CONVERTER = "paper_pipeline.convert.marker:MarkerConverter"


class Tolerance(BaseModel):
    minimum: int = Field(ge=0)
    maximum: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> Tolerance:
        if self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class GoldenDocument(BaseModel):
    id: str
    environment_variable: str
    page_range: str
    minimum_characters: int = Field(gt=0)
    headings: Tolerance
    tables: Tolerance
    figure_references: Tolerance
    extracted_figures: Tolerance


class GoldenExpectations(BaseModel):
    schema_version: int
    documents: list[GoldenDocument]

    @model_validator(mode="after")
    def representative_set(self) -> GoldenExpectations:
        if not 2 <= len(self.documents) <= 3:
            raise ValueError("golden suite must contain two or three corpus documents")
        ids = [document.id for document in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("golden corpus document ids must be unique")
        return self


GOLDEN = GoldenExpectations.model_validate_json(EXPECTATIONS_PATH.read_text(encoding="utf-8"))


@pytest.mark.gpu
@pytest.mark.parametrize("expected", GOLDEN.documents, ids=lambda item: item.id)
def test_marker_golden_structure(
    expected: GoldenDocument,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
    pytestconfig: pytest.Config,
) -> None:
    """Convert a verified local corpus PDF and enforce broad structural bounds."""
    _require_marker_gpu()
    pdf_path = _configured_pdf(expected)
    manifest = _manifest_document(expected.id)
    assert expected.page_range == manifest["sample_page_range"], (
        f"golden page range for {expected.id!r} drifted from the corpus manifest"
    )
    assert _sha256(pdf_path) == manifest["sha256"], (
        f"{expected.environment_variable} does not match corpus manifest SHA-256"
    )

    staging = tmp_path / expected.id
    staging.mkdir()
    result = run_conversion(
        ConverterSpec(
            MARKER_CONVERTER,
            {"config": _marker_config(expected)},
        ),
        ConversionRequest(
            pdf_path=pdf_path,
            staging_dir=staging,
            timeout_seconds=_timeout_seconds(),
        ),
    )

    assert result.ok, result.error
    assert result.backend == "marker"
    assert result.transcription_path is not None
    markdown = result.transcription_path.read_text(encoding="utf-8")
    metrics = _structural_metrics(markdown, len(result.figure_paths))
    assert len(markdown) >= expected.minimum_characters
    _assert_tolerance("headings", metrics["headings"], expected.headings)
    _assert_tolerance("tables", metrics["tables"], expected.tables)
    _assert_tolerance(
        "figure references",
        metrics["figure_references"],
        expected.figure_references,
    )
    _assert_tolerance(
        "extracted figures",
        metrics["extracted_figures"],
        expected.extracted_figures,
    )
    assert all(path.is_relative_to(staging / "figures") for path in result.figure_paths)

    record_property("corpus_id", expected.id)
    record_property("marker_version", result.backend_version)
    record_property("runtime_seconds", round(result.duration_seconds, 3))
    report_line = (
        "GOLDEN_RUNTIME "
        f"corpus={expected.id} marker={result.backend_version} "
        f"seconds={result.duration_seconds:.3f} metrics={json.dumps(metrics, sort_keys=True)}"
    )
    _write_report_line(pytestconfig, report_line)


def _require_marker_gpu() -> None:
    if importlib.util.find_spec("marker") is None:
        pytest.skip("Marker extra is not installed; run `uv sync --extra marker`")
    if importlib.util.find_spec("torch") is None:
        pytest.skip("PyTorch is not installed; run `uv sync --extra marker`")
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        pytest.skip("no NVIDIA GPU was detected (nvidia-smi is unavailable)")
    probe = subprocess.run(
        [nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        pytest.skip("no available NVIDIA GPU was reported by nvidia-smi")


def _configured_pdf(expected: GoldenDocument) -> Path:
    configured = os.environ.get(expected.environment_variable)
    if not configured:
        pytest.skip(f"set {expected.environment_variable} to the local {expected.id} corpus PDF")
    path = Path(configured).expanduser().resolve()
    if not path.is_file():
        pytest.skip(f"configured corpus PDF is unavailable: {path}")
    return path


def _marker_config(expected: GoldenDocument) -> dict[str, object]:
    config: dict[str, object] = {"disable_image_extraction": False}
    if expected.page_range != "all":
        config["page_range"] = expected.page_range
    return config


def _manifest_document(document_id: str) -> dict[str, object]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    matches = [item for item in manifest["documents"] if item["id"] == document_id]
    assert len(matches) == 1, f"golden document {document_id!r} is not unique in the manifest"
    return matches[0]


def _structural_metrics(markdown: str, extracted_figures: int) -> dict[str, int]:
    headings = len(re.findall(r"^#{1,6}\s+\S", markdown, flags=re.MULTILINE))
    markdown_tables = sum(
        "|" in line and bool(re.search(r":?-{3,}:?", line)) for line in markdown.splitlines()
    )
    html_tables = len(re.findall(r"<table\b", markdown, flags=re.IGNORECASE))
    markdown_figures = len(re.findall(r"!\[[^\]]*\]\([^)]+\)", markdown))
    html_figures = len(re.findall(r"<img\b", markdown, flags=re.IGNORECASE))
    return {
        "characters": len(markdown),
        "headings": headings,
        "tables": markdown_tables + html_tables,
        "figure_references": markdown_figures + html_figures,
        "extracted_figures": extracted_figures,
    }


def _assert_tolerance(label: str, actual: int, expected: Tolerance) -> None:
    assert expected.minimum <= actual <= expected.maximum, (
        f"{label} count {actual} is outside [{expected.minimum}, {expected.maximum}]"
    )


def _write_report_line(config: pytest.Config, line: str) -> None:
    reporter: Any = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        print(line)
    else:
        reporter.write_line(line)


def _timeout_seconds() -> int:
    raw = os.environ.get("PAPER_PIPELINE_GOLDEN_TIMEOUT", "900")
    try:
        return min(max(int(raw), 60), 1800)
    except ValueError:
        pytest.fail("PAPER_PIPELINE_GOLDEN_TIMEOUT must be an integer")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
