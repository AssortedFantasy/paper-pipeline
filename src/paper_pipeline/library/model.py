"""Serialized library data types.

These models define the JSON stored in ``library.json`` and each paper's
``paper.json``. They are a versioned library format contract. Changes require
an ADR update, compatibility tests, and a format-version review; the schema is
expected to evolve as implementation feedback arrives.

All stored paths are library-relative POSIX paths (forward slashes).
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _SerializedModel(BaseModel):
    """Reject schema drift instead of silently discarding misspelled fields."""

    model_config = ConfigDict(extra="forbid")


class LibraryInfo(_SerializedModel):
    """Contents of ``library.json`` at the library root."""

    format_version: int
    created_at: datetime
    # Human-readable label only; identity is the folder itself.
    name: str = ""


class PaperMetadata(_SerializedModel):
    """Bibliographic metadata, normalized from the import source.

    Zotero owns these fields: a metadata refresh from a later export
    replaces them wholesale.
    """

    citekey: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    url: str | None = None
    abstract: str | None = None
    keywords: list[str] = Field(default_factory=list)


class AttemptState(StrEnum):
    """Terminal state of a completed processing attempt.

    Live ``queued``/``running`` state belongs to the in-memory job queue and
    disposable ``.pp/attempts`` markers, not to durable artifact truth.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptRecord(_SerializedModel):
    """The latest normally completed attempt, whether or not it succeeded."""

    id: str
    state: AttemptState
    started_at: datetime
    finished_at: datetime
    error: str | None = None
    # Library-relative path under .pp/. Logs are diagnostics, never artifact truth.
    log_path: str | None = None


class ConversionRecord(_SerializedModel):
    """Provenance of the installed transcription bundle plus the latest attempt.

    A failed rerun changes ``last_attempt`` without erasing the provenance of
    an older valid transcription. Freshness is derived by comparing
    ``source_sha256`` with ``PaperRecord.source_sha256``.
    """

    source_sha256: str | None = None
    transcription_sha256: str | None = None
    backend: str | None = None  # e.g. "marker"; describes installed output
    backend_version: str | None = None
    completed_at: datetime | None = None
    last_attempt: AttemptRecord | None = None


class PageRenderRecord(_SerializedModel):
    """Provenance of an independently installed set of rendered PDF pages."""

    source_sha256: str | None = None
    renderer: str | None = None
    renderer_version: str | None = None
    dpi: int | None = Field(default=None, gt=0)
    page_count: int = Field(default=0, ge=0)
    # Library-relative page-image paths mapped to their SHA-256 hashes.
    artifacts: dict[str, str] = Field(default_factory=dict)
    completed_at: datetime | None = None
    last_attempt: AttemptRecord | None = None


class RecipeRecord(_SerializedModel):
    """Provenance of an installed recipe output plus the latest attempt.

    Provenance only — never credentials.
    """

    recipe_version: int | None = None
    provider: str | None = None
    model: str | None = None
    # Library-relative path to the consumed PDF or transcription.
    input_artifact: str | None = None
    input_sha256: str | None = None
    # Library-relative path to the installed generated Markdown artifact.
    output_artifact: str | None = None
    output_sha256: str | None = None
    # Provider-reported usage for the installed run. Cached reads and cache
    # writes are disjoint subsets of prompt tokens.
    prompt_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    completed_at: datetime | None = None
    last_attempt: AttemptRecord | None = None


class PaperRecord(_SerializedModel):
    """Contents of ``paper.json`` in each paper directory.

    The durable source of truth for per-paper metadata and completed artifact
    provenance. In-flight work is operational state (ADR-0004).
    """

    format_version: int
    metadata: PaperMetadata
    # Library-relative path to the source PDF, e.g. "papers/<citekey>/source/foo.pdf".
    source_pdf: str | None = None
    source_sha256: str | None = None
    imported_at: datetime | None = None
    conversion: ConversionRecord = Field(default_factory=ConversionRecord)
    pages: PageRenderRecord = Field(default_factory=PageRenderRecord)
    # Keyed by recipe name, e.g. "summary", "contributions".
    recipes: dict[str, RecipeRecord] = Field(default_factory=dict)
