"""Job data types. FROZEN for parallel work — changes require an ADR."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class JobKind(StrEnum):
    CONVERSION = "conversion"  # local, GPU-bound, serialized
    RECIPE = "recipe"  # remote API, concurrent across papers, sequential per paper
    MAINTENANCE = "maintenance"  # index rebuild, validation


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
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
    citekey: str | None  # None for library-wide maintenance jobs
    label: str  # e.g. "convert", "recipe:summary"
    state: JobState = JobState.QUEUED
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    log_path: str | None = None  # library-relative path under .pp/
    meta: dict[str, str] = field(default_factory=dict)
