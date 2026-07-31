"""Import planning: compare an export snapshot with the current library.

Produces an ``ImportPlan``:

- ``additions``: records whose citekey is not in the library.
- ``refreshes``: records whose citekey exists and source hash is unchanged;
  metadata will be replaced.
- ``source_replacements``: same citekey but different PDF bytes; the preview
  requires explicit acceptance and hash comparison makes old outputs stale.
- ``problems``: missing attachments, invalid citekeys, and parser problems.
- ``duplicate_candidates``: same DOI or normalized-title match under a
  different citekey; advisory only, never silently merged.

The plan is pure data — applying it is a service-layer operation.
Papers absent from the export are retained untouched.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from paper_pipeline.ingest.rdf import ImportRecord
from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.storage import validate_citekey


class PaperCollection(Protocol):
    """Read surface needed to compare an import against existing papers."""

    def list_papers(self) -> tuple[list[PaperRecord], list[str]]: ...


class PlannedImport(BaseModel):
    """Serializable copy of a parsed import record for preview and later apply."""

    metadata: PaperMetadata
    attachment_path: Path | None
    attachment_sha256: str | None
    # Source identity observed during preview. None means the paper did not
    # exist yet or existed without a recorded source.
    expected_source_sha256: str | None

    @classmethod
    def from_record(
        cls,
        record: ImportRecord,
        *,
        expected_source_sha256: str | None,
    ) -> PlannedImport:
        return cls(
            metadata=record.metadata,
            attachment_path=record.attachment_path,
            attachment_sha256=record.attachment_sha256,
            expected_source_sha256=expected_source_sha256,
        )


class DuplicateCandidate(BaseModel):
    """Two distinct citekeys that may refer to the same paper."""

    citekey: str
    candidate_citekey: str
    reason: str


class ImportPlan(BaseModel):
    """Pure, JSON-serializable preview of an RDF import."""

    additions: list[PlannedImport] = Field(default_factory=list)
    refreshes: list[PlannedImport] = Field(default_factory=list)
    source_replacements: list[PlannedImport] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    duplicate_candidates: list[DuplicateCandidate] = Field(default_factory=list)

    @property
    def duplicates(self) -> list[DuplicateCandidate]:
        """Concise alias for callers that label duplicate candidates as duplicates."""
        return self.duplicate_candidates


def build_import_plan(library: PaperCollection, records: list[ImportRecord]) -> ImportPlan:
    """Compare parsed records with a library without mutating either input."""
    existing_records, library_problems = library.list_papers()
    existing = {record.metadata.citekey: record for record in existing_records}
    plan = ImportPlan(problems=list(library_problems))

    citekey_counts = Counter(
        record.metadata.citekey for record in records if record.metadata.citekey
    )
    actionable: list[ImportRecord] = []
    for record in records:
        citekey = record.metadata.citekey
        record_problems = list(record.problems)
        try:
            validate_citekey(citekey)
        except ValueError as error:
            record_problems.append(str(error))
        if citekey and citekey_counts[citekey] > 1:
            record_problems.append(f"duplicate citekey in import export: {citekey}")

        if record_problems:
            label = citekey or "<no citekey>"
            plan.problems.extend(f"{label}: {problem}" for problem in record_problems)
            continue
        if record.attachment_path is None or record.attachment_sha256 is None:
            plan.problems.append(f"{citekey}: missing PDF attachment")
            continue
        actionable.append(record)

        current = existing.get(citekey)
        planned = PlannedImport.from_record(
            record,
            expected_source_sha256=current.source_sha256 if current is not None else None,
        )
        if current is None:
            plan.additions.append(planned)
        elif record.attachment_sha256 == current.source_sha256:
            plan.refreshes.append(planned)
        else:
            plan.source_replacements.append(planned)

    plan.duplicate_candidates = _duplicate_candidates(actionable, existing)
    return plan


def _duplicate_candidates(
    incoming: list[ImportRecord], existing: dict[str, PaperRecord]
) -> list[DuplicateCandidate]:
    metadata_by_citekey = {record.metadata.citekey: record.metadata for record in incoming}
    for citekey, record in existing.items():
        metadata_by_citekey.setdefault(citekey, record.metadata)

    candidates: list[DuplicateCandidate] = []
    citekeys = sorted(metadata_by_citekey, key=str.casefold)
    for index, citekey in enumerate(citekeys):
        metadata = metadata_by_citekey[citekey]
        for candidate_citekey in citekeys[index + 1 :]:
            candidate_metadata = metadata_by_citekey[candidate_citekey]
            reason = _duplicate_reason(metadata, candidate_metadata)
            if reason is not None:
                candidates.append(
                    DuplicateCandidate(
                        citekey=citekey,
                        candidate_citekey=candidate_citekey,
                        reason=reason,
                    )
                )
    return candidates


def _duplicate_reason(first: PaperMetadata, second: PaperMetadata) -> str | None:
    first_doi = _normalized_doi(first.doi)
    second_doi = _normalized_doi(second.doi)
    if first_doi and first_doi == second_doi:
        return f"same DOI: {first_doi}"

    first_title = _normalized_title(first.title)
    second_title = _normalized_title(second.title)
    if first_title and first_title == second_title:
        return "normalized title match"
    return None


def _normalized_doi(value: str | None) -> str:
    if value is None:
        return ""
    normalized = value.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized.removeprefix(prefix).strip()
    return normalized


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())
