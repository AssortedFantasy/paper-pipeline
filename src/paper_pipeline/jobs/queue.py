"""Async job queue with mandatory resource-aware entry points."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import uuid4

from paper_pipeline.jobs.events import EventBus, JobEventKind
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.jobs.recovery import (
    AttemptMarker,
    CompletionResult,
    RecoveryHooks,
    TerminalOutcome,
)

type JobWorker = Callable[[Job], Awaitable[None]]
type CancellableJobWorker = Callable[[Job, "CancellationToken"], Awaitable[None]]
type Worker = JobWorker | CancellableJobWorker
type KillHook = Callable[[], Awaitable[None] | None]

_LEGAL_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.PARTIAL}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.PARTIAL: frozenset(),
    JobState.INTERRUPTED: frozenset(),
}


class InvalidJobTransition(ValueError):
    """Raised when code attempts a state transition outside the live lifecycle."""


class PartialJobError(RuntimeError):
    """A coordinator completed with a durable mixture of outcomes."""


class CancellationReason(StrEnum):
    USER = "user"
    SHUTDOWN = "shutdown"


class CancellationToken:
    """Cooperative cancellation signal passed to workers that accept it."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._commit_started = False
        self.reason: CancellationReason | None = None

    def is_set(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._event.is_set()

    async def wait(self) -> None:
        """Wait until cancellation is requested."""
        await self._event.wait()

    def begin_commit(self) -> bool:
        """Close cancellation before a worker starts its durable commit.

        Returns ``False`` when cancellation already won the race. Once this
        returns ``True``, later cancellation requests are rejected so a
        successfully committed artifact cannot be reported as cancelled.
        """
        if self._event.is_set():
            return False
        self._commit_started = True
        return True

    def _cancel(self, reason: CancellationReason) -> bool:
        if self._commit_started:
            return False
        self.reason = reason
        self._event.set()
        return True


@dataclass(frozen=True)
class _JobDefinition:
    library_key: str
    citekey: str | None
    kind: JobKind
    scope: JobScope
    label: str
    worker: Worker
    meta: dict[str, str]
    kill_hook: KillHook | None
    recovery: RecoveryHooks | None


class _LibraryBarrier:
    """Writer-preferring barrier between paper lanes and library writes."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_papers = 0
        self._active_readers = 0
        self._writer_active = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def paper(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._writer_active and self._waiting_writers == 0
            )
            self._active_papers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active_papers -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._writer_active and self._waiting_writers == 0
            )
            self._active_readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active_readers -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(
                    lambda: (
                        self._active_papers == 0
                        and self._active_readers == 0
                        and not self._writer_active
                    )
                )
                self._writer_active = True
            finally:
                self._waiting_writers -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._writer_active = False
                self._condition.notify_all()


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
        self._library_barriers: dict[str, _LibraryBarrier] = {}
        self._conversion_slots = asyncio.Semaphore(1)
        self._page_render_slots = asyncio.Semaphore(2)
        self._tokens: dict[str, CancellationToken] = {}
        self._definitions: dict[str, _JobDefinition] = {}
        self._closed = False

    async def enqueue_paper(
        self,
        library_key: str,
        citekey: str,
        kind: JobKind,
        label: str,
        worker: Worker,
        *,
        meta: dict[str, str] | None = None,
        kill_hook: KillHook | None = None,
        recovery: RecoveryHooks | None = None,
    ) -> Job:
        """Enqueue work that acquires its paper lane before invoking ``worker``."""
        if not citekey:
            raise ValueError("citekey must not be empty for a paper job")
        self._ensure_open()
        job = self._new_job(
            library_key=library_key,
            citekey=citekey,
            kind=kind,
            scope=JobScope.PAPER,
            label=label,
            meta=meta,
            worker=worker,
            kill_hook=kill_hook,
            recovery=recovery,
        )
        lane = self._paper_lanes.setdefault((library_key, citekey), asyncio.Lock())
        barrier = self._library_barriers.setdefault(library_key, _LibraryBarrier())
        self._start(job, worker, lane=lane, barrier=barrier)
        return job

    async def enqueue_library_read(
        self,
        library_key: str,
        kind: JobKind,
        label: str,
        worker: Worker,
        *,
        meta: dict[str, str] | None = None,
        kill_hook: KillHook | None = None,
        recovery: RecoveryHooks | None = None,
    ) -> Job:
        """Enqueue explicitly read-only library work."""
        self._ensure_open()
        job = self._new_job(
            library_key=library_key,
            citekey=None,
            kind=kind,
            scope=JobScope.LIBRARY_READ,
            label=label,
            meta=meta,
            worker=worker,
            kill_hook=kill_hook,
            recovery=recovery,
        )
        barrier = self._library_barriers.setdefault(library_key, _LibraryBarrier())
        self._start(job, worker, barrier=barrier)
        return job

    async def enqueue_library_write(
        self,
        library_key: str,
        kind: JobKind,
        label: str,
        worker: Worker,
        *,
        meta: dict[str, str] | None = None,
        kill_hook: KillHook | None = None,
        recovery: RecoveryHooks | None = None,
    ) -> Job:
        """Enqueue mutating library work through the dedicated write API."""
        self._ensure_open()
        job = self._new_job(
            library_key=library_key,
            citekey=None,
            kind=kind,
            scope=JobScope.LIBRARY_WRITE,
            label=label,
            meta=meta,
            worker=worker,
            kill_hook=kill_hook,
            recovery=recovery,
        )
        barrier = self._library_barriers.setdefault(library_key, _LibraryBarrier())
        self._start(job, worker, barrier=barrier)
        return job

    async def enqueue_remote(
        self,
        library_key: str,
        kind: JobKind,
        label: str,
        worker: Worker,
        *,
        meta: dict[str, str] | None = None,
        kill_hook: KillHook | None = None,
    ) -> Job:
        """Enqueue resumable remote work without holding a library barrier."""
        self._ensure_open()
        job = self._new_job(
            library_key=library_key,
            citekey=None,
            kind=kind,
            scope=JobScope.REMOTE,
            label=label,
            meta=meta,
            worker=worker,
            kill_hook=kill_hook,
            recovery=None,
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
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self._tasks[job_id])
        return job

    async def join(self) -> None:
        """Wait for all jobs that are currently enqueued."""
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)

    async def cancel(
        self,
        job_id: str,
        *,
        reason: CancellationReason = CancellationReason.USER,
    ) -> bool:
        """Cancel queued work immediately or signal a running worker."""
        job = self._jobs[job_id]
        if job.state.is_terminal:
            return False

        token = self._tokens[job_id]
        if token.is_set():
            return False
        if not token._cancel(reason):
            return False
        task = self._tasks[job_id]
        if job.state is JobState.QUEUED:
            self._transition(job, JobState.CANCELLED, error="job cancelled before start")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return True

        kill_hook = self._definitions[job_id].kill_hook
        if kill_hook is not None:
            try:
                hook_result = kill_hook()
                if inspect.isawaitable(hook_result):
                    await hook_result
            except Exception:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        return True

    async def retry(self, job_id: str) -> Job:
        """Create a fresh job from a failed or cancelled job's definition."""
        original = self._jobs[job_id]
        if original.state not in {JobState.FAILED, JobState.CANCELLED, JobState.PARTIAL}:
            raise ValueError("only failed, cancelled, or partial jobs can be retried")
        definition = self._definitions[job_id]
        meta = {**definition.meta, "retry_of": original.id}
        if definition.scope is JobScope.PAPER:
            assert definition.citekey is not None
            return await self.enqueue_paper(
                definition.library_key,
                definition.citekey,
                definition.kind,
                definition.label,
                definition.worker,
                meta=meta,
                kill_hook=definition.kill_hook,
                recovery=definition.recovery,
            )
        if definition.scope is JobScope.LIBRARY_READ:
            return await self.enqueue_library_read(
                definition.library_key,
                definition.kind,
                definition.label,
                definition.worker,
                meta=meta,
                kill_hook=definition.kill_hook,
                recovery=definition.recovery,
            )
        if definition.scope is JobScope.LIBRARY_WRITE:
            return await self.enqueue_library_write(
                definition.library_key,
                definition.kind,
                definition.label,
                definition.worker,
                meta=meta,
                kill_hook=definition.kill_hook,
                recovery=definition.recovery,
            )
        return await self.enqueue_remote(
            definition.library_key,
            definition.kind,
            definition.label,
            definition.worker,
            meta=meta,
            kill_hook=definition.kill_hook,
        )

    async def shutdown(self, *, grace_seconds: float = 1.0) -> None:
        """Stop accepting jobs and leave no queue-owned tasks running."""
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        self._closed = True
        for job in tuple(self._jobs.values()):
            if not job.state.is_terminal:
                await self.cancel(job.id, reason=CancellationReason.SHUTDOWN)

        pending = [task for task in self._tasks.values() if not task.done()]
        if pending:
            _, still_pending = await asyncio.wait(pending, timeout=grace_seconds)
            for task in still_pending:
                task.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)

    def publish_progress(self, job_id: str, message: str) -> None:
        """Publish an informational event without changing job state."""
        if job_id not in self._jobs:
            raise KeyError(job_id)
        self._jobs[job_id].progress = message
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
        worker: Worker,
        kill_hook: KillHook | None,
        recovery: RecoveryHooks | None,
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
        self._tokens[job.id] = CancellationToken()
        self._definitions[job.id] = _JobDefinition(
            library_key=library_key,
            citekey=citekey,
            kind=kind,
            scope=scope,
            label=label,
            worker=worker,
            meta=dict(meta or {}),
            kill_hook=kill_hook,
            recovery=recovery,
        )
        self._publish_state(job)
        return job

    def _start(
        self,
        job: Job,
        worker: Worker,
        *,
        lane: asyncio.Lock | None = None,
        barrier: _LibraryBarrier | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._run(job, worker, lane=lane, barrier=barrier),
            name=f"paper-pipeline-job-{job.id}",
        )
        self._tasks[job.id] = task

    async def _run(
        self,
        job: Job,
        worker: Worker,
        *,
        lane: asyncio.Lock | None,
        barrier: _LibraryBarrier | None,
    ) -> None:
        token = self._tokens[job.id]
        try:
            if job.scope is JobScope.LIBRARY_WRITE:
                assert barrier is not None
                async with barrier.write():
                    await self._invoke(job, worker, token)
            elif job.scope is JobScope.LIBRARY_READ:
                assert barrier is not None
                async with barrier.read():
                    await self._invoke(job, worker, token)
            elif lane is not None:
                assert barrier is not None
                async with lane, self._kind_slot(job.kind), barrier.paper():
                    await self._invoke(job, worker, token)
            else:
                await self._invoke(job, worker, token)
        except asyncio.CancelledError:
            if not job.state.is_terminal:
                await self._finish_guarded(
                    job,
                    JobState.CANCELLED,
                    error="job task cancelled",
                )
        except PartialJobError as exc:
            if not job.state.is_terminal:
                await self._finish_guarded(job, JobState.PARTIAL, error=str(exc))
        except Exception as exc:
            if not job.state.is_terminal:
                if token.is_set():
                    await self._finish_guarded(job, JobState.CANCELLED, error="job cancelled")
                else:
                    await self._finish_guarded(
                        job,
                        JobState.FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                    )

    async def _invoke(self, job: Job, worker: Worker, token: CancellationToken) -> None:
        self._transition(job, JobState.RUNNING)
        recovery = self._definitions[job.id].recovery
        if recovery is not None:
            recovery.marker_store.create(
                AttemptMarker(
                    job_id=job.id,
                    target=recovery.target,
                    operation=recovery.operation,
                    kind=job.kind,
                    scope=job.scope,
                    started_at=job.started_at or datetime.now(UTC),
                )
            )
        if _accepts_cancellation_token(worker):
            cancellable_worker = cast(CancellableJobWorker, worker)
            await cancellable_worker(job, token)
        else:
            ordinary_worker = cast(JobWorker, worker)
            await ordinary_worker(job)
        if token.is_set():
            await self._finish_guarded(job, JobState.CANCELLED, error="job cancelled")
            return

        completion = CompletionResult()
        if recovery is not None and recovery.validate_completion is not None:
            validation = recovery.validate_completion()
            completion = await validation if inspect.isawaitable(validation) else validation
        await self._finish_guarded(
            job,
            JobState.SUCCEEDED,
            error=None,
            completion=completion,
        )

    async def _finish_guarded(
        self,
        job: Job,
        state: JobState,
        *,
        error: str | None,
        completion: CompletionResult | None = None,
    ) -> None:
        try:
            await self._finish(job, state, error=error, completion=completion)
        except Exception as completion_error:
            if not job.state.is_terminal:
                self._transition(
                    job,
                    JobState.FAILED,
                    error=(
                        f"{error + '; ' if error else ''}terminal recording failed: "
                        f"{type(completion_error).__name__}: {completion_error}"
                    ),
                )

    async def _finish(
        self,
        job: Job,
        state: JobState,
        *,
        error: str | None,
        completion: CompletionResult | None,
    ) -> None:
        recovery = self._definitions[job.id].recovery
        if recovery is not None:
            if recovery.record_terminal is not None:
                recorded = recovery.record_terminal(
                    TerminalOutcome(
                        attempt_id=job.id,
                        state=state,
                        started_at=job.started_at or datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                        error=error,
                        artifact_hashes=dict((completion or CompletionResult()).artifact_hashes),
                    )
                )
                if inspect.isawaitable(recorded):
                    await recorded
            recovery.marker_store.remove(job.id)
        self._transition(job, state, error=error)

    @asynccontextmanager
    async def _kind_slot(self, kind: JobKind) -> AsyncIterator[None]:
        semaphore = None
        if kind is JobKind.CONVERSION:
            semaphore = self._conversion_slots
        elif kind is JobKind.PAGE_RENDER:
            semaphore = self._page_render_slots
        if semaphore is None:
            yield
        else:
            async with semaphore:
                yield

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("job queue is shut down")

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


def _accepts_cancellation_token(worker: Worker) -> bool:
    parameters = inspect.signature(worker).parameters.values()
    positional = 0
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            positional += 1
    return positional >= 2
