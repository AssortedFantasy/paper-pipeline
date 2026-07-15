"""Async-friendly in-process job events with isolated subscribers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from paper_pipeline.jobs.model import JobState


class JobEventKind(StrEnum):
    STATE = "state"
    PROGRESS = "progress"


@dataclass(frozen=True)
class JobEvent:
    """An immutable state snapshot or progress notification for one job."""

    sequence: int
    job_id: str
    kind: JobEventKind
    state: JobState | None = None
    message: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EventSubscription:
    """One subscriber's bounded buffer; overflow never affects other clients."""

    def __init__(self, bus: EventBus, max_queue_size: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[JobEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._closed = False
        self.dropped_count = 0

    async def get(self) -> JobEvent:
        """Wait for the next event."""
        if self._closed and self._queue.empty():
            raise RuntimeError("event subscription is closed")
        return await self._queue.get()

    def get_nowait(self) -> JobEvent:
        """Return the next buffered event without waiting."""
        return self._queue.get_nowait()

    def close(self) -> None:
        """Detach this subscription; already buffered events remain readable."""
        if not self._closed:
            self._closed = True
            self._bus._unsubscribe(self)

    def __aiter__(self) -> AsyncIterator[JobEvent]:
        return self

    async def __anext__(self) -> JobEvent:
        if self._closed and self._queue.empty():
            raise StopAsyncIteration
        return await self.get()

    def _offer(self, event: JobEvent) -> None:
        if self._closed:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped_count += 1
        self._queue.put_nowait(event)


class EventBus:
    """Fan out events without awaiting subscriber consumption."""

    def __init__(self, *, default_queue_size: int = 100) -> None:
        if default_queue_size < 1:
            raise ValueError("default_queue_size must be at least 1")
        self._default_queue_size = default_queue_size
        self._subscriptions: set[EventSubscription] = set()
        self._sequence = 0

    def subscribe(self, *, max_queue_size: int | None = None) -> EventSubscription:
        """Create an independent bounded event subscription."""
        queue_size = self._default_queue_size if max_queue_size is None else max_queue_size
        if queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        subscription = EventSubscription(self, queue_size)
        self._subscriptions.add(subscription)
        return subscription

    def publish(
        self,
        *,
        job_id: str,
        kind: JobEventKind,
        state: JobState | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> JobEvent:
        """Publish synchronously using only non-blocking queue operations."""
        self._sequence += 1
        event = JobEvent(
            sequence=self._sequence,
            job_id=job_id,
            kind=kind,
            state=state,
            message=message,
            error=error,
        )
        for subscription in tuple(self._subscriptions):
            subscription._offer(event)
        return event

    def _unsubscribe(self, subscription: EventSubscription) -> None:
        self._subscriptions.discard(subscription)
