from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import PaperRecord, PaperStatus

STATUS_FILENAME = "status.json"


def _status_path(paper_dir: Path) -> Path:
    return paper_dir / STATUS_FILENAME


def is_pending_transcription(transcribed_path: Path) -> bool:
    if not transcribed_path.exists():
        return True
    content = transcribed_path.read_text(encoding="utf-8")
    return content.lstrip().startswith("# Pending transcription")


def restore_transcription_from_raw(raw_dir: Path, transcribed_path: Path) -> bool:
    from .formatting import write_text

    mmd_files = sorted(raw_dir.glob("*.mmd"))
    if not mmd_files:
        return False
    transcribed_content = mmd_files[0].read_text(encoding="utf-8")
    write_text(transcribed_path, transcribed_content.rstrip() + "\n", overwrite=True)
    return True


def load_paper_status(paper_dir: Path, citation_key: str) -> PaperStatus:
    path = _status_path(paper_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return PaperStatus.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            pass

    # Infer status from existing artifacts for backwards compatibility
    status = PaperStatus(citation_key=citation_key)
    transcribed_path = paper_dir / "transcribed.md"
    if transcribed_path.exists() and not is_pending_transcription(transcribed_path):
        status.transcription_status = "completed"
    return status


def save_paper_status(paper_dir: Path, status: PaperStatus) -> None:
    path = _status_path(paper_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status.to_dict(), indent=2) + "\n", encoding="utf-8")


def mark_running(paper_dir: Path, citation_key: str) -> PaperStatus:
    status = load_paper_status(paper_dir, citation_key)
    status.transcription_status = "running"
    status.last_run_iso = datetime.now(timezone.utc).isoformat()
    status.error_message = None
    save_paper_status(paper_dir, status)
    return status


def mark_completed(paper_dir: Path, citation_key: str) -> PaperStatus:
    status = load_paper_status(paper_dir, citation_key)
    status.transcription_status = "completed"
    status.last_run_iso = datetime.now(timezone.utc).isoformat()
    status.error_message = None
    save_paper_status(paper_dir, status)
    return status


def mark_failed(paper_dir: Path, citation_key: str, error: str) -> PaperStatus:
    status = load_paper_status(paper_dir, citation_key)
    status.transcription_status = "failed"
    status.last_run_iso = datetime.now(timezone.utc).isoformat()
    status.error_message = error
    save_paper_status(paper_dir, status)
    return status


def scan_all_status(
    papers_dir: Path, records: list[PaperRecord]
) -> dict[str, PaperStatus]:
    result: dict[str, PaperStatus] = {}
    for record in records:
        paper_dir = papers_dir / record.citation_key
        status = load_paper_status(paper_dir, record.citation_key)

        # Enrich with PDF info if available
        if record.local_pdf and record.local_pdf.exists():
            status.size_mb = round(record.local_pdf.stat().st_size / (1024 * 1024), 2)
            if status.page_count is None:
                from .runner import get_pdf_page_count

                status.page_count = get_pdf_page_count(record.local_pdf)

        # Check log path
        log_path = paper_dir / "nougat_raw" / "nougat.log"
        if log_path.exists():
            status.log_path = str(log_path)

        result[record.citation_key] = status
    return result
