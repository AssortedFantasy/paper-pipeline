import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.queue import JobQueue
from paper_pipeline.jobs.recovery import (
    AttemptMarker,
    AttemptMarkerStore,
    RecoveryHooks,
    TerminalOutcome,
    reconcile_attempts,
    validate_artifacts,
)


def marker_store(tmp_path: Path) -> AttemptMarkerStore:
    return AttemptMarkerStore(tmp_path / ".pp" / "attempts")


def marker(*, job_id: str = "attempt-1") -> AttemptMarker:
    return AttemptMarker(
        job_id=job_id,
        target="papers/Smith2024",
        operation="convert",
        kind=JobKind.CONVERSION,
        scope=JobScope.PAPER,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_marker_write_is_atomic_and_discoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = marker_store(tmp_path)
    attempt = marker()
    store.create(attempt)

    def interrupted_replace(source: Path, destination: Path) -> None:
        raise OSError("interrupted before marker install")

    monkeypatch.setattr("paper_pipeline.jobs.recovery.os.replace", interrupted_replace)
    with pytest.raises(OSError, match="interrupted before marker install"):
        store.create(marker(job_id="attempt-2"))

    assert store.scan() == [attempt]


@pytest.mark.parametrize(
    "linked_relative",
    [Path(".pp"), Path(".pp/attempts")],
    ids=["operational-directory", "attempt-directory"],
)
def test_marker_store_rejects_symlinked_managed_ancestor(
    tmp_path: Path, linked_relative: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / linked_relative
    linked.parent.mkdir(parents=True, exist_ok=True)
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="must not contain symlinks"):
        marker_store(tmp_path).create(marker())

    assert list(outside.iterdir()) == []


async def test_success_validates_hashes_records_then_removes_marker(tmp_path: Path) -> None:
    store = marker_store(tmp_path)
    artifact = tmp_path / "transcription.md"
    outcomes: list[TerminalOutcome] = []
    marker_was_present_during_record = False

    async def worker(job: Job) -> None:
        assert [item.job_id for item in store.scan()] == [job.id]
        artifact.write_text("converted\n", encoding="utf-8")

    def record(outcome: TerminalOutcome) -> None:
        nonlocal marker_was_present_during_record
        marker_was_present_during_record = any(
            item.job_id == outcome.attempt_id for item in store.scan()
        )
        outcomes.append(outcome)

    queue = JobQueue()
    job = await queue.enqueue_paper(
        "library",
        "Smith2024",
        JobKind.CONVERSION,
        "convert",
        worker,
        recovery=RecoveryHooks(
            marker_store=store,
            target="papers/Smith2024",
            operation="convert",
            validate_completion=lambda: validate_artifacts(
                {"papers/Smith2024/transcription.md": artifact}
            ),
            record_terminal=record,
        ),
    )

    result = await queue.wait(job.id)

    expected_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert result.state is JobState.SUCCEEDED
    assert marker_was_present_during_record is True
    assert outcomes[0].state is JobState.SUCCEEDED
    assert outcomes[0].artifact_hashes == {"papers/Smith2024/transcription.md": expected_hash}
    assert store.scan() == []


@pytest.mark.parametrize(
    ("artifact_bytes", "expected_hashes"),
    [
        (None, None),
        (b"", None),
        (b"actual", {"papers/Smith2024/summary.md": "0" * 64}),
    ],
    ids=["missing", "empty", "hash-mismatch"],
)
async def test_invalid_artifact_fails_before_success(
    tmp_path: Path,
    artifact_bytes: bytes | None,
    expected_hashes: dict[str, str] | None,
) -> None:
    store = marker_store(tmp_path)
    artifact = tmp_path / "output.md"
    outcomes: list[TerminalOutcome] = []

    async def worker(job: Job) -> None:
        del job
        if artifact_bytes is not None:
            artifact.write_bytes(artifact_bytes)

    def validate():  # type: ignore[no-untyped-def]
        return validate_artifacts(
            {"papers/Smith2024/summary.md": artifact},
            expected_hashes=expected_hashes,
        )

    queue = JobQueue()
    job = await queue.enqueue_paper(
        "library",
        "Smith2024",
        JobKind.RECIPE_FINALIZE,
        "summary",
        worker,
        recovery=RecoveryHooks(
            marker_store=store,
            target="papers/Smith2024",
            operation="recipe:summary",
            validate_completion=validate,
            record_terminal=outcomes.append,
        ),
    )

    result = await queue.wait(job.id)

    assert result.state is JobState.FAILED
    assert outcomes[-1].state is JobState.FAILED
    assert outcomes[-1].artifact_hashes == {}
    assert store.scan() == []


async def test_crash_before_terminal_record_leaves_marker_and_prior_truth(
    tmp_path: Path,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    store = marker_store(tmp_path)
    durable_truth = {"artifact_hash": "prior-valid-hash", "attempt_id": "prior"}

    async def crash(job: Job) -> None:
        assert [item.job_id for item in store.scan()] == [job.id]
        raise SimulatedProcessCrash

    def record(outcome: TerminalOutcome) -> None:
        durable_truth["attempt_id"] = outcome.attempt_id

    queue = JobQueue()
    job = await queue.enqueue_paper(
        "library",
        "Smith2024",
        JobKind.CONVERSION,
        "convert",
        crash,
        recovery=RecoveryHooks(
            marker_store=store,
            target="papers/Smith2024",
            operation="convert",
            record_terminal=record,
        ),
    )
    await queue.join()

    assert job.state is JobState.RUNNING
    assert [item.job_id for item in store.scan()] == [job.id]
    assert durable_truth == {"artifact_hash": "prior-valid-hash", "attempt_id": "prior"}


def test_startup_synthesizes_interrupted_and_cleans_stale_terminal_markers(
    tmp_path: Path,
) -> None:
    store = marker_store(tmp_path)
    interrupted_marker = marker(job_id="interrupted-1")
    terminal_marker = marker(job_id="terminal-1")
    store.create(interrupted_marker)
    store.create(terminal_marker)

    interrupted = reconcile_attempts(store, {"terminal-1"}.__contains__)

    assert len(interrupted) == 1
    assert interrupted[0].job_id == "interrupted-1"
    assert interrupted[0].state is JobState.INTERRUPTED
    assert interrupted[0].retryable is True
    assert interrupted[0].target == "papers/Smith2024"
    assert store.scan() == [interrupted_marker]
