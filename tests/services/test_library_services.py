import asyncio
from pathlib import Path

from paper_pipeline.cli import main
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.services.library_ops import (
    create_library,
    get_paper,
    list_papers,
    open_library,
    rebuild_indexes,
    validate_library,
)
from paper_pipeline.services.runtime import LibraryRuntime, PaperSession, RuntimeRegistry


def paper(citekey: str = "Smith2024") -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(
            citekey=citekey,
            title="A useful paper",
            authors=["Ada Smith"],
        ),
    )


async def seed(runtime: LibraryRuntime) -> None:
    async def write(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        session.write_record(paper())

    job = await runtime.enqueue_paper("Smith2024", JobKind.IMPORT, "seed", write)
    await runtime.queue.wait(job.id)


def test_create_and_open_services_reuse_runtime(tmp_path: Path) -> None:
    registry = RuntimeRegistry()

    created = create_library(tmp_path / "library", name="Thesis", registry=registry)
    reopened = open_library(tmp_path / "library", registry=registry)

    assert reopened is created
    assert created.root.joinpath("library.json").is_file()


async def test_validate_service_uses_library_read_scope(tmp_path: Path) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())

    report = await validate_library(runtime)

    assert report.ok is True
    assert runtime.queue.list_jobs()[-1].scope is JobScope.LIBRARY_READ


async def test_validate_reports_corrupt_library_content(tmp_path: Path) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())
    (runtime.root / "papers" / "unexpected.txt").write_text("bad", encoding="utf-8")

    report = await validate_library(runtime)

    assert report.ok is False
    assert any("Unexpected file" in problem.message for problem in report.problems)


async def test_reindex_builds_all_indexes_and_support_files(tmp_path: Path) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())
    await seed(runtime)

    job = await rebuild_indexes(runtime)

    assert job.state is JobState.SUCCEEDED
    assert job.scope is JobScope.LIBRARY_WRITE
    for filename in (
        "titles.md",
        "authors.md",
        "years.md",
        "venues.md",
        "summaries.md",
    ):
        assert (runtime.root / "indexes" / filename).is_file()
    assert "papers/<citekey>/" in (runtime.root / "AGENTS.md").read_text(encoding="utf-8")
    assert (runtime.root / ".gitignore").read_text(encoding="utf-8") == (
        "**/.pp/\npapers/*/source/\n"
    )


async def test_list_and_get_papers_apply_service_owned_filters(tmp_path: Path) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())
    await seed(runtime)

    page = await list_papers(runtime, query="useful", author="smith", limit=10)

    assert page.total == 1
    assert [record.metadata.citekey for record in page.papers] == ["Smith2024"]
    assert (await get_paper(runtime, "Smith2024")).metadata.title == "A useful paper"
    assert (await list_papers(runtime, query="absent")).total == 0


async def test_reindex_waits_behind_active_paper_lane(tmp_path: Path) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())
    await seed(runtime)
    started = asyncio.Event()
    release = asyncio.Event()

    async def paper_work(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del session, job, token
        started.set()
        await release.wait()

    await runtime.enqueue_paper("Smith2024", JobKind.IMPORT, "hold-paper", paper_work)
    await started.wait()
    reindex_task = asyncio.create_task(rebuild_indexes(runtime))
    await asyncio.sleep(0)

    assert (runtime.root / "indexes" / "titles.md").exists() is False
    release.set()
    await reindex_task
    assert (runtime.root / "indexes" / "titles.md").is_file()


def test_cli_validate_exit_codes_and_messages(tmp_path: Path, capsys) -> None:
    healthy = create_library(tmp_path / "healthy", registry=RuntimeRegistry())

    assert main(["validate", str(healthy.root)]) == 0
    assert "Library is valid" in capsys.readouterr().out

    broken = create_library(tmp_path / "broken", registry=RuntimeRegistry())
    (broken.root / "papers" / "unexpected.txt").write_text("bad", encoding="utf-8")

    assert main(["validate", str(broken.root)]) == 1
    output = capsys.readouterr().out
    assert "ERROR" in output
    assert "Action:" in output


def test_cli_reindex_and_missing_library(tmp_path: Path, capsys) -> None:
    runtime = create_library(tmp_path / "library", registry=RuntimeRegistry())

    assert main(["reindex", str(runtime.root)]) == 0
    assert "Rebuilt indexes, AGENTS.md, and .gitignore" in capsys.readouterr().out

    assert main(["validate", str(tmp_path / "missing")]) == 2
    assert "Could not validate library" in capsys.readouterr().err
