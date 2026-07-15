"""Server-rendered operational UI routes and presentation models."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from paper_pipeline.ingest.plan import ImportPlan
from paper_pipeline.services.import_ops import (
    ImportReport,
    apply_import,
    preview_import,
    select_source_replacements,
)
from paper_pipeline.services.job_ops import job_counts
from paper_pipeline.services.library_ops import (
    create_library,
    open_library,
    rebuild_indexes,
    validate_library,
)
from paper_pipeline.services.paper_browse import browse_papers
from paper_pipeline.services.paper_detail import get_figure, get_paper_detail, get_source_pdf
from paper_pipeline.services.processing import (
    queue_conversion,
    queue_recipes,
)
from paper_pipeline.services.runtime import LibraryRuntime
from paper_pipeline.web.api import WebContext
from paper_pipeline.web.paper_detail import build_paper_view

_WEB_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=_WEB_ROOT / "templates")


def create_ui_router(context: WebContext) -> APIRouter:
    """Create stable page and fragment routes over the shared web context."""
    router = APIRouter(include_in_schema=False)
    import_previews: dict[str, tuple[str, ImportPlan]] = {}

    @router.get("/", response_class=HTMLResponse)
    @router.get("/papers", response_class=HTMLResponse)
    async def papers_page(request: Request) -> HTMLResponse:
        page = await _page_context(context, request)
        return templates.TemplateResponse(request, "papers.html", page)

    @router.get("/import", response_class=HTMLResponse)
    async def import_page(request: Request) -> HTMLResponse:
        page = _import_page_context(context, request)
        return templates.TemplateResponse(request, "import.html", page)

    @router.post("/library/create", response_class=HTMLResponse)
    async def create_library_control(request: Request) -> HTMLResponse:
        values = await _form_values(request)
        path = _first(values, "library_path")
        if not path:
            return _library_result_response(request, error="Choose a folder for the new library.")
        try:
            context.runtime = create_library(
                Path(path),
                name=_first(values, "library_name"),
                registry=context.registry,
            )
        except (OSError, ValueError, RuntimeError) as error:
            return _library_result_response(request, error=str(error))
        return _library_result_response(
            request,
            message=f"Created and opened library {context.runtime.root.name!r}.",
            runtime=context.runtime,
        )

    @router.post("/library/open", response_class=HTMLResponse)
    async def open_library_control(request: Request) -> HTMLResponse:
        values = await _form_values(request)
        path = _first(values, "library_path")
        if not path:
            return _library_result_response(request, error="Choose a library folder to open.")
        try:
            context.runtime = open_library(Path(path), registry=context.registry)
        except (OSError, ValueError, RuntimeError) as error:
            return _library_result_response(request, error=str(error))
        return _library_result_response(
            request,
            message=f"Opened library {context.runtime.root.name!r}.",
            runtime=context.runtime,
        )

    @router.post("/library/validate", response_class=HTMLResponse)
    async def validate_library_control(request: Request) -> HTMLResponse:
        values = await _form_values(request)
        runtime, error = _guard_library_operation(context, values)
        if error is not None:
            return _library_result_response(request, error=error)
        assert runtime is not None
        try:
            report = await validate_library(runtime)
        except (OSError, ValueError, RuntimeError) as operation_error:
            return _library_result_response(request, error=str(operation_error))
        if report.problems:
            return _library_result_response(request, report=report, runtime=runtime)
        return _library_result_response(
            request,
            message="Library validation passed with no problems.",
            runtime=runtime,
        )

    @router.post("/library/reindex", response_class=HTMLResponse)
    async def reindex_library_control(request: Request) -> HTMLResponse:
        values = await _form_values(request)
        runtime, error = _guard_library_operation(context, values)
        if error is not None:
            return _library_result_response(request, error=error)
        assert runtime is not None
        try:
            await rebuild_indexes(runtime)
        except (OSError, ValueError, RuntimeError) as operation_error:
            return _library_result_response(request, error=str(operation_error))
        return _library_result_response(
            request,
            message="Rebuilt indexes and library support files.",
            runtime=runtime,
        )

    @router.post("/import/preview", response_class=HTMLResponse)
    async def import_preview(request: Request) -> HTMLResponse:
        values = await _form_values(request)
        export_path = _first(values, "export_path")
        library_path = _first(values, "library_path")
        if not export_path:
            return _import_preview_response(request, error="Choose a Zotero RDF export.")
        try:
            if library_path:
                selected = Path(library_path).expanduser().resolve()
                if context.runtime is None or context.runtime.root != selected:
                    context.runtime = open_library(selected, registry=context.registry)
            if context.runtime is None:
                return _import_preview_response(request, error="Choose a library to import into.")
            plan = await preview_import(context.runtime, Path(export_path))
            preview_id = uuid4().hex
            import_previews[preview_id] = (context.runtime.library_key, plan)
            while len(import_previews) > 32:
                import_previews.pop(next(iter(import_previews)))
        except (OSError, ValueError, RuntimeError) as error:
            return _import_preview_response(request, error=str(error))
        return _import_preview_response(request, plan=plan, preview_id=preview_id)

    @router.post("/import/apply", response_class=HTMLResponse)
    async def import_apply(request: Request) -> HTMLResponse:
        values = await _form_values(request)
        preview_id = _first(values, "preview_id")
        if context.runtime is None:
            return _import_preview_response(request, error="Choose a library before applying.")
        stored = import_previews.pop(preview_id, None) if preview_id else None
        if stored is None:
            return _import_preview_response(
                request,
                error="This preview is missing or expired. Preview the export again.",
            )
        import_library_key, import_plan = stored
        if context.runtime.library_key != import_library_key:
            return _import_preview_response(
                request,
                error="The selected library changed. Preview the export again.",
            )
        accepted = set(values.get("source_replacements", []))
        plan = select_source_replacements(import_plan, accepted)
        try:
            report = await apply_import(context.runtime, plan)
        except (OSError, ValueError, RuntimeError) as error:
            return _import_preview_response(request, error=str(error))
        return _import_preview_response(request, report=report)

    @router.post("/import/cancel", response_class=HTMLResponse)
    async def import_cancel(request: Request) -> HTMLResponse:
        values = await _form_values(request)
        preview_id = _first(values, "preview_id")
        if preview_id:
            import_previews.pop(preview_id, None)
        return _import_preview_response(
            request,
            message="Import cancelled. The library was not changed.",
        )

    @router.get("/papers/table", response_class=HTMLResponse)
    async def papers_table(
        request: Request,
        q: str = "",
        conversion: str = "all",
        recipe: str = "all",
        selection: str = "",
    ) -> HTMLResponse:
        table = await _table_context(
            context,
            q=q,
            conversion=conversion,
            recipe=recipe,
            selection=selection,
        )
        return templates.TemplateResponse(request, "_papers_table.html", table)

    @router.get("/fragments/job-strip", response_class=HTMLResponse)
    async def job_strip(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_job_strip.html",
            _job_context(context),
        )

    @router.post("/papers/actions/convert", response_class=HTMLResponse)
    async def convert_selected(request: Request) -> HTMLResponse:
        values = await _form_values(request)
        citekeys = values.get("citekeys", [])
        if context.runtime is None:
            return _action_response(request, error="Open a library before launching work.")
        if _first(values, "library_key") != context.runtime.library_key:
            return _action_response(
                request,
                error="The selected library changed. Reload the papers page before launching work.",
            )
        if not citekeys:
            return _action_response(request, error="Select at least one paper to convert.")
        try:
            jobs = await queue_conversion(
                context.runtime,
                citekeys,
                converter_spec=context.converter_spec,
                timeout_seconds=context.config.converter_timeout_seconds,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            return _action_response(request, error=str(error))
        return _action_response(
            request,
            message=f"Queued {len(jobs)} conversion job{'s' if len(jobs) != 1 else ''}.",
        )

    @router.post("/papers/actions/recipes", response_class=HTMLResponse)
    async def recipes_selected(request: Request) -> HTMLResponse:
        values = await _form_values(request)
        citekeys = values.get("citekeys", [])
        recipe_names = values.get("recipe_names", [])
        if context.runtime is None:
            return _action_response(request, error="Open a library before launching work.")
        if _first(values, "library_key") != context.runtime.library_key:
            return _action_response(
                request,
                error="The selected library changed. Reload the papers page before launching work.",
            )
        if not citekeys:
            return _action_response(request, error="Select at least one paper for a recipe.")
        if not recipe_names:
            return _action_response(request, error="Choose a recipe to run.")
        try:
            jobs = await queue_recipes(
                context.runtime,
                recipe_names,
                citekeys,
                provider_name=context.provider_name,
                model=context.config.llm_model or "",
            )
        except (KeyError, ValueError, RuntimeError) as error:
            return _action_response(request, error=str(error))
        return _action_response(
            request,
            message=f"Queued {len(jobs)} recipe job{'s' if len(jobs) != 1 else ''}.",
        )

    @router.get("/papers/{citekey}/source", name="paper_source_pdf")
    async def paper_source_pdf(citekey: str) -> Response:
        if context.runtime is None:
            raise HTTPException(status_code=404, detail="No library is open")
        try:
            artifact = await get_source_pdf(context.runtime, citekey)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="Source PDF is unavailable") from None
        return Response(
            artifact.content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": 'inline; filename="source.pdf"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/papers/{citekey}/figures/{figure:path}", name="paper_figure")
    async def paper_figure(citekey: str, figure: str) -> Response:
        if context.runtime is None:
            raise HTTPException(status_code=404, detail="No library is open")
        try:
            artifact = await get_figure(context.runtime, citekey, figure)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="Figure is unavailable") from None
        return Response(
            artifact.content,
            media_type=artifact.media_type,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @router.get("/papers/{citekey}", response_class=HTMLResponse, name="paper_detail_page")
    async def paper_detail_page(request: Request, citekey: str) -> HTMLResponse:
        page: dict[str, object] = {
            "request": request,
            "active_page": "papers",
            "library_root": context.runtime.root if context.runtime is not None else None,
        }
        page.update(_job_context(context))
        if context.runtime is None:
            page["no_library"] = True
            return templates.TemplateResponse(request, "paper.html", page, status_code=404)
        try:
            page["paper_view"] = build_paper_view(await get_paper_detail(context.runtime, citekey))
        except (FileNotFoundError, ValueError):
            page["missing_paper"] = citekey
            return templates.TemplateResponse(request, "paper.html", page, status_code=404)
        return templates.TemplateResponse(request, "paper.html", page)

    return router


async def _page_context(context: WebContext, request: Request) -> dict[str, object]:
    result: dict[str, object] = {
        "request": request,
        "active_page": "papers",
        "library_root": context.runtime.root if context.runtime is not None else None,
    }
    if context.runtime is None:
        result["no_library"] = True
        result.update({"rows": [], "total": 0, "problems": []})
    else:
        result.update(await _table_context(context))
    result.update(_job_context(context))
    return result


async def _table_context(
    context: WebContext,
    *,
    q: str = "",
    conversion: str = "all",
    recipe: str = "all",
    selection: str = "",
) -> dict[str, object]:
    if context.runtime is None:
        return {"rows": [], "total": 0, "problems": [], "no_library": True}
    runtime = context.runtime
    page = await browse_papers(
        runtime,
        query=q,
        conversion=conversion,
        recipe=recipe,
        select_pending_conversion=selection == "conversion",
    )
    return {
        "rows": page.rows,
        "total": len(page.rows),
        "problems": page.problems,
        "filters": {"q": q, "conversion": conversion, "recipe": recipe},
        "library_key": runtime.library_key,
    }


def _job_context(context: WebContext) -> dict[str, object]:
    if context.runtime is None:
        return {"job_counts": {}, "job_total": 0, "job_error": "No library open"}
    counts = job_counts(context.runtime)
    return {"job_counts": counts, "job_total": sum(counts.values()), "job_error": None}


def _guard_library_operation(
    context: WebContext,
    values: dict[str, list[str]],
) -> tuple[LibraryRuntime | None, str | None]:
    if context.runtime is None:
        return None, "Open a library before running maintenance."
    if _first(values, "library_key") != context.runtime.library_key:
        return None, "The selected library changed. Reload the page before running maintenance."
    return context.runtime, None


async def _form_values(request: Request) -> dict[str, list[str]]:
    body = (await request.body()).decode("utf-8")
    return parse_qs(body, keep_blank_values=False)


def _action_response(
    request: Request,
    *,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_action_result.html",
        {"message": message, "error": error},
    )


def _library_result_response(
    request: Request,
    *,
    message: str | None = None,
    error: str | None = None,
    report: object | None = None,
    runtime: LibraryRuntime | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_library_result.html",
        {
            "message": message,
            "error": error,
            "report": report,
            "library_root": runtime.root if runtime is not None else None,
        },
    )


def _import_page_context(
    context: WebContext,
    request: Request,
) -> dict[str, object]:
    result: dict[str, object] = {
        "request": request,
        "active_page": "import",
        "library_root": context.runtime.root if context.runtime is not None else None,
        "plan": None,
    }
    result.update(_job_context(context))
    return result


def _import_preview_response(
    request: Request,
    *,
    plan: ImportPlan | None = None,
    preview_id: str | None = None,
    report: ImportReport | None = None,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_import_preview.html",
        {
            "plan": plan,
            "preview_id": preview_id,
            "report": report,
            "message": message,
            "error": error,
        },
    )


def _first(values: dict[str, list[str]], key: str) -> str:
    items = values.get(key, [])
    return items[0].strip() if items else ""
