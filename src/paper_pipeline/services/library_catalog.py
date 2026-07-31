"""Prepared, disposable read model for interactive library browsing.

The filesystem remains canonical.  A catalog is an in-process projection of
``paper.json`` records plus expensive presentation facts that do not belong in
the versioned library format.  Paper Pipeline writes update it immediately;
``refresh_catalog`` rebuilds it when another process changed the library.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.library.model import PaperRecord
from paper_pipeline.library.paths import OPERATIONAL_DIR, PAPER_FILE, PAPERS_DIR
from paper_pipeline.library.storage import (
    Library,
    conversion_is_fresh,
    page_render_is_fresh,
    recipe_is_fresh,
    sha256_file,
)
from paper_pipeline.services.pdf_info import LARGE_DOCUMENT_PAGE_THRESHOLD, pdf_page_count

if TYPE_CHECKING:
    from paper_pipeline.services.runtime import LibraryRuntime, LibrarySession

_CACHE_VERSION = 1
_CACHE_FILE = "catalog-cache.json"
_STALE_CHECK_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True)
class CatalogPaper:
    """One immutable paper projection used by read-only application services."""

    record: PaperRecord
    conversion_pending: bool
    page_render_pending: bool
    pending_recipes: frozenset[str]
    page_count: int | None
    is_large_document: bool


@dataclass(frozen=True)
class CatalogSnapshot:
    """Atomically replaceable view of one open library."""

    papers: tuple[CatalogPaper, ...]
    problems: tuple[str, ...]
    generation: int
    refreshed_at: datetime


class LibraryCatalog:
    """Maintain one prepared browsing snapshot for an open library runtime.

    The catalog is never durable truth.  Its optional file under ``.pp`` only
    avoids reopening unchanged PDFs after a process restart and may be deleted
    at any time.
    """

    def __init__(self, library: Library) -> None:
        self._library = library
        self._lock = threading.RLock()
        self._page_counts = self._load_cache()
        self._cache_dirty = False
        self._snapshot = CatalogSnapshot((), (), 0, datetime.now(UTC))
        self._fingerprint: tuple[tuple[object, ...], ...] = ()
        self._stale = False
        self._last_stale_check = 0.0
        records, problems = library.list_papers()
        self.replace(records, problems)

    def snapshot(self) -> CatalogSnapshot:
        """Return the current immutable snapshot without touching the library."""
        with self._lock:
            return self._snapshot

    def replace(self, records: list[PaperRecord], problems: list[str]) -> CatalogSnapshot:
        """Build and atomically install a complete snapshot from one disk scan."""
        projected = tuple(
            sorted((self._project(record) for record in records), key=_catalog_sort_key)
        )
        with self._lock:
            return self._install(projected, problems)

    def replace_if_generation(
        self,
        records: list[PaperRecord],
        problems: list[str],
        *,
        expected_generation: int,
    ) -> CatalogSnapshot | None:
        """Install a rescan only if no paper write updated the catalog meanwhile."""
        projected = tuple(
            sorted((self._project(record) for record in records), key=_catalog_sort_key)
        )
        with self._lock:
            if self._snapshot.generation != expected_generation:
                return None
            return self._install(projected, problems)

    def upsert(self, record: PaperRecord) -> CatalogSnapshot:
        """Replace one entry after its canonical ``paper.json`` write succeeds."""
        with self._lock:
            entry = self._project(record)
            citekey = record.metadata.citekey
            sort_key = (citekey.casefold(), citekey)
            papers = list(self._snapshot.papers)
            index = bisect_left(papers, sort_key, key=_catalog_sort_key)
            if index < len(papers) and papers[index].record.metadata.citekey == citekey:
                papers[index] = entry
            else:
                papers.insert(index, entry)
            self._snapshot = CatalogSnapshot(
                papers=tuple(papers),
                problems=self._snapshot.problems,
                generation=self._snapshot.generation + 1,
                refreshed_at=datetime.now(UTC),
            )
            fingerprint = list(self._fingerprint)
            fingerprint_index = bisect_left(
                fingerprint,
                (citekey.casefold(), citekey),
                key=lambda row: (str(row[0]).casefold(), str(row[0])),
            )
            row = self._disk_fingerprint_row(entry)
            if (
                fingerprint_index < len(fingerprint)
                and fingerprint[fingerprint_index][0] == citekey
            ):
                fingerprint[fingerprint_index] = row
            else:
                fingerprint.insert(fingerprint_index, row)
            self._fingerprint = tuple(fingerprint)
            self._stale = False
            self._last_stale_check = time.monotonic()
            self._save_cache()
            return self._snapshot

    def _install(
        self,
        projected: tuple[CatalogPaper, ...],
        problems: list[str],
    ) -> CatalogSnapshot:
        self._snapshot = CatalogSnapshot(
            papers=projected,
            problems=tuple(problems),
            generation=self._snapshot.generation + 1,
            refreshed_at=datetime.now(UTC),
        )
        self._fingerprint = self._disk_fingerprint(projected)
        self._stale = False
        self._last_stale_check = time.monotonic()
        self._save_cache()
        return self._snapshot

    def is_stale(self) -> bool:
        """Cheaply detect likely out-of-process changes without blocking every request."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_stale_check < _STALE_CHECK_INTERVAL_SECONDS:
                return self._stale
            self._last_stale_check = now
            self._stale = self._disk_fingerprint(self._snapshot.papers) != self._fingerprint
            return self._stale

    def _project(self, record: PaperRecord) -> CatalogPaper:
        durable = record.model_copy(deep=True)
        page_count = self._page_count(durable)
        pending_recipes = frozenset(
            name
            for name, recipe in durable.recipes.items()
            if (
                not recipe_is_fresh(durable, name)
                or recipe.output_artifact is None
                or recipe.output_sha256 is None
                or not self._artifact_matches(recipe.output_artifact, recipe.output_sha256)
            )
        )
        conversion_pending = (
            not conversion_is_fresh(durable)
            or durable.conversion.transcription_sha256 is None
            or not self._artifact_matches(
                f"{PAPERS_DIR}/{durable.metadata.citekey}/transcription.md",
                durable.conversion.transcription_sha256,
            )
        )
        return CatalogPaper(
            record=durable,
            conversion_pending=conversion_pending,
            # Full page-image hashing belongs to explicit pending selection and
            # validation, not the interactive catalog hot path (ADR-0007).
            page_render_pending=not page_render_is_fresh(durable),
            pending_recipes=pending_recipes,
            page_count=page_count,
            is_large_document=(
                page_count is not None and page_count >= LARGE_DOCUMENT_PAGE_THRESHOLD
            ),
        )

    def _page_count(self, record: PaperRecord) -> int | None:
        if record.source_pdf is None:
            return None
        source = self._library.root.joinpath(*record.source_pdf.split("/"))
        signature = _stat_signature(source)
        identity = record.source_sha256 or record.source_pdf
        cache_key = f"{identity}:{signature[0]}:{signature[1]}" if signature is not None else None
        if cache_key is not None and cache_key in self._page_counts:
            return self._page_counts[cache_key]
        count = pdf_page_count(source)
        if cache_key is not None:
            self._page_counts[cache_key] = count
            self._cache_dirty = True
        return count

    def _artifact_matches(self, relative: str, expected_sha256: str) -> bool:
        try:
            path = self._library.root.joinpath(*relative.split("/"))
            current = self._library.root.resolve()
            for part in path.relative_to(self._library.root).parts:
                current /= part
                if current.is_symlink():
                    return False
            resolved = path.resolve(strict=True)
            return (
                resolved.is_relative_to(self._library.root.resolve())
                and resolved.is_file()
                and sha256_file(resolved) == expected_sha256
            )
        except (OSError, ValueError):
            return False

    def _disk_fingerprint(
        self, entries: tuple[CatalogPaper, ...] | list[CatalogPaper]
    ) -> tuple[tuple[object, ...], ...]:
        sources = {entry.record.metadata.citekey: entry.record.source_pdf for entry in entries}
        papers_root = self._library.root / PAPERS_DIR
        rows: list[tuple[object, ...]] = []
        try:
            directories = sorted(
                (path for path in papers_root.iterdir() if path.is_dir()),
                key=lambda path: (path.name.casefold(), path.name),
            )
        except OSError:
            return (("<papers-unavailable>",),)
        for directory in directories:
            rows.append(self._disk_fingerprint_values(directory.name, sources.get(directory.name)))
        return tuple(rows)

    def _disk_fingerprint_row(self, entry: CatalogPaper) -> tuple[object, ...]:
        record = entry.record
        return self._disk_fingerprint_values(record.metadata.citekey, record.source_pdf)

    def _disk_fingerprint_values(self, citekey: str, source: str | None) -> tuple[object, ...]:
        paper_stat = _stat_signature(self._library.root / PAPERS_DIR / citekey / PAPER_FILE)
        source_stat = (
            _stat_signature(self._library.root.joinpath(*source.split("/")))
            if source is not None
            else None
        )
        return citekey, paper_stat, source, source_stat

    def _load_cache(self) -> dict[str, int | None]:
        path = self._library.root / OPERATIONAL_DIR / _CACHE_FILE
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != _CACHE_VERSION:
                return {}
            values = payload.get("pdf_page_counts", {})
            if not isinstance(values, dict):
                return {}
            return {
                key: value
                for key, value in values.items()
                if isinstance(key, str) and (value is None or isinstance(value, int))
            }
        except (OSError, ValueError, TypeError):
            return {}

    def _save_cache(self) -> None:
        if not self._cache_dirty:
            return
        try:
            operational = self._library.operational_dir()
        except OSError:
            return
        temporary = operational / f".{_CACHE_FILE}.{uuid4().hex}.tmp"
        destination = operational / _CACHE_FILE
        payload = {
            "version": _CACHE_VERSION,
            "pdf_page_counts": self._page_counts,
        }
        try:
            try:
                temporary.write_text(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                os.replace(temporary, destination)
                self._cache_dirty = False
            except OSError:
                # A disposable optimization must never fail a canonical paper write.
                return
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


async def refresh_catalog(runtime: LibraryRuntime) -> CatalogSnapshot:
    """Rescan and replace the catalog without overwriting concurrent paper writes."""
    for _attempt in range(3):
        expected_generation = runtime.catalog.snapshot().generation
        result: list[tuple[list[PaperRecord], list[str]]] = []

        async def read(
            session: LibrarySession,
            job: Job,
            token: CancellationToken,
            *,
            results: list[tuple[list[PaperRecord], list[str]]] = result,
        ) -> None:
            del job, token
            results.append(session.list_papers())

        job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "catalog:refresh", read)
        completed = await runtime.queue.wait(job.id)
        if completed.state is not JobState.SUCCEEDED:
            raise RuntimeError(completed.error or "could not refresh library catalog")
        records, problems = result[0]
        installed = runtime.catalog.replace_if_generation(
            records,
            problems,
            expected_generation=expected_generation,
        )
        if installed is not None:
            return installed
    raise RuntimeError("library kept changing while its catalog was refreshed; try again")


def _stat_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def _catalog_sort_key(item: CatalogPaper) -> tuple[str, str]:
    citekey = item.record.metadata.citekey
    return citekey.casefold(), citekey
