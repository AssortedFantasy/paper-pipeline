"""Server-rendered jobs dashboard routes."""

from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from paper_pipeline.services.job_ops import job_dashboard, read_log_tail, retry_selected_jobs
from paper_pipeline.services.processing import cancel_job, retry_job
from paper_pipeline.web.api import WebContext
from paper_pipeline.web.ui import templates


def create_jobs_router(context: WebContext) -> APIRouter:
    """Create job page, fragment, action, and log-tail routes."""
    router = APIRouter(include_in_schema=False)

    @router.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request) -> HTMLResponse:
        page = _page_context(context, request)
        return templates.TemplateResponse(request, "jobs.html", page)

    @router.get("/jobs/list", response_class=HTMLResponse)
    async def jobs_list(request: Request) -> HTMLResponse:
        return _jobs_response(context, request)

    @router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
    async def cancel_job_route(job_id: str, request: Request) -> HTMLResponse:
        if context.runtime is None:
            return _jobs_response(context, request, error="No library is open.")
        try:
            await cancel_job(context.runtime, job_id)
        except (KeyError, ValueError) as error:
            return _jobs_response(context, request, error=str(error))
        return _jobs_response(context, request, message="Cancellation requested.")

    @router.post("/jobs/{job_id}/retry", response_class=HTMLResponse)
    async def retry_job_route(job_id: str, request: Request) -> HTMLResponse:
        if context.runtime is None:
            return _jobs_response(context, request, error="No library is open.")
        try:
            await retry_job(
                context.runtime,
                job_id,
                converter_spec=context.converter_spec,
                timeout_seconds=context.config.converter_timeout_seconds,
                provider_name=context.provider_name,
                model=context.config.llm_model or "",
            )
        except (KeyError, ValueError, RuntimeError) as error:
            return _jobs_response(context, request, error=str(error))
        return _jobs_response(context, request, message="Retry queued as a new job.")

    @router.post("/jobs/retry-selected", response_class=HTMLResponse)
    async def retry_selected_route(request: Request) -> HTMLResponse:
        if context.runtime is None:
            return _jobs_response(context, request, error="No library is open.")
        values = parse_qs((await request.body()).decode("utf-8"))
        try:
            jobs = await retry_selected_jobs(
                context.runtime,
                values.get("job_ids", []),
                converter_spec=context.converter_spec,
                timeout_seconds=context.config.converter_timeout_seconds,
                provider_name=context.provider_name,
                model=context.config.llm_model or "",
            )
        except (KeyError, ValueError, RuntimeError) as error:
            return _jobs_response(context, request, error=str(error))
        return _jobs_response(
            context,
            request,
            message=f"Queued {len(jobs)} selected job{'s' if len(jobs) != 1 else ''} for retry.",
        )

    @router.get("/jobs/{job_id}/log", response_class=HTMLResponse)
    async def job_log(job_id: str, request: Request) -> HTMLResponse:
        if context.runtime is None:
            return templates.TemplateResponse(
                request,
                "_log_tail.html",
                {"log_error": "No library is open."},
            )
        try:
            tail = read_log_tail(context.runtime, job_id)
            values: dict[str, object] = {"tail": tail}
        except (KeyError, OSError, ValueError) as error:
            values = {"log_error": str(error)}
        return templates.TemplateResponse(request, "_log_tail.html", values)

    return router


def _page_context(context: WebContext, request: Request) -> dict[str, object]:
    page: dict[str, object] = {
        "request": request,
        "active_page": "jobs",
        "library_root": context.runtime.root if context.runtime is not None else None,
        "no_library": context.runtime is None,
    }
    page.update(_dashboard_context(context))
    return page


def _dashboard_context(context: WebContext) -> dict[str, object]:
    if context.runtime is None:
        return {"active_jobs": (), "terminal_jobs": (), "interrupted_jobs": ()}
    dashboard = job_dashboard(context.runtime)
    return {
        "active_jobs": dashboard.active,
        "terminal_jobs": dashboard.terminal,
        "interrupted_jobs": dashboard.interrupted,
    }


def _jobs_response(
    context: WebContext,
    request: Request,
    *,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    values = _dashboard_context(context)
    values.update({"message": message, "error": error})
    return templates.TemplateResponse(request, "_jobs_list.html", values)
