import hashlib
import shutil
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


def test_marker_write_is_atomic_and_round_trips(tmp_path: Path) -> None:
    store = marker_store(tmp_path)
    attempt = marker()

    store.create(attempt)

    marker_path = store.attempts_dir / "attempt-1.json"
    assert marker_path.is_file()
    assert store.scan() == [attempt]
    assert list(store.attempts_dir.glob("*.tmp")) == []


def test_marker_store_rejects_symlinked_attempt_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside-attempts"
    outside.mkdir()
    attempts = tmp_path / ".pp" / "attempts"
    attempts.parent.mkdir()
    try:
        attempts.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="must not contain symlinks"):
        AttemptMarkerStore(attempts).create(marker())

    assert list(outside.iterdir()) == []


def test_marker_store_rejects_symlinked_operational_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside-operational"
    outside.mkdir()
    operational = tmp_path / ".pp"
    try:
        operational.symlink_to(outside, target_is_directory=True)
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
        assert (store.attempts_dir / f"{job.id}.json").is_file()
        artifact.write_text("converted\n", encoding="utf-8")

    def record(outcome: TerminalOutcome) -> None:
        nonlocal marker_was_present_during_record
        marker_was_present_during_record = (
            store.attempts_dir / f"{outcome.attempt_id}.json"
        ).is_file()
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


@pytest.mark.parametrize("failure", ["missing", "empty", "hash-mismatch"])
async def test_invalid_artifact_fails_before_success(tmp_path: Path, failure: str) -> None:
    store = marker_store(tmp_path)
    artifact = tmp_path / "output.md"
    outcomes: list[TerminalOutcome] = []

    async def worker(job: Job) -> None:
        del job
        if failure == "empty":
            artifact.write_bytes(b"")
        elif failure == "hash-mismatch":
            artifact.write_text("actual", encoding="utf-8")

    def validate():  # type: ignore[no-untyped-def]
        expected = {"papers/Smith2024/summary.md": "0" * 64}
        return validate_artifacts(
            {"papers/Smith2024/summary.md": artifact},
            expected_hashes=expected if failure == "hash-mismatch" else None,
        )

    queue = JobQueue()
    job = await queue.enqueue_paper(
        "library",
        "Smith2024",
        JobKind.RECIPE,
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
    assert result.error is not None
    assert failure.replace("-", " ") in result.error
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
        assert (store.attempts_dir / f"{job.id}.json").is_file()
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


def test_startup_synthesizes_interrupted_without_durable_rewrite(tmp_path: Path) -> None:
    store = marker_store(tmp_path)
    store.create(marker())
    durable_attempts: set[str] = set()

    interrupted = reconcile_attempts(store, durable_attempts.__contains__)

    assert len(interrupted) == 1
    assert interrupted[0].job_id == "attempt-1"
    assert interrupted[0].state is JobState.INTERRUPTED
    assert interrupted[0].retryable is True
    assert interrupted[0].target == "papers/Smith2024"
    assert durable_attempts == set()
    assert store.scan() == [marker()]


def test_terminal_attempt_marker_is_cleaned_as_stale(tmp_path: Path) -> None:
    store = marker_store(tmp_path)
    store.create(marker())

    interrupted = reconcile_attempts(store, {"attempt-1"}.__contains__)

    assert interrupted == []
    assert store.scan() == []


def test_deleting_operational_directory_loses_no_durable_truth(tmp_path: Path) -> None:
    store = marker_store(tmp_path)
    store.create(marker())
    durable_truth = {"artifact_hash": "still-valid", "attempt_id": "prior"}

    shutil.rmtree(tmp_path / ".pp")

    assert reconcile_attempts(store, lambda attempt_id: attempt_id == "prior") == []
    assert durable_truth == {"artifact_hash": "still-valid", "attempt_id": "prior"}
