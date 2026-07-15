"""Async job queue with mandatory resource-aware entry points."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from paper_pipeline.jobs.events import EventBus, JobEventKind
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState

type JobWorker = Callable[[Job], Awaitable[None]]

_LEGAL_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.INTERRUPTED: frozenset(),
}


class InvalidJobTransition(ValueError):
    """Raised when code attempts a state transition outside the live lifecycle."""


def transition_job(
    job: Job,
    new_state: JobState,
    *,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """Apply one legal live-job transition and its timestamps."""
    if new_state not in _LEGAL_TRANSITIONS[job.state]:
        raise InvalidJobTransition(f"cannot transition job from {job.state} to {new_state}")

    changed_at = now or datetime.now(UTC)
    job.state = new_state
    if new_state is JobState.RUNNING:
        job.started_at = changed_at
    if new_state.is_terminal:
        job.finished_at = changed_at
    job.error = error


class JobQueue:
    """Own live jobs, paper lanes, and ordered event publication."""

    def __init__(self, *, events: EventBus | None = None) -> None:
        self.events = events or EventBus()
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._paper_lanes: dict[tuple[str, str], asyncio.Lock] = {}

    async def enqueue_paper(
        self,
        library_key: str,
        citekey: str,
        kind: JobKind,
        label: str,
        worker: JobWorker,
        *,
        meta: dict[str, str] | None = None,
    ) -> Job:
        """Enqueue work that acquires its paper lane before invoking ``worker``."""
        if not citekey:
            raise ValueError("citekey must not be empty for a paper job")
        job = self._new_job(
            library_key=library_key,
            citekey=citekey,
            kind=kind,
            scope=JobScope.PAPER,
            label=label,
            meta=meta,
        )
        lane = self._paper_lanes.setdefault((library_key, citekey), asyncio.Lock())
        self._start(job, worker, lane=lane)
        return job

    async def enqueue_library_read(
        self,
        library_key: str,
        kind: JobKind,
        label: str,
        worker: JobWorker,
        *,
        meta: dict[str, str] | None = None,
    ) -> Job:
        """Enqueue explicitly read-only library work."""
        job = self._new_job(
            library_key=library_key,
            citekey=None,
            kind=kind,
            scope=JobScope.LIBRARY_READ,
            label=label,
            meta=meta,
        )
        self._start(job, worker)
        return job

    async def enqueue_library_write(
        self,
        library_key: str,
        kind: JobKind,
        label: str,
        worker: JobWorker,
        *,
        meta: dict[str, str] | None = None,
    ) -> Job:
        """Enqueue mutating library work through the dedicated write API."""
        job = self._new_job(
            library_key=library_key,
            citekey=None,
            kind=kind,
            scope=JobScope.LIBRARY_WRITE,
            label=label,
            meta=meta,
        )
        self._start(job, worker)
        return job

    def get(self, job_id: str) -> Job | None:
        """Return a live in-memory job by ID."""
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        """Return jobs in creation order."""
        return list(self._jobs.values())

    async def wait(self, job_id: str) -> Job:
        """Wait until one job reaches a terminal state."""
        job = self._jobs[job_id]
        await asyncio.shield(self._tasks[job_id])
        return job

    async def join(self) -> None:
        """Wait for all jobs that are currently enqueued."""
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))

    def publish_progress(self, job_id: str, message: str) -> None:
        """Publish an informational event without changing job state."""
        if job_id not in self._jobs:
            raise KeyError(job_id)
        self.events.publish(
            job_id=job_id,
            kind=JobEventKind.PROGRESS,
            state=self._jobs[job_id].state,
            message=message,
        )

    def _new_job(
        self,
        *,
        library_key: str,
        citekey: str | None,
        kind: JobKind,
        scope: JobScope,
        label: str,
        meta: dict[str, str] | None,
    ) -> Job:
        job = Job(
            id=str(uuid4()),
            kind=kind,
            scope=scope,
            library_key=library_key,
            citekey=citekey,
            label=label,
            created_at=datetime.now(UTC),
            meta=dict(meta or {}),
        )
        self._jobs[job.id] = job
        self._publish_state(job)
        return job

    def _start(self, job: Job, worker: JobWorker, *, lane: asyncio.Lock | None = None) -> None:
        task = asyncio.create_task(
            self._run(job, worker, lane=lane),
            name=f"paper-pipeline-job-{job.id}",
        )
        self._tasks[job.id] = task

    async def _run(
        self,
        job: Job,
        worker: JobWorker,
        *,
        lane: asyncio.Lock | None,
    ) -> None:
        try:
            if lane is None:
                await self._invoke(job, worker)
            else:
                async with lane:
                    await self._invoke(job, worker)
        except asyncio.CancelledError:
            if not job.state.is_terminal:
                self._transition(job, JobState.CANCELLED, error="job task cancelled")
        except Exception as exc:
            if not job.state.is_terminal:
                self._transition(job, JobState.FAILED, error=f"{type(exc).__name__}: {exc}")

    async def _invoke(self, job: Job, worker: JobWorker) -> None:
        self._transition(job, JobState.RUNNING)
        await worker(job)
        self._transition(job, JobState.SUCCEEDED)

    def _transition(
        self,
        job: Job,
        new_state: JobState,
        *,
        error: str | None = None,
    ) -> None:
        transition_job(job, new_state, error=error)
        self._publish_state(job)

    def _publish_state(self, job: Job) -> None:
        self.events.publish(
            job_id=job.id,
            kind=JobEventKind.STATE,
            state=job.state,
            error=job.error,
        )
