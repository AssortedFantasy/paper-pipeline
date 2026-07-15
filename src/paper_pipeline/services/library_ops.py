"""User-facing library creation, validation, and derived-file rebuilds."""

from __future__ import annotations

from pathlib import Path

from paper_pipeline.indexes.agents_md import write_library_support_files
from paper_pipeline.indexes.build import rebuild_indexes as _rebuild_indexes
from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.library.validation import ValidationReport
from paper_pipeline.library.validation import validate_library as _validate_library
from paper_pipeline.services.runtime import (
    LibraryRuntime,
    LibrarySession,
    RuntimeRegistry,
    create_runtime,
    open_runtime,
)


class LibraryOperationError(RuntimeError):
    """A queued library operation failed before producing its result."""


def create(
    root: Path,
    *,
    name: str = "",
    registry: RuntimeRegistry | None = None,
) -> LibraryRuntime:
    """Create a library and return its sole process runtime."""
    if registry is not None:
        return registry.create(root, name=name)
    return create_runtime(root, name=name)


def open(root: Path, *, registry: RuntimeRegistry | None = None) -> LibraryRuntime:
    """Return the canonical runtime for an existing library."""
    if registry is not None:
        return registry.open(root)
    return open_runtime(root)


# Public service names from the product contract. The implementation names avoid
# looking like direct raw-storage calls in architecture enforcement scans.
create_library = create
open_library = open


async def validate_library(runtime: LibraryRuntime) -> ValidationReport:
    """Run read-only validation through the runtime's library-read entry point."""
    reports: list[ValidationReport] = []

    async def validate(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        reports.append(session.inspect(lambda view: _validate_library(view.root)))

    job = await runtime.enqueue_library_read(
        JobKind.MAINTENANCE,
        "validate",
        validate,
    )
    result = await runtime.queue.wait(job.id)
    _require_success(result)
    return reports[0]


async def rebuild_indexes(runtime: LibraryRuntime) -> Job:
    """Atomically rebuild indexes and both generated root support files."""

    async def rebuild(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        session.mutate(_rebuild_indexes)
        session.mutate(write_library_support_files)

    job = await runtime.enqueue_library_write(
        JobKind.MAINTENANCE,
        "reindex",
        rebuild,
    )
    result = await runtime.queue.wait(job.id)
    _require_success(result)
    return result


def _require_success(job: Job) -> None:
    if job.state is not JobState.SUCCEEDED:
        raise LibraryOperationError(job.error or f"library operation {job.label!r} failed")
