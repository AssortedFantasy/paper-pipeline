import os
from pathlib import Path

import pytest

from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.jobs.recovery import CompletionResult, TerminalOutcome
from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.services.runtime import (
    LibraryRuntime,
    LibrarySession,
    PaperSession,
    RuntimeRegistry,
)


def record(
    citekey: str = "Smith2024",
    *,
    title: str = "Original",
) -> PaperRecord:
    return PaperRecord(
        format_version=FORMAT_VERSION,
        metadata=PaperMetadata(
            citekey=citekey,
            title=title,
            authors=["Ada"],
            abstract="Keep this abstract",
        ),
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
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED
    return records[0]


def catalog_record(runtime: LibraryRuntime, citekey: str) -> PaperRecord:
    return next(
        paper.record
        for paper in runtime.catalog.snapshot().papers
        if paper.record.metadata.citekey == citekey
    )


def test_equivalent_paths_reuse_one_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "Library"
    registry = RuntimeRegistry()
    created = registry.create(root, name="Test")

    monkeypatch.chdir(tmp_path)
    assert registry.open(Path("Library") / ".") is created
    assert registry.open(root.resolve()) is created
    if os.name == "nt":
        assert registry.open(Path(str(root).upper())) is created


async def test_providers_and_library_state_are_isolated_per_runtime(
    tmp_path: Path,
) -> None:
    registry = RuntimeRegistry(provider_factories={"llm": object})
    first = registry.create(tmp_path / "one")
    second = registry.create(tmp_path / "two")
    first_provider = first.provider("llm")

    await seed(first, record(title="First library"))
    await seed(second, record(title="Second library"))

    assert first.provider("llm") is first_provider
    assert registry.open(tmp_path / "one").provider("llm") is first_provider
    assert second.provider("llm") is not first_provider
    assert (await read(first, "Smith2024")).metadata.title == "First library"
    assert (await read(second, "Smith2024")).metadata.title == "Second library"
    assert catalog_record(first, "Smith2024").metadata.title == "First library"
    assert catalog_record(second, "Smith2024").metadata.title == "Second library"


async def test_paper_session_write_updates_catalog_without_losing_fields(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    await seed(runtime, record())
    captured: list[PaperSession] = []

    async def update(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        captured.append(session)
        with pytest.raises(ValueError):
            session.read_paper("Other2024")

        current = session.read_record()
        current.metadata.title = "Updated"
        session.write_record(current)

    job = await runtime.enqueue_paper("Smith2024", JobKind.IMPORT, "update", update)
    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED

    durable = await read(runtime, "Smith2024")
    projected = catalog_record(runtime, "Smith2024")
    assert projected == durable
    assert durable.metadata.title == "Updated"
    assert durable.metadata.authors == ["Ada"]
    assert durable.metadata.abstract == "Keep this abstract"
    with pytest.raises(RuntimeError):
        captured[0].read_record()


async def test_library_read_session_can_inspect_but_not_mutate(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    await seed(runtime, record())

    async def read_worker(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        assert session.read_paper("Smith2024").metadata.title == "Original"
        with pytest.raises(RuntimeError):
            session.stage_dir()
        with pytest.raises(RuntimeError):
            session.mutate(lambda library: library.stage_dir())

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "read-only", read_worker)

    assert (await runtime.queue.wait(job.id)).state is JobState.SUCCEEDED


async def test_recovery_callbacks_receive_scoped_paper_sessions(
    tmp_path: Path,
) -> None:
    runtime = RuntimeRegistry().create(tmp_path / "library")
    await seed(runtime, record())
    validation_sessions: list[PaperSession] = []
    terminal_sessions: list[PaperSession] = []
    terminal_attempts: set[str] = set()

    async def worker(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job, token
        assert session.read_record().metadata.title == "Original"

    def validate(session: PaperSession) -> CompletionResult:
        validation_sessions.append(session)
        assert session.read_record().metadata.title == "Original"
        return CompletionResult()

    def record_terminal(session: PaperSession, outcome: TerminalOutcome) -> None:
        terminal_sessions.append(session)
        terminal_attempts.add(outcome.attempt_id)

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
    assert validation_sessions
    assert terminal_sessions
    assert terminal_attempts == {job.id}
    assert job.id in (await read(runtime, "Smith2024")).metadata.keywords
    assert job.id in catalog_record(runtime, "Smith2024").metadata.keywords
    for session in [*validation_sessions, *terminal_sessions]:
        with pytest.raises(RuntimeError):
            session.read_record()
