from __future__ import annotations

import asyncio
import json
from pathlib import Path
from queue import Empty

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..models import PaperRecord
from ..rdf_parser import load_records
from ..runner import get_pdf_page_count
from ..state import scan_all_status
from .worker import TranscriptionWorker

DEFAULT_ALLOWED_TYPES = {"conferencePaper", "journalArticle", "preprint"}

_WEB_DIR = Path(__file__).parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


def _asset_version() -> str:
    latest_mtime_ns = 0
    for path in _STATIC_DIR.rglob("*"):
        if path.is_file():
            latest_mtime_ns = max(latest_mtime_ns, path.stat().st_mtime_ns)
    return str(latest_mtime_ns)


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_run_config(config: dict | None) -> dict:
    config = config or {}
    return {
        "max_pages": max(1, _as_int(config.get("max_pages"), 50)),
        "max_size_mb": max(1.0, _as_float(config.get("max_size_mb"), 40.0)),
        "page_chunk_size": max(0, _as_int(config.get("page_chunk_size"), 0)),
        "page_timeout_seconds": max(
            60, _as_int(config.get("page_timeout_seconds"), 1800)
        ),
        "model": str(config.get("model") or "0.1.0-small"),
        "recompute": bool(config.get("recompute", False)),
        "no_skipping": bool(config.get("no_skipping", False)),
    }


def create_app(
    workspace_root: Path | None = None,
    rdf_path: Path | None = None,
    papers_dir: Path | None = None,
) -> FastAPI:
    workspace_root = (workspace_root or Path(".")).resolve()
    if rdf_path is None:
        candidates = sorted(workspace_root.glob("*.rdf"))
        if not candidates:
            raise RuntimeError(
                f"No .rdf file found in {workspace_root}. "
                "Pass --rdf explicitly or place a Zotero RDF export in the workspace root."
            )
        rdf_path = candidates[0]
    rdf_path = rdf_path.resolve()
    papers_dir = (papers_dir or workspace_root / "papers").resolve()

    app = FastAPI(title="Paper Pipeline")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    asset_version = _asset_version()

    worker = TranscriptionWorker(papers_dir, workspace_root)

    def _base_context() -> dict:
        return {
            "gpu": worker.get_gpu_status(),
            "worker_running": worker.is_running,
            "current_citekey": worker.current_citekey,
            "workspace_root": str(workspace_root),
            "asset_version": asset_version,
        }

    def _load_records() -> list[PaperRecord]:
        return load_records(rdf_path, workspace_root, DEFAULT_ALLOWED_TYPES)

    def _papers_json(records: list[PaperRecord]) -> list[dict]:
        statuses = scan_all_status(papers_dir, records)
        rows = []
        for record in records:
            status = statuses.get(record.citation_key)
            has_pdf = record.local_pdf is not None and record.local_pdf.exists()
            page_count = status.page_count if status else None
            size_mb = status.size_mb if status else None

            # Compute page count on demand if missing
            if page_count is None and has_pdf:
                page_count = get_pdf_page_count(record.local_pdf)

            if size_mb is None and has_pdf:
                size_mb = round(record.local_pdf.stat().st_size / (1024 * 1024), 2)

            rows.append(
                {
                    "citation_key": record.citation_key,
                    "title": record.title or record.citation_key,
                    "item_type": record.item_type,
                    "authors": ", ".join(record.authors[:3])
                    + ("..." if len(record.authors) > 3 else ""),
                    "date": record.date or "",
                    "page_count": page_count,
                    "size_mb": size_mb,
                    "has_pdf": has_pdf,
                    "transcription_status": status.transcription_status
                    if status
                    else "pending",
                    "last_run_iso": status.last_run_iso if status else None,
                    "error_message": status.error_message if status else None,
                }
            )
        return rows

    def _plan_pending_batch(
        records: list[PaperRecord], statuses: dict, config: dict
    ) -> dict:
        max_pages = config["max_pages"]
        max_size_mb = config["max_size_mb"]
        selected: list[PaperRecord] = []
        summary = {
            "queued": 0,
            "excluded_completed": 0,
            "excluded_no_pdf": 0,
            "excluded_page_cap": 0,
            "excluded_size_cap": 0,
            "queued_citekeys": [],
        }

        for record in records:
            status = statuses.get(record.citation_key)
            has_pdf = record.local_pdf is not None and record.local_pdf.exists()
            if not has_pdf:
                summary["excluded_no_pdf"] += 1
                continue

            if status and status.transcription_status == "completed":
                summary["excluded_completed"] += 1
                continue

            if (
                status
                and status.page_count is not None
                and status.page_count > max_pages
            ):
                summary["excluded_page_cap"] += 1
                continue

            if status and status.size_mb is not None and status.size_mb > max_size_mb:
                summary["excluded_size_cap"] += 1
                continue

            selected.append(record)
            summary["queued"] += 1
            summary["queued_citekeys"].append(record.citation_key)

        return {"records": selected, "summary": summary}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        records = _load_records()
        papers = _papers_json(records)
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "papers": papers,
                "papers_json": json.dumps(papers),
                "total": len(papers),
                "completed": sum(
                    1 for p in papers if p["transcription_status"] == "completed"
                ),
                "pending": sum(
                    1 for p in papers if p["transcription_status"] == "pending"
                ),
                "failed": sum(
                    1 for p in papers if p["transcription_status"] == "failed"
                ),
                **_base_context(),
            },
        )

    @app.get("/api/papers")
    async def api_papers():
        records = _load_records()
        return _papers_json(records)

    @app.get("/api/status")
    async def api_status():
        gpu = worker.get_gpu_status()
        return {
            "worker_running": worker.is_running,
            "current_citekey": worker.current_citekey,
            "queued": worker.queued_citekeys,
            "gpu": gpu,
        }

    @app.post("/api/transcribe/preview")
    async def api_transcribe_preview(request: Request):
        body = await request.json()
        config = _normalize_run_config(body.get("config"))
        records = _load_records()
        statuses = scan_all_status(papers_dir, records)
        plan = _plan_pending_batch(records, statuses, config)
        return {
            **plan["summary"],
            "config": config,
        }

    @app.post("/api/transcribe")
    async def api_transcribe(request: Request):
        body = await request.json()
        citekeys: list[str] = body.get("citekeys", [])
        config = _normalize_run_config(body.get("config"))

        if worker.is_running:
            return {"error": "A transcription batch is already running.", "started": 0}

        records = _load_records()
        record_map = {r.citation_key: r for r in records}
        statuses = scan_all_status(papers_dir, records)

        if citekeys:
            selected = [record_map[k] for k in citekeys if k in record_map]
        else:
            plan = _plan_pending_batch(records, statuses, config)
            selected = plan["records"]

        try:
            count = worker.enqueue(selected, config)
        except RuntimeError as exc:
            return {"error": str(exc), "started": 0}
        return {"started": count}

    @app.post("/api/transcribe/stop")
    async def api_stop():
        worker.request_stop()
        return {"stopped": True}

    @app.get("/api/stream")
    async def api_stream():
        q = worker.subscribe()

        async def event_generator():
            try:
                yield "event: connected\ndata: {}\n\n"
                while True:
                    try:
                        event = q.get_nowait()
                        yield event.to_sse()
                    except Empty:
                        # Send keepalive
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.5)
            finally:
                worker.unsubscribe(q)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    def _detail_context(request: Request, citekey: str) -> dict | None:
        records = _load_records()
        record = next((r for r in records if r.citation_key == citekey), None)
        if record is None:
            return None

        paper_dir = papers_dir / citekey
        transcribed_path = paper_dir / "transcribed.md"
        log_path = paper_dir / "nougat_raw" / "nougat.log"

        transcribed_content = (
            transcribed_path.read_text(encoding="utf-8")
            if transcribed_path.exists()
            else ""
        )
        log_content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""

        has_pdf = record.local_pdf is not None and record.local_pdf.exists()
        page_count = get_pdf_page_count(record.local_pdf) if has_pdf else None
        size_mb = (
            round(record.local_pdf.stat().st_size / (1024 * 1024), 2)
            if has_pdf
            else None
        )

        statuses = scan_all_status(papers_dir, [record])
        status = statuses.get(citekey)

        return {
            "request": request,
            "record": record,
            "citekey": citekey,
            "transcribed_content": transcribed_content,
            "log_content": log_content,
            "page_count": page_count,
            "size_mb": size_mb,
            "status": status,
            **_base_context(),
        }

    @app.get("/fragment/detail/{citekey}", response_class=HTMLResponse)
    async def detail_fragment(request: Request, citekey: str):
        ctx = _detail_context(request, citekey)
        if ctx is None:
            return HTMLResponse("<p>Not found</p>", status_code=404)
        return templates.TemplateResponse("detail_fragment.html", ctx)

    @app.get("/paper/{citekey}", response_class=HTMLResponse)
    async def paper_detail(request: Request, citekey: str):
        """Full-page detail view (kept for direct URL access)."""
        ctx = _detail_context(request, citekey)
        if ctx is None:
            return HTMLResponse("<h1>Not found</h1>", status_code=404)
        return templates.TemplateResponse("detail.html", ctx)

    return app
