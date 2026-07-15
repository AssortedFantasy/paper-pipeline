"""Zotero import preview and paper-lane apply orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from paper_pipeline.ingest.plan import ImportPlan, PlannedImport, build_import_plan
from paper_pipeline.ingest.rdf import parse_rdf
from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.library.model import PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION, PAPER_FILE, PAPERS_DIR, SOURCE_DIR
from paper_pipeline.services.runtime import LibraryRuntime, LibrarySession, PaperSession

ImportAction = Literal["addition", "refresh", "source_replacement"]


class ImportJobResult(BaseModel):
    """Terminal result of one accepted record's paper-lane operation."""

    id: str
    citekey: str
    action: ImportAction
    state: JobState
    error: str | None = None


class ImportReport(BaseModel):
    """Serializable outcome of applying all accepted records in a preview."""

    jobs: list[ImportJobResult] = Field(default_factory=list)
    added: list[str] = Field(default_factory=list)
    refreshed: list[str] = Field(default_factory=list)
    replaced: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


class ImportOperationError(RuntimeError):
    """An import preview failed before it could produce a plan."""


async def preview_import(runtime: LibraryRuntime, export_path: Path) -> ImportPlan:
    """Parse a Zotero export and compare it with the current library."""
    plans: list[ImportPlan] = []

    async def preview(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job
        if token.is_set():
            raise asyncio.CancelledError
        records = parse_rdf(export_path)
        plans.append(session.call(lambda library: build_import_plan(library, records)))

    job = await runtime.enqueue_library_read(JobKind.IMPORT, "import:preview", preview)
    result = await runtime.queue.wait(job.id)
    if result.state is not JobState.SUCCEEDED:
        raise ImportOperationError(result.error or "import preview failed")
    return plans[0]


async def apply_import(runtime: LibraryRuntime, plan: ImportPlan) -> ImportReport:
    """Apply every actionable plan entry through its mandatory paper lane.

    Passing a plan is the acceptance boundary. Callers can omit proposed source
    replacements before invoking this function; entries left in
    ``source_replacements`` are explicit replacements.
    """
    report = ImportReport(skipped=list(plan.problems))
    scheduled: list[tuple[Job, ImportAction]] = []
    seen: set[str] = set()

    action_groups: tuple[tuple[ImportAction, list[PlannedImport]], ...] = (
        ("addition", plan.additions),
        ("refresh", plan.refreshes),
        ("source_replacement", plan.source_replacements),
    )
    for action, items in action_groups:
        for item in items:
            citekey = item.metadata.citekey
            if citekey in seen:
                raise ValueError(f"import plan contains citekey more than once: {citekey}")
            seen.add(citekey)
            if item.attachment_path is None or item.attachment_sha256 is None:
                report.skipped.append(f"{citekey}: missing PDF attachment")
                continue
            worker = _import_worker(item.model_copy(deep=True), action)
            job = await runtime.enqueue_paper(
                citekey,
                JobKind.IMPORT,
                f"import:{action}",
                worker,
                meta={"action": action},
            )
            scheduled.append((job, action))

    for job, action in scheduled:
        terminal = await runtime.queue.wait(job.id)
        assert terminal.citekey is not None
        report.jobs.append(
            ImportJobResult(
                id=terminal.id,
                citekey=terminal.citekey,
                action=action,
                state=terminal.state,
                error=terminal.error,
            )
        )
        if terminal.state is JobState.SUCCEEDED:
            target = {
                "addition": report.added,
                "refresh": report.refreshed,
                "source_replacement": report.replaced,
            }[action]
            target.append(terminal.citekey)
        else:
            report.failed[terminal.citekey] = terminal.error or terminal.state.value
    return report


def _import_worker(item: PlannedImport, action: ImportAction):  # type: ignore[no-untyped-def]
    async def worker(session: PaperSession, job: Job, token: CancellationToken) -> None:
        del job
        assert item.attachment_path is not None
        assert item.attachment_sha256 is not None

        if action == "refresh":
            current = session.read_record()
            if current.source_sha256 != item.attachment_sha256:
                raise ValueError("import preview is stale; source replacement was not accepted")
            _refresh_metadata(session, current, item)
            return

        stage = session.stage_dir()
        staged_source = stage / "source.pdf"
        try:
            copied_hash = await _copy_to_stage(item.attachment_path, staged_source, token)
            if copied_hash != item.attachment_sha256:
                raise ValueError("PDF attachment changed after import preview")
            if token.is_set():
                raise asyncio.CancelledError

            current = _read_if_present(session)
            if (
                action == "addition"
                and current is not None
                and current.source_sha256
                not in (
                    None,
                    item.attachment_sha256,
                )
            ):
                raise ValueError(
                    "import preview is stale; addition now requires source replacement"
                )

            # Create a valid metadata-only record only after the source has been copied
            # and validated in disposable staging. If interruption follows, validation
            # reports merely "not reprocessable", never a corrupt half-record.
            if current is None:
                current = PaperRecord(
                    format_version=FORMAT_VERSION,
                    metadata=item.metadata.model_copy(deep=True),
                )
                session.write_record(current)

            destination = _source_destination(item)
            installed_hash = session.install_artifact(staged_source, destination)
            if installed_hash != item.attachment_sha256:
                raise ValueError("installed PDF hash differs from import preview")

            current = session.read_record()
            changed = (
                current.metadata != item.metadata
                or current.source_pdf != destination
                or current.source_sha256 != item.attachment_sha256
            )
            if changed:
                current.metadata = item.metadata.model_copy(deep=True)
                current.source_pdf = destination
                current.source_sha256 = item.attachment_sha256
                current.imported_at = datetime.now(UTC)
                session.write_record(current)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    return worker


def _refresh_metadata(session: PaperSession, current: PaperRecord, item: PlannedImport) -> None:
    if current.metadata == item.metadata:
        return
    current.metadata = item.metadata.model_copy(deep=True)
    current.imported_at = datetime.now(UTC)
    session.write_record(current)


def _read_if_present(session: PaperSession) -> PaperRecord | None:
    paper_json = session.root_path(f"{PAPERS_DIR}/{session.citekey}/{PAPER_FILE}")
    if not paper_json.is_file():
        return None
    return session.read_record()


def _source_destination(item: PlannedImport) -> str:
    assert item.attachment_sha256 is not None
    return f"{PAPERS_DIR}/{item.metadata.citekey}/{SOURCE_DIR}/{item.attachment_sha256}.pdf"


async def _copy_to_stage(
    source: Path,
    destination: Path,
    token: CancellationToken,
    *,
    chunk_size: int = 1024 * 1024,
) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as incoming, destination.open("xb") as staged:
        while chunk := incoming.read(chunk_size):
            if token.is_set():
                raise asyncio.CancelledError
            staged.write(chunk)
            digest.update(chunk)
            await asyncio.sleep(0)
        staged.flush()
    return digest.hexdigest()
