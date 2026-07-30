import asyncio
import os
from pathlib import Path

import pytest

from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.jobs.recovery import TerminalOutcome, validate_artifacts
from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.services.runtime import (
    LibraryRuntime,
    LibrarySession,
    PaperSession,
    RuntimeRegistry,
)


def record(citekey: str = "Smith2024") -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(citekey=citekey, title="Original", authors=["Ada"]),
    )


async def seed(runtime: LibraryRuntime, paper: PaperRecord) -> None:
    async def write(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        session.write_record(paper)

    job = await runtime.enqueue_paper(
        paper.metadata.citekey,
        JobKind.IMPORT,
        "seed",
        write,
    )
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED


async def read(runtime: LibraryRuntime, citekey: str) -> PaperRecord:
    records: list[PaperRecord] = []

    async def read_worker(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        records.append(session.read_paper(citekey))

    job = await runtime.enqueue_library_read(
        JobKind.MAINTENANCE,
        "read",
        read_worker,
    )
    await runtime.queue.wait(job.id)
    return records[0]


def test_equivalent_paths_reuse_one_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "Library"
    registry = RuntimeRegistry()
    created = registry.create(root, name="Test")

    monkeypatch.chdir(tmp_path)
    relative = registry.open(Path("Library") / ".")
    resolved = registry.open(root.resolve())

    assert relative is created
    assert resolved is created
    if os.name == "nt":
        case_variant = registry.open(Path(str(root).upper()))
        assert case_variant is created


def test_provider_instances_are_per_runtime_but_reused_on_reopen(tmp_path: Path) -> None:
    registry = RuntimeRegistry(provider_factories={"llm": object})
    first = registry.create(tmp_path / "one")
    second = registry.create(tmp_path / "two")

    assert first.provider("llm") is first.provider("llm")
    assert first.provider("llm") is not second.provider("llm")
    assert registry.open(tmp_path / "one") is first


async def test_different_libraries_do_not_share_paper_lanes(tmp_path: Path) -> None:
    registry = RuntimeRegistry()
    first = registry.create(tmp_path / "one")
    second = registry.create(tmp_path / "two")
    await seed(first, record())
    await seed(second, record())
    both_started = asyncio.Event()
    release = asyncio.Event()
    running = 0

    async def worker(session: PaperSession, job: Job, token: CancellationToken) -> None:
        nonlocal running
        del session, job, token
        running += 1
        if running == 2:
            both_started.set()
        await release.wait()
        running -= 1

    await first.enqueue_paper("Smith2024", JobKind.RECIPE, "summary", worker)
    await second.enqueue_paper("Smith2024", JobKind.RECIPE, "summary", worker)

    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    await registry.queue.join()


async def test_paper_session_expires_when_lane_worker_returns(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    await seed(runtime, record())
    captured: list[PaperSession] = []

    async def capture(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        captured.append(session)
        assert session.read_record().metadata.title == "Original"
        assert not hasattr(session, "call")
        with pytest.raises(ValueError, match="inside this paper"):
            session.root_path("papers/Other2024/paper.json")

    job = await runtime.enqueue_paper("Smith2024", JobKind.IMPORT, "refresh", capture)
    await runtime.queue.wait(job.id)

    with pytest.raises(RuntimeError, match="no longer inside"):
        captured[0].read_record()


async def test_cross_category_read_modify_write_preserves_every_field(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry(llm_concurrency=2).create(tmp_path / "library")
    await seed(runtime, record())

    async def update_title(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        current = session.read_record()
        await asyncio.sleep(0)
        current.metadata.title = "Updated title"
        session.write_record(current)

    async def update_abstract(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        current = session.read_record()
        await asyncio.sleep(0)
        current.metadata.abstract = "Preserved abstract"
        session.write_record(current)

    conversion = await runtime.enqueue_paper(
        "Smith2024", JobKind.CONVERSION, "convert-metadata", update_title
    )
    imported = await runtime.enqueue_paper("Smith2024", JobKind.IMPORT, "refresh", update_abstract)
    await runtime.queue.wait(conversion.id)
    await runtime.queue.wait(imported.id)

    final = await read(runtime, "Smith2024")
    assert final.metadata.title == "Updated title"
    assert final.metadata.abstract == "Preserved abstract"
    assert final.metadata.authors == ["Ada"]


async def test_library_read_session_has_no_storage_mutation_capability(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")

    async def read_worker(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        assert session.inspect(lambda view: view.root) == runtime.root
        with pytest.raises(RuntimeError, match="cannot stage"):
            session.stage_dir()
        with pytest.raises(RuntimeError, match="cannot mutate"):
            session.mutate(lambda library: library.stage_dir())

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "read", read_worker)

    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED


async def test_runtime_wires_recovery_callbacks_inside_paper_lane(tmp_path: Path) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    await seed(runtime, record())
    artifact_relative = "papers/Smith2024/test.md"
    outcomes: list[TerminalOutcome] = []

    async def worker(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        stage = session.stage_dir()
        staged = stage / "test.md"
        staged.write_text("generated", encoding="utf-8")
        session.install_artifact(staged, artifact_relative)

    def validate(session: PaperSession):  # type: ignore[no-untyped-def]
        return validate_artifacts({artifact_relative: session.root_path(artifact_relative)})

    def record_terminal(session: PaperSession, outcome: TerminalOutcome) -> None:
        outcomes.append(outcome)

        def update(paper: PaperRecord) -> None:
            paper.metadata.keywords.append(outcome.attempt_id)

        session.update_record(update)

    job = await runtime.enqueue_paper(
        "Smith2024",
        JobKind.RECIPE,
        "recipe:test",
        worker,
        validate_completion=validate,
        record_terminal=record_terminal,
    )
    result = await runtime.queue.wait(job.id)

    assert result.state is JobState.SUCCEEDED
    assert outcomes[0].artifact_hashes.keys() == {artifact_relative}
    assert (runtime.root / ".pp" / "attempts" / f"{job.id}.json").exists() is False
    assert (await read(runtime, "Smith2024")).metadata.keywords == [job.id]
