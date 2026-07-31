"""Processing services: conversion, recipe batches, cancellation, and retry."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast

from paper_pipeline.convert.contract import ConversionRequest, ConversionResult
from paper_pipeline.convert.runner import ConverterSpec, run_conversion
from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import CancellationToken
from paper_pipeline.jobs.recovery import CompletionResult, TerminalOutcome, validate_artifacts
from paper_pipeline.library.model import (
    AttemptRecord,
    AttemptState,
    ConversionRecord,
    PageRenderRecord,
    RecipeRecord,
)
from paper_pipeline.library.paths import PAPERS_DIR
from paper_pipeline.library.storage import (
    conversion_is_fresh,
    page_render_is_fresh,
    recipe_is_fresh,
    sha256_file,
)
from paper_pipeline.pages.contract import PageRenderRequest, PageRenderResult
from paper_pipeline.pages.runner import PageRendererSpec, run_page_render
from paper_pipeline.recipes.model import RecipeDefinition, load_builtin_recipes
from paper_pipeline.recipes.provider import LLMProvider
from paper_pipeline.recipes.runner import RecipeRunResult, run_recipe
from paper_pipeline.services.runtime import LibraryRuntime, LibrarySession, PaperSession


class ProcessingError(RuntimeError):
    """A queued processing operation failed safely."""


class ProcessingMode(StrEnum):
    """How configured work treats an already valid result."""

    RUN = "run"
    OVERWRITE = "overwrite"


@dataclass(frozen=True)
class ProcessingSelection:
    """The operations configured for a multi-paper queue request."""

    conversion: ProcessingMode | None = None
    page_render: ProcessingMode | None = None
    recipes: tuple[tuple[str, ProcessingMode], ...] = ()


@dataclass
class _ConversionState:
    source_sha256: str | None = None
    result: ConversionResult | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    log_path: str | None = None


@dataclass
class _RecipeBatchState:
    results: dict[str, RecipeRunResult] = field(default_factory=dict)
    active_recipe: str | None = None
    log_path: str | None = None


@dataclass
class _PageRenderState:
    source_sha256: str | None = None
    result: PageRenderResult | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    log_path: str | None = None


async def queue_conversion(
    runtime: LibraryRuntime,
    citekeys: list[str],
    *,
    converter_spec: ConverterSpec,
    timeout_seconds: int,
) -> list[Job]:
    """Queue one isolated conversion job per citekey."""
    jobs: list[Job] = []
    for citekey in citekeys:
        state = _ConversionState()

        async def worker(
            session: PaperSession,
            job: Job,
            token: CancellationToken,
            *,
            state: _ConversionState = state,
        ) -> None:
            state.source_sha256 = None
            state.result = None
            state.artifacts.clear()
            state.log_path = None
            try:
                record = session.read_record()
                if record.source_pdf is None or record.source_sha256 is None:
                    raise ProcessingError(f"paper {session.citekey!r} has no usable source PDF")
                source = _safe_input_path(session, record.source_pdf)
                runtime.queue.publish_progress(job.id, "Preparing source PDF")
                stage = session.stage_dir()
                input_stage = session.stage_dir()
                try:
                    snapshot = input_stage / "source.pdf"
                    shutil.copy2(source, snapshot)
                    snapshot_hash = sha256_file(snapshot)
                    if snapshot_hash != record.source_sha256:
                        raise ProcessingError(
                            f"paper {session.citekey!r} source PDF hash no longer "
                            "matches paper.json"
                        )
                    state.source_sha256 = snapshot_hash
                    request = ConversionRequest(
                        pdf_path=snapshot,
                        staging_dir=stage,
                        timeout_seconds=timeout_seconds,
                    )
                    runtime.queue.publish_progress(job.id, "Converting PDF")
                    result = await asyncio.to_thread(
                        run_conversion,
                        converter_spec,
                        request,
                        cancel_event=token,
                    )
                    state.result = result
                    if not result.ok:
                        state.log_path = _install_conversion_log(session, job, result)
                        job.log_path = state.log_path
                        raise ProcessingError(result.error or "conversion failed")
                    state.log_path = _install_conversion_log(session, job, result)
                    job.log_path = state.log_path
                    if not token.begin_commit():
                        raise asyncio.CancelledError
                    runtime.queue.publish_progress(job.id, "Installing transcription")
                    state.artifacts = session.install_transcription_bundle(stage)
                finally:
                    shutil.rmtree(stage, ignore_errors=True)
                    shutil.rmtree(input_stage, ignore_errors=True)
            except Exception as error:
                if state.log_path is None:
                    state.log_path = _install_text_log(
                        session,
                        f"conversion-{job.id}.log",
                        f"{type(error).__name__}: {error}",
                    )
                    job.log_path = state.log_path
                raise

        def validate(
            session: PaperSession,
            *,
            state: _ConversionState = state,
        ) -> CompletionResult:
            artifacts = {path: session.root_path(path) for path in state.artifacts}
            return validate_artifacts(artifacts, expected_hashes=state.artifacts)

        def record_terminal(
            session: PaperSession,
            outcome: TerminalOutcome,
            *,
            state: _ConversionState = state,
        ) -> None:
            def update(paper):  # type: ignore[no-untyped-def]
                attempt = _attempt(outcome, state.log_path)
                if outcome.state is JobState.SUCCEEDED:
                    result = state.result
                    if result is None or state.source_sha256 is None:
                        raise ProcessingError("conversion completed without provenance")
                    transcription_path = f"{PAPERS_DIR}/{session.citekey}/transcription.md"
                    paper.conversion = ConversionRecord(
                        source_sha256=state.source_sha256,
                        transcription_sha256=outcome.artifact_hashes[transcription_path],
                        backend=result.backend,
                        backend_version=result.backend_version,
                        completed_at=outcome.finished_at,
                        last_attempt=attempt,
                    )
                else:
                    paper.conversion.last_attempt = attempt

            session.update_record(update)

        jobs.append(
            await runtime.enqueue_paper(
                citekey,
                JobKind.CONVERSION,
                "convert",
                worker,
                validate_completion=validate,
                record_terminal=record_terminal,
            )
        )
    return jobs


async def queue_page_render(
    runtime: LibraryRuntime,
    citekeys: list[str],
    *,
    renderer_spec: PageRendererSpec,
    timeout_seconds: int,
    dpi: int = 96,
) -> list[Job]:
    """Queue one local, isolated page-render job per citekey."""
    jobs: list[Job] = []
    for citekey in citekeys:
        state = _PageRenderState()

        async def worker(
            session: PaperSession,
            job: Job,
            token: CancellationToken,
            *,
            state: _PageRenderState = state,
        ) -> None:
            state.source_sha256 = None
            state.result = None
            state.artifacts.clear()
            state.log_path = None
            try:
                record = session.read_record()
                if record.source_pdf is None or record.source_sha256 is None:
                    raise ProcessingError(f"paper {session.citekey!r} has no usable source PDF")
                source = _safe_input_path(session, record.source_pdf)
                runtime.queue.publish_progress(job.id, "Preparing source PDF")
                stage = session.stage_dir()
                input_stage = session.stage_dir()
                try:
                    snapshot = input_stage / "source.pdf"
                    shutil.copy2(source, snapshot)
                    snapshot_hash = sha256_file(snapshot)
                    if snapshot_hash != record.source_sha256:
                        raise ProcessingError(
                            f"paper {session.citekey!r} source PDF hash no longer "
                            "matches paper.json"
                        )
                    state.source_sha256 = snapshot_hash
                    request = PageRenderRequest(
                        pdf_path=snapshot,
                        staging_dir=stage,
                        timeout_seconds=timeout_seconds,
                        dpi=dpi,
                    )
                    runtime.queue.publish_progress(job.id, "Rendering page images")
                    result = await asyncio.to_thread(
                        run_page_render,
                        renderer_spec,
                        request,
                        cancel_event=token,
                    )
                    state.result = result
                    state.log_path = _install_page_render_log(session, job, result)
                    job.log_path = state.log_path
                    if not result.ok:
                        raise ProcessingError(result.error or "page rendering failed")
                    if not token.begin_commit():
                        raise asyncio.CancelledError
                    runtime.queue.publish_progress(job.id, "Installing page images")
                    state.artifacts = session.install_pages_bundle(stage)
                finally:
                    shutil.rmtree(stage, ignore_errors=True)
                    shutil.rmtree(input_stage, ignore_errors=True)
            except Exception as error:
                if state.log_path is None:
                    state.log_path = _install_text_log(
                        session,
                        f"pages-{job.id}.log",
                        f"{type(error).__name__}: {error}",
                    )
                    job.log_path = state.log_path
                raise

        def validate(
            session: PaperSession,
            *,
            state: _PageRenderState = state,
        ) -> CompletionResult:
            artifacts = {path: session.root_path(path) for path in state.artifacts}
            return validate_artifacts(artifacts, expected_hashes=state.artifacts)

        def record_terminal(
            session: PaperSession,
            outcome: TerminalOutcome,
            *,
            state: _PageRenderState = state,
        ) -> None:
            def update(paper):  # type: ignore[no-untyped-def]
                attempt = _attempt(outcome, state.log_path)
                if outcome.state is JobState.SUCCEEDED:
                    result = state.result
                    if result is None or state.source_sha256 is None:
                        raise ProcessingError("page rendering completed without provenance")
                    paper.pages = PageRenderRecord(
                        source_sha256=state.source_sha256,
                        renderer=result.renderer,
                        renderer_version=result.renderer_version,
                        dpi=dpi,
                        page_count=len(outcome.artifact_hashes),
                        artifacts=outcome.artifact_hashes,
                        completed_at=outcome.finished_at,
                        last_attempt=attempt,
                    )
                else:
                    paper.pages.last_attempt = attempt

            session.update_record(update)

        jobs.append(
            await runtime.enqueue_paper(
                citekey,
                JobKind.PAGE_RENDER,
                "render-pages",
                worker,
                validate_completion=validate,
                record_terminal=record_terminal,
            )
        )
    return jobs


async def queue_recipes(
    runtime: LibraryRuntime,
    recipe_names: list[str],
    citekeys: list[str],
    *,
    provider_name: str,
    model: str = "",
    recipes: dict[str, RecipeDefinition] | None = None,
) -> list[Job]:
    """Queue one sequential recipe batch per paper, concurrent across papers."""
    definitions = recipes or load_builtin_recipes()
    selected: list[RecipeDefinition] = []
    for name in recipe_names:
        try:
            selected.append(definitions[name])
        except KeyError as error:
            raise ValueError(f"unknown recipe: {name}") from error
    outputs: dict[str, str] = {}
    for recipe in selected:
        collision = outputs.get(recipe.output.casefold())
        if collision is not None:
            raise ValueError(
                f"recipes {collision!r} and {recipe.name!r} declare the same output "
                f"filename: {recipe.output}"
            )
        outputs[recipe.output.casefold()] = recipe.name
    provider = cast(LLMProvider, runtime.provider(provider_name))
    jobs: list[Job] = []
    for citekey in citekeys:
        state = _RecipeBatchState()

        async def worker(
            session: PaperSession,
            job: Job,
            token: CancellationToken,
            *,
            state: _RecipeBatchState = state,
        ) -> None:
            state.results.clear()
            state.active_recipe = None
            state.log_path = None
            staged_results: list[tuple[RecipeDefinition, RecipeRunResult]] = []
            try:
                for index, recipe in enumerate(selected, start=1):
                    if token.is_set():
                        raise asyncio.CancelledError
                    state.active_recipe = recipe.name
                    runtime.queue.publish_progress(
                        job.id,
                        f"Running {recipe.name} ({index}/{len(selected)})",
                    )
                    result = await asyncio.to_thread(
                        run_recipe,
                        session,
                        session.citekey,
                        recipe,
                        provider,
                        model=model,
                    )
                    staged_results.append((recipe, result))
                for recipe, result in staged_results:
                    if not token.begin_commit():
                        raise asyncio.CancelledError
                    state.active_recipe = recipe.name
                    runtime.queue.publish_progress(job.id, f"Installing {recipe.name}")
                    session.install_artifact(result.staged_path, result.destination)
                    state.results[recipe.name] = result
                state.log_path = _install_text_log(
                    session,
                    f"recipe-{job.id}.log",
                    _recipe_usage_text(staged_results),
                )
                job.log_path = state.log_path
            except Exception as error:
                state.log_path = _install_text_log(
                    session,
                    f"recipe-{job.id}.log",
                    _recipe_usage_text(staged_results, error=error),
                )
                job.log_path = state.log_path
                raise
            finally:
                for _recipe, result in staged_results:
                    shutil.rmtree(result.staged_path.parent, ignore_errors=True)

        def validate(
            session: PaperSession,
            *,
            state: _RecipeBatchState = state,
        ) -> CompletionResult:
            expected = {
                result.record.output_artifact or result.destination: result.record.output_sha256
                or ""
                for result in state.results.values()
            }
            return validate_artifacts(
                {path: session.root_path(path) for path in expected},
                expected_hashes=expected,
            )

        def record_terminal(
            session: PaperSession,
            outcome: TerminalOutcome,
            *,
            state: _RecipeBatchState = state,
        ) -> None:
            def update(paper):  # type: ignore[no-untyped-def]
                attempt = _attempt(outcome, state.log_path)
                if outcome.state is JobState.SUCCEEDED:
                    for recipe_name, result in state.results.items():
                        recipe_record = result.record.model_copy(deep=True)
                        recipe_record.last_attempt = attempt
                        paper.recipes[recipe_name] = recipe_record
                else:
                    for recipe_name, result in state.results.items():
                        recipe_record = result.record.model_copy(deep=True)
                        recipe_record.last_attempt = AttemptRecord(
                            id=outcome.attempt_id,
                            state=AttemptState.SUCCEEDED,
                            started_at=outcome.started_at,
                            finished_at=outcome.finished_at,
                        )
                        paper.recipes[recipe_name] = recipe_record
                if outcome.state is not JobState.SUCCEEDED and state.active_recipe is not None:
                    recipe_record = paper.recipes.setdefault(state.active_recipe, RecipeRecord())
                    recipe_record.last_attempt = attempt

            session.update_record(update)

        jobs.append(
            await runtime.enqueue_paper(
                citekey,
                JobKind.RECIPE,
                "recipes:" + ",".join(recipe_names),
                worker,
                validate_completion=validate,
                record_terminal=record_terminal,
            )
        )
    return jobs


async def queue_configured_processing(
    runtime: LibraryRuntime,
    citekeys: list[str],
    selection: ProcessingSelection,
    *,
    converter_spec: ConverterSpec,
    converter_timeout_seconds: int,
    renderer_spec: PageRendererSpec,
    page_render_timeout_seconds: int,
    provider_name: str,
    model: str = "",
) -> list[Job]:
    """Queue configured work, skipping valid results unless overwrite was selected."""
    selected_citekeys = list(dict.fromkeys(citekeys))
    definitions = load_builtin_recipes()
    recipe_modes = dict(selection.recipes)
    unknown_recipes = sorted(set(recipe_modes) - set(definitions))
    if unknown_recipes:
        raise ValueError(f"unknown recipe: {unknown_recipes[0]}")
    if selection.conversion is None and selection.page_render is None and not recipe_modes:
        raise ValueError("Choose at least one operation to queue.")

    catalog = {item.record.metadata.citekey: item for item in runtime.catalog.snapshot().papers}
    unknown_citekeys = [citekey for citekey in selected_citekeys if citekey not in catalog]
    if unknown_citekeys:
        raise KeyError(f"unknown paper: {unknown_citekeys[0]}")

    jobs: list[Job] = []
    if selection.conversion is not None:
        conversion_targets = [
            citekey
            for citekey in selected_citekeys
            if selection.conversion is ProcessingMode.OVERWRITE
            or catalog[citekey].conversion_pending
        ]
        jobs.extend(
            await queue_conversion(
                runtime,
                conversion_targets,
                converter_spec=converter_spec,
                timeout_seconds=converter_timeout_seconds,
            )
        )

    if selection.page_render is not None:
        page_targets = [
            citekey
            for citekey in selected_citekeys
            if selection.page_render is ProcessingMode.OVERWRITE
            or catalog[citekey].page_render_pending
        ]
        jobs.extend(
            await queue_page_render(
                runtime,
                page_targets,
                renderer_spec=renderer_spec,
                timeout_seconds=page_render_timeout_seconds,
            )
        )

    recipe_groups: dict[tuple[str, ...], list[str]] = {}
    for citekey in selected_citekeys:
        item = catalog[citekey]
        names = tuple(
            name
            for name, mode in selection.recipes
            if mode is ProcessingMode.OVERWRITE
            or name not in item.record.recipes
            or name in item.pending_recipes
        )
        if names:
            recipe_groups.setdefault(names, []).append(citekey)
    for names, targets in recipe_groups.items():
        jobs.extend(
            await queue_recipes(
                runtime,
                list(names),
                targets,
                provider_name=provider_name,
                model=model,
                recipes=definitions,
            )
        )
    return jobs


async def cancel_job(runtime: LibraryRuntime, job_id: str) -> bool:
    _runtime_job(runtime, job_id)
    return await runtime.queue.cancel(job_id)


async def retry_job(
    runtime: LibraryRuntime,
    job_id: str,
    *,
    converter_spec: ConverterSpec | None = None,
    page_renderer_spec: PageRendererSpec | None = None,
    timeout_seconds: int | None = None,
    page_render_timeout_seconds: int | None = None,
    provider_name: str = "openai",
    model: str = "",
    recipes: dict[str, RecipeDefinition] | None = None,
) -> Job:
    """Retry a live failure or reconstruct a startup-interrupted operation."""
    existing = runtime.queue.get(job_id)
    if existing is not None:
        _runtime_job(runtime, job_id)
        return await runtime.queue.retry(job_id)

    interrupted = runtime.interrupted(job_id)
    if interrupted is None:
        raise KeyError(f"unknown job: {job_id}")
    target_parts = interrupted.target.split("/")
    if len(target_parts) != 2 or target_parts[0] != PAPERS_DIR:
        raise ValueError("interrupted paper target is invalid")
    citekey = target_parts[1]
    if interrupted.kind is JobKind.CONVERSION and interrupted.operation == "convert":
        if converter_spec is None or timeout_seconds is None:
            raise ValueError("retrying interrupted conversion requires converter settings")
        replacement = (
            await queue_conversion(
                runtime,
                [citekey],
                converter_spec=converter_spec,
                timeout_seconds=timeout_seconds,
            )
        )[0]
    elif interrupted.kind is JobKind.PAGE_RENDER and interrupted.operation == "render-pages":
        if page_renderer_spec is None or page_render_timeout_seconds is None:
            raise ValueError("retrying interrupted page rendering requires renderer settings")
        replacement = (
            await queue_page_render(
                runtime,
                [citekey],
                renderer_spec=page_renderer_spec,
                timeout_seconds=page_render_timeout_seconds,
            )
        )[0]
    elif interrupted.kind is JobKind.RECIPE and interrupted.operation.startswith("recipes:"):
        names = [name for name in interrupted.operation.removeprefix("recipes:").split(",") if name]
        if not names:
            raise ValueError("interrupted recipe operation declares no recipes")
        replacement = (
            await queue_recipes(
                runtime,
                names,
                [citekey],
                provider_name=provider_name,
                model=model,
                recipes=recipes,
            )
        )[0]
    else:
        raise ValueError(f"interrupted operation is not reconstructable: {interrupted.operation}")
    replacement.meta["retry_of"] = job_id
    runtime.acknowledge_interrupted(job_id)
    return replacement


async def pending_conversion_citekeys(runtime: LibraryRuntime) -> list[str]:
    # Queue selection is an explicit integrity check, so it deliberately reads
    # canonical records and hashes artifacts. Interactive presentation uses the
    # prepared catalog instead; keep that distinction when adding callers
    # (ADR-0007).
    return await _select(
        runtime,
        lambda record: (
            not conversion_is_fresh(record)
            or record.conversion.transcription_sha256 is None
            or not _safe_hashed_file(
                runtime.root,
                f"{PAPERS_DIR}/{record.metadata.citekey}/transcription.md",
                record.conversion.transcription_sha256,
            )
        ),
    )


async def pending_page_render_citekeys(runtime: LibraryRuntime) -> list[str]:
    """Return papers whose recorded page images are absent, stale, or invalid."""

    def pending(record):  # type: ignore[no-untyped-def]
        return not page_render_is_fresh(record) or not _page_artifacts_match(
            runtime.root,
            record.metadata.citekey,
            record.pages.artifacts,
        )

    return await _select(runtime, pending)


async def pending_recipe_citekeys(runtime: LibraryRuntime, recipe_name: str) -> list[str]:
    def pending(record):  # type: ignore[no-untyped-def]
        recipe = record.recipes.get(recipe_name)
        return (
            not recipe_is_fresh(record, recipe_name)
            or recipe is None
            or recipe.output_artifact is None
            or recipe.output_sha256 is None
            or not _safe_hashed_file(
                runtime.root,
                recipe.output_artifact,
                recipe.output_sha256,
            )
        )

    return await _select(runtime, pending)


async def _select(runtime: LibraryRuntime, predicate) -> list[str]:  # type: ignore[no-untyped-def]
    selected: list[str] = []

    async def worker(session: LibrarySession, job: Job, token: CancellationToken) -> None:
        del job, token
        records, _problems = session.list_papers()
        selected.extend(record.metadata.citekey for record in records if predicate(record))

    job = await runtime.enqueue_library_read(JobKind.MAINTENANCE, "select-pending", worker)
    completed = await runtime.queue.wait(job.id)
    if completed.state is not JobState.SUCCEEDED:
        raise ProcessingError(completed.error or "could not inspect pending papers")
    return sorted(selected)


def _attempt(outcome: TerminalOutcome, log_path: str | None = None) -> AttemptRecord:
    states = {
        JobState.SUCCEEDED: AttemptState.SUCCEEDED,
        JobState.FAILED: AttemptState.FAILED,
        JobState.CANCELLED: AttemptState.CANCELLED,
    }
    return AttemptRecord(
        id=outcome.attempt_id,
        state=states[outcome.state],
        started_at=outcome.started_at,
        finished_at=outcome.finished_at,
        error=outcome.error,
        log_path=log_path,
    )


def _install_conversion_log(session: PaperSession, job: Job, result: ConversionResult) -> str:
    relative = f"{PAPERS_DIR}/{session.citekey}/.pp/conversion-{job.id}.log"
    stage = session.stage_dir()
    staged = stage / "conversion.log"
    status = "conversion succeeded" if result.ok else result.error or "conversion failed"
    lines = [status, f"duration_seconds={result.duration_seconds:.3f}"]
    lines.extend(f"[{name}]\n{text}" for name, text in sorted(result.diagnostics.items()) if text)
    staged.write_text("\n\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    try:
        session.install_artifact(staged, relative)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return relative


def _install_page_render_log(session: PaperSession, job: Job, result: PageRenderResult) -> str:
    relative = f"{PAPERS_DIR}/{session.citekey}/.pp/pages-{job.id}.log"
    stage = session.stage_dir()
    staged = stage / "pages.log"
    status = "page rendering succeeded" if result.ok else result.error or "page rendering failed"
    lines = [status, f"duration_seconds={result.duration_seconds:.3f}"]
    lines.extend(f"[{name}]\n{text}" for name, text in sorted(result.diagnostics.items()) if text)
    staged.write_text("\n\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    try:
        session.install_artifact(staged, relative)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return relative


def _install_text_log(session: PaperSession, filename: str, text: str) -> str:
    relative = f"{PAPERS_DIR}/{session.citekey}/.pp/{filename}"
    stage = session.stage_dir()
    staged = stage / "operation.log"
    staged.write_text(text + "\n", encoding="utf-8", newline="\n")
    try:
        session.install_artifact(staged, relative)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return relative


def _recipe_usage_text(
    results: list[tuple[RecipeDefinition, RecipeRunResult]],
    *,
    error: Exception | None = None,
) -> str:
    lines = []
    for recipe, result in results:
        usage = result.record
        hit_rate = usage.cached_tokens / usage.prompt_tokens if usage.prompt_tokens else 0.0
        lines.append(
            f"{recipe.name}: prompt={usage.prompt_tokens} cached={usage.cached_tokens} "
            f"cache_write={usage.cache_write_tokens} cache_hit_rate={hit_rate:.1%} "
            f"completion={usage.completion_tokens} "
            f"cost_usd={usage.cost_usd:.8f}"
        )
    totals = (
        sum(result.record.prompt_tokens for _recipe, result in results),
        sum(result.record.cached_tokens for _recipe, result in results),
        sum(result.record.cache_write_tokens for _recipe, result in results),
        sum(result.record.completion_tokens for _recipe, result in results),
        sum(result.record.cost_usd for _recipe, result in results),
    )
    hit_rate = totals[1] / totals[0] if totals[0] else 0.0
    lines.append(
        f"total: prompt={totals[0]} cached={totals[1]} cache_hit_rate={hit_rate:.1%} "
        f"cache_write={totals[2]} completion={totals[3]} cost_usd={totals[4]:.8f}"
    )
    if error is not None:
        lines.append(f"{type(error).__name__}: {error}")
    return "\n".join(lines)


def _runtime_job(runtime: LibraryRuntime, job_id: str) -> Job:
    job = runtime.queue.get(job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    if job.library_key != runtime.library_key:
        raise ValueError("job belongs to a different library")
    return job


def _safe_input_path(session: PaperSession, relative: str) -> Path:
    path = session.root_path(relative)
    root = session.root.resolve()
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ProcessingError(f"paper input must not contain symlinks: {relative}")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ProcessingError(f"paper {session.citekey!r} source PDF is missing") from error
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ProcessingError(f"paper input is outside the library or is not a file: {relative}")
    return resolved


def _safe_hashed_file(root: Path, relative: str, expected_sha256: str) -> bool:
    try:
        path = root.joinpath(*relative.split("/"))
        current = root.resolve()
        for part in path.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                return False
        resolved = path.resolve(strict=True)
        return (
            resolved.is_relative_to(root.resolve())
            and resolved.is_file()
            and sha256_file(resolved) == expected_sha256
        )
    except (OSError, ValueError):
        return False


def _page_artifacts_match(
    root: Path,
    citekey: str,
    expected_hashes: dict[str, str],
) -> bool:
    pages_root = root / PAPERS_DIR / citekey / "pages"
    if pages_root.is_symlink() or not pages_root.is_dir():
        return False
    actual: set[str] = set()
    for path in pages_root.rglob("*"):
        if path.is_symlink():
            return False
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    return actual == set(expected_hashes) and all(
        _safe_hashed_file(root, stored, digest) for stored, digest in expected_hashes.items()
    )
