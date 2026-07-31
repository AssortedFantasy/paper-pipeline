from pathlib import Path

import pytest

from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.services.job_ops import retry_selected_jobs
from paper_pipeline.services.runtime import PaperSession, RuntimeRegistry


async def test_retry_selected_jobs_is_library_scoped_and_validate_first(tmp_path: Path) -> None:
    registry = RuntimeRegistry()
    first = registry.create(tmp_path / "first")
    second = registry.create(tmp_path / "second")

    async def fail(
        _session: PaperSession,
        _job: Job,
        _token: CancellationToken,
    ) -> None:
        raise RuntimeError("expected failure")

    own = await first.enqueue_paper("Own2026", JobKind.RECIPE_FINALIZE, "own", fail)
    other = await second.enqueue_paper("Other2026", JobKind.RECIPE_FINALIZE, "other", fail)
    assert (await first.queue.wait(own.id)).state is JobState.FAILED
    assert (await second.queue.wait(other.id)).state is JobState.FAILED
    before = len(first.queue.list_jobs())

    with pytest.raises(ValueError, match="does not belong to this library"):
        await retry_selected_jobs(
            first,
            [own.id, other.id],
            converter_spec=ConverterSpec("tests.fakes:FakeConverter"),
            timeout_seconds=5,
        )

    assert len(first.queue.list_jobs()) == before
    await registry.queue.shutdown()


async def test_retry_selected_jobs_enqueues_one_replacement_per_job(tmp_path: Path) -> None:
    registry = RuntimeRegistry()
    runtime = registry.create(tmp_path / "library")

    async def fail(
        _session: PaperSession,
        _job: Job,
        _token: CancellationToken,
    ) -> None:
        raise RuntimeError("expected failure")

    originals = [
        await runtime.enqueue_paper(citekey, JobKind.RECIPE_FINALIZE, "batch", fail)
        for citekey in ("First2026", "Second2026")
    ]
    for job in originals:
        assert (await runtime.queue.wait(job.id)).state is JobState.FAILED

    replacements = await retry_selected_jobs(
        runtime,
        [job.id for job in originals],
        converter_spec=ConverterSpec("tests.fakes:FakeConverter"),
        timeout_seconds=5,
    )

    assert [job.meta["retry_of"] for job in replacements] == [job.id for job in originals]
    for job in replacements:
        assert (await runtime.queue.wait(job.id)).state is JobState.FAILED
    await registry.queue.shutdown()
