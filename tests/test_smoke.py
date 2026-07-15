"""Core-only clean-environment smoke coverage for the complete library build."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.indexes.build import INDEX_FILES
from paper_pipeline.jobs.model import JobState
from paper_pipeline.library.model import PaperRecord
from paper_pipeline.library.storage import sha256_file
from paper_pipeline.services.import_ops import apply_import, preview_import
from paper_pipeline.services.library_ops import create_library
from paper_pipeline.services.processing import queue_conversion, queue_recipes
from paper_pipeline.services.runtime import RuntimeRegistry
from tests.fakes import FakeLLMProvider

FIXTURE_EXPORT = Path(__file__).parent / "fixtures" / "zotero" / "clean"
FAKE_CONVERTER = "tests.fakes:FakeConverter"


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
    assert len(plan.additions) == 5
    assert not plan.problems
    imported = await apply_import(runtime, plan)
    assert imported.ok
    assert len(imported.added) == 5

    conversions = await queue_conversion(
        runtime,
        imported.added,
        converter_spec=ConverterSpec(FAKE_CONVERTER, {"figure_count": 1}),
        timeout_seconds=5,
    )
    assert conversions
    for job in conversions:
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
    assert len(provider.calls) == 5

    reindex = _run_cli("reindex", runtime.root)
    assert reindex.returncode == 0, reindex.stderr
    assert "Rebuilt indexes" in reindex.stdout

    validation = _run_cli("validate", runtime.root)
    assert validation.returncode == 0, validation.stdout + validation.stderr
    assert "Library is valid" in validation.stdout

    _assert_durable_library(runtime.root, set(imported.added))


def _run_cli(command: str, library: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "paper_pipeline.cli", command, str(library)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _assert_durable_library(root: Path, citekeys: set[str]) -> None:
    library_data = json.loads((root / "library.json").read_text(encoding="utf-8"))
    assert library_data["name"] == "Clean Environment Smoke"
    assert (root / "AGENTS.md").is_file()
    assert (root / ".gitignore").is_file()

    for filename in INDEX_FILES:
        index = root / "indexes" / filename
        assert index.is_file()
        assert index.read_text(encoding="utf-8").strip()

    for citekey in citekeys:
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
        assert transcription.read_text(encoding="utf-8").strip()
        assert record.conversion.transcription_sha256 == sha256_file(transcription)

        summary_record = record.recipes["summary"]
        assert summary_record.output_artifact is not None
        assert not Path(summary_record.output_artifact).is_absolute()
        summary = root / summary_record.output_artifact
        assert "A durable smoke-test summary." in summary.read_text(encoding="utf-8")
        assert summary_record.output_sha256 == sha256_file(summary)

        figures = list((paper_root / "figures").glob("*.png"))
        assert len(figures) == 1
