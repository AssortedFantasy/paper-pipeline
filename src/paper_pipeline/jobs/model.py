"""Versioned job data types and scheduling intent (ADR-0004)."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class JobKind(StrEnum):
    CONVERSION = "conversion"  # local, GPU-bound, serialized
    RECIPE = "recipe"  # one sequential recipe batch for one paper
    IMPORT = "import"  # one import add/refresh applied through a paper lane
    MAINTENANCE = "maintenance"  # index rebuild, validation


class JobScope(StrEnum):
    """Resource policy selected through queue entry points, not job labels."""

    PAPER = "paper"
    LIBRARY_READ = "library_read"
    LIBRARY_WRITE = "library_write"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Never assigned to a live queue job. Interrupted rows in the jobs
    # dashboard are synthesized from paper.json records found by startup
    # reconciliation (ADR-0004); retrying enqueues a fresh job.
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self not in (JobState.QUEUED, JobState.RUNNING)


@dataclass
class Job:
    """One unit of work over one paper (or the whole library for maintenance).

    In-memory only. Durable per-paper outcomes are recorded in ``paper.json``
    by the job's completion handler, never by UI code.
    """

    id: str
    kind: JobKind
    scope: JobScope
    # Resolved library-root identity. In-memory only; never serialized into a library.
    library_key: str
    citekey: str | None  # None for library-wide maintenance jobs
    label: str  # e.g. "convert", "recipe:summary"
    state: JobState = JobState.QUEUED
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    log_path: str | None = None  # library-relative path under .pp/
    progress: str | None = None  # latest in-memory progress from the shared queue
    meta: dict[str, str] = field(default_factory=dict)
