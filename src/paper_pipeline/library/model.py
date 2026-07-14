"""Serialized library data types.

These models define the JSON stored in ``library.json`` and each paper's
``paper.json``. They are part of the library format contract: changing a
field here is a format change and requires a version bump plus an ADR.

All stored paths are library-relative POSIX paths (forward slashes).
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class LibraryInfo(BaseModel):
    """Contents of ``library.json`` at the library root."""

    format_version: int
    created_at: datetime
    # Human-readable label only; identity is the folder itself.
    name: str = ""


class PaperMetadata(BaseModel):
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


class ArtifactState(StrEnum):
    """Durable state of one processing artifact for one paper."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ConversionRecord(BaseModel):
    """Durable record of the most recent conversion attempt."""

    state: ArtifactState = ArtifactState.PENDING
    backend: str | None = None  # e.g. "marker"
    backend_version: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None  # short human-readable summary; details in .pp/ logs


class RecipeRecord(BaseModel):
    """Durable record of the most recent run of one recipe for one paper.

    Provenance only — never credentials.
    """

    state: ArtifactState = ArtifactState.PENDING
    recipe_version: int | None = None
    provider: str | None = None
    model: str | None = None
    input_artifact: str | None = None  # e.g. "transcription.md" or "source pdf"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class PaperRecord(BaseModel):
    """Contents of ``paper.json`` in each paper directory.

    The durable source of truth for per-paper metadata and processing
    status. Interruption recovery reconciles against this file and the
    artifacts on disk — never against in-memory job state.
    """

    format_version: int
    metadata: PaperMetadata
    # Library-relative path to the source PDF, e.g. "papers/<citekey>/source/foo.pdf".
    source_pdf: str | None = None
    imported_at: datetime | None = None
    conversion: ConversionRecord = Field(default_factory=ConversionRecord)
    # Keyed by recipe name, e.g. "summary", "contributions".
    recipes: dict[str, RecipeRecord] = Field(default_factory=dict)
