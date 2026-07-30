"""Explicit-only structural golden runs over the reference PDF corpus."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Literal

import pytest
from pydantic import BaseModel, Field, model_validator

from paper_pipeline.convert.contract import ConversionRequest, ConversionResult
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
    schema_version: Literal[1]
    documents: list[GoldenDocument]

    @model_validator(mode="after")
    def representative_set(self) -> GoldenExpectations:
        if not 2 <= len(self.documents) <= 3:
            raise ValueError("golden suite must contain two or three corpus documents")
        ids = [document.id for document in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("golden corpus document ids must be unique")
        return self


class ManifestDocument(BaseModel):
    id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_page_range: str


class CorpusManifest(BaseModel):
    schema_version: Literal[1]
    documents: list[ManifestDocument]

    @model_validator(mode="after")
    def unique_documents(self) -> CorpusManifest:
        ids = [document.id for document in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("corpus document ids must be unique")
        return self


GOLDEN = GoldenExpectations.model_validate_json(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
MANIFEST = CorpusManifest.model_validate_json(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.mark.gpu
def test_marker_reference_corpus_meets_structural_quality_gate(
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """Convert the small verified corpus within one bounded, explicit GPU run."""
    _require_marker_gpu()
    corpus = _verified_corpus()
    started = time.monotonic()
    deadline = started + _suite_timeout_seconds()

    for expected, pdf_path in corpus:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            pytest.fail("golden corpus exceeded its total runtime budget")
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
                timeout_seconds=remaining,
            ),
        )

        assert result.ok, result.error
        markdown = _read_portable_outputs(result, staging)
        metrics = _structural_metrics(markdown, len(result.figure_paths))
        assert metrics["characters"] >= expected.minimum_characters
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
        _assert_portable_image_references(markdown)

        record_property(f"{expected.id}_marker_version", result.backend_version)
        record_property(f"{expected.id}_runtime_seconds", round(result.duration_seconds, 3))
        record_property(f"{expected.id}_metrics", json.dumps(metrics, sort_keys=True))

    record_property("golden_total_runtime_seconds", round(time.monotonic() - started, 3))


def _require_marker_gpu() -> None:
    if importlib.util.find_spec("marker") is None:
        pytest.skip("Marker extra is not installed; run `uv sync --extra marker`")
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU was detected (nvidia-smi is unavailable)")


def _verified_corpus() -> list[tuple[GoldenDocument, Path]]:
    missing = [
        expected.environment_variable
        for expected in GOLDEN.documents
        if not os.environ.get(expected.environment_variable)
    ]
    if missing:
        pytest.skip(f"set all golden corpus PDF variables: {', '.join(missing)}")

    corpus: list[tuple[GoldenDocument, Path]] = []
    for expected in GOLDEN.documents:
        configured = os.environ[expected.environment_variable]
        pdf_path = Path(configured).expanduser().resolve()
        if not pdf_path.is_file():
            pytest.fail(f"configured corpus PDF is unavailable: {pdf_path}")
        manifest = _manifest_document(expected.id)
        assert expected.page_range == manifest.sample_page_range, (
            f"golden page range for {expected.id!r} drifted from the corpus manifest"
        )
        assert _sha256(pdf_path) == manifest.sha256, (
            f"{expected.environment_variable} does not match corpus manifest SHA-256"
        )
        corpus.append((expected, pdf_path))
    return corpus


def _marker_config(expected: GoldenDocument) -> dict[str, object]:
    config: dict[str, object] = {"disable_image_extraction": False}
    if expected.page_range != "all":
        config["page_range"] = expected.page_range
    return config


def _manifest_document(document_id: str) -> ManifestDocument:
    matches = [item for item in MANIFEST.documents if item.id == document_id]
    assert len(matches) == 1, f"golden document {document_id!r} is not unique in the manifest"
    return matches[0]


def _read_portable_outputs(result: ConversionResult, staging: Path) -> str:
    transcription = result.transcription_path
    assert transcription is not None
    assert transcription.is_relative_to(staging)
    assert transcription.is_file() and transcription.stat().st_size > 0
    assert all(
        path.is_relative_to(staging / "figures") and path.is_file() and path.stat().st_size > 0
        for path in result.figure_paths
    )
    assert not (staging / "pages").exists()
    return transcription.read_text(encoding="utf-8")


def _assert_portable_image_references(markdown: str) -> None:
    targets = re.findall(r"!\[[^\]]*\]\(([^)\s]+)", markdown)
    targets.extend(re.findall(r"<img\b[^>]*\bsrc=[\"']([^\"']+)", markdown, flags=re.IGNORECASE))
    for target in targets:
        path = PurePosixPath(target)
        assert (
            target
            and "\\" not in target
            and not path.is_absolute()
            and ".." not in path.parts
            and path.parts[0] == "figures"
        ), f"image reference is not portable within the transcription bundle: {target!r}"


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


def _suite_timeout_seconds() -> int:
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
