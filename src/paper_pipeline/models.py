from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PaperRecord:
    citation_key: str
    item_type: str
    title: str
    authors: list[str]
    abstract: str
    date: str
    venue: str
    publisher: str
    url: str
    identifiers: list[str]
    tags: list[str]
    notes: list[str]
    local_pdf: Path | None
    local_html: list[Path]


@dataclass
class PaperStatus:
    citation_key: str
    page_count: int | None = None
    size_mb: float | None = None
    transcription_status: str = "pending"  # pending | running | completed | failed
    last_run_iso: str | None = None
    error_message: str | None = None
    log_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "citation_key": self.citation_key,
            "page_count": self.page_count,
            "size_mb": self.size_mb,
            "transcription_status": self.transcription_status,
            "last_run_iso": self.last_run_iso,
            "error_message": self.error_message,
            "log_path": self.log_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PaperStatus:
        return cls(
            citation_key=data["citation_key"],
            page_count=data.get("page_count"),
            size_mb=data.get("size_mb"),
            transcription_status=data.get("transcription_status", "pending"),
            last_run_iso=data.get("last_run_iso"),
            error_message=data.get("error_message"),
            log_path=data.get("log_path"),
        )


@dataclass
class StepEstimate:
    skip: bool = False
    skip_reason: str | None = None
    estimated_pages: int | None = None


@dataclass
class StepResult:
    success: bool
    output_path: Path | None = None
    error: str | None = None
    duration_seconds: float = 0.0
