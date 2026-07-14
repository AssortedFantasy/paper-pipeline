from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import monotonic

from ..formatting import write_text
from ..models import PaperRecord, StepEstimate, StepResult
from ..nougat_setup import ensure_nougat_compatibility
from ..runner import (
    build_nougat_command,
    combine_markdown_chunks,
    default_nougat_command,
    get_pdf_page_count,
    run_nougat_subprocess,
)
from ..state import is_pending_transcription, restore_transcription_from_raw
from . import ProcessingStep


class NougatStep(ProcessingStep):
    """Transcribe PDFs using Meta's Nougat OCR."""

    @property
    def name(self) -> str:
        return "nougat"

    def is_completed(self, paper_dir: Path) -> bool:
        return not is_pending_transcription(paper_dir / "transcribed.md")

    def estimate(self, record: PaperRecord, paper_dir: Path) -> StepEstimate:
        if not record.local_pdf or not record.local_pdf.exists():
            return StepEstimate(skip=True, skip_reason="no PDF found")

        size_mb = record.local_pdf.stat().st_size / (1024 * 1024)
        page_count = get_pdf_page_count(record.local_pdf)

        return StepEstimate(
            skip=False,
            estimated_pages=page_count,
        )

    def run(
        self,
        record: PaperRecord,
        paper_dir: Path,
        config: dict,
        on_log: Callable[[str], None] | None = None,
    ) -> StepResult:
        """Run one paper through Nougat.

        This method is the shared execution path for both the CLI and the GUI.
        Keeping all retry, restore, timeout, and cancellation rules here avoids
        the two entry points drifting apart again.
        """
        start = monotonic()
        raw_dir = paper_dir / "nougat_raw"
        transcribed_path = paper_dir / "transcribed.md"

        def log(msg: str) -> None:
            if on_log:
                on_log(msg)

        # Try restoring from existing raw output first
        if not config.get("recompute", False):
            if not is_pending_transcription(transcribed_path):
                log(f"skip {record.citation_key}: already transcribed")
                return StepResult(
                    success=True,
                    output_path=transcribed_path,
                    duration_seconds=monotonic() - start,
                )
            if restore_transcription_from_raw(raw_dir, transcribed_path):
                log(f"restored {record.citation_key} from raw .mmd")
                return StepResult(
                    success=True,
                    output_path=transcribed_path,
                    duration_seconds=monotonic() - start,
                )

        if not record.local_pdf or not record.local_pdf.exists():
            return StepResult(
                success=False,
                error="no PDF found",
                duration_seconds=monotonic() - start,
            )

        size_mb = record.local_pdf.stat().st_size / (1024 * 1024)
        max_size = config.get("max_size_mb", 40.0)
        if size_mb > max_size:
            return StepResult(
                success=False,
                error=f"size={size_mb:.1f}MB exceeds limit={max_size}MB",
                duration_seconds=monotonic() - start,
            )

        page_count = get_pdf_page_count(record.local_pdf)
        max_pages = config.get("max_pages", 50)
        if page_count is not None and page_count > max_pages:
            return StepResult(
                success=False,
                error=f"pages={page_count} exceeds limit={max_pages}",
                duration_seconds=monotonic() - start,
            )

        workspace_root = Path(config.get("workspace_root", ".")).resolve()
        nougat_cmd = config.get("nougat_cmd") or default_nougat_command(workspace_root)
        dry_run = config.get("dry_run", False)
        cancel_requested = config.get("cancel_requested")

        if cancel_requested and cancel_requested():
            return StepResult(
                success=False,
                error="cancelled by user",
                duration_seconds=monotonic() - start,
            )

        if not dry_run:
            try:
                for message in ensure_nougat_compatibility(
                    workspace_root=workspace_root
                ):
                    log(message)
            except RuntimeError as exc:
                msg = str(exc)
                log(msg)
                return StepResult(
                    success=False,
                    error=msg,
                    duration_seconds=monotonic() - start,
                )

        # Validate nougat is reachable before attempting subprocess
        from shutil import which

        if not dry_run and not Path(nougat_cmd).exists() and which(nougat_cmd) is None:
            msg = f"nougat not found at {nougat_cmd}. Run `uv sync` and retry."
            log(msg)
            return StepResult(
                success=False,
                error=msg,
                duration_seconds=monotonic() - start,
            )

        model = config.get("model", "0.1.0-small")
        batchsize = config.get("batchsize")
        no_skipping = config.get("no_skipping", False)
        recompute = config.get("recompute", False)
        timeout = config.get("page_timeout_seconds", 1800)
        chunk_size = config.get("page_chunk_size", 0)

        raw_dir.mkdir(parents=True, exist_ok=True)

        page_summary = page_count if page_count is not None else "unknown"
        log(f"running {record.citation_key}: pages={page_summary} size={size_mb:.1f}MB")

        if page_count is not None and chunk_size > 0:
            success = self._run_chunked(
                record,
                paper_dir,
                raw_dir,
                nougat_cmd,
                model,
                batchsize,
                no_skipping,
                recompute,
                timeout,
                dry_run,
                cancel_requested,
                chunk_size,
                page_count,
                log,
            )
        else:
            success = self._run_whole(
                record,
                paper_dir,
                raw_dir,
                nougat_cmd,
                model,
                batchsize,
                no_skipping,
                recompute,
                timeout,
                dry_run,
                cancel_requested,
                log,
            )

        duration = monotonic() - start
        if success:
            return StepResult(
                success=True,
                output_path=transcribed_path,
                duration_seconds=duration,
            )
        return StepResult(
            success=False,
            error=f"nougat failed for {record.citation_key}",
            duration_seconds=duration,
        )

    def _run_whole(
        self,
        record: PaperRecord,
        paper_dir: Path,
        raw_dir: Path,
        nougat_cmd: str,
        model: str | None,
        batchsize: int | None,
        no_skipping: bool,
        recompute: bool,
        timeout: int,
        dry_run: bool,
        cancel_requested: Callable[[], bool] | None,
        log: Callable[[str], None],
    ) -> bool:
        command = build_nougat_command(
            nougat_cmd,
            record.local_pdf,
            raw_dir,
            model=model,
            batchsize=batchsize,
            no_skipping=no_skipping,
            recompute=recompute,
        )
        log(f"command: {' '.join(command)}")

        if dry_run:
            return True

        completed = run_nougat_subprocess(
            command,
            raw_dir / "nougat.log",
            timeout_seconds=timeout,
            on_output=log,
            should_cancel=cancel_requested,
        )
        if completed.cancelled:
            log(f"cancelled for {record.citation_key}")
            return False
        if completed.timed_out:
            log(f"timed out for {record.citation_key}")
            return False
        if completed.returncode != 0:
            log(f"exit code {completed.returncode} for {record.citation_key}")
            return False

        mmd_files = sorted(raw_dir.glob("*.mmd"))
        if not mmd_files:
            log(f"no .mmd output for {record.citation_key}")
            return False

        content = mmd_files[0].read_text(encoding="utf-8")
        write_text(
            paper_dir / "transcribed.md", content.rstrip() + "\n", overwrite=True
        )
        return True

    def _run_chunked(
        self,
        record: PaperRecord,
        paper_dir: Path,
        raw_dir: Path,
        nougat_cmd: str,
        model: str | None,
        batchsize: int | None,
        no_skipping: bool,
        recompute: bool,
        timeout: int,
        dry_run: bool,
        cancel_requested: Callable[[], bool] | None,
        chunk_size: int,
        page_count: int,
        log: Callable[[str], None],
    ) -> bool:
        chunk_root = raw_dir / "chunks"
        combined_chunks: list[str] = []

        for start_page in range(1, page_count + 1, chunk_size):
            end_page = min(start_page + chunk_size - 1, page_count)
            page_spec = (
                str(start_page)
                if start_page == end_page
                else f"{start_page}-{end_page}"
            )
            chunk_dir = chunk_root / f"{start_page:03d}-{end_page:03d}"
            command = build_nougat_command(
                nougat_cmd,
                record.local_pdf,
                chunk_dir,
                model=model,
                batchsize=batchsize,
                no_skipping=no_skipping,
                recompute=recompute,
                pages=page_spec,
            )
            log(f"chunk {page_spec}: {' '.join(command)}")

            if cancel_requested and cancel_requested():
                log(f"cancelled before chunk {page_spec}")
                return False

            if dry_run:
                continue

            completed = run_nougat_subprocess(
                command,
                chunk_dir / "nougat.log",
                timeout_seconds=timeout,
                on_output=log,
                should_cancel=cancel_requested,
            )
            if completed.cancelled:
                log(f"chunk {page_spec} cancelled")
                return False
            if completed.timed_out or completed.returncode != 0:
                log(f"chunk {page_spec} failed")
                combined_chunks.append(f"\n\n[MISSING_PAGE_FAIL:{page_spec}]\n\n")
                continue

            mmd_files = sorted(chunk_dir.glob("*.mmd"))
            if not mmd_files:
                log(f"chunk {page_spec} produced no .mmd output")
                combined_chunks.append(f"\n\n[MISSING_PAGE_FAIL:{page_spec}]\n\n")
                continue
            combined_chunks.append(mmd_files[0].read_text(encoding="utf-8"))

        if dry_run:
            return True

        content = combine_markdown_chunks(combined_chunks)
        if not content:
            return False

        combined_raw = raw_dir / f"{record.local_pdf.stem}.mmd"
        write_text(combined_raw, content, overwrite=True)
        write_text(paper_dir / "transcribed.md", content, overwrite=True)
        return True
