import asyncio

import pytest

from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.queue import CancellationToken, JobQueue, PartialJobError


async def test_two_conversions_never_overlap_even_across_libraries() -> None:
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

    await queue.enqueue_paper("one", "a", JobKind.CONVERSION, "convert", first)
    await first_started.wait()
    await queue.enqueue_paper("two", "b", JobKind.CONVERSION, "convert", second)
    await asyncio.sleep(0)
    assert second_started.is_set() is False
    release_first.set()
    await queue.join()
    assert second_started.is_set() is True


async def test_conversion_recipe_and_import_share_one_paper_lane() -> None:
    queue = JobQueue(llm_concurrency=3)
    running = 0
    maximum = 0
    completed: set[JobKind] = set()

    async def worker(job: Job) -> None:
        nonlocal maximum, running
        running += 1
        maximum = max(maximum, running)
        await asyncio.sleep(0)
        running -= 1
        completed.add(job.kind)

    await queue.enqueue_paper("library", "paper", JobKind.CONVERSION, "convert", worker)
    await queue.enqueue_paper("library", "paper", JobKind.RECIPE, "recipes", worker)
    await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "refresh", worker)

    await queue.join()
    assert maximum == 1
    assert completed == {JobKind.CONVERSION, JobKind.RECIPE, JobKind.IMPORT}


async def test_recipes_are_concurrent_across_papers_but_sequential_within_a_paper() -> None:
    queue = JobQueue(llm_concurrency=2)
    first_started = asyncio.Event()
    other_started = asyncio.Event()
    same_paper_started = asyncio.Event()
    third_paper_started = asyncio.Event()
    releases = {
        "first": asyncio.Event(),
        "other": asyncio.Event(),
        "same-paper": asyncio.Event(),
        "third-paper": asyncio.Event(),
    }
    running = 0
    maximum = 0

    async def recipe_batch(job: Job) -> None:
        nonlocal maximum, running
        running += 1
        maximum = max(maximum, running)
        {
            "first": first_started,
            "other": other_started,
            "same-paper": same_paper_started,
            "third-paper": third_paper_started,
        }[job.label].set()
        await releases[job.label].wait()
        running -= 1

    await queue.enqueue_paper("library", "one", JobKind.RECIPE, "first", recipe_batch)
    await queue.enqueue_paper("library", "two", JobKind.RECIPE, "other", recipe_batch)
    await asyncio.gather(first_started.wait(), other_started.wait())

    await queue.enqueue_paper("library", "one", JobKind.RECIPE, "same-paper", recipe_batch)
    await queue.enqueue_paper("library", "three", JobKind.RECIPE, "third-paper", recipe_batch)
    await asyncio.sleep(0)
    assert same_paper_started.is_set() is False
    assert third_paper_started.is_set() is False

    releases["other"].set()
    await third_paper_started.wait()
    assert same_paper_started.is_set() is False

    releases["first"].set()
    await same_paper_started.wait()
    releases["third-paper"].set()
    releases["same-paper"].set()
    await queue.join()

    assert maximum == 2


async def test_library_write_waits_for_papers_and_blocks_new_papers() -> None:
    queue = JobQueue()
    first_paper_started = asyncio.Event()
    release_first_paper = asyncio.Event()
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    second_paper_started = asyncio.Event()

    async def first_paper(job: Job) -> None:
        del job
        first_paper_started.set()
        await release_first_paper.wait()

    async def write(job: Job) -> None:
        del job
        write_started.set()
        await release_write.wait()

    async def second_paper(job: Job) -> None:
        del job
        second_paper_started.set()

    await queue.enqueue_paper("library", "one", JobKind.IMPORT, "import", first_paper)
    await first_paper_started.wait()
    await queue.enqueue_library_write("library", JobKind.MAINTENANCE, "reindex", write)
    await queue.enqueue_paper("library", "two", JobKind.IMPORT, "import", second_paper)

    await asyncio.sleep(0)
    assert write_started.is_set() is False
    assert second_paper_started.is_set() is False
    release_first_paper.set()
    await write_started.wait()
    assert second_paper_started.is_set() is False
    release_write.set()
    await queue.join()
    assert second_paper_started.is_set() is True


async def test_library_read_is_nonexclusive_with_paper_work() -> None:
    queue = JobQueue()
    paper_started = asyncio.Event()
    release = asyncio.Event()
    read_started = asyncio.Event()

    async def paper(job: Job) -> None:
        del job
        paper_started.set()
        await release.wait()

    async def read(job: Job) -> None:
        del job
        read_started.set()

    await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "import", paper)
    await paper_started.wait()
    read_job = await queue.enqueue_library_read("library", JobKind.MAINTENANCE, "validate", read)

    await asyncio.wait_for(read_started.wait(), timeout=1)
    assert read_job.state is JobState.SUCCEEDED
    release.set()
    await queue.join()


async def test_remote_coordinator_holds_no_library_or_paper_barrier() -> None:
    queue = JobQueue()
    remote_started = asyncio.Event()
    release_remote = asyncio.Event()
    paper_started = asyncio.Event()
    write_started = asyncio.Event()

    async def remote(job: Job) -> None:
        del job
        remote_started.set()
        await release_remote.wait()

    async def paper(job: Job) -> None:
        del job
        paper_started.set()

    async def write(job: Job) -> None:
        del job
        write_started.set()

    coordinator = await queue.enqueue_remote(
        "library",
        JobKind.RECIPE_BATCH,
        "batch",
        remote,
    )
    await remote_started.wait()
    await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "import", paper)
    await asyncio.wait_for(paper_started.wait(), timeout=1)
    await queue.enqueue_library_write("library", JobKind.MAINTENANCE, "write", write)
    await asyncio.wait_for(write_started.wait(), timeout=1)

    assert coordinator.scope is JobScope.REMOTE
    release_remote.set()
    await queue.join()


async def test_partial_coordinator_has_a_first_class_terminal_state() -> None:
    queue = JobQueue()

    async def worker(job: Job) -> None:
        del job
        raise PartialJobError("one request failed")

    job = await queue.enqueue_remote(
        "library",
        JobKind.RECIPE_BATCH,
        "batch",
        worker,
    )

    assert (await queue.wait(job.id)).state is JobState.PARTIAL
    assert job.error == "one request failed"


async def test_library_reads_and_writes_are_mutually_exclusive() -> None:
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

    await queue.enqueue_library_write("library", JobKind.MAINTENANCE, "reindex", first)
    await first_started.wait()
    await queue.enqueue_library_read("library", JobKind.MAINTENANCE, "validate", second)
    await asyncio.sleep(0)
    assert second_started.is_set() is False
    release_first.set()
    await queue.join()
    assert second_started.is_set() is True

    first_started.clear()
    release_first.clear()
    second_started.clear()
    await queue.enqueue_library_read("library", JobKind.MAINTENANCE, "validate", first)
    await first_started.wait()
    await queue.enqueue_library_write("library", JobKind.MAINTENANCE, "reindex", second)
    await asyncio.sleep(0)
    assert second_started.is_set() is False
    release_first.set()
    await queue.join()
    assert second_started.is_set() is True


async def test_cancel_is_rejected_after_worker_begins_durable_commit() -> None:
    queue = JobQueue()
    committing = asyncio.Event()
    release = asyncio.Event()

    async def worker(job: Job, token: CancellationToken) -> None:
        del job
        assert token.begin_commit() is True
        committing.set()
        await release.wait()

    job = await queue.enqueue_paper("library", "paper", JobKind.CONVERSION, "convert", worker)
    await committing.wait()

    assert await queue.cancel(job.id) is False
    release.set()

    assert (await queue.wait(job.id)).state is JobState.SUCCEEDED


async def test_clean_shutdown_forces_uncooperative_worker_and_rejects_enqueue() -> None:
    queue = JobQueue()
    started = asyncio.Event()
    stopped = asyncio.Event()
    never = asyncio.Event()

    async def uncooperative(job: Job) -> None:
        del job
        started.set()
        try:
            await never.wait()
        finally:
            stopped.set()

    job = await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "import", uncooperative)
    await started.wait()

    await queue.shutdown(grace_seconds=0.01)

    assert job.state is JobState.CANCELLED
    assert stopped.is_set()
    with pytest.raises(RuntimeError):
        await queue.enqueue_paper("library", "other", JobKind.IMPORT, "import", uncooperative)
