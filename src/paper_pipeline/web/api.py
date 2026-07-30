"""HTTP translation models and routes over application services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from paper_pipeline.config import AppConfig
from paper_pipeline.convert.runner import ConverterSpec
from paper_pipeline.ingest.plan import ImportPlan
from paper_pipeline.jobs.events import JobEvent, JobEventKind
from paper_pipeline.jobs.model import Job, JobKind, JobScope, JobState
from paper_pipeline.library.model import PaperRecord
from paper_pipeline.library.validation import ValidationReport
from paper_pipeline.pages.runner import PageRendererSpec
from paper_pipeline.services.import_ops import ImportReport, apply_import, preview_import
from paper_pipeline.services.job_ops import list_interrupted_attempts, list_runtime_jobs
from paper_pipeline.services.library_ops import (
    PaperPage,
    create_library,
    get_paper,
    list_papers,
    open_library,
    rebuild_indexes,
    validate_library,
)
from paper_pipeline.services.processing import (
    cancel_job,
    pending_conversion_citekeys,
    pending_page_render_citekeys,
    pending_recipe_citekeys,
    queue_conversion,
    queue_page_render,
    queue_recipes,
    retry_job,
)
from paper_pipeline.services.runtime import (
    LibraryRuntime,
    RuntimeRegistry,
)


@dataclass
class WebContext:
    registry: RuntimeRegistry
    config: AppConfig
    converter_spec: ConverterSpec
    page_renderer_spec: PageRendererSpec
    provider_name: str = "openai"
    runtime: LibraryRuntime | None = None


class LibraryPathRequest(BaseModel):
    path: Path


class LibraryCreateRequest(LibraryPathRequest):
    name: str = ""


class LibraryResponse(BaseModel):
    root: Path

    @classmethod
    def from_runtime(cls, runtime: LibraryRuntime) -> LibraryResponse:
        return cls(root=runtime.root)


class ImportPreviewRequest(BaseModel):
    export_path: Path


class ImportApplyRequest(BaseModel):
    plan: ImportPlan


class SelectionRequest(BaseModel):
    citekeys: list[str] = Field(default_factory=list)
    pending: bool = False

    @model_validator(mode="after")
    def valid_selection(self) -> SelectionRequest:
        if self.pending == bool(self.citekeys):
            raise ValueError("provide citekeys or set pending=true, but not both")
        if len(set(self.citekeys)) != len(self.citekeys):
            raise ValueError("citekeys must not contain duplicates")
        return self


class QueueConversionRequest(SelectionRequest):
    pass


class QueuePageRenderRequest(SelectionRequest):
    pass


class QueueRecipesRequest(SelectionRequest):
    recipe_names: list[str]
    provider: str = "openai"
    model: str = ""

    @model_validator(mode="after")
    def has_recipes(self) -> QueueRecipesRequest:
        if not self.recipe_names:
            raise ValueError("recipe_names must not be empty")
        if len(set(self.recipe_names)) != len(self.recipe_names):
            raise ValueError("recipe_names must not contain duplicates")
        return self


class JobResponse(BaseModel):
    id: str
    kind: JobKind
    scope: JobScope
    citekey: str | None
    label: str
    state: JobState
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    log_path: str | None
    progress: str | None
    meta: dict[str, str]

    @classmethod
    def from_job(cls, job: Job) -> JobResponse:
        return cls(
            id=job.id,
            kind=job.kind,
            scope=job.scope,
            citekey=job.citekey,
            label=job.label,
            state=job.state,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            error=job.error,
            log_path=job.log_path,
            progress=job.progress,
            meta=job.meta,
        )


class InterruptedResponse(BaseModel):
    id: str
    target: str
    operation: str
    kind: JobKind
    scope: JobScope
    started_at: datetime
    state: Literal[JobState.INTERRUPTED] = JobState.INTERRUPTED
    retryable: bool = True


class JobsResponse(BaseModel):
    jobs: list[JobResponse]
    interrupted: list[InterruptedResponse] = Field(default_factory=list)


class JobBatchResponse(BaseModel):
    jobs: list[JobResponse]


class JobEventResponse(BaseModel):
    sequence: int
    job_id: str
    kind: JobEventKind
    state: JobState | None
    message: str | None
    error: str | None
    created_at: datetime
    citekey: str | None
    job_kind: JobKind
    label: str
    progress: str | None

    @classmethod
    def from_event(cls, event: JobEvent, job: Job) -> JobEventResponse:
        return cls(
            **event.__dict__,
            citekey=job.citekey,
            job_kind=job.kind,
            label=job.label,
            progress=job.progress,
        )


def create_api_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.post("/api/library/create", response_model=LibraryResponse)
    async def create_library_route(body: LibraryCreateRequest) -> LibraryResponse:
        try:
            context.runtime = create_library(
                body.path,
                name=body.name,
                registry=context.registry,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return LibraryResponse.from_runtime(context.runtime)

    @router.post("/api/library/open", response_model=LibraryResponse)
    async def open_library_route(body: LibraryPathRequest) -> LibraryResponse:
        try:
            context.runtime = open_library(body.path, registry=context.registry)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return LibraryResponse.from_runtime(context.runtime)

    @router.get("/api/library", response_model=LibraryResponse)
    async def current_library(request: Request) -> LibraryResponse:
        return LibraryResponse.from_runtime(_runtime(request))

    @router.post("/api/library/validate", response_model=ValidationReport)
    async def validate_route(request: Request) -> ValidationReport:
        try:
            return await validate_library(_runtime(request))
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/api/library/reindex", response_model=JobResponse)
    async def reindex_route(request: Request) -> JobResponse:
        try:
            job = await rebuild_indexes(_runtime(request))
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return JobResponse.from_job(job)

    @router.get("/api/papers", response_model=PaperPage)
    async def papers_route(
        request: Request,
        query: str | None = None,
        author: str | None = None,
        year: int | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> PaperPage:
        try:
            return await list_papers(
                _runtime(request),
                query=query,
                author=author,
                year=year,
                offset=offset,
                limit=limit,
            )
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/api/papers/{citekey}", response_model=PaperRecord)
    async def paper_route(citekey: str, request: Request) -> PaperRecord:
        try:
            return await get_paper(_runtime(request), citekey)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/import/preview", response_model=ImportPlan)
    async def import_preview_route(body: ImportPreviewRequest, request: Request) -> ImportPlan:
        try:
            return await preview_import(_runtime(request), body.export_path)
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/import/apply", response_model=ImportReport)
    async def import_apply_route(body: ImportApplyRequest, request: Request) -> ImportReport:
        try:
            return await apply_import(_runtime(request), body.plan)
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/api/jobs", response_model=JobsResponse)
    async def jobs_route(
        request: Request,
        state: JobState | None = None,
        kind: JobKind | None = None,
    ) -> JobsResponse:
        runtime = _runtime(request)
        jobs = [
            JobResponse.from_job(job) for job in list_runtime_jobs(runtime, state=state, kind=kind)
        ]
        interrupted = [
            InterruptedResponse(
                id=attempt.job_id,
                target=attempt.target,
                operation=attempt.operation,
                kind=attempt.kind,
                scope=attempt.scope,
                started_at=attempt.started_at,
            )
            for attempt in list_interrupted_attempts(runtime, state=state, kind=kind)
        ]
        return JobsResponse(jobs=jobs, interrupted=interrupted)

    @router.post("/api/jobs/conversion", response_model=JobBatchResponse, status_code=202)
    async def queue_conversion_route(
        body: QueueConversionRequest, request: Request
    ) -> JobBatchResponse:
        runtime = _runtime(request)
        citekeys = await pending_conversion_citekeys(runtime) if body.pending else body.citekeys
        try:
            jobs = await queue_conversion(
                runtime,
                citekeys,
                converter_spec=context.converter_spec,
                timeout_seconds=context.config.converter_timeout_seconds,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return JobBatchResponse(jobs=[JobResponse.from_job(job) for job in jobs])

    @router.post("/api/jobs/recipes", response_model=JobBatchResponse, status_code=202)
    async def queue_recipes_route(body: QueueRecipesRequest, request: Request) -> JobBatchResponse:
        runtime = _runtime(request)
        if body.pending:
            selected: set[str] = set()
            for recipe_name in body.recipe_names:
                selected.update(await pending_recipe_citekeys(runtime, recipe_name))
            citekeys = sorted(selected)
        else:
            citekeys = body.citekeys
        try:
            jobs = await queue_recipes(
                runtime,
                body.recipe_names,
                citekeys,
                provider_name=body.provider,
                model=body.model or context.config.llm_model or "",
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return JobBatchResponse(jobs=[JobResponse.from_job(job) for job in jobs])

    @router.post("/api/jobs/pages", response_model=JobBatchResponse, status_code=202)
    async def queue_page_render_route(
        body: QueuePageRenderRequest, request: Request
    ) -> JobBatchResponse:
        runtime = _runtime(request)
        citekeys = await pending_page_render_citekeys(runtime) if body.pending else body.citekeys
        try:
            jobs = await queue_page_render(
                runtime,
                citekeys,
                renderer_spec=context.page_renderer_spec,
                timeout_seconds=context.config.page_render_timeout_seconds,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return JobBatchResponse(jobs=[JobResponse.from_job(job) for job in jobs])

    @router.post("/api/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_route(job_id: str, request: Request) -> JobResponse:
        runtime = _runtime(request)
        job = _job(runtime, job_id)
        await cancel_job(runtime, job_id)
        return JobResponse.from_job(job)

    @router.post("/api/jobs/{job_id}/retry", response_model=JobResponse, status_code=202)
    async def retry_route(job_id: str, request: Request) -> JobResponse:
        runtime = _runtime(request)
        try:
            job = await retry_job(
                runtime,
                job_id,
                converter_spec=context.converter_spec,
                page_renderer_spec=context.page_renderer_spec,
                timeout_seconds=context.config.converter_timeout_seconds,
                page_render_timeout_seconds=context.config.page_render_timeout_seconds,
                provider_name=context.provider_name,
                model=context.config.llm_model or "",
            )
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return JobResponse.from_job(job)

    @router.get("/events")
    async def events_route(request: Request) -> StreamingResponse:
        runtime = _runtime(request)
        return StreamingResponse(
            event_stream(request, runtime),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


async def event_stream(request: Request, runtime: LibraryRuntime):  # type: ignore[no-untyped-def]
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
            payload = JobEventResponse.from_event(event, job).model_dump_json()
            yield f"id: {event.sequence}\nevent: {event.kind.value}\ndata: {payload}\n\n"
    finally:
        subscription.close()


def _runtime(request: Request) -> LibraryRuntime:
    context: WebContext = request.app.state.web_context
    if context.runtime is None:
        raise HTTPException(status_code=409, detail="no library is open")
    return context.runtime


def _job(runtime: LibraryRuntime, job_id: str) -> Job:
    job = runtime.queue.get(job_id)
    if job is None or job.library_key != runtime.library_key:
        raise HTTPException(status_code=404, detail="job not found")
    return job
