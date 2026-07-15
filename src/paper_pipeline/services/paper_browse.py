"""Paper-list query, processing-state, filter, and selection policy."""

from __future__ import annotations

from dataclasses import dataclass

from paper_pipeline.library.model import AttemptState, PaperRecord
from paper_pipeline.services.library_ops import list_papers
from paper_pipeline.services.processing import (
    pending_conversion_citekeys,
    pending_recipe_citekeys,
)
from paper_pipeline.services.runtime import LibraryRuntime


@dataclass(frozen=True)
class PaperBrowseRow:
    record: PaperRecord
    conversion_state: str
    recipe_state: str
    selected: bool = False


@dataclass(frozen=True)
class PaperBrowsePage:
    rows: tuple[PaperBrowseRow, ...]
    problems: tuple[str, ...]


async def browse_papers(
    runtime: LibraryRuntime,
    *,
    query: str = "",
    conversion: str = "all",
    recipe: str = "all",
    select_pending_conversion: bool = False,
) -> PaperBrowsePage:
    """Return one filtered paper table with durable processing states."""
    page = await list_papers(runtime)
    conversion_pending = set(await pending_conversion_citekeys(runtime))
    recipe_pending = set(await pending_recipe_citekeys(runtime, "summary"))
    query_text = query.casefold().strip()
    rows: list[PaperBrowseRow] = []
    for paper in page.papers:
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
        if query_text and query_text not in searchable:
            continue
        if conversion != "all" and conversion_state != conversion:
            continue
        if recipe != "all" and recipe_state != recipe:
            continue
        rows.append(
            PaperBrowseRow(
                record=paper,
                conversion_state=conversion_state,
                recipe_state=recipe_state,
                selected=select_pending_conversion and citekey in conversion_pending,
            )
        )
    return PaperBrowsePage(tuple(rows), tuple(page.problems))


def _processing_state(pending: bool, attempt: AttemptState | None) -> str:
    if attempt is AttemptState.FAILED:
        return "failed"
    return "pending" if pending else "ready"
