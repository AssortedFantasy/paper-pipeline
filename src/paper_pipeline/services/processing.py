"""Processing services: conversion, recipe batches, cancellation, and retry."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
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
    RecipeRecord,
)
from paper_pipeline.library.paths import PAPERS_DIR
from paper_pipeline.library.storage import conversion_is_fresh, recipe_is_fresh
from paper_pipeline.recipes.model import RecipeDefinition, load_builtin_recipes
from paper_pipeline.recipes.provider import LLMProvider
from paper_pipeline.recipes.runner import RecipeRunResult, run_recipe
from paper_pipeline.services.runtime import LibraryRuntime, LibrarySession, PaperSession


class ProcessingError(RuntimeError):
    """A queued processing operation failed safely."""


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
            record = session.read_record()
            if record.source_pdf is None or record.source_sha256 is None:
                raise ProcessingError(f"paper {session.citekey!r} has no usable source PDF")
            source = session.root_path(record.source_pdf)
            if not source.is_file():
                raise ProcessingError(f"paper {session.citekey!r} source PDF is missing")
            state.source_sha256 = record.source_sha256
            stage = session.stage_dir()
            request = ConversionRequest(
                pdf_path=source,
                staging_dir=stage,
                timeout_seconds=timeout_seconds,
            )
            try:
                result = await asyncio.to_thread(
                    run_conversion,
                    converter_spec,
                    request,
                    cancel_event=token,
                )
                state.result = result
                if not result.ok:
                    state.log_path = _install_conversion_log(session, job, result)
                    raise ProcessingError(result.error or "conversion failed")
                state.artifacts = session.install_conversion_bundle(stage)
            finally:
                shutil.rmtree(stage, ignore_errors=True)

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
                for recipe in selected:
                    if token.is_set():
                        raise asyncio.CancelledError
                    state.active_recipe = recipe.name
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
                    state.active_recipe = recipe.name
                    session.install_artifact(result.staged_path, result.destination)
                    state.results[recipe.name] = result
            except Exception as error:
                state.log_path = _install_text_log(
                    session,
                    f"recipe-{job.id}.log",
                    f"{type(error).__name__}: {error}",
                )
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


async def cancel_job(runtime: LibraryRuntime, job_id: str) -> bool:
    return await runtime.queue.cancel(job_id)


async def retry_job(runtime: LibraryRuntime, job_id: str) -> Job:
    return await runtime.queue.retry(job_id)


async def pending_conversion_citekeys(runtime: LibraryRuntime) -> list[str]:
    return await _select(runtime, lambda record: not conversion_is_fresh(record))


async def pending_recipe_citekeys(runtime: LibraryRuntime, recipe_name: str) -> list[str]:
    return await _select(runtime, lambda record: not recipe_is_fresh(record, recipe_name))


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
    lines = [result.error or "conversion failed"]
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
