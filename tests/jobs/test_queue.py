import asyncio

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


@pytest.mark.parametrize(
    "path",
    [
        (JobState.RUNNING, JobState.SUCCEEDED),
        (JobState.RUNNING, JobState.FAILED),
        (JobState.RUNNING, JobState.CANCELLED),
        (JobState.CANCELLED,),
    ],
)
def test_legal_live_lifecycle_paths_record_phase_timing(path: tuple[JobState, ...]) -> None:
    job = bare_job()

    for state in path:
        transition_job(
            job,
            state,
            error="failure detail" if state is JobState.FAILED else None,
        )

    assert job.state is path[-1]
    assert (job.started_at is not None) is (JobState.RUNNING in path)
    assert job.finished_at is not None
    assert (job.error is not None) is (path[-1] is JobState.FAILED)


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

    with pytest.raises(InvalidJobTransition):
        transition_job(job, target)

    assert job.state is initial


@pytest.mark.parametrize(
    ("worker_fails", "terminal_state"),
    [(False, JobState.SUCCEEDED), (True, JobState.FAILED)],
)
async def test_job_lifecycle_is_published_in_order(
    worker_fails: bool, terminal_state: JobState
) -> None:
    async def worker(job: Job) -> None:
        del job
        if worker_fails:
            raise RuntimeError("expected failure")

    queue = JobQueue()
    subscription = queue.events.subscribe()
    job = await queue.enqueue_library_read(
        "library",
        JobKind.MAINTENANCE,
        "validate",
        worker,
    )

    result = await queue.wait(job.id)
    events = [await subscription.get() for _ in range(3)]

    assert result.state is terminal_state
    assert [event.state for event in events] == [
        JobState.QUEUED,
        JobState.RUNNING,
        terminal_state,
    ]
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    assert all(event.job_id == job.id for event in events)
    assert all(event.kind is JobEventKind.STATE for event in events)
    assert events[-1].error == result.error
    assert (result.error is not None) is worker_fails
    subscription.close()


async def test_entry_points_assign_resource_scopes_and_citekey_shape() -> None:
    queue = JobQueue()

    paper = await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "refresh", no_op)
    read = await queue.enqueue_library_read("library", JobKind.MAINTENANCE, "validate", no_op)
    write = await queue.enqueue_library_write("library", JobKind.MAINTENANCE, "reindex", no_op)
    await queue.join()

    assert (paper.scope, paper.citekey) == (JobScope.PAPER, "paper")
    assert (read.scope, read.citekey) == (JobScope.LIBRARY_READ, None)
    assert (write.scope, write.citekey) == (JobScope.LIBRARY_WRITE, None)


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

    slow.close()
    another = await queue.enqueue_paper("library", "another", JobKind.IMPORT, "refresh", no_op)
    await queue.wait(another.id)
    with pytest.raises(asyncio.QueueEmpty):
        slow.get_nowait()
    fast.close()


async def test_progress_event_does_not_change_state() -> None:
    queue = JobQueue()
    subscription = queue.events.subscribe()
    progress_published = asyncio.Event()
    release = asyncio.Event()

    async def worker(job: Job) -> None:
        queue.publish_progress(job.id, "halfway")
        progress_published.set()
        await release.wait()

    job = await queue.enqueue_paper("library", "paper", JobKind.CONVERSION, "convert", worker)
    await progress_published.wait()
    events = [await subscription.get() for _ in range(3)]

    assert events[-1].kind is JobEventKind.PROGRESS
    assert events[-1].message == "halfway"
    assert events[-1].state is JobState.RUNNING
    assert job.state is JobState.RUNNING
    assert job.progress == "halfway"
    release.set()
    await queue.wait(job.id)
    subscription.close()
