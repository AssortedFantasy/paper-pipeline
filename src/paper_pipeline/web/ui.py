"""Server-rendered operational UI routes and presentation models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from paper_pipeline.ingest.plan import ImportPlan
from paper_pipeline.jobs.model import JobState
from paper_pipeline.library.model import AttemptState, PaperRecord
from paper_pipeline.services.import_ops import ImportReport, apply_import, preview_import
from paper_pipeline.services.library_ops import list_papers, open_library
from paper_pipeline.services.paper_detail import get_figure, get_paper_detail, get_source_pdf
from paper_pipeline.services.processing import (
    pending_conversion_citekeys,
    pending_recipe_citekeys,
    queue_conversion,
    queue_recipes,
)
from paper_pipeline.services.runtime import LibraryRuntime
from paper_pipeline.web.api import WebContext
from paper_pipeline.web.paper_detail import build_paper_view

_WEB_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=_WEB_ROOT / "templates")


@dataclass(frozen=True)
class PaperRow:
    record: PaperRecord
    conversion_state: str
    recipe_state: str
    selected: bool = False


def create_ui_router(context: WebContext) -> APIRouter:
    """Create stable page and fragment routes over the shared web context."""
    router = APIRouter(include_in_schema=False)
    import_plan: ImportPlan | None = None
    import_library_key: str | None = None

    @router.get("/", response_class=HTMLResponse)
    @router.get("/papers", response_class=HTMLResponse)
    async def papers_page(request: Request) -> HTMLResponse:
        page = await _page_context(context, request)
        return templates.TemplateResponse(request, "papers.html", page)

    @router.get("/import", response_class=HTMLResponse)
    async def import_page(request: Request) -> HTMLResponse:
        page = _import_page_context(context, request, import_plan)
        return templates.TemplateResponse(request, "import.html", page)

    @router.post("/import/preview", response_class=HTMLResponse)
    async def import_preview(request: Request) -> HTMLResponse:
        nonlocal import_library_key, import_plan
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
            import_plan = await preview_import(context.runtime, Path(export_path))
            import_library_key = context.runtime.library_key
        except (OSError, ValueError, RuntimeError) as error:
            import_plan = None
            import_library_key = None
            return _import_preview_response(request, error=str(error))
        return _import_preview_response(request, plan=import_plan)

    @router.post("/import/apply", response_class=HTMLResponse)
    async def import_apply(request: Request) -> HTMLResponse:
        nonlocal import_library_key, import_plan
        if context.runtime is None:
            return _import_preview_response(request, error="Choose a library before applying.")
        if import_plan is None:
            return _import_preview_response(
                request,
                error="Preview the export again before applying it.",
            )
        if context.runtime.library_key != import_library_key:
            import_plan = None
            import_library_key = None
            return _import_preview_response(
                request,
                error="The selected library changed. Preview the export again.",
            )
        values = await _form_values(request)
        accepted = set(values.get("source_replacements", []))
        plan = import_plan.model_copy(deep=True)
        declined = [
            item.metadata.citekey
            for item in plan.source_replacements
            if item.metadata.citekey not in accepted
        ]
        plan.source_replacements = [
            item for item in plan.source_replacements if item.metadata.citekey in accepted
        ]
        plan.problems.extend(
            f"{citekey}: source replacement was not accepted" for citekey in declined
        )
        import_plan = None
        import_library_key = None
        try:
            report = await apply_import(context.runtime, plan)
        except (OSError, ValueError, RuntimeError) as error:
            return _import_preview_response(request, error=str(error))
        return _import_preview_response(request, report=report)

    @router.post("/import/cancel", response_class=HTMLResponse)
    async def import_cancel(request: Request) -> HTMLResponse:
        nonlocal import_library_key, import_plan
        import_plan = None
        import_library_key = None
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
    page = await list_papers(runtime)
    papers, problems = page.papers, page.problems
    conversion_pending, recipe_pending = await _pending_sets(runtime)
    query = q.casefold().strip()
    rows: list[PaperRow] = []
    for paper in papers:
        citekey = paper.metadata.citekey
        conversion_state = _processing_state(
            citekey in conversion_pending,
            paper.conversion.last_attempt.state if paper.conversion.last_attempt else None,
        )
        recipe_record = paper.recipes.get("summary")
        recipe_state = _processing_state(
            citekey in recipe_pending,
            recipe_record.last_attempt.state
            if recipe_record and recipe_record.last_attempt
            else None,
        )
        searchable = " ".join((citekey, paper.metadata.title, *paper.metadata.authors)).casefold()
        if query and query not in searchable:
            continue
        if conversion != "all" and conversion_state != conversion:
            continue
        if recipe != "all" and recipe_state != recipe:
            continue
        rows.append(
            PaperRow(
                record=paper,
                conversion_state=conversion_state,
                recipe_state=recipe_state,
                selected=selection == "conversion" and citekey in conversion_pending,
            )
        )
    return {
        "rows": rows,
        "total": len(rows),
        "problems": problems,
        "filters": {"q": q, "conversion": conversion, "recipe": recipe},
    }


async def _pending_sets(context_runtime: LibraryRuntime) -> tuple[set[str], set[str]]:
    conversion_pending = set(await pending_conversion_citekeys(context_runtime))
    recipe_pending = set(await pending_recipe_citekeys(context_runtime, "summary"))
    return conversion_pending, recipe_pending


def _processing_state(pending: bool, attempt: AttemptState | None) -> str:
    if attempt is AttemptState.FAILED:
        return "failed"
    return "pending" if pending else "ready"


def _job_context(context: WebContext) -> dict[str, object]:
    if context.runtime is None:
        return {"job_counts": {}, "job_total": 0, "job_error": "No library open"}
    jobs = [
        job
        for job in context.runtime.queue.list_jobs()
        if job.library_key == context.runtime.library_key
    ]
    counts = {state.value: sum(job.state is state for job in jobs) for state in JobState}
    return {"job_counts": counts, "job_total": len(jobs), "job_error": None}


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


def _import_page_context(
    context: WebContext,
    request: Request,
    plan: ImportPlan | None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "request": request,
        "active_page": "import",
        "library_root": context.runtime.root if context.runtime is not None else None,
        "plan": plan,
    }
    result.update(_job_context(context))
    return result


def _import_preview_response(
    request: Request,
    *,
    plan: ImportPlan | None = None,
    report: ImportReport | None = None,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_import_preview.html",
        {"plan": plan, "report": report, "message": message, "error": error},
    )


def _first(values: dict[str, list[str]], key: str) -> str:
    items = values.get(key, [])
    return items[0].strip() if items else ""
