"""Core-only clean-environment smoke coverage for the complete library build."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.indexes.build import INDEX_FILES
from paper_pipeline.jobs.model import JobState
from paper_pipeline.library.model import PaperRecord
from paper_pipeline.library.storage import sha256_file
from paper_pipeline.pages.runner import PageRendererSpec
from paper_pipeline.services.import_ops import apply_import, preview_import
from paper_pipeline.services.library_ops import create_library
from paper_pipeline.services.processing import queue_conversion, queue_page_render, queue_recipes
from paper_pipeline.services.runtime import RuntimeRegistry
from tests.fakes import FakeLLMProvider

FIXTURE_EXPORT = Path(__file__).parent / "fixtures" / "zotero" / "clean"
FAKE_CONVERTER = "tests.fakes:FakeConverter"
FAKE_PAGE_RENDERER = "tests.fakes:FakePageRenderer"


@pytest.mark.slow
async def test_core_smoke_builds_and_validates_durable_library(tmp_path: Path) -> None:
    """Exercise real orchestration with deterministic fake external edges."""
    provider = FakeLLMProvider(response="A durable smoke-test summary.")
    registry = RuntimeRegistry(provider_factories={"fake": lambda: provider})
    runtime = create_library(
        tmp_path / "library",
        name="Clean Environment Smoke",
        registry=registry,
    )

    plan = await preview_import(runtime, FIXTURE_EXPORT)
    assert not plan.problems
    assert plan.additions
    representative = plan.additions[0]
    selected_plan = plan.model_copy(update={"additions": [representative]})

    imported = await apply_import(runtime, selected_plan)
    assert imported.ok
    citekey = representative.metadata.citekey
    assert set(imported.added) == {citekey}

    conversions = await queue_conversion(
        runtime,
        imported.added,
        converter_spec=ConverterSpec(FAKE_CONVERTER),
        timeout_seconds=5,
    )
    assert conversions
    for job in conversions:
        assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED

    page_renders = await queue_page_render(
        runtime,
        imported.added,
        renderer_spec=PageRendererSpec(FAKE_PAGE_RENDERER),
        timeout_seconds=5,
    )
    for job in page_renders:
        assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED

    recipes = await queue_recipes(
        runtime,
        ["summary"],
        imported.added,
        provider_name="fake",
        model="smoke-model",
    )
    assert recipes
    for job in recipes:
        assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED
    assert provider.calls

    reindex = _run_cli("reindex", runtime.root)
    assert reindex.returncode == 0, reindex.stderr

    portable_root = tmp_path / "portable-library"
    shutil.copytree(runtime.root, portable_root)
    validation = _run_cli("validate", portable_root)
    assert validation.returncode == 0, validation.stdout + validation.stderr

    _assert_durable_library(portable_root, citekey)


def _run_cli(command: str, library: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "paper_pipeline.cli", command, str(library)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _assert_durable_library(root: Path, citekey: str) -> None:
    for filename in INDEX_FILES:
        index = root / "indexes" / filename
        assert index.is_file()
        assert citekey in index.read_text(encoding="utf-8")

    paper_root = root / "papers" / citekey
    record = PaperRecord.model_validate_json(
        (paper_root / "paper.json").read_text(encoding="utf-8")
    )
    assert record.source_pdf is not None
    assert not Path(record.source_pdf).is_absolute()
    source = root / record.source_pdf
    assert source.is_file()
    assert record.source_sha256 == sha256_file(source)

    transcription = paper_root / "transcription.md"
    assert transcription.stat().st_size > 0
    assert record.conversion.transcription_sha256 == sha256_file(transcription)

    summary_record = record.recipes["summary"]
    assert summary_record.output_artifact is not None
    assert not Path(summary_record.output_artifact).is_absolute()
    summary = root / summary_record.output_artifact
    assert summary.stat().st_size > 0
    assert summary_record.output_sha256 == sha256_file(summary)

    assert any((paper_root / "pages").glob("*.png"))
    assert record.pages.page_count == 1
    assert set(record.pages.artifacts) == {f"papers/{citekey}/pages/page1.png"}
