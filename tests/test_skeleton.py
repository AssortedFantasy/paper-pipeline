"""Skeleton sanity checks: package imports and contracts are in place.

Replaced by real suites as work packages land. Keeping this green proves the
tooling loop (uv, pytest, ruff, pyright) works end to end.
"""

import paper_pipeline
from paper_pipeline.convert.contract import ConversionRequest, ConversionResult, Converter
from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.library import paths
from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.recipes.model import RecipeDefinition
from paper_pipeline.recipes.provider import LLMProvider, ProviderRequest, ProviderResult


def test_version() -> None:
    assert paper_pipeline.__version__


def test_core_contracts_importable() -> None:
    assert Converter is not None
    assert LLMProvider is not None
    assert ConversionRequest and ConversionResult
    assert ProviderRequest and ProviderResult
    assert RecipeDefinition and Job and JobKind and JobState
    assert PaperMetadata and PaperRecord


def test_terminal_job_states() -> None:
    assert not JobState.QUEUED.is_terminal
    assert not JobState.RUNNING.is_terminal
    assert JobState.SUCCEEDED.is_terminal
    assert JobState.INTERRUPTED.is_terminal


def test_layout_constants() -> None:
    assert paths.FORMAT_VERSION == 1
    assert str(paths.relative_paper_dir("smith2024")) == "papers/smith2024"


def test_healthz() -> None:
    from fastapi.testclient import TestClient
    from paper_pipeline.web.app import create_app

    client = TestClient(create_app())
    assert client.get("/healthz").json() == {"status": "ok"}
