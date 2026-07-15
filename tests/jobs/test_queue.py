import asyncio
from datetime import UTC, datetime

import pytest

from paper_pipeline.jobs.events import EventBus, JobEventKind
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.queue import InvalidJobTransition, JobQueue, transition_job


def bare_job(*, state: JobState = JobState.QUEUED) -> Job:
    return Job(
        id="job-1",
        kind=JobKind.CONVERSION,
        scope=JobScope.PAPER,
        library_key="library",
        citekey="paper",
        label="convert",
        state=state,
    )


async def no_op(job: Job) -> None:
    del job


def test_legal_transitions_set_timestamps_and_error() -> None:
    job = bare_job()
    started = datetime(2026, 1, 1, tzinfo=UTC)
    finished = datetime(2026, 1, 2, tzinfo=UTC)

    transition_job(job, JobState.RUNNING, now=started)
    transition_job(job, JobState.FAILED, error="broken", now=finished)

    assert job.state is JobState.FAILED
    assert job.started_at == started
    assert job.finished_at == finished
    assert job.error == "broken"


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        (JobState.QUEUED, JobState.SUCCEEDED),
        (JobState.QUEUED, JobState.FAILED),
        (JobState.RUNNING, JobState.QUEUED),
        (JobState.SUCCEEDED, JobState.RUNNING),
        (JobState.FAILED, JobState.RUNNING),
        (JobState.CANCELLED, JobState.RUNNING),
        (JobState.INTERRUPTED, JobState.QUEUED),
    ],
)
def test_illegal_transitions_are_rejected(initial: JobState, target: JobState) -> None:
    job = bare_job(state=initial)

    with pytest.raises(InvalidJobTransition, match="cannot transition"):
        transition_job(job, target)

    assert job.state is initial


async def test_event_order_follows_job_lifecycle() -> None:
    queue = JobQueue()
    subscription = queue.events.subscribe()

    job = await queue.enqueue_paper(
        "library",
        "paper",
        JobKind.CONVERSION,
        "convert",
        no_op,
    )
    await queue.wait(job.id)
    events = [await subscription.get() for _ in range(3)]

    assert [event.state for event in events] == [
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.SUCCEEDED,
    ]
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    assert all(event.job_id == job.id for event in events)
    assert all(event.kind is JobEventKind.STATE for event in events)


async def test_worker_failure_becomes_failed_job_and_event() -> None:
    async def fail(job: Job) -> None:
        del job
        raise RuntimeError("boom")

    queue = JobQueue()
    subscription = queue.events.subscribe()
    job = await queue.enqueue_library_read(
        "library",
        JobKind.MAINTENANCE,
        "validate",
        fail,
    )

    result = await queue.wait(job.id)
    events = [await subscription.get() for _ in range(3)]

    assert result.state is JobState.FAILED
    assert result.error == "RuntimeError: boom"
    assert events[-1].state is JobState.FAILED
    assert events[-1].error == "RuntimeError: boom"


async def test_paper_lane_is_acquired_before_worker_runs() -> None:
    queue = JobQueue()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first(job: Job) -> None:
        del job
        first_started.set()
        await release_first.wait()

    async def second(job: Job) -> None:
        del job
        second_started.set()

    first_job = await queue.enqueue_paper("library", "paper", JobKind.CONVERSION, "convert", first)
    second_job = await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "refresh", second)

    await first_started.wait()
    await asyncio.sleep(0)
    assert second_started.is_set() is False
    assert second_job.state is JobState.QUEUED

    release_first.set()
    await queue.join()
    assert second_started.is_set() is True
    assert first_job.state is JobState.SUCCEEDED
    assert second_job.state is JobState.SUCCEEDED


async def test_same_citekey_in_different_libraries_uses_different_lanes() -> None:
    queue = JobQueue()
    both_running = asyncio.Event()
    release = asyncio.Event()
    running = 0

    async def worker(job: Job) -> None:
        nonlocal running
        del job
        running += 1
        if running == 2:
            both_running.set()
        await release.wait()
        running -= 1

    await queue.enqueue_paper("one", "paper", JobKind.RECIPE, "summary", worker)
    await queue.enqueue_paper("two", "paper", JobKind.RECIPE, "summary", worker)

    await asyncio.wait_for(both_running.wait(), timeout=1)
    release.set()
    await queue.join()


async def test_separate_library_entry_points_assign_scopes() -> None:
    queue = JobQueue()

    read = await queue.enqueue_library_read("library", JobKind.MAINTENANCE, "validate", no_op)
    write = await queue.enqueue_library_write("library", JobKind.MAINTENANCE, "reindex", no_op)
    await queue.join()

    assert read.scope is JobScope.LIBRARY_READ
    assert write.scope is JobScope.LIBRARY_WRITE
    assert read.citekey is None
    assert write.citekey is None


async def test_slow_subscriber_does_not_block_job_or_fast_subscriber() -> None:
    bus = EventBus()
    slow = bus.subscribe(max_queue_size=1)
    fast = bus.subscribe(max_queue_size=10)
    queue = JobQueue(events=bus)

    job = await queue.enqueue_paper("library", "paper", JobKind.CONVERSION, "convert", no_op)
    result = await asyncio.wait_for(queue.wait(job.id), timeout=1)
    fast_events = [await fast.get() for _ in range(3)]

    assert result.state is JobState.SUCCEEDED
    assert [event.state for event in fast_events] == [
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.SUCCEEDED,
    ]
    assert slow.dropped_count == 2
    assert slow.get_nowait().state is JobState.SUCCEEDED


async def test_progress_event_does_not_change_state() -> None:
    queue = JobQueue()
    subscription = queue.events.subscribe()
    release = asyncio.Event()

    async def worker(job: Job) -> None:
        queue.publish_progress(job.id, "halfway")
        await release.wait()

    job = await queue.enqueue_paper("library", "paper", JobKind.CONVERSION, "convert", worker)
    await asyncio.sleep(0)
    events = [await subscription.get() for _ in range(3)]

    assert events[-1].kind is JobEventKind.PROGRESS
    assert events[-1].message == "halfway"
    assert events[-1].state is JobState.RUNNING
    assert job.state is JobState.RUNNING
    assert job.progress == "halfway"
    release.set()
    await queue.wait(job.id)
