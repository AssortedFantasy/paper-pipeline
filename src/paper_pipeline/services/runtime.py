"""Process-wide library runtimes and scoped storage sessions.

This is the only application-service module that opens raw library storage.
User-facing services operate on :class:`LibraryRuntime` and receive scoped
sessions only while the shared job queue holds the corresponding resource.
"""

from __future__ import annotations

import inspect
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, TypeVar

from paper_pipeline.jobs.model import Job, JobKind
from paper_pipeline.jobs.queue import CancellationToken, JobQueue, KillHook
from paper_pipeline.jobs.recovery import (
    AttemptMarkerStore,
    CompletionResult,
    InterruptedAttempt,
    RecoveryHooks,
    TerminalOutcome,
    reconcile_attempts,
)
from paper_pipeline.library.model import PaperRecord
from paper_pipeline.library.paths import ATTEMPTS_DIR, PAPERS_DIR
from paper_pipeline.library.storage import Library, create_library, open_library
from paper_pipeline.services.library_catalog import LibraryCatalog

T = TypeVar("T")
type ProviderFactory = Callable[[], object]
type PaperWorker = Callable[["PaperSession", Job, CancellationToken], Awaitable[None]]
type LibraryWorker = Callable[["LibrarySession", Job, CancellationToken], Awaitable[None]]
type PaperCompletionValidator = Callable[
    ["PaperSession"], CompletionResult | Awaitable[CompletionResult]
]
type PaperTerminalRecorder = Callable[["PaperSession", TerminalOutcome], None | Awaitable[None]]


class _ScopedSession:
    def __init__(self, library: Library) -> None:
        self._library = library
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("session is no longer inside its held job lane")

    def _close(self) -> None:
        self._active = False


class LibrarySession(_ScopedSession):
    """Short-lived storage surface inside a library read or write operation."""

    def __init__(self, library: Library, *, writable: bool) -> None:
        super().__init__(library)
        self._writable = writable

    def inspect(self, operation: Callable[[Any], T]) -> T:
        """Invoke read-only infrastructure against a mutation-free view."""
        self._require_active()
        return operation(_LibraryReadView(self._library))

    def mutate(self, operation: Callable[[Library], T]) -> T:
        """Invoke raw library infrastructure only under the write barrier."""
        self._require_active()
        if not self._writable:
            raise RuntimeError("library read session cannot mutate storage")
        return operation(self._library)

    def root_path(self, relative_path: str) -> Path:
        self._require_active()
        relative = _relative_path(relative_path)
        return self._library.root.joinpath(*relative.parts)

    def list_papers(self) -> tuple[list[PaperRecord], list[str]]:
        self._require_active()
        return self._library.list_papers()

    def read_paper(self, citekey: str) -> PaperRecord:
        self._require_active()
        return self._library.read_paper(citekey)

    def stage_dir(self) -> Path:
        self._require_active()
        if not self._writable:
            raise RuntimeError("library read session cannot stage artifacts")
        return self._library.stage_dir()

    def install_artifact(self, staged_path: Path, destination: str) -> str:
        self._require_active()
        if not self._writable:
            raise RuntimeError("library read session cannot install artifacts")
        return self._library.install_artifact(staged_path, destination)


class _LibraryReadView:
    """Library surface without mutation methods for nonexclusive readers."""

    def __init__(self, library: Library) -> None:
        self._library = library

    @property
    def root(self) -> Path:
        return self._library.root

    @property
    def info(self):  # type: ignore[no-untyped-def]
        return self._library.info

    def list_papers(self) -> tuple[list[PaperRecord], list[str]]:
        return self._library.list_papers()

    def read_paper(self, citekey: str) -> PaperRecord:
        return self._library.read_paper(citekey)


class PaperSession(_ScopedSession):
    """Citekey-scoped mutation surface valid only inside one paper lane.

    Runtime-created sessions must carry the catalog so the canonical write
    boundary also maintains the disposable read projection (ADR-0007).
    """

    def __init__(
        self,
        library: Library,
        citekey: str,
        *,
        catalog: LibraryCatalog,
    ) -> None:
        super().__init__(library)
        self.citekey = citekey
        self._catalog = catalog

    @property
    def root(self) -> Path:
        self._require_active()
        return self._library.root

    def read_paper(self, citekey: str) -> PaperRecord:
        self._require_active()
        if citekey != self.citekey:
            raise ValueError("paper session cannot read a different citekey")
        return self._library.read_paper(citekey)

    def read_record(self) -> PaperRecord:
        self._require_active()
        return self.read_paper(self.citekey)

    def root_path(self, relative_path: str) -> Path:
        """Resolve a path only within this paper's directory."""
        self._require_active()
        relative = _relative_path(relative_path)
        self._require_paper_path(relative)
        return self._library.root.joinpath(*relative.parts)

    def write_record(self, record: PaperRecord) -> None:
        self._require_active()
        if record.metadata.citekey != self.citekey:
            raise ValueError(
                f"paper session for {self.citekey!r} cannot write {record.metadata.citekey!r}"
            )
        self._library.write_paper(record)
        # Keep this beside the canonical write: new mutation workflows must not
        # publish paper.json without updating the runtime projection (ADR-0007).
        self._catalog.upsert(record)

    def update_record(self, update: Callable[[PaperRecord], PaperRecord | None]) -> PaperRecord:
        """Read-modify-write the record while the queue holds this paper lane."""
        self._require_active()
        record = self.read_record()
        replacement = update(record)
        result = replacement if replacement is not None else record
        self.write_record(result)
        return result

    def stage_dir(self) -> Path:
        self._require_active()
        return self._library.stage_dir()

    def install_artifact(self, staged_path: Path, destination: str) -> str:
        self._require_active()
        relative = _relative_path(destination)
        self._require_paper_path(relative)
        return self._library.install_artifact(staged_path, relative)

    def install_conversion_bundle(self, staging_dir: Path) -> dict[str, str]:
        self._require_active()
        return self._library.install_conversion_bundle(self.citekey, staging_dir)

    def _require_paper_path(self, path: PurePosixPath) -> None:
        if path.parts[:2] != (PAPERS_DIR, self.citekey):
            raise ValueError("paper path must stay inside this paper directory")


class LibraryRuntime:
    """One open library's storage/providers on the shared application queue."""

    def __init__(
        self,
        library: Library,
        queue: JobQueue,
        providers: Mapping[str, object],
        library_key: str,
    ) -> None:
        self._library = library
        self.queue = queue
        self.library_key = library_key
        self.root = library.root
        self.providers = MappingProxyType(dict(providers))
        self.catalog = LibraryCatalog(library)
        self._marker_store = AttemptMarkerStore(
            library.operational_dir() / ATTEMPTS_DIR,
            managed_root=library.root,
        )
        self.interrupted_attempts: tuple[InterruptedAttempt, ...] = tuple(
            reconcile_attempts(self._marker_store, self._terminal_attempt_exists)
        )

    def provider(self, name: str) -> object:
        """Return this runtime's long-lived provider instance."""
        try:
            return self.providers[name]
        except KeyError as error:
            raise KeyError(f"provider is not configured: {name}") from error

    def interrupted(self, attempt_id: str) -> InterruptedAttempt | None:
        """Return one startup-synthesized interrupted attempt by ID."""
        return next(
            (attempt for attempt in self.interrupted_attempts if attempt.job_id == attempt_id),
            None,
        )

    def acknowledge_interrupted(self, attempt_id: str) -> None:
        """Remove an interrupted hint after a replacement job is enqueued."""
        if self.interrupted(attempt_id) is None:
            raise KeyError(f"unknown interrupted attempt: {attempt_id}")
        self._marker_store.remove(attempt_id)
        self.interrupted_attempts = tuple(
            attempt for attempt in self.interrupted_attempts if attempt.job_id != attempt_id
        )

    async def enqueue_paper(
        self,
        citekey: str,
        kind: JobKind,
        label: str,
        worker: PaperWorker,
        *,
        meta: dict[str, str] | None = None,
        kill_hook: KillHook | None = None,
        validate_completion: PaperCompletionValidator | None = None,
        record_terminal: PaperTerminalRecorder | None = None,
    ) -> Job:
        """Schedule a paper worker and create its session only inside the lane."""

        async def queued_worker(job: Job, token: CancellationToken) -> None:
            session = PaperSession(self._library, citekey, catalog=self.catalog)
            try:
                await worker(session, job, token)
            finally:
                session._close()

        recovery = self._paper_recovery(
            citekey,
            label,
            validate_completion,
            record_terminal,
        )
        return await self.queue.enqueue_paper(
            self.library_key,
            citekey,
            kind,
            label,
            queued_worker,
            meta=meta,
            kill_hook=kill_hook,
            recovery=recovery,
        )

    async def enqueue_library_read(
        self,
        kind: JobKind,
        label: str,
        worker: LibraryWorker,
        *,
        meta: dict[str, str] | None = None,
    ) -> Job:
        """Schedule nonexclusive read-only library work."""
        return await self.queue.enqueue_library_read(
            self.library_key,
            kind,
            label,
            self._library_worker(worker, writable=False),
            meta=meta,
        )

    async def enqueue_library_write(
        self,
        kind: JobKind,
        label: str,
        worker: LibraryWorker,
        *,
        meta: dict[str, str] | None = None,
    ) -> Job:
        """Schedule library work behind the paper-lane write barrier."""
        return await self.queue.enqueue_library_write(
            self.library_key,
            kind,
            label,
            self._library_worker(worker, writable=True),
            meta=meta,
        )

    def _library_worker(
        self, worker: LibraryWorker, *, writable: bool
    ) -> Callable[[Job, CancellationToken], Awaitable[None]]:
        async def queued_worker(job: Job, token: CancellationToken) -> None:
            session = LibrarySession(self._library, writable=writable)
            try:
                await worker(session, job, token)
            finally:
                session._close()

        return queued_worker

    def _paper_recovery(
        self,
        citekey: str,
        operation: str,
        validate_completion: PaperCompletionValidator | None,
        record_terminal: PaperTerminalRecorder | None,
    ) -> RecoveryHooks | None:
        if validate_completion is None and record_terminal is None:
            return None
        if record_terminal is None:
            raise ValueError("record_terminal is required with completion validation")

        async def validate() -> CompletionResult:
            if validate_completion is None:
                return CompletionResult()
            session = PaperSession(self._library, citekey, catalog=self.catalog)
            try:
                result = validate_completion(session)
                return await result if inspect.isawaitable(result) else result
            finally:
                session._close()

        async def record(outcome: TerminalOutcome) -> None:
            session = PaperSession(self._library, citekey, catalog=self.catalog)
            try:
                result = record_terminal(session, outcome)
                if inspect.isawaitable(result):
                    await result
            finally:
                session._close()

        return RecoveryHooks(
            marker_store=self._marker_store,
            target=f"{PAPERS_DIR}/{citekey}",
            operation=operation,
            validate_completion=validate,
            record_terminal=record,
        )

    def _terminal_attempt_exists(self, attempt_id: str) -> bool:
        records, _problems = self._library.list_papers()
        for record in records:
            if (
                record.conversion.last_attempt is not None
                and record.conversion.last_attempt.id == attempt_id
            ):
                return True
            if any(
                recipe.last_attempt is not None and recipe.last_attempt.id == attempt_id
                for recipe in record.recipes.values()
            ):
                return True
        return False


class RuntimeRegistry:
    """Canonical-path runtime cache owning the application-wide queue."""

    def __init__(
        self,
        *,
        llm_concurrency: int = 4,
        provider_factories: Mapping[str, ProviderFactory] | None = None,
        queue: JobQueue | None = None,
    ) -> None:
        self.queue = queue or JobQueue(llm_concurrency=llm_concurrency)
        self._provider_factories = dict(provider_factories or {})
        self._runtimes: dict[str, LibraryRuntime] = {}
        self._lock = threading.RLock()

    def open(self, root: Path) -> LibraryRuntime:
        """Open once per canonical resolved, case-normalized root."""
        resolved, key = _canonical_root(root)
        with self._lock:
            existing = self._runtimes.get(key)
            if existing is not None:
                return existing
            return self._register(open_library(resolved), key)

    def create(self, root: Path, *, name: str = "") -> LibraryRuntime:
        """Create a library and immediately register its sole runtime."""
        resolved, key = _canonical_root(root)
        with self._lock:
            if key in self._runtimes:
                raise ValueError(f"library already has an open runtime: {resolved}")
            return self._register(create_library(resolved, name=name), key)

    def _register(self, library: Library, key: str) -> LibraryRuntime:
        providers = {name: factory() for name, factory in self._provider_factories.items()}
        runtime = LibraryRuntime(library, self.queue, providers, key)
        self._runtimes[key] = runtime
        return runtime


def _canonical_root(root: Path) -> tuple[Path, str]:
    resolved = root.expanduser().resolve()
    return resolved, os.path.normcase(str(resolved))


def _relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ValueError("path must be a non-empty library-relative POSIX path")
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in path.parts
        or str(path) != value
        or value == "."
    ):
        raise ValueError(f"path must be normalized and library-relative: {value!r}")
    return path


_PROCESS_REGISTRY = RuntimeRegistry()


def open_runtime(root: Path) -> LibraryRuntime:
    """Return the process-wide runtime for an existing library."""
    return _PROCESS_REGISTRY.open(root)


def create_runtime(root: Path, *, name: str = "") -> LibraryRuntime:
    """Create a library in the process-wide registry."""
    return _PROCESS_REGISTRY.create(root, name=name)
