"""End-to-end processing service tests using only fake external edges."""

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.fakes import FakeLLMProvider

from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.jobs.events import JobEventKind
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.jobs.recovery import InterruptedAttempt
from paper_pipeline.library.model import (
    AttemptState,
    ConversionRecord,
    PaperMetadata,
    PaperRecord,
)
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.pages.runner import PageRendererSpec
from paper_pipeline.recipes.batch_model import RecipeRunPhase
from paper_pipeline.recipes.batch_store import RecipeRunStore
from paper_pipeline.recipes.model import RecipeDefinition
from paper_pipeline.services import recipe_batches as recipe_batches_module
from paper_pipeline.services.processing import (
    cancel_job,
    pending_conversion_citekeys,
    pending_page_render_citekeys,
    pending_recipe_citekeys,
    queue_conversion,
    queue_page_render,
    queue_recipes,
    retry_job,
)
from paper_pipeline.services.recipe_batches import resume_recipe_runs
from paper_pipeline.services.runtime import (
    LibraryRuntime,
    LibrarySession,
    PaperSession,
    RuntimeRegistry,
)

FAKE_CONVERTER = "tests.fakes:FakeConverter"
FAKE_PAGE_RENDERER = "tests.fakes:FakePageRenderer"


async def _runtime(tmp_path: Path, provider: FakeLLMProvider | None = None) -> LibraryRuntime:
    provider = provider or FakeLLMProvider(response="Generated result")
    return RuntimeRegistry(provider_factories={"fake": lambda: provider}).create(
        tmp_path / "library"
    )


async def _seed(
    runtime: LibraryRuntime,
    *,
    citekey: str = "Smith2024",
    converted: bool = False,
) -> None:
    source_bytes = b"%PDF-1.4 fake source"
    transcription_bytes = b"converted text"
    source_relative = f"papers/{citekey}/source/source.pdf"
    record = PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(citekey=citekey, title=f"Test paper {citekey}"),
        source_pdf=source_relative,
        source_sha256=_bytes_sha256(source_bytes),
    )

    async def worker(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        stage = session.stage_dir()
        staged = stage / "source.pdf"
        staged.write_bytes(source_bytes)
        session.install_artifact(staged, source_relative)
        if converted:
            stage = session.stage_dir()
            staged = stage / "transcription.md"
            staged.write_bytes(transcription_bytes)
            transcription_sha256 = session.install_artifact(
                staged, f"papers/{citekey}/transcription.md"
            )
            record.conversion = ConversionRecord(
                source_sha256=record.source_sha256,
                transcription_sha256=transcription_sha256,
            )
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


async def test_recipe_cohort_uses_one_remote_batch_and_one_upload_per_distinct_pdf(
    tmp_path: Path,
) -> None:
    provider = FakeLLMProvider(response="Generated in one remote cohort.")
    runtime = RuntimeRegistry(
        provider_factories={"fake": lambda: provider},
    ).create(tmp_path / "library")
    await _seed(runtime)
    await _seed(runtime, citekey="Jones2025")

    recipes = {
        "first": RecipeDefinition("first", 1, "pdf", "first.md", "First."),
        "second": RecipeDefinition("second", 1, "pdf", "second.md", "Second."),
    }
    recipe_jobs = await queue_recipes(
        runtime,
        ["first", "second"],
        ["Smith2024", "Jones2025"],
        provider_name="fake",
        model="test-model",
        recipes=recipes,
    )
    completed = await asyncio.gather(*(runtime.queue.wait(job.id) for job in recipe_jobs))

    assert all(job.state is JobState.SUCCEEDED for job in completed)
    assert provider.created_batch_count == 1
    # The two fixture papers deliberately contain identical PDF bytes, so the
    # cohort uploads that distinct input hash once and reuses its file ID.
    assert provider.input_upload_count == 1
    parent = completed[0]
    assert parent.meta["progress_stage"] == "done"
    assert parent.meta["progress_upload_done"] == "1"
    assert parent.meta["progress_remote_finished"] == "4"
    assert parent.meta["progress_install_successes"] == "4"
    assert parent.meta["progress_cleanup_done"] == parent.meta["progress_cleanup_total"]
    assert parent.meta["progress_local_cleanup"].endswith("of working files removed")
    run_dir = RecipeRunStore(runtime.root).run_dir(parent.meta["run_id"])
    assert {item.name for item in run_dir.iterdir()} == {
        "manifest.json",
        "state.json",
        "summary.log",
    }
    (run_dir / "snapshots").mkdir()
    (run_dir / "snapshots" / "legacy.pdf").write_bytes(b"duplicated PDF")
    (run_dir / "requests.jsonl").write_text("legacy request\n", encoding="utf-8")
    assert await resume_recipe_runs(runtime) == []
    assert not (run_dir / "snapshots").exists()
    assert not (run_dir / "requests.jsonl").exists()
    assert [request.prompt for request in provider.calls] == [
        "First.",
        "Second.",
        "First.",
        "Second.",
    ]
    for citekey in ("Smith2024", "Jones2025"):
        assert set((await _read(runtime, citekey)).recipes) == {"first", "second"}


async def test_recipe_batch_publishes_each_remote_poll_as_visible_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recipe_batches_module, "_POLL_INTERVAL_SECONDS", 0)
    provider = FakeLLMProvider(
        response="Generated after visible polling.",
        batch_statuses=["validating", "in_progress", "completed"],
    )
    runtime = await _runtime(tmp_path, provider)
    await _seed(runtime)
    subscription = runtime.queue.events.subscribe(max_queue_size=200)

    parent = (
        await queue_recipes(
            runtime,
            ["summary"],
            ["Smith2024"],
            provider_name="fake",
            model="test-model",
        )
    )[0]
    assert (await runtime.queue.wait(parent.id)).state is JobState.SUCCEEDED

    events = []
    while True:
        try:
            events.append(subscription.get_nowait())
        except asyncio.QueueEmpty:
            break
    subscription.close()
    progress = [
        event.message
        for event in events
        if event.job_id == parent.id and event.kind is JobEventKind.PROGRESS
    ]
    assert any(message and "Provider validating" in message for message in progress)
    assert any(message and "Provider in progress" in message for message in progress)
    assert any(message and "Provider completed" in message for message in progress)
    assert parent.meta["progress_poll_count"] == "3"
    assert parent.meta["progress_last_provider_check"]
    assert parent.meta["progress_stage"] == "done"


async def test_recipe_batch_download_does_not_block_application_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeLLMProvider(response="Downloaded without freezing the dashboard.")
    runtime = await _runtime(tmp_path, provider)
    await _seed(runtime)
    download_started = threading.Event()
    download_finished = threading.Event()
    original_download = provider.download_file

    def slow_download(file_id: str, destination: Path) -> None:
        download_started.set()
        try:
            time.sleep(0.2)
            original_download(file_id, destination)
        finally:
            download_finished.set()

    monkeypatch.setattr(provider, "download_file", slow_download)
    loop_ticks = 0

    async def observe_download() -> None:
        nonlocal loop_ticks
        while not download_started.is_set():
            await asyncio.sleep(0)
        while not download_finished.is_set():
            loop_ticks += 1
            await asyncio.sleep(0.01)

    observer = asyncio.create_task(observe_download())
    parent = (
        await queue_recipes(
            runtime,
            ["summary"],
            ["Smith2024"],
            provider_name="fake",
            model="test-model",
        )
    )[0]

    assert (await runtime.queue.wait(parent.id)).state is JobState.SUCCEEDED
    await observer
    assert loop_ticks >= 5


async def test_recipe_batch_cancellation_matches_durable_run_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recipe_batches_module, "_POLL_INTERVAL_SECONDS", 0.01)
    provider = FakeLLMProvider(
        response="Should not be installed.",
        batch_statuses=["in_progress"] * 20,
    )
    runtime = await _runtime(tmp_path, provider)
    await _seed(runtime)
    parent = (
        await queue_recipes(
            runtime,
            ["summary"],
            ["Smith2024"],
            provider_name="fake",
            model="test-model",
        )
    )[0]
    for _attempt in range(100):
        if provider.created_batch_count:
            break
        await asyncio.sleep(0.01)

    assert await cancel_job(runtime, parent.id)
    assert (await runtime.queue.wait(parent.id)).state is JobState.CANCELLED
    state = RecipeRunStore(runtime.root).read_state(parent.meta["run_id"])

    assert state.phase is RecipeRunPhase.CANCELLED
    assert state.finalized == []
    assert (await _read(runtime)).recipes == {}


async def test_partial_batch_installs_valid_siblings_and_retries_only_failures(
    tmp_path: Path,
) -> None:
    provider = FakeLLMProvider(
        response="Valid generated result.",
        fail_prompts={"Second."},
    )
    runtime = await _runtime(tmp_path, provider)
    await _seed(runtime)
    recipes = {
        "first": RecipeDefinition("first", 1, "pdf", "first.md", "First."),
        "second": RecipeDefinition("second", 1, "pdf", "second.md", "Second."),
    }

    parent = (
        await queue_recipes(
            runtime,
            ["first", "second"],
            ["Smith2024"],
            provider_name="fake",
            model="test-model",
            recipes=recipes,
        )
    )[0]
    assert (await runtime.queue.wait(parent.id)).state is JobState.PARTIAL

    record = await _read(runtime)
    assert record.recipes["first"].output_artifact == "papers/Smith2024/first.md"
    assert record.recipes["second"].output_artifact is None
    assert record.recipes["second"].last_attempt is not None
    assert record.recipes["second"].last_attempt.state is AttemptState.FAILED
    first_attempt = record.recipes["first"].last_attempt
    second_attempt = record.recipes["second"].last_attempt
    assert first_attempt is not None and first_attempt.log_path is not None
    assert second_attempt is not None and second_attempt.log_path is not None
    first_log = runtime.root / first_attempt.log_path
    second_log = runtime.root / second_attempt.log_path
    assert "recipe=first" in first_log.read_text(encoding="utf-8")
    assert "recipe=second" not in first_log.read_text(encoding="utf-8")
    assert "recipe=second" in second_log.read_text(encoding="utf-8")
    assert "recipe=first" not in second_log.read_text(encoding="utf-8")

    provider.fail_prompts.clear()
    retry = await retry_job(runtime, parent.id)
    assert (await runtime.queue.wait(retry.id)).state is JobState.SUCCEEDED
    assert provider.created_batch_count == 2
    assert [request.prompt for request in provider.calls] == ["First.", "Second.", "Second."]
    assert (await _read(runtime)).recipes["second"].output_artifact == (
        "papers/Smith2024/second.md"
    )


async def test_submitted_batch_resumes_from_durable_run_state_without_resubmission(
    tmp_path: Path,
) -> None:
    provider = FakeLLMProvider(response="Recovered result.", retain_deleted_files=True)
    runtime = await _runtime(tmp_path, provider)
    await _seed(runtime)
    parent = (
        await queue_recipes(
            runtime,
            ["summary"],
            ["Smith2024"],
            provider_name="fake",
            model="test-model",
        )
    )[0]
    assert (await runtime.queue.wait(parent.id)).state is JobState.SUCCEEDED
    run_id = parent.meta["run_id"]
    store = RecipeRunStore(runtime.root)
    state = store.read_state(run_id)
    state.phase = RecipeRunPhase.IN_PROGRESS
    state.outcomes = {}
    state.finalized = []
    store.write_state(state)
    # Reconstruct the empty working directory that would exist for a genuinely
    # interrupted (and therefore not yet pruned) run.
    (store.run_dir(run_id) / "collected").mkdir()

    reopened = RuntimeRegistry(provider_factories={"fake": lambda: provider}).open(runtime.root)
    recovered = await resume_recipe_runs(reopened)
    assert len(recovered) == 1
    assert (await reopened.queue.wait(recovered[0].id)).state is JobState.SUCCEEDED
    assert provider.created_batch_count == 1
    assert store.read_state(run_id).phase is RecipeRunPhase.COMPLETED


async def test_unreadable_disposable_recipe_run_is_discarded(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    store = RecipeRunStore(runtime.root)
    run_dir = store.initialize("broken-run")
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (run_dir / "state.json").write_text("{}", encoding="utf-8")

    assert await resume_recipe_runs(runtime) == []
    assert not run_dir.exists()


async def test_failed_conversion_rerun_preserves_last_good_artifact(tmp_path: Path) -> None:
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
    transcription = runtime.root / "papers" / "Smith2024" / "transcription.md"
    original_bytes = transcription.read_bytes()

    failed = (
        await queue_conversion(
            runtime,
            ["Smith2024"],
            converter_spec=ConverterSpec(FAKE_CONVERTER, {"mode": "failure"}),
            timeout_seconds=5,
        )
    )[0]
    assert (await runtime.queue.wait(failed.id)).state is JobState.FAILED

    after = await _read(runtime)
    assert transcription.read_bytes() == original_bytes
    assert after.conversion.transcription_sha256 == original_hash
    assert after.conversion.last_attempt is not None
    assert after.conversion.last_attempt.state is AttemptState.FAILED


async def test_page_rendering_is_independent_and_tracks_its_own_artifacts(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime)

    job = (
        await queue_page_render(
            runtime,
            ["Smith2024"],
            renderer_spec=PageRendererSpec(FAKE_PAGE_RENDERER, {"page_count": 2}),
            timeout_seconds=5,
        )
    )[0]
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED

    record = await _read(runtime)
    assert record.conversion.transcription_sha256 is None
    assert record.pages.source_sha256 == record.source_sha256
    assert record.pages.renderer == "fake-pages"
    assert record.pages.page_count == 2
    assert set(record.pages.artifacts) == {
        "papers/Smith2024/pages/page1.png",
        "papers/Smith2024/pages/page2.png",
    }
    assert await pending_page_render_citekeys(runtime) == []

    first_page = runtime.root / "papers" / "Smith2024" / "pages" / "page1.png"
    first_page.write_bytes(b"tampered")
    assert await pending_page_render_citekeys(runtime) == ["Smith2024"]


async def test_failed_page_rerender_preserves_last_good_pages(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime)
    succeeded = (
        await queue_page_render(
            runtime,
            ["Smith2024"],
            renderer_spec=PageRendererSpec(FAKE_PAGE_RENDERER),
            timeout_seconds=5,
        )
    )[0]
    assert (await runtime.queue.wait(succeeded.id)).state is JobState.SUCCEEDED
    before = await _read(runtime)
    page = runtime.root / "papers" / "Smith2024" / "pages" / "page1.png"
    before_bytes = page.read_bytes()

    failed = (
        await queue_page_render(
            runtime,
            ["Smith2024"],
            renderer_spec=PageRendererSpec(FAKE_PAGE_RENDERER, {"mode": "failure"}),
            timeout_seconds=5,
        )
    )[0]
    assert (await runtime.queue.wait(failed.id)).state is JobState.FAILED

    after = await _read(runtime)
    assert page.read_bytes() == before_bytes
    assert after.pages.artifacts == before.pages.artifacts
    assert after.pages.last_attempt is not None
    assert after.pages.last_attempt.state is AttemptState.FAILED
    assert await pending_page_render_citekeys(runtime) == []


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
    assert record.conversion.last_attempt.state is AttemptState.FAILED


async def test_cancellation_propagates_to_conversion_attempt(tmp_path: Path) -> None:
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

    assert job.state is JobState.RUNNING
    assert await cancel_job(runtime, job.id)
    assert (await runtime.queue.wait(job.id)).state is JobState.CANCELLED
    record = await _read(runtime)
    assert record.conversion.last_attempt is not None
    assert record.conversion.last_attempt.state is AttemptState.CANCELLED


async def test_pending_selection_uses_provenance_and_installed_bytes(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime, converted=True)
    await _seed(runtime, citekey="Jones2025")

    assert await pending_conversion_citekeys(runtime) == ["Jones2025"]
    assert await pending_recipe_citekeys(runtime, "summary") == [
        "Jones2025",
        "Smith2024",
    ]

    transcription = runtime.root / "papers" / "Smith2024" / "transcription.md"
    transcription.write_text("tampered", encoding="utf-8")

    assert await pending_conversion_citekeys(runtime) == ["Jones2025", "Smith2024"]


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


async def test_retry_reconstructs_interrupted_page_render(tmp_path: Path) -> None:
    runtime = await _runtime(tmp_path)
    await _seed(runtime)
    runtime.interrupted_attempts = (
        InterruptedAttempt(
            job_id="interrupted-pages",
            target="papers/Smith2024",
            operation="render-pages",
            kind=JobKind.PAGE_RENDER,
            scope=JobScope.PAPER,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )

    retried = await retry_job(
        runtime,
        "interrupted-pages",
        page_renderer_spec=PageRendererSpec(FAKE_PAGE_RENDERER),
        page_render_timeout_seconds=5,
    )

    assert retried.meta["retry_of"] == "interrupted-pages"
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
    await _seed(runtime, converted=True)

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
    after = await _read(runtime)
    attempt = after.recipes["summary"].last_attempt
    assert attempt is not None
    assert attempt.state is AttemptState.FAILED
    after_recipe = after.recipes["summary"]
    assert output.read_bytes() == before_bytes
    assert after_recipe.output_sha256 == before_recipe.output_sha256
    assert await pending_recipe_citekeys(runtime, "summary") == []

    output.write_text("tampered", encoding="utf-8")
    assert await pending_recipe_citekeys(runtime, "summary") == ["Smith2024"]


def _bytes_sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()
