"""End-to-end processing service tests using only fake external edges."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.fakes import FakeLLMProvider

from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.jobs.recovery import InterruptedAttempt
from paper_pipeline.library.model import AttemptState, PaperMetadata, PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import sha256_file
from paper_pipeline.recipes.model import RecipeDefinition
from paper_pipeline.services.processing import (
    cancel_job,
    pending_conversion_citekeys,
    pending_recipe_citekeys,
    queue_conversion,
    queue_recipes,
    retry_job,
)
from paper_pipeline.services.runtime import (
    LibraryRuntime,
    LibrarySession,
    PaperSession,
    RuntimeRegistry,
)

FAKE_CONVERTER = "tests.fakes:FakeConverter"


async def _runtime(tmp_path: Path, provider: FakeLLMProvider | None = None) -> LibraryRuntime:
    provider = provider or FakeLLMProvider(response="Generated result")
    return RuntimeRegistry(provider_factories={"fake": lambda: provider}).create(
        tmp_path / "library"
    )


async def _seed(
    runtime: LibraryRuntime,
    *,
    install_source: bool = True,
) -> None:
    source_bytes = b"%PDF-1.4 fake source"
    source_relative = "papers/Smith2024/source/source.pdf"
    record = PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(citekey="Smith2024", title="Test paper"),
        source_pdf=source_relative,
        source_sha256=_bytes_sha256(source_bytes),
    )

    async def worker(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        if install_source:
            stage = session.stage_dir()
            staged = stage / "source.pdf"
            staged.write_bytes(source_bytes)
            session.install_artifact(staged, source_relative)
        session.write_record(record)

    job = await runtime.enqueue_paper("Smith2024", JobKind.IMPORT, "seed", worker)
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED


async def _read(runtime: LibraryRuntime) -> PaperRecord:
    records: list[PaperRecord] = []

    async def worker(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        records.append(session.read_paper("Smith2024"))

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "read", worker)
    await runtime.queue.wait(job.id)
    return records[0]


async def test_conversion_and_recipe_batch_flow_through_real_queue(tmp_path: Path) -> None:
    provider = FakeLLMProvider(response="One-line generated result")
    runtime = await _runtime(tmp_path, provider)
    await _seed(runtime)

    conversion = (
        await queue_conversion(
            runtime,
            ["Smith2024"],
            converter_spec=ConverterSpec(FAKE_CONVERTER, {"figure_count": 1}),
            timeout_seconds=5,
        )
    )[0]
    assert (await runtime.queue.wait(conversion.id)).state is JobState.SUCCEEDED
    converted = await _read(runtime)
    assert converted.conversion.transcription_sha256 == sha256_file(
        runtime.root / "papers" / "Smith2024" / "transcription.md"
    )
    assert converted.conversion.last_attempt is not None
    assert converted.conversion.last_attempt.state is AttemptState.SUCCEEDED

    recipe_job = (
        await queue_recipes(
            runtime,
            ["summary", "contributions"],
            ["Smith2024"],
            provider_name="fake",
            model="test-model",
        )
    )[0]
    assert (await runtime.queue.wait(recipe_job.id)).state is JobState.SUCCEEDED
    enriched = await _read(runtime)
    assert set(enriched.recipes) == {"summary", "contributions"}
    assert len(provider.calls) == 2
    assert provider.calls[0].input_sha256 == provider.calls[1].input_sha256
    for name in ("summary", "contributions"):
        recipe = enriched.recipes[name]
        assert recipe.last_attempt is not None
        assert recipe.last_attempt.state is AttemptState.SUCCEEDED
        assert recipe.output_artifact is not None
        assert (runtime.root / recipe.output_artifact).is_file()


async def test_failed_rerun_preserves_last_good_conversion_and_records_log(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime)
    success = (
        await queue_conversion(
            runtime,
            ["Smith2024"],
            converter_spec=ConverterSpec(FAKE_CONVERTER),
            timeout_seconds=5,
        )
    )[0]
    await runtime.queue.wait(success.id)
    original = await _read(runtime)
    original_hash = original.conversion.transcription_sha256

    failed = (
        await queue_conversion(
            runtime,
            ["Smith2024"],
            converter_spec=ConverterSpec(FAKE_CONVERTER, {"mode": "failure"}),
            timeout_seconds=5,
        )
    )[0]
    assert (await runtime.queue.wait(failed.id)).state is JobState.FAILED
    assert failed.log_path is not None
    failed_record = await _read(runtime)
    assert failed_record.conversion.last_attempt is not None
    assert failed_record.conversion.last_attempt.log_path is not None

    after = await _read(runtime)
    assert after.conversion.transcription_sha256 == original_hash
    assert after.conversion.last_attempt is not None
    assert after.conversion.last_attempt.state is AttemptState.FAILED
    assert after.conversion.last_attempt.log_path is not None
    assert after.conversion.last_attempt.log_path.startswith("papers/Smith2024/.pp/")
    assert (runtime.root / after.conversion.last_attempt.log_path).is_file()


async def test_conversion_rejects_source_bytes_that_no_longer_match_record(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime)
    source = runtime.root / "papers" / "Smith2024" / "source" / "source.pdf"
    source.write_bytes(b"%PDF-1.4 tampered source")

    job = (
        await queue_conversion(
            runtime,
            ["Smith2024"],
            converter_spec=ConverterSpec(FAKE_CONVERTER),
            timeout_seconds=5,
        )
    )[0]

    assert (await runtime.queue.wait(job.id)).state is JobState.FAILED
    record = await _read(runtime)
    assert record.conversion.transcription_sha256 is None
    assert record.conversion.last_attempt is not None
    assert "hash no longer matches" in (record.conversion.last_attempt.error or "")


async def test_cancel_mid_conversion_kills_child_and_records_cancelled(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime)
    job = (
        await queue_conversion(
            runtime,
            ["Smith2024"],
            converter_spec=ConverterSpec(FAKE_CONVERTER, {"mode": "hang", "hang_seconds": 30}),
            timeout_seconds=30,
        )
    )[0]
    for _ in range(100):
        if job.state is JobState.RUNNING:
            break
        await asyncio.sleep(0.01)

    assert await cancel_job(runtime, job.id)
    assert (await runtime.queue.wait(job.id)).state is JobState.CANCELLED
    record = await _read(runtime)
    assert record.conversion.last_attempt is not None
    assert record.conversion.last_attempt.state is AttemptState.CANCELLED


async def test_retry_succeeds_after_missing_source_is_restored(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime, install_source=False)
    failed = (
        await queue_conversion(
            runtime,
            ["Smith2024"],
            converter_spec=ConverterSpec(FAKE_CONVERTER),
            timeout_seconds=5,
        )
    )[0]
    assert (await runtime.queue.wait(failed.id)).state is JobState.FAILED
    failed_record = await _read(runtime)
    assert failed_record.conversion.last_attempt is not None
    assert failed_record.conversion.last_attempt.log_path is not None

    async def restore(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        stage = session.stage_dir()
        source = stage / "source.pdf"
        source.write_bytes(b"%PDF-1.4 fake source")
        session.install_artifact(source, "papers/Smith2024/source/source.pdf")

    restored = await runtime.enqueue_paper("Smith2024", JobKind.IMPORT, "restore-source", restore)
    await runtime.queue.wait(restored.id)
    retried = await retry_job(runtime, failed.id)

    assert retried.meta["retry_of"] == failed.id
    assert (await runtime.queue.wait(retried.id)).state is JobState.SUCCEEDED


async def test_pending_selection_uses_recorded_input_hashes(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime)
    assert await pending_conversion_citekeys(runtime) == ["Smith2024"]
    assert await pending_recipe_citekeys(runtime, "summary") == ["Smith2024"]


async def test_pending_selection_detects_tampered_output(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime)
    conversion = (
        await queue_conversion(
            runtime,
            ["Smith2024"],
            converter_spec=ConverterSpec(FAKE_CONVERTER),
            timeout_seconds=5,
        )
    )[0]
    await runtime.queue.wait(conversion.id)
    assert await pending_conversion_citekeys(runtime) == []

    transcription = runtime.root / "papers" / "Smith2024" / "transcription.md"
    transcription.write_text("tampered", encoding="utf-8")

    assert await pending_conversion_citekeys(runtime) == ["Smith2024"]


async def test_job_control_rejects_a_job_from_another_runtime(tmp_path: Path) -> None:
    registry = RuntimeRegistry()
    first = registry.create(tmp_path / "first")
    second = registry.create(tmp_path / "second")

    async def fail(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del session, job, token
        raise RuntimeError("failure")

    job = await first.enqueue_paper("Smith2024", JobKind.IMPORT, "fail", fail)
    await first.queue.wait(job.id)

    with pytest.raises(ValueError, match="different library"):
        await cancel_job(second, job.id)
    with pytest.raises(ValueError, match="different library"):
        await retry_job(second, job.id)


async def test_retry_reconstructs_interrupted_conversion(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime)
    runtime.interrupted_attempts = (
        InterruptedAttempt(
            job_id="interrupted-1",
            target="papers/Smith2024",
            operation="convert",
            kind=JobKind.CONVERSION,
            scope=JobScope.PAPER,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )

    retried = await retry_job(
        runtime,
        "interrupted-1",
        converter_spec=ConverterSpec(FAKE_CONVERTER),
        timeout_seconds=5,
    )

    assert retried.meta["retry_of"] == "interrupted-1"
    assert runtime.interrupted_attempts == ()
    assert (await runtime.queue.wait(retried.id)).state is JobState.SUCCEEDED


async def test_recipe_batch_rejects_duplicate_output_destinations(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    recipes = {
        "one": RecipeDefinition("one", 1, "transcription", "same.md", "One."),
        "two": RecipeDefinition("two", 1, "transcription", "SAME.md", "Two."),
    }

    with pytest.raises(ValueError, match="same output filename"):
        await queue_recipes(
            runtime,
            ["one", "two"],
            ["Smith2024"],
            provider_name="fake",
            recipes=recipes,
        )


async def test_recipe_failure_records_attempt_and_operational_log(tmp_path: Path) -> None:
    provider = FakeLLMProvider(fail=True)
    runtime = await _runtime(tmp_path, provider)
    await _seed(runtime)
    transcription = runtime.root / "papers" / "Smith2024" / "transcription.md"
    transcription.write_text("converted text", encoding="utf-8")
    record = await _read(runtime)
    record.conversion.transcription_sha256 = sha256_file(transcription)
    record.conversion.source_sha256 = record.source_sha256

    async def update(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        session.write_record(record)

    update_job = await runtime.enqueue_paper(
        "Smith2024", JobKind.IMPORT, "record-conversion", update
    )
    await runtime.queue.wait(update_job.id)

    failed = (
        await queue_recipes(
            runtime,
            ["summary"],
            ["Smith2024"],
            provider_name="fake",
            model="test-model",
        )
    )[0]
    assert (await runtime.queue.wait(failed.id)).state is JobState.FAILED
    assert failed.log_path is not None
    after = await _read(runtime)
    attempt = after.recipes["summary"].last_attempt
    assert attempt is not None
    assert attempt.state is AttemptState.FAILED
    assert attempt.log_path is not None
    assert (runtime.root / attempt.log_path).is_file()


def _bytes_sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()
