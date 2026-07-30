"""End-to-end processing service tests using only fake external edges."""

import asyncio
import threading
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
from paper_pipeline.library.storage import recipe_is_fresh, sha256_file
from paper_pipeline.recipes.model import RecipeDefinition
from paper_pipeline.recipes.provider import ProviderRequest, ProviderResult
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
    citekey: str = "Smith2024",
    install_source: bool = True,
) -> None:
    source_bytes = b"%PDF-1.4 fake source"
    source_relative = f"papers/{citekey}/source/source.pdf"
    record = PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(citekey=citekey, title=f"Test paper {citekey}"),
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

    job = await runtime.enqueue_paper(citekey, JobKind.IMPORT, "seed", worker)
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED


async def _read(runtime: LibraryRuntime, citekey: str = "Smith2024") -> PaperRecord:
    records: list[PaperRecord] = []

    async def worker(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        records.append(session.read_paper(citekey))

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "read", worker)
    await runtime.queue.wait(job.id)
    return records[0]


async def test_processing_uses_real_queue_and_recipe_batching_contract(tmp_path: Path) -> None:
    class BlockingProvider:
        name = "recording"

        def __init__(self) -> None:
            self.barrier = threading.Barrier(2)
            self.lock = threading.Lock()
            self.active = 0
            self.maximum_active = 0
            self.calls: list[tuple[str, str]] = []

        def generate(self, request: ProviderRequest) -> ProviderResult:
            assert request.pdf_input is not None
            citekey = request.pdf_input.parents[1].name
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            try:
                if request.prompt == "First.":
                    self.barrier.wait(timeout=2)
                with self.lock:
                    self.calls.append((citekey, request.prompt))
                return ProviderResult(
                    ok=True,
                    text=f"{citekey}: {request.prompt}",
                    provider=self.name,
                    model=request.model,
                )
            finally:
                with self.lock:
                    self.active -= 1

    provider = BlockingProvider()
    runtime = RuntimeRegistry(
        llm_concurrency=2,
        provider_factories={"recording": lambda: provider},
    ).create(tmp_path / "library")
    await _seed(runtime)
    await _seed(runtime, citekey="Jones2025")

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
    assert converted.conversion.last_attempt.log_path is not None
    conversion_log = runtime.root / converted.conversion.last_attempt.log_path
    assert "conversion succeeded" in conversion_log.read_text(encoding="utf-8")
    assert (runtime.root / "papers" / "Smith2024" / "pages" / "page1.png").is_file()

    recipes = {
        "first": RecipeDefinition("first", 1, "pdf", "first.md", "First."),
        "second": RecipeDefinition("second", 1, "pdf", "second.md", "Second."),
    }
    recipe_jobs = await queue_recipes(
        runtime,
        ["first", "second"],
        ["Smith2024", "Jones2025"],
        provider_name="recording",
        model="test-model",
        recipes=recipes,
    )
    completed = await asyncio.gather(*(runtime.queue.wait(job.id) for job in recipe_jobs))

    assert all(job.state is JobState.SUCCEEDED for job in completed)
    assert provider.maximum_active == 2
    for citekey in ("Smith2024", "Jones2025"):
        assert [prompt for paper, prompt in provider.calls if paper == citekey] == [
            "First.",
            "Second.",
        ]
        enriched = await _read(runtime, citekey)
        assert set(enriched.recipes) == {"first", "second"}
        for recipe in enriched.recipes.values():
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


async def test_failed_recipe_rerun_preserves_last_good_artifact(tmp_path: Path) -> None:
    provider = FakeLLMProvider(response="Last good result")
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

    succeeded = (
        await queue_recipes(
            runtime,
            ["summary"],
            ["Smith2024"],
            provider_name="fake",
            model="test-model",
        )
    )[0]
    assert (await runtime.queue.wait(succeeded.id)).state is JobState.SUCCEEDED
    before = await _read(runtime)
    before_recipe = before.recipes["summary"]
    assert before_recipe.output_artifact is not None
    output = runtime.root / before_recipe.output_artifact
    before_bytes = output.read_bytes()

    provider.fail = True
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
    after_recipe = after.recipes["summary"]
    assert output.read_bytes() == before_bytes
    assert after_recipe.output_sha256 == before_recipe.output_sha256
    assert after_recipe.input_sha256 == before_recipe.input_sha256
    assert after_recipe.completed_at == before_recipe.completed_at
    assert after_recipe.provider == before_recipe.provider
    assert after_recipe.model == before_recipe.model
    assert recipe_is_fresh(after, "summary")
    assert await pending_recipe_citekeys(runtime, "summary") == []


def _bytes_sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()
