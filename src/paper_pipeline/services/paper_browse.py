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
    llm_cost_usd: float
    cache_hit_rate: float
    live_state: str | None = None
    live_progress: str | None = None
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
    sort: str = "title",
    direction: str = "asc",
    select_pending_conversion: bool = False,
) -> PaperBrowsePage:
    """Return one filtered paper table with durable processing states."""
    page = await list_papers(runtime)
    conversion_pending = set(await pending_conversion_citekeys(runtime))
    recipe_pending = set(await pending_recipe_citekeys(runtime, "summary"))
    query_text = query.casefold().strip()
    if sort not in {"title", "citekey", "year"}:
        raise ValueError(f"unsupported paper sort: {sort}")
    if direction not in {"asc", "desc"}:
        raise ValueError(f"unsupported paper sort direction: {direction}")
    active_by_citekey = {}
    for job in runtime.queue.list_jobs():
        if job.citekey is None or job.state.is_terminal:
            continue
        current = active_by_citekey.get(job.citekey)
        if current is None or job.state.value == "running":
            active_by_citekey[job.citekey] = job

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
        prompt_tokens = sum(item.prompt_tokens for item in paper.recipes.values())
        cached_tokens = sum(item.cached_tokens for item in paper.recipes.values())
        active = active_by_citekey.get(citekey)
        rows.append(
            PaperBrowseRow(
                record=paper,
                conversion_state=conversion_state,
                recipe_state=recipe_state,
                llm_cost_usd=sum(item.cost_usd for item in paper.recipes.values()),
                cache_hit_rate=cached_tokens / prompt_tokens if prompt_tokens else 0.0,
                live_state=active.state.value if active is not None else None,
                live_progress=(active.progress or active.label) if active is not None else None,
                selected=select_pending_conversion and citekey in conversion_pending,
            )
        )
    key_functions = {
        "title": lambda row: (row.record.metadata.title.casefold(), row.record.metadata.citekey),
        "citekey": lambda row: row.record.metadata.citekey.casefold(),
        "year": lambda row: (
            row.record.metadata.year is None,
            row.record.metadata.year or 0,
            row.record.metadata.citekey,
        ),
    }
    rows.sort(key=key_functions[sort], reverse=direction == "desc")
    return PaperBrowsePage(tuple(rows), tuple(page.problems))


def _processing_state(pending: bool, attempt: AttemptState | None) -> str:
    if attempt is AttemptState.FAILED:
        return "failed"
    return "pending" if pending else "ready"
