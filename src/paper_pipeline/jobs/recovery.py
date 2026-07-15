"""Disposable attempt markers and injected completion validation.

This module deliberately knows nothing about library models. WP-3.0 supplies
callbacks that translate :class:`TerminalOutcome` into durable paper records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from paper_pipeline.jobs.model import JobKind, JobScope, JobState

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class AttemptMarker:
    """Minimal disposable hint that external work was in flight."""

    job_id: str
    target: str
    operation: str
    kind: JobKind
    scope: JobScope
    started_at: datetime


@dataclass(frozen=True)
class CompletionResult:
    """Validated hashes keyed by library-relative artifact path."""

    artifact_hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TerminalOutcome:
    """Library-model-neutral completed attempt supplied to durable storage."""

    attempt_id: str
    state: JobState
    started_at: datetime
    finished_at: datetime
    error: str | None
    artifact_hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InterruptedAttempt:
    """Startup view synthesized from a marker, never written as artifact truth."""

    job_id: str
    target: str
    operation: str
    kind: JobKind
    scope: JobScope
    started_at: datetime
    state: JobState = JobState.INTERRUPTED
    retryable: bool = True


type CompletionValidator = Callable[[], CompletionResult | Awaitable[CompletionResult]]
type TerminalRecorder = Callable[[TerminalOutcome], None | Awaitable[None]]


@dataclass(frozen=True)
class RecoveryHooks:
    """Per-job operational storage and durable completion callbacks."""

    marker_store: AttemptMarkerStore
    target: str
    operation: str
    validate_completion: CompletionValidator | None = None
    record_terminal: TerminalRecorder | None = None

    def __post_init__(self) -> None:
        _validate_relative_target(self.target)
        if not self.operation.strip():
            raise ValueError("operation must not be empty")
        if self.record_terminal is None:
            raise ValueError("record_terminal is required when recovery hooks are enabled")


class AttemptMarkerStore:
    """Atomic JSON files in a caller-supplied ``.pp/attempts`` directory."""

    def __init__(self, attempts_dir: Path) -> None:
        self.attempts_dir = attempts_dir

    def create(self, marker: AttemptMarker) -> None:
        """Atomically install one marker without overwriting an existing attempt."""
        _validate_marker(marker)
        if self.attempts_dir.is_symlink():
            raise ValueError("attempt marker directory must not be a symlink")
        self.attempts_dir.mkdir(parents=True, exist_ok=True)
        if self.attempts_dir.is_symlink():
            raise ValueError("attempt marker directory must not be a symlink")
        destination = self._path(marker.job_id)
        if destination.exists():
            raise FileExistsError(f"attempt marker already exists: {marker.job_id}")
        temporary = self.attempts_dir / f".{marker.job_id}.{uuid.uuid4().hex}.tmp"
        payload = {
            **asdict(marker),
            "kind": marker.kind.value,
            "scope": marker.scope.value,
            "started_at": marker.started_at.astimezone(UTC).isoformat(),
        }
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            if destination.exists():
                raise FileExistsError(f"attempt marker already exists: {marker.job_id}")
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def remove(self, job_id: str) -> None:
        """Remove a marker idempotently after durable terminal recording."""
        self._path(job_id).unlink(missing_ok=True)

    def scan(self) -> list[AttemptMarker]:
        """Read valid markers; corrupt operational hints are ignored."""
        if not self.attempts_dir.is_dir():
            return []
        markers: list[AttemptMarker] = []
        for path in sorted(self.attempts_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                marker = AttemptMarker(
                    job_id=raw["job_id"],
                    target=raw["target"],
                    operation=raw["operation"],
                    kind=JobKind(raw["kind"]),
                    scope=JobScope(raw["scope"]),
                    started_at=datetime.fromisoformat(raw["started_at"]),
                )
                _validate_marker(marker)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            markers.append(marker)
        return markers

    def _path(self, job_id: str) -> Path:
        if not _SAFE_ID.fullmatch(job_id):
            raise ValueError(f"unsafe attempt id: {job_id!r}")
        return self.attempts_dir / f"{job_id}.json"


def validate_artifacts(
    artifacts: Mapping[str, Path],
    *,
    expected_hashes: Mapping[str, str] | None = None,
) -> CompletionResult:
    """Require non-empty files and return their SHA-256 hashes."""
    missing_declarations = set(expected_hashes or {}) - set(artifacts)
    if missing_declarations:
        missing = sorted(missing_declarations)[0]
        raise ValueError(f"missing expected artifact: {missing}")
    hashes: dict[str, str] = {}
    for artifact_name, path in sorted(artifacts.items()):
        _validate_relative_target(artifact_name)
        if not path.is_file():
            raise ValueError(f"missing expected artifact: {artifact_name}")
        if path.stat().st_size == 0:
            raise ValueError(f"expected artifact is empty: {artifact_name}")
        digest = _sha256(path)
        expected = (expected_hashes or {}).get(artifact_name)
        if expected is not None and digest != expected:
            raise ValueError(
                f"artifact hash mismatch for {artifact_name}: expected {expected}, got {digest}"
            )
        hashes[artifact_name] = digest
    return CompletionResult(artifact_hashes=hashes)


def reconcile_attempts(
    marker_store: AttemptMarkerStore,
    terminal_attempt_exists: Callable[[str], bool],
) -> list[InterruptedAttempt]:
    """Remove stale terminal markers and synthesize interrupted views for the rest."""
    interrupted: list[InterruptedAttempt] = []
    for marker in marker_store.scan():
        if terminal_attempt_exists(marker.job_id):
            marker_store.remove(marker.job_id)
            continue
        interrupted.append(
            InterruptedAttempt(
                job_id=marker.job_id,
                target=marker.target,
                operation=marker.operation,
                kind=marker.kind,
                scope=marker.scope,
                started_at=marker.started_at,
            )
        )
    return interrupted


def _validate_marker(marker: AttemptMarker) -> None:
    if not _SAFE_ID.fullmatch(marker.job_id):
        raise ValueError(f"unsafe attempt id: {marker.job_id!r}")
    _validate_relative_target(marker.target)
    if not marker.operation.strip():
        raise ValueError("operation must not be empty")
    if marker.started_at.tzinfo is None:
        raise ValueError("attempt marker started_at must be timezone-aware")


def _validate_relative_target(value: str) -> None:
    if not value or "\\" in value:
        raise ValueError("target must be a non-empty library-relative POSIX path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in posix.parts
        or str(posix) != value
        or value == "."
    ):
        raise ValueError(f"target must be a normalized library-relative POSIX path: {value!r}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
