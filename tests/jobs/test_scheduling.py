import asyncio

import pytest

from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken, JobQueue


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
    await queue.enqueue_paper("two", "b", JobKind.CONVERSION, "convert", second)

    await first_started.wait()
    await asyncio.sleep(0)
    assert second_started.is_set() is False
    release_first.set()
    await queue.join()
    assert second_started.is_set() is True


async def test_conversion_recipe_and_import_share_one_paper_lane() -> None:
    queue = JobQueue(llm_concurrency=3)
    release = [asyncio.Event(), asyncio.Event()]
    order: list[str] = []

    def worker(name: str, release_event: asyncio.Event | None = None):  # type: ignore[no-untyped-def]
        async def run(job: Job) -> None:
            del job
            order.append(f"start:{name}")
            if release_event is not None:
                await release_event.wait()
            order.append(f"end:{name}")

        return run

    await queue.enqueue_paper(
        "library", "paper", JobKind.CONVERSION, "convert", worker("conversion", release[0])
    )
    await queue.enqueue_paper(
        "library", "paper", JobKind.RECIPE, "recipes", worker("recipe", release[1])
    )
    await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "refresh", worker("import"))

    await asyncio.sleep(0)
    assert order == ["start:conversion"]
    release[0].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert order == ["start:conversion", "end:conversion", "start:recipe"]
    release[1].set()
    await queue.join()
    assert order == [
        "start:conversion",
        "end:conversion",
        "start:recipe",
        "end:recipe",
        "start:import",
        "end:import",
    ]


async def test_recipe_batches_respect_configured_cross_paper_concurrency() -> None:
    queue = JobQueue(llm_concurrency=2)
    two_started = asyncio.Event()
    release = asyncio.Event()
    running = 0
    maximum = 0

    async def recipe_batch(job: Job) -> None:
        nonlocal maximum, running
        del job
        running += 1
        maximum = max(maximum, running)
        if running == 2:
            two_started.set()
        await release.wait()
        running -= 1

    jobs = [
        await queue.enqueue_paper("library", citekey, JobKind.RECIPE, "recipes", recipe_batch)
        for citekey in ("one", "two", "three")
    ]

    await asyncio.wait_for(two_started.wait(), timeout=1)
    assert sum(job.state is JobState.RUNNING for job in jobs) == 2
    release.set()
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


async def test_cancel_queued_job_is_immediate() -> None:
    queue = JobQueue()
    blocker_started = asyncio.Event()
    release = asyncio.Event()
    queued_ran = False

    async def blocker(job: Job) -> None:
        del job
        blocker_started.set()
        await release.wait()

    async def queued(job: Job) -> None:
        nonlocal queued_ran
        del job
        queued_ran = True

    await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "first", blocker)
    await blocker_started.wait()
    queued_job = await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "second", queued)

    assert await queue.cancel(queued_job.id) is True
    assert queued_job.state is JobState.CANCELLED
    assert (await queue.wait(queued_job.id)).state is JobState.CANCELLED
    assert queued_ran is False
    release.set()
    await queue.join()


async def test_cancel_running_job_sets_token_and_calls_kill_hook() -> None:
    queue = JobQueue()
    started = asyncio.Event()
    hook_called = asyncio.Event()

    async def worker(job: Job, token: CancellationToken) -> None:
        del job
        started.set()
        await token.wait()

    async def kill_hook() -> None:
        hook_called.set()

    job = await queue.enqueue_paper(
        "library",
        "paper",
        JobKind.CONVERSION,
        "convert",
        worker,
        kill_hook=kill_hook,
    )
    await started.wait()

    assert await queue.cancel(job.id) is True
    result = await queue.wait(job.id)

    assert hook_called.is_set() is True
    assert result.state is JobState.CANCELLED
    assert result.error == "job cancelled"


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


async def test_retry_creates_fresh_job_and_preserves_old_terminal_job() -> None:
    queue = JobQueue()
    attempts = 0

    async def flaky(job: Job) -> None:
        nonlocal attempts
        del job
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first attempt")

    failed = await queue.enqueue_paper(
        "library", "paper", JobKind.RECIPE, "summary", flaky, meta={"recipe": "summary"}
    )
    await queue.wait(failed.id)

    retried = await queue.retry(failed.id)
    await queue.wait(retried.id)

    assert failed.state is JobState.FAILED
    assert retried.id != failed.id
    assert retried.state is JobState.SUCCEEDED
    assert retried.meta == {"recipe": "summary", "retry_of": failed.id}


async def test_clean_shutdown_forces_uncooperative_worker_and_rejects_enqueue() -> None:
    queue = JobQueue()
    started = asyncio.Event()
    never = asyncio.Event()

    async def uncooperative(job: Job) -> None:
        del job
        started.set()
        await never.wait()

    job = await queue.enqueue_paper("library", "paper", JobKind.IMPORT, "import", uncooperative)
    await started.wait()

    await queue.shutdown(grace_seconds=0.01)

    assert job.state is JobState.CANCELLED
    assert not any(
        task.get_name().startswith("paper-pipeline-job-") and not task.done()
        for task in asyncio.all_tasks()
    )
    with pytest.raises(RuntimeError, match="shut down"):
        await queue.enqueue_paper("library", "other", JobKind.IMPORT, "import", uncooperative)
