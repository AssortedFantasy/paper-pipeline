import asyncio
import json
from pathlib import Path

from paper_pipeline.cli import main
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.services.library_ops import (
    RebuildTarget,
    create_library,
    get_paper,
    list_papers,
    open_library,
    rebuild_indexes,
    start_library_validation,
    validate_library,
)
from paper_pipeline.services.runtime import LibraryRuntime, PaperSession, RuntimeRegistry


def paper(
    citekey: str = "Smith2024",
    *,
    title: str = "A useful paper",
    authors: list[str] | None = None,
    year: int | None = 2024,
) -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(
            citekey=citekey,
            title=title,
            authors=authors or ["Ada Smith"],
            year=year,
        ),
    )


async def seed(runtime: LibraryRuntime, record: PaperRecord) -> None:
    async def write(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        session.write_record(record)

    job = await runtime.enqueue_paper(record.metadata.citekey, JobKind.IMPORT, "seed", write)
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED


def test_create_and_open_services_reuse_runtime(tmp_path: Path) -> None:
    registry = RuntimeRegistry()

    created = create_library(tmp_path / "library", name="Thesis", registry=registry)
    reopened = open_library(tmp_path / "library", registry=registry)

    assert reopened is created


async def test_validation_is_read_only_and_returns_structured_corruption(
    tmp_path: Path,
) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())

    healthy = await validate_library(runtime)
    assert healthy.ok
    assert runtime.queue.list_jobs()[-1].scope is JobScope.LIBRARY_READ

    unexpected = runtime.root / "papers" / "unexpected.txt"
    unexpected.write_text("bad", encoding="utf-8")
    corrupt = await validate_library(runtime)

    assert not corrupt.ok
    assert any(problem.severity == "error" and problem.action for problem in corrupt.problems)
    assert unexpected.read_text(encoding="utf-8") == "bad"
    assert runtime.queue.list_jobs()[-1].scope is JobScope.LIBRARY_READ


async def test_validation_publishes_one_completed_event_per_category(tmp_path: Path) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())
    await seed(runtime, paper())
    subscription = runtime.queue.events.subscribe()
    try:
        run = await start_library_validation(runtime)
        report = await run.result
        events = []
        while True:
            try:
                events.append(subscription.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        subscription.close()

    phases = [
        json.loads(event.message)
        for event in events
        if event.job_id == run.job.id and event.message is not None
    ]
    assert [phase["key"] for phase in phases] == [
        "metadata",
        "records",
        "sources",
        "transcriptions",
        "pages",
        "recipes",
        "folders",
        "indexes",
    ]
    assert report.phases[-1].key == "indexes"


async def test_reindex_uses_the_library_write_barrier_and_builds_derived_files(
    tmp_path: Path,
) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())
    await seed(runtime, paper())
    started = asyncio.Event()
    release = asyncio.Event()

    async def paper_work(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del session, job, token
        started.set()
        await release.wait()

    await runtime.enqueue_paper("Smith2024", JobKind.IMPORT, "hold-paper", paper_work)
    await started.wait()
    rebuilding = asyncio.create_task(rebuild_indexes(runtime))
    was_blocked = False
    try:
        await asyncio.sleep(0)
        was_blocked = (
            not rebuilding.done() and not (runtime.root / "indexes" / "titles.md").exists()
        )
    finally:
        release.set()

    job = await rebuilding
    assert was_blocked
    assert job.state is JobState.SUCCEEDED
    assert job.scope is JobScope.LIBRARY_WRITE
    assert (runtime.root / "indexes" / "titles.md").is_file()
    assert (runtime.root / "AGENTS.md").is_file()
    assert (runtime.root / ".gitignore").is_file()


async def test_reindex_accepts_a_granular_derived_file_selection(tmp_path: Path) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())
    await seed(runtime, paper())

    job = await rebuild_indexes(
        runtime,
        (RebuildTarget.TITLES, RebuildTarget.AGENTS),
    )

    assert job.state is JobState.SUCCEEDED
    assert (runtime.root / "indexes" / "titles.md").is_file()
    assert (runtime.root / "AGENTS.md").is_file()
    assert not (runtime.root / "indexes" / "authors.md").exists()
    assert not (runtime.root / ".gitignore").exists()


async def test_list_and_get_apply_service_owned_filters_and_pagination(
    tmp_path: Path,
) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())
    for record in (
        paper("Alpha2024", title="Useful methods", authors=["Ada Smith"], year=2024),
        paper("Beta2024", title="Useful results", authors=["Ben Jones"], year=2024),
        paper("Gamma2023", title="Other topic", authors=["Ada Smith"], year=2023),
    ):
        await seed(runtime, record)

    selected = await list_papers(
        runtime,
        query="useful",
        author="smith",
        year=2024,
    )
    paged = await list_papers(runtime, query="useful", offset=1, limit=1)

    assert selected.total == 1
    assert selected.papers[0].metadata.citekey == "Alpha2024"
    assert paged.total == 2
    assert len(paged.papers) == 1
    assert (await get_paper(runtime, "Alpha2024")).metadata.title == "Useful methods"
    assert (await list_papers(runtime, query="absent")).total == 0


def test_cli_exit_categories_for_healthy_corrupt_and_missing_libraries(
    tmp_path: Path, capsys
) -> None:
    healthy = create_library(tmp_path / "healthy", registry=RuntimeRegistry())
    corrupt = create_library(tmp_path / "corrupt", registry=RuntimeRegistry())
    (corrupt.root / "papers" / "unexpected.txt").write_text("bad", encoding="utf-8")
    missing = tmp_path / "missing"

    assert main(["validate", str(healthy.root)]) == 0
    healthy_output = capsys.readouterr()
    assert healthy_output.out
    assert not healthy_output.err

    assert main(["validate", str(corrupt.root)]) == 1
    corrupt_output = capsys.readouterr()
    assert corrupt_output.out
    assert not corrupt_output.err

    assert main(["validate", str(missing)]) == 2
    missing_validation_output = capsys.readouterr()
    assert missing_validation_output.err

    assert main(["reindex", str(healthy.root)]) == 0
    reindex_output = capsys.readouterr()
    assert reindex_output.out
    assert not reindex_output.err

    assert main(["reindex", str(missing)]) == 2
    missing_reindex_output = capsys.readouterr()
    assert missing_reindex_output.err
