"""Server-Sent Events transport for live job updates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from paper_pipeline.jobs.events import JobEvent
from paper_pipeline.jobs.model import Job
from paper_pipeline.services.runtime import LibraryRuntime
from paper_pipeline.web.context import WebContext


def create_events_router(context: WebContext) -> APIRouter:
    """Create the one machine-readable transport used by the htmx client."""
    router = APIRouter(include_in_schema=False)

    @router.get("/events")
    async def events_route(request: Request) -> StreamingResponse:
        if context.runtime is None:
            raise HTTPException(status_code=409, detail="no library is open")
        return StreamingResponse(
            event_stream(request, context.runtime),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


async def event_stream(request: Request, runtime: LibraryRuntime) -> AsyncIterator[str]:
    """Forward runtime job events until this HTTP client disconnects."""
    subscription = runtime.queue.events.subscribe()
    try:
        yield ": connected\n\n"
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(subscription.get(), timeout=15)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            job = runtime.queue.get(event.job_id)
            if job is None or job.library_key != runtime.library_key:
                continue
            payload = json.dumps(_event_payload(event, job), separators=(",", ":"))
            yield f"id: {event.sequence}\nevent: {event.kind.value}\ndata: {payload}\n\n"
    finally:
        subscription.close()


def _event_payload(event: JobEvent, job: Job) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "job_id": event.job_id,
        "kind": event.kind.value,
        "state": event.state.value if event.state is not None else None,
        "message": event.message,
        "error": event.error,
        "created_at": event.created_at.isoformat(),
        "citekey": job.citekey,
        "job_kind": job.kind.value,
        "label": job.label,
        "progress": job.progress,
    }
