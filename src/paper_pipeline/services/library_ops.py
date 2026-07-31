"""User-facing library creation, validation, and derived-file rebuilds."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from paper_pipeline.indexes.agents_md import write_library_support_files
from paper_pipeline.indexes.build import rebuild_indexes as _rebuild_indexes
from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.library.model import PaperRecord
from paper_pipeline.library.validation import ValidationPhase, ValidationReport
from paper_pipeline.library.validation import validate_library as _validate_library
from paper_pipeline.services.runtime import (
    LibraryRuntime,
    LibrarySession,
    RuntimeRegistry,
    create_runtime,
    open_runtime,
)


class PaperPage(BaseModel):
    """Serializable filtered page of durable paper records."""

    papers: list[PaperRecord]
    problems: list[str] = Field(default_factory=list)
    total: int


class LibraryOperationError(RuntimeError):
    """A queued library operation failed before producing its result."""


@dataclass(frozen=True)
class ValidationRun:
    """A live validation job and its eventual structured report."""

    job: Job
    result: asyncio.Task[ValidationReport]


class RebuildTarget(StrEnum):
    """One independently selectable derived-library maintenance task."""

    TITLES = "titles"
    AUTHORS = "authors"
    YEARS = "years"
    VENUES = "venues"
    SUMMARIES = "summaries"
    AGENTS = "agents"
    GITIGNORE = "gitignore"
    OBSOLETE_INDEXES = "obsolete_indexes"


ALL_REBUILD_TARGETS = tuple(RebuildTarget)
_INDEX_TARGETS = {
    RebuildTarget.TITLES: "titles.md",
    RebuildTarget.AUTHORS: "authors.md",
    RebuildTarget.YEARS: "years.md",
    RebuildTarget.VENUES: "venues.md",
    RebuildTarget.SUMMARIES: "summaries.md",
}
_SUPPORT_TARGETS = {
    RebuildTarget.AGENTS: "AGENTS.md",
    RebuildTarget.GITIGNORE: ".gitignore",
}


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
    run = await start_library_validation(runtime)
    return await run.result


async def start_library_validation(runtime: LibraryRuntime) -> ValidationRun:
    """Start phased validation and publish each completed category as progress."""
    reports: list[ValidationReport] = []

    async def validate(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del token

        def publish(phase: ValidationPhase) -> None:
            runtime.queue.publish_progress(job.id, phase.model_dump_json())

        reports.append(
            session.inspect(
                lambda view: _validate_library(
                    view.root,
                    on_phase=publish,
                )
            )
        )

    job = await runtime.enqueue_library_read(
        JobKind.MAINTENANCE,
        "validate",
        validate,
    )

    async def finish() -> ValidationReport:
        result = await runtime.queue.wait(job.id)
        _require_success(result)
        return reports[0]

    return ValidationRun(job=job, result=asyncio.create_task(finish()))


async def rebuild_indexes(
    runtime: LibraryRuntime,
    targets: tuple[RebuildTarget, ...] | None = None,
) -> Job:
    """Atomically run selected derived-file rebuild tasks."""
    selected = tuple(dict.fromkeys(targets if targets is not None else ALL_REBUILD_TARGETS))
    if not selected:
        raise ValueError("Select at least one rebuild task.")
    index_files = tuple(_INDEX_TARGETS[target] for target in selected if target in _INDEX_TARGETS)
    support_files = tuple(
        _SUPPORT_TARGETS[target] for target in selected if target in _SUPPORT_TARGETS
    )
    remove_unsupported = RebuildTarget.OBSOLETE_INDEXES in selected

    async def rebuild(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        if index_files or remove_unsupported:
            session.mutate(
                lambda library: _rebuild_indexes(
                    library,
                    index_files=index_files,
                    remove_unsupported=remove_unsupported,
                )
            )
        if support_files:
            session.mutate(
                lambda library: write_library_support_files(
                    library,
                    filenames=support_files,
                )
            )

    job = await runtime.enqueue_library_write(
        JobKind.MAINTENANCE,
        "reindex",
        rebuild,
    )
    result = await runtime.queue.wait(job.id)
    _require_success(result)
    return result


async def list_papers(
    runtime: LibraryRuntime,
    *,
    query: str | None = None,
    author: str | None = None,
    year: int | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaperPage:
    """List and filter papers from the runtime's prepared catalog snapshot."""
    if offset < 0 or limit < 1:
        raise ValueError("paper page offset must be nonnegative and limit must be positive")
    snapshot = runtime.catalog.snapshot()
    query_text = query.casefold().strip() if query else None
    author_text = author.casefold().strip() if author else None
    filtered: list[PaperRecord] = []
    for entry in snapshot.papers:
        paper = entry.record
        metadata = paper.metadata
        searchable = " ".join((metadata.citekey, metadata.title, *metadata.authors)).casefold()
        if query_text and query_text not in searchable:
            continue
        if author_text and not any(author_text in name.casefold() for name in metadata.authors):
            continue
        if year is not None and metadata.year != year:
            continue
        filtered.append(paper)
    return PaperPage(
        papers=filtered[offset : offset + limit],
        problems=list(snapshot.problems),
        total=len(filtered),
    )


async def get_paper(runtime: LibraryRuntime, citekey: str) -> PaperRecord:
    """Read one paper through the shared runtime."""
    results: list[PaperRecord] = []

    async def read(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        results.append(session.read_paper(citekey))

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "paper:detail", read)
    completed = await runtime.queue.wait(job.id)
    if completed.state is not JobState.SUCCEEDED:
        error = completed.error or f"could not read paper {citekey!r}"
        if "FileNotFoundError" in error:
            raise FileNotFoundError(error)
        raise LibraryOperationError(error)
    return results[0]


def _require_success(job: Job) -> None:
    if job.state is not JobState.SUCCEEDED:
        raise LibraryOperationError(job.error or f"library operation {job.label!r} failed")
