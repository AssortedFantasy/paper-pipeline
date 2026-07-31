"""Durable OpenAI Batch orchestration for built-in recipe runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from paper_pipeline.jobs.model import Job, JobKind, JobState
from paper_pipeline.jobs.queue import (
    CancellationReason,
    CancellationToken,
    PartialJobError,
)
from paper_pipeline.library.model import AttemptRecord, AttemptState, RecipeRecord
from paper_pipeline.library.paths import OPERATIONAL_DIR, PAPERS_DIR, RECIPE_RUNS_DIR
from paper_pipeline.library.storage import recipe_is_fresh, sha256_file
from paper_pipeline.recipes.batch_model import (
    CollectedRecipeResult,
    RecipeInvocation,
    RecipeRunManifest,
    RecipeRunPhase,
    RecipeRunState,
)
from paper_pipeline.recipes.batch_store import RecipeRunStore
from paper_pipeline.recipes.input import resolve_recipe_input
from paper_pipeline.recipes.model import RecipeDefinition, load_builtin_recipes
from paper_pipeline.recipes.provider import BatchLLMProvider
from paper_pipeline.services.runtime import LibraryRuntime, PaperSession

_TERMINAL_REMOTE = frozenset({"completed", "failed", "expired", "cancelled"})
_PDF_INPUT_LIMIT = 50 * 1024 * 1024
_BATCH_FILE_LIMIT = 200 * 1024 * 1024
_INPUT_EXPIRY_SECONDS = 48 * 60 * 60
_POLL_INTERVAL_SECONDS = 5
_BATCH_PROGRESS_STAGES = {
    "queued": (0, "Waiting to start"),
    "prepare": (1, "Preparing inputs"),
    "upload": (2, "Uploading inputs"),
    "generate": (3, "Generating responses"),
    "collect": (4, "Collecting responses"),
    "install": (5, "Installing results"),
    "cleanup": (6, "Cleaning up"),
    "done": (7, "Finished"),
}
_BATCH_PROGRESS_STAGE_COUNT = 6


async def queue_recipe_batch(
    runtime: LibraryRuntime,
    recipe_names: list[str],
    citekeys: list[str],
    *,
    provider_name: str,
    model: str,
    recipes: dict[str, RecipeDefinition] | None = None,
    overwrite_targets: frozenset[tuple[str, str]] | None = None,
) -> Job:
    """Queue one durable logical recipe run across all selected papers."""
    definitions = recipes or load_builtin_recipes()
    selected = _select_recipes(definitions, recipe_names)
    if not model:
        raise ValueError("LLM model is not configured")
    citekeys = list(dict.fromkeys(citekeys))
    if not citekeys:
        raise ValueError("select at least one paper for recipe processing")
    targets = {citekey: list(selected) for citekey in citekeys}
    return await _queue_recipe_targets(
        runtime,
        targets,
        provider_name=provider_name,
        model=model,
        definitions=definitions,
        overwrite_targets=overwrite_targets,
    )


async def retry_failed_recipe_batch(runtime: LibraryRuntime, job: Job) -> Job:
    """Create a fresh paid Batch containing only failed requests from a partial run."""
    if job.kind is not JobKind.RECIPE_BATCH or job.state not in {
        JobState.PARTIAL,
        JobState.FAILED,
        JobState.CANCELLED,
    }:
        raise ValueError("job is not a retryable recipe Batch")
    run_id = job.meta.get("run_id")
    if not run_id:
        raise ValueError("recipe Batch job has no durable run ID")
    store = RecipeRunStore(runtime.root)
    manifest = store.read_manifest(run_id)
    state = store.read_state(run_id)
    if not state.phase.is_terminal:
        raise ValueError("recipe Batch has resumable local work, not paid failures")
    failed = [
        invocation
        for invocation in manifest.invocations
        if invocation.custom_id not in state.finalized
        or not state.outcomes.get(invocation.custom_id)
        or not state.outcomes[invocation.custom_id].ok
    ]
    if not failed:
        raise ValueError("recipe Batch has no failed requests to retry")
    targets: dict[str, list[RecipeDefinition]] = defaultdict(list)
    definitions: dict[str, RecipeDefinition] = {}
    overwrite_targets: set[tuple[str, str]] = set()
    for invocation in failed:
        definition = RecipeDefinition(
            invocation.recipe_name,
            invocation.recipe_version,
            invocation.input_kind,
            invocation.output_filename,
            invocation.recipe_prompt,
        )
        definitions[definition.name] = definition
        targets[invocation.citekey].append(definition)
        if invocation.overwrite:
            overwrite_targets.add((invocation.citekey, invocation.recipe_name))
    replacement = await _queue_recipe_targets(
        runtime,
        dict(targets),
        provider_name=manifest.provider,
        model=manifest.model,
        definitions=definitions,
        overwrite_targets=frozenset(overwrite_targets),
    )
    replacement.meta["retry_of"] = job.id
    return replacement


async def _queue_recipe_targets(
    runtime: LibraryRuntime,
    targets: dict[str, list[RecipeDefinition]],
    *,
    provider_name: str,
    model: str,
    definitions: dict[str, RecipeDefinition],
    overwrite_targets: frozenset[tuple[str, str]] | None,
) -> Job:
    requested_targets = {
        f"{citekey}/{recipe.name}" for citekey, recipes in targets.items() for recipe in recipes
    }
    _reject_duplicate_targets(runtime, requested_targets)

    run_id = uuid4().hex
    store = RecipeRunStore(runtime.root)
    store.initialize(run_id)
    store.write_state(
        RecipeRunState(
            run_id=run_id,
            phase=RecipeRunPhase.PLANNING,
            updated_at=_now(),
        )
    )
    if overwrite_targets is None:
        overwrite_targets = frozenset(
            (citekey, recipe.name) for citekey, recipes in targets.items() for recipe in recipes
        )

    async def worker(job: Job, token: CancellationToken) -> None:
        try:
            await _run_coordinator(
                runtime,
                job,
                token,
                run_id=run_id,
                provider_name=provider_name,
                model=model,
                definitions=definitions,
                selected_by_citekey=targets,
                overwrite_targets=overwrite_targets,
            )
        except PartialJobError:
            raise
        except asyncio.CancelledError:
            if token.reason is CancellationReason.USER:
                state = store.read_state(run_id)
                state.phase = RecipeRunPhase.CANCELLED
                state.error = "Recipe Batch was cancelled"
                state.updated_at = _now()
                store.write_state(state)
            raise
        except Exception as error:
            state = store.read_state(run_id)
            state.error = f"{type(error).__name__}: {error}"
            state.updated_at = _now()
            store.write_state(state)
            raise

    target_keys = ",".join(
        f"{citekey}/{recipe.name}" for citekey, recipes in targets.items() for recipe in recipes
    )
    request_count = sum(len(recipes) for recipes in targets.values())
    paper_label = "paper" if len(targets) == 1 else "papers"
    request_label = "request" if request_count == 1 else "requests"
    return await runtime.enqueue_remote(
        JobKind.RECIPE_BATCH,
        (f"Recipe Batch · {len(targets)} {paper_label} · {request_count} {request_label}"),
        worker,
        meta={
            "run_id": run_id,
            "paper_count": str(len(targets)),
            "request_count": str(request_count),
            "target_keys": target_keys,
            "provider_label": "OpenAI" if provider_name == "openai" else provider_name,
            "progress_stage": "queued",
            "progress_stage_index": "0",
            "progress_stage_count": str(_BATCH_PROGRESS_STAGE_COUNT),
            "progress_stage_label": "Waiting to start",
        },
    )


async def resume_recipe_runs(runtime: LibraryRuntime) -> list[Job]:
    """Re-enqueue submitted or collected runs after the library is reopened."""
    jobs: list[Job] = []
    store = RecipeRunStore(runtime.root)
    active_run_ids = {
        job.meta.get("run_id")
        for job in runtime.queue.list_jobs()
        if job.library_key == runtime.library_key
        and job.kind is JobKind.RECIPE_BATCH
        and not job.state.is_terminal
    }
    for run_id in store.list_run_ids():
        try:
            state = store.read_state(run_id)
            manifest = store.read_manifest(run_id)
        except (OSError, ValueError):
            await asyncio.to_thread(store.discard, run_id)
            continue
        if run_id in active_run_ids:
            continue
        if state.phase.is_terminal and not state.cleanup_pending:
            await asyncio.to_thread(_prune_local_payload, store, manifest, state)
            continue
        definitions = {
            invocation.recipe_name: RecipeDefinition(
                invocation.recipe_name,
                invocation.recipe_version,
                invocation.input_kind,
                invocation.output_filename,
                invocation.recipe_prompt,
            )
            for invocation in manifest.invocations
        }

        async def worker(
            job: Job,
            token: CancellationToken,
            *,
            run_id: str = run_id,
            manifest: RecipeRunManifest = manifest,
            definitions: dict[str, RecipeDefinition] = definitions,
        ) -> None:
            await _run_coordinator(
                runtime,
                job,
                token,
                run_id=run_id,
                provider_name=manifest.provider,
                model=manifest.model,
                definitions=definitions,
                selected_by_citekey={},
                overwrite_targets=frozenset(),
            )

        jobs.append(
            await runtime.enqueue_remote(
                JobKind.RECIPE_BATCH,
                (f"Recipe Batch · recovered · {len(manifest.invocations)} requests"),
                worker,
                meta={
                    "run_id": run_id,
                    "paper_count": str(len({item.citekey for item in manifest.invocations})),
                    "request_count": str(len(manifest.invocations)),
                    "recovered": "true",
                    "provider_label": (
                        "OpenAI" if manifest.provider == "openai" else manifest.provider
                    ),
                    "progress_stage": "queued",
                    "progress_stage_index": "0",
                    "progress_stage_count": str(_BATCH_PROGRESS_STAGE_COUNT),
                    "progress_stage_label": "Waiting to resume",
                },
            )
        )
    return jobs


async def _run_coordinator(
    runtime: LibraryRuntime,
    job: Job,
    token: CancellationToken,
    *,
    run_id: str,
    provider_name: str,
    model: str,
    definitions: dict[str, RecipeDefinition],
    selected_by_citekey: dict[str, list[RecipeDefinition]],
    overwrite_targets: frozenset[tuple[str, str]],
) -> None:
    store = RecipeRunStore(runtime.root)
    provider = cast(BatchLLMProvider, runtime.provider(provider_name))
    manifest_path = store.path(run_id, "manifest.json")
    if manifest_path.is_file():
        manifest = store.read_manifest(run_id)
    else:
        manifest = await _prepare_manifest(
            runtime,
            job,
            token,
            store,
            run_id=run_id,
            provider_name=provider_name,
            model=model,
            selected_by_citekey=selected_by_citekey,
            overwrite_targets=overwrite_targets,
        )
    state = store.read_state(run_id)
    if not state.outcomes:
        state = await _submit_and_collect(runtime, job, token, store, manifest, state, provider)
    if token.is_set() and token.reason is CancellationReason.SHUTDOWN:
        return
    if token.is_set() and token.reason is CancellationReason.USER:
        await _finish_cancelled(runtime, job, store, manifest, state, provider)
        return
    state.phase = RecipeRunPhase.INSTALLING
    state.updated_at = _now()
    store.write_state(state)
    _publish_batch_progress(
        runtime,
        job,
        stage="install",
        detail=f"Installing results into {job.meta.get('paper_count', '?')} papers",
        install_done=len(state.finalized),
        install_total=len(manifest.invocations),
    )
    await _finalize_results(runtime, job, token, store, manifest, state, definitions)
    state = store.read_state(run_id)
    if token.is_set() and token.reason is CancellationReason.USER:
        await _finish_cancelled(runtime, job, store, manifest, state, provider)
        return
    await _cleanup_remote(runtime, job, store, state, provider)
    state = store.read_state(run_id)
    successes = sum(
        outcome.ok and custom_id in state.finalized for custom_id, outcome in state.outcomes.items()
    )
    failures = len(manifest.invocations) - successes
    state.completed = successes
    state.failed = failures
    state.total = len(manifest.invocations)
    if failures == 0:
        state.phase = RecipeRunPhase.COMPLETED
    elif successes:
        state.phase = RecipeRunPhase.PARTIAL
    else:
        state.phase = RecipeRunPhase.FAILED
    state.updated_at = _now()
    store.write_state(state)
    local_cleanup = await asyncio.to_thread(_prune_local_payload, store, manifest, state)
    _write_summary_log(store, manifest, state)
    job.log_path = f"{OPERATIONAL_DIR}/{RECIPE_RUNS_DIR}/{run_id}/summary.log"
    total_cost_usd = _total_cost_usd(state)
    job.meta.update(
        {
            "completed": str(successes),
            "failed": str(failures),
            "remote_status": state.remote_status or "",
            "total_cost_usd": f"{total_cost_usd:.8f}",
        }
    )
    _publish_batch_progress(
        runtime,
        job,
        stage="done",
        detail=(
            f"Installed {successes}/{len(manifest.invocations)} results"
            if failures == 0
            else f"Installed {successes}; {failures} need attention"
        ),
        install_done=len(state.finalized),
        install_successes=successes,
        install_total=len(manifest.invocations),
        local_cleanup=local_cleanup,
    )
    if state.phase is RecipeRunPhase.PARTIAL:
        raise PartialJobError(f"{successes} recipes installed; {failures} need attention")
    if state.phase is RecipeRunPhase.FAILED:
        raise RuntimeError(state.error or f"all {failures} recipe requests failed")


async def _prepare_manifest(
    runtime: LibraryRuntime,
    parent: Job,
    token: CancellationToken,
    store: RecipeRunStore,
    *,
    run_id: str,
    provider_name: str,
    model: str,
    selected_by_citekey: dict[str, list[RecipeDefinition]],
    overwrite_targets: frozenset[tuple[str, str]],
) -> RecipeRunManifest:
    state = store.read_state(run_id)
    state.phase = RecipeRunPhase.SNAPSHOTTING
    state.updated_at = _now()
    store.write_state(state)
    invocations: list[RecipeInvocation] = []
    next_request = 1
    total_requests = sum(len(recipes) for recipes in selected_by_citekey.values())
    _publish_batch_progress(
        runtime,
        parent,
        stage="prepare",
        detail=f"Preparing 0/{total_requests} recipe inputs",
        prepare_done=0,
        prepare_total=total_requests,
    )
    for citekey, selected in selected_by_citekey.items():
        if token.is_set():
            raise asyncio.CancelledError
        prepared: list[RecipeInvocation] = []

        async def snapshot(
            session: PaperSession,
            job: Job,
            child_token: CancellationToken,
            *,
            citekey: str = citekey,
            selected: list[RecipeDefinition] = selected,
            prepared: list[RecipeInvocation] = prepared,
        ) -> None:
            nonlocal next_request
            del job
            if child_token.is_set() or token.is_set():
                raise asyncio.CancelledError
            paper = session.read_record()
            snapshots: dict[tuple[Path, str], tuple[str, str]] = {}
            for recipe in selected:
                input_path, input_artifact = resolve_recipe_input(
                    session,
                    citekey,
                    recipe,
                    paper.source_pdf,
                )
                if recipe.input == "pdf" and input_path.stat().st_size >= _PDF_INPUT_LIMIT:
                    raise ValueError(
                        f"source PDF for {citekey!r} is too large for Responses file input"
                    )
                snapshot_key = (input_path, input_artifact)
                cached_snapshot = snapshots.get(snapshot_key)
                if cached_snapshot is None:
                    input_sha256 = await asyncio.to_thread(sha256_file, input_path)
                    if (
                        recipe.input == "pdf"
                        and paper.source_sha256 is not None
                        and input_sha256 != paper.source_sha256
                    ):
                        raise ValueError(f"source PDF for {citekey!r} no longer matches paper.json")
                    suffix = input_path.suffix or ".txt"
                    snapshot_name = f"{input_sha256}{suffix.casefold()}"
                    snapshot_path = store.path(run_id, "snapshots", snapshot_name)
                    if not snapshot_path.is_file():
                        await asyncio.to_thread(
                            _copy_input_snapshot,
                            input_path,
                            session.stage_dir(),
                            snapshot_path,
                            input_sha256,
                        )
                    snapshots[snapshot_key] = input_sha256, snapshot_name
                else:
                    input_sha256, snapshot_name = cached_snapshot
                prepared.append(
                    RecipeInvocation(
                        custom_id=f"r{next_request:06d}",
                        citekey=citekey,
                        recipe_name=recipe.name,
                        recipe_version=recipe.version,
                        recipe_prompt=recipe.prompt,
                        prompt_sha256=hashlib.sha256(recipe.prompt.encode()).hexdigest(),
                        output_filename=recipe.output,
                        input_kind=recipe.input,
                        input_artifact=input_artifact,
                        input_sha256=input_sha256,
                        snapshot_filename=snapshot_name,
                        overwrite=(citekey, recipe.name) in overwrite_targets,
                    )
                )
                next_request += 1

        prep = await runtime.enqueue_paper(
            citekey,
            JobKind.RECIPE_FINALIZE,
            f"Prepare recipe Batch · {citekey}",
            snapshot,
            meta={"parent_id": parent.id, "internal": "true", "run_id": run_id},
        )
        completed = await runtime.queue.wait(prep.id)
        if completed.state is not JobState.SUCCEEDED:
            raise RuntimeError(completed.error or f"could not snapshot {citekey}")
        invocations.extend(prepared)
        _publish_batch_progress(
            runtime,
            parent,
            stage="prepare",
            detail=f"Prepared {len(invocations)}/{total_requests} recipe inputs",
            prepare_done=len(invocations),
            prepare_total=total_requests,
        )
    manifest = RecipeRunManifest(
        run_id=run_id,
        provider=provider_name,
        model=model,
        created_at=_now(),
        invocations=invocations,
    )
    store.write_manifest(manifest)
    state.total = len(invocations)
    state.updated_at = _now()
    store.write_state(state)
    return manifest


async def _submit_and_collect(
    runtime: LibraryRuntime,
    job: Job,
    token: CancellationToken,
    store: RecipeRunStore,
    manifest: RecipeRunManifest,
    state: RecipeRunState,
    provider: BatchLLMProvider,
) -> RecipeRunState:
    run_id = manifest.run_id
    if state.batch_id is None and state.phase in {
        RecipeRunPhase.SUBMISSION_ATTEMPTED,
        RecipeRunPhase.SUBMISSION_UNCERTAIN,
    }:
        _publish_batch_progress(
            runtime,
            job,
            stage="generate",
            detail="Reconciling an interrupted provider submission",
            remote_status="reconciling",
        )
        matches = [
            item
            for item in await asyncio.to_thread(provider.list_batches, limit=100)
            if item.input_file_id == state.input_file_id
        ]
        if len(matches) != 1:
            state.phase = RecipeRunPhase.SUBMISSION_UNCERTAIN
            state.error = (
                "Could not uniquely reconcile the attempted OpenAI Batch submission; "
                "no replacement was submitted."
            )
            state.updated_at = _now()
            store.write_state(state)
            raise RuntimeError(state.error)
        state.batch_id = matches[0].id
        state.remote_status = matches[0].status
        state.updated_at = _now()
        store.write_state(state)

    if state.input_file_id is None:
        state.phase = RecipeRunPhase.UPLOADING
        state.updated_at = _now()
        store.write_state(state)
        pdf_hashes = {
            invocation.input_sha256
            for invocation in manifest.invocations
            if invocation.input_kind == "pdf"
        }
        upload_total = len(pdf_hashes)
        _publish_batch_progress(
            runtime,
            job,
            stage="upload",
            detail=(
                f"Uploading {upload_total} distinct PDFs"
                if upload_total
                else "Building Batch request file"
            ),
            upload_done=len(state.uploads),
            upload_total=upload_total,
        )
        for invocation in manifest.invocations:
            if invocation.input_kind != "pdf" or invocation.input_sha256 in state.uploads:
                continue
            if token.is_set():
                raise asyncio.CancelledError
            _publish_batch_progress(
                runtime,
                job,
                stage="upload",
                detail=(
                    f"Uploading PDF {len(state.uploads) + 1}/{upload_total} · {invocation.citekey}"
                ),
                upload_done=len(state.uploads),
                upload_total=upload_total,
            )
            remote = await asyncio.to_thread(
                provider.upload_input,
                store.path(run_id, "snapshots", invocation.snapshot_filename),
                expires_after_seconds=_INPUT_EXPIRY_SECONDS,
            )
            state.uploads[invocation.input_sha256] = remote.id
            state.cleanup_pending.append(remote.id)
            state.updated_at = _now()
            store.write_state(state)
            _publish_batch_progress(
                runtime,
                job,
                stage="upload",
                detail=f"Uploaded {len(state.uploads)}/{upload_total} distinct PDFs",
                upload_done=len(state.uploads),
                upload_total=upload_total,
            )
        requests_path = store.path(run_id, "requests.jsonl")
        _publish_batch_progress(
            runtime,
            job,
            stage="upload",
            detail=f"Building request file for {len(manifest.invocations)} recipes",
            upload_done=len(state.uploads),
            upload_total=upload_total,
        )
        await asyncio.to_thread(_write_requests, requests_path, store, manifest, state, provider)
        if requests_path.stat().st_size >= _BATCH_FILE_LIMIT:
            raise ValueError("recipe Batch input exceeds the 200 MB provider limit")
        _publish_batch_progress(
            runtime,
            job,
            stage="upload",
            detail=f"Uploading Batch request file · {requests_path.stat().st_size:,} bytes",
            upload_done=len(state.uploads),
            upload_total=upload_total,
        )
        remote_input = await asyncio.to_thread(provider.upload_batch_file, requests_path)
        state.input_file_id = remote_input.id
        state.cleanup_pending.append(remote_input.id)
        state.phase = RecipeRunPhase.SUBMISSION_READY
        state.updated_at = _now()
        store.write_state(state)

    if state.batch_id is None:
        state.phase = RecipeRunPhase.SUBMISSION_ATTEMPTED
        state.updated_at = _now()
        state.error = None
        store.write_state(state)
        _publish_batch_progress(
            runtime,
            job,
            stage="generate",
            detail=f"Submitting {len(manifest.invocations)} requests to the provider",
            remote_status="submitting",
            remote_finished=0,
            remote_failed=0,
            poll_count=0,
        )
        try:
            remote_batch = await asyncio.to_thread(
                provider.create_batch,
                input_file_id=state.input_file_id,
                endpoint=manifest.endpoint,
                metadata={
                    "operation": "paper-pipeline-recipes",
                    "run_id": run_id,
                },
            )
        except Exception:
            state.phase = RecipeRunPhase.SUBMISSION_UNCERTAIN
            state.updated_at = _now()
            state.error = (
                "OpenAI Batch submission returned ambiguously; Paper Pipeline "
                "will reconcile before any retry."
            )
            store.write_state(state)
            raise RuntimeError(state.error) from None
        state.batch_id = remote_batch.id
        state.remote_status = remote_batch.status
        state.updated_at = _now()
        store.write_state(state)
        _publish_batch_progress(
            runtime,
            job,
            stage="generate",
            detail="Batch accepted; waiting for the provider to begin processing",
            remote_status=remote_batch.status,
            remote_finished=remote_batch.completed + remote_batch.failed,
            remote_failed=remote_batch.failed,
            poll_count=0,
        )

    cancellation_sent = False
    poll_failures = 0
    poll_count = 0
    while True:
        if token.is_set() and token.reason is CancellationReason.SHUTDOWN:
            return state
        if (
            token.is_set()
            and token.reason is CancellationReason.USER
            and not cancellation_sent
            and state.batch_id is not None
        ):
            _publish_batch_progress(
                runtime,
                job,
                stage="generate",
                detail=(
                    "Cancellation requested; waiting for the provider to stop remaining requests"
                ),
                remote_status="cancelling",
                poll_count=poll_count,
            )
            await asyncio.to_thread(provider.cancel_batch, state.batch_id)
            cancellation_sent = True
        assert state.batch_id is not None
        try:
            remote = await asyncio.to_thread(provider.retrieve_batch, state.batch_id)
            poll_failures = 0
        except Exception:
            poll_failures += 1
            delay = min(60, 5 * (2 ** min(poll_failures - 1, 4)))
            state.error = (
                f"Remote status check failed; retrying in {delay} seconds (attempt {poll_failures})"
            )
            state.updated_at = _now()
            store.write_state(state)
            _publish_batch_progress(
                runtime,
                job,
                stage="generate",
                detail=state.error,
                remote_status=state.remote_status or "status unavailable",
                poll_count=poll_count,
            )
            await asyncio.sleep(delay)
            continue
        poll_count += 1
        state.error = None
        state.remote_status = remote.status
        state.output_file_id = remote.output_file_id
        state.error_file_id = remote.error_file_id
        state.total = remote.total or len(manifest.invocations)
        state.completed = remote.completed
        state.failed = remote.failed
        state.phase = _remote_phase(remote.status)
        state.updated_at = _now()
        store.write_state(state)
        remote_finished = remote.completed + remote.failed
        _publish_batch_progress(
            runtime,
            job,
            stage="generate",
            detail=(
                f"Provider {remote.status.replace('_', ' ')} · "
                f"{remote_finished}/{state.total} requests returned"
            ),
            remote_status=remote.status,
            remote_finished=remote_finished,
            remote_failed=remote.failed,
            poll_count=poll_count,
            last_provider_check=_now().strftime("%H:%M:%S"),
        )
        if remote.status in _TERMINAL_REMOTE:
            break
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    state.phase = RecipeRunPhase.COLLECTING
    state.updated_at = _now()
    store.write_state(state)
    result_files = [
        (file_id, filename)
        for file_id, filename in (
            (state.output_file_id, "output.jsonl"),
            (state.error_file_id, "errors.jsonl"),
        )
        if file_id is not None
    ]
    _publish_batch_progress(
        runtime,
        job,
        stage="collect",
        detail=f"Downloading {len(result_files)} provider result files",
        collect_done=0,
        collect_total=len(result_files),
    )
    for file_index, (file_id, filename) in enumerate(result_files, 1):
        destination = store.path(run_id, filename)
        if not destination.is_file():
            await asyncio.to_thread(_download_atomic, store, provider, file_id, destination)
        if file_id not in state.cleanup_pending:
            state.cleanup_pending.append(file_id)
        _publish_batch_progress(
            runtime,
            job,
            stage="collect",
            detail=f"Downloaded {file_index}/{len(result_files)} result files",
            collect_done=file_index,
            collect_total=len(result_files),
        )
    await asyncio.to_thread(_collect_results, store, manifest, state, provider)
    _publish_batch_progress(
        runtime,
        job,
        stage="collect",
        detail=f"Validated {len(state.outcomes)}/{len(manifest.invocations)} responses",
        collect_done=len(state.outcomes),
        collect_total=len(manifest.invocations),
    )
    state.updated_at = _now()
    store.write_state(state)
    return state


def _copy_input_snapshot(
    source: Path,
    stage: Path,
    destination: Path,
    expected_sha256: str,
) -> None:
    """Copy and verify a recipe input without blocking the application loop."""
    try:
        staged = stage / destination.name
        shutil.copy2(source, staged)
        if sha256_file(staged) != expected_sha256:
            raise ValueError("recipe input changed while being snapshotted")
        os.replace(staged, destination)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _write_requests(
    destination: Path,
    store: RecipeRunStore,
    manifest: RecipeRunManifest,
    state: RecipeRunState,
    provider: BatchLLMProvider,
) -> None:
    store.temp_root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="batch-requests-",
        suffix=".jsonl",
        dir=store.temp_root,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            for invocation in manifest.invocations:
                if invocation.input_kind == "pdf":
                    file_id = state.uploads[invocation.input_sha256]
                    text_input = None
                else:
                    file_id = None
                    text_input = store.path(
                        manifest.run_id,
                        "snapshots",
                        invocation.snapshot_filename,
                    ).read_text(encoding="utf-8")
                line = {
                    "custom_id": invocation.custom_id,
                    "method": "POST",
                    "url": manifest.endpoint,
                    "body": provider.request_body(
                        prompt=invocation.recipe_prompt,
                        model=manifest.model,
                        input_sha256=invocation.input_sha256,
                        file_id=file_id,
                        text_input=text_input,
                    ),
                }
                output.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _collect_results(
    store: RecipeRunStore,
    manifest: RecipeRunManifest,
    state: RecipeRunState,
    provider: BatchLLMProvider,
) -> None:
    expected = {item.custom_id: item for item in manifest.invocations}
    seen: set[str] = set()
    for filename in ("output.jsonl", "errors.jsonl"):
        path = store.path(manifest.run_id, filename)
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as lines:
            for line_number, raw in enumerate(lines, 1):
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw)
                    parsed = provider.parse_batch_line(value)
                except Exception as error:
                    raise ValueError(f"{filename}:{line_number}: invalid Batch result") from error
                if parsed.custom_id in seen:
                    raise ValueError(f"duplicate Batch result custom_id: {parsed.custom_id}")
                seen.add(parsed.custom_id)
                invocation = expected.get(parsed.custom_id)
                if invocation is None:
                    raise ValueError(f"unknown Batch result custom_id: {parsed.custom_id}")
                text_filename = None
                if parsed.ok:
                    text_filename = f"{parsed.custom_id}.md"
                    _atomic_text(
                        store,
                        store.path(manifest.run_id, "collected", text_filename),
                        parsed.text.strip() + "\n",
                    )
                state.outcomes[parsed.custom_id] = CollectedRecipeResult(
                    custom_id=parsed.custom_id,
                    ok=parsed.ok,
                    text_filename=text_filename,
                    provider=parsed.provider or manifest.provider,
                    model=parsed.model or manifest.model,
                    prompt_tokens=parsed.prompt_tokens,
                    cached_tokens=parsed.cached_tokens,
                    cache_write_tokens=parsed.cache_write_tokens,
                    completion_tokens=parsed.completion_tokens,
                    cost_usd=parsed.cost_usd,
                    request_id=parsed.request_id,
                    error=parsed.error,
                )
    for custom_id in sorted(set(expected) - seen):
        state.outcomes[custom_id] = CollectedRecipeResult(
            custom_id=custom_id,
            ok=False,
            provider=manifest.provider,
            model=manifest.model,
            error=(
                f"Batch ended as {state.remote_status or 'terminal'} without "
                "a result for this request"
            ),
        )


async def _finalize_results(
    runtime: LibraryRuntime,
    parent: Job,
    token: CancellationToken,
    store: RecipeRunStore,
    manifest: RecipeRunManifest,
    state: RecipeRunState,
    definitions: dict[str, RecipeDefinition],
) -> None:
    by_paper: dict[str, list[RecipeInvocation]] = defaultdict(list)
    for invocation in manifest.invocations:
        if invocation.custom_id not in state.finalized:
            by_paper[invocation.citekey].append(invocation)
    for citekey, invocations in by_paper.items():
        if token.is_set():
            return

        async def finalize(
            session: PaperSession,
            job: Job,
            child_token: CancellationToken,
            *,
            invocations: list[RecipeInvocation] = invocations,
        ) -> None:
            del job, child_token
            paper = session.read_record()
            paper_changed = False
            input_hashes: dict[Path, str] = {}
            for invocation in invocations:
                outcome = state.outcomes[invocation.custom_id]
                error = outcome.error
                installed_record: RecipeRecord | None = None
                finalized = not outcome.ok
                if outcome.ok:
                    try:
                        current_definition = definitions.get(invocation.recipe_name)
                        if current_definition is not None and (
                            current_definition.version != invocation.recipe_version
                            or current_definition.output != invocation.output_filename
                            or hashlib.sha256(current_definition.prompt.encode()).hexdigest()
                            != invocation.prompt_sha256
                        ):
                            raise ValueError("recipe definition changed after Batch submission")
                        input_path, _artifact = resolve_recipe_input(
                            session,
                            session.citekey,
                            RecipeDefinition(
                                invocation.recipe_name,
                                invocation.recipe_version,
                                invocation.input_kind,
                                invocation.output_filename,
                                invocation.recipe_prompt,
                            ),
                            paper.source_pdf,
                        )
                        input_sha256 = input_hashes.get(input_path)
                        if input_sha256 is None:
                            input_sha256 = await asyncio.to_thread(sha256_file, input_path)
                            input_hashes[input_path] = input_sha256
                        if input_sha256 != invocation.input_sha256:
                            raise ValueError("recipe input changed after Batch submission")
                        if not invocation.overwrite and recipe_is_fresh(
                            paper, invocation.recipe_name
                        ):
                            state.finalized.append(invocation.custom_id)
                            continue
                        assert outcome.text_filename is not None
                        collected = store.path(
                            manifest.run_id,
                            "collected",
                            outcome.text_filename,
                        )
                        stage = session.stage_dir()
                        try:
                            staged = stage / invocation.output_filename
                            shutil.copy2(collected, staged)
                            destination = (
                                f"{PAPERS_DIR}/{session.citekey}/{invocation.output_filename}"
                            )
                            output_sha256 = session.install_artifact(staged, destination)
                        finally:
                            shutil.rmtree(stage, ignore_errors=True)
                        log_path = _install_attempt_log(
                            session,
                            manifest.run_id,
                            invocation,
                            outcome,
                            status="installed",
                        )
                        finished_at = _now()
                        installed_record = RecipeRecord(
                            recipe_version=invocation.recipe_version,
                            provider=outcome.provider,
                            model=outcome.model,
                            input_artifact=invocation.input_artifact,
                            input_sha256=invocation.input_sha256,
                            output_artifact=destination,
                            output_sha256=output_sha256,
                            prompt_tokens=outcome.prompt_tokens,
                            cached_tokens=outcome.cached_tokens,
                            cache_write_tokens=outcome.cache_write_tokens,
                            completion_tokens=outcome.completion_tokens,
                            cost_usd=outcome.cost_usd,
                            completed_at=finished_at,
                            last_attempt=AttemptRecord(
                                id=f"{manifest.run_id}-{invocation.custom_id}",
                                state=AttemptState.SUCCEEDED,
                                started_at=manifest.created_at,
                                finished_at=finished_at,
                                log_path=log_path,
                            ),
                        )
                        outcome.local_error = None
                        finalized = True
                    except Exception as install_error:
                        error = str(install_error)
                        if "changed after Batch submission" in error:
                            outcome.ok = False
                            outcome.error = error
                            finalized = True
                        else:
                            outcome.local_error = error
                            finalized = False
                if installed_record is not None:
                    paper.recipes[invocation.recipe_name] = installed_record
                    paper_changed = True
                else:
                    log_path = _install_attempt_log(
                        session,
                        manifest.run_id,
                        invocation,
                        outcome,
                        status="failed",
                    )
                    finished_at = _now()
                    attempt_id = f"{manifest.run_id}-{invocation.custom_id}"
                    attempt_error = error or "recipe request failed"
                    record = paper.recipes.setdefault(
                        invocation.recipe_name,
                        RecipeRecord(),
                    )
                    record.last_attempt = AttemptRecord(
                        id=attempt_id,
                        state=AttemptState.FAILED,
                        started_at=manifest.created_at,
                        finished_at=finished_at,
                        error=attempt_error,
                        log_path=log_path,
                    )
                    paper_changed = True
                if finalized:
                    state.finalized.append(invocation.custom_id)

            if paper_changed:
                session.write_record(paper)
            state.updated_at = _now()
            store.write_state(state)
            pending_local = [
                invocation.custom_id
                for invocation in invocations
                if invocation.custom_id not in state.finalized
            ]
            if pending_local:
                raise RuntimeError(f"could not install {len(pending_local)} local recipe result(s)")

        child = await runtime.enqueue_paper(
            citekey,
            JobKind.RECIPE_FINALIZE,
            f"Finalize recipe Batch · {citekey}",
            finalize,
            meta={"parent_id": parent.id, "internal": "true", "run_id": manifest.run_id},
        )
        completed = await runtime.queue.wait(child.id)
        if completed.state is not JobState.SUCCEEDED:
            raise RuntimeError(completed.error or f"could not finalize {citekey}")
        installed_successes = sum(
            state.outcomes[custom_id].ok
            for custom_id in state.finalized
            if custom_id in state.outcomes
        )
        _publish_batch_progress(
            runtime,
            parent,
            stage="install",
            detail=(
                f"Finalized {len(state.finalized)}/{len(manifest.invocations)} results · "
                f"{installed_successes} installed"
            ),
            install_done=len(state.finalized),
            install_successes=installed_successes,
            install_total=len(manifest.invocations),
        )


async def _cleanup_remote(
    runtime: LibraryRuntime,
    job: Job,
    store: RecipeRunStore,
    state: RecipeRunState,
    provider: BatchLLMProvider,
) -> None:
    state.phase = RecipeRunPhase.CLEANING_UP
    state.updated_at = _now()
    store.write_state(state)
    pending = list(dict.fromkeys(state.cleanup_pending))
    state.cleanup_pending = []
    _publish_batch_progress(
        runtime,
        job,
        stage="cleanup",
        detail=f"Deleting 0/{len(pending)} temporary provider files",
        cleanup_done=0,
        cleanup_total=len(pending),
    )
    for file_index, file_id in enumerate(pending, 1):
        try:
            await asyncio.to_thread(provider.delete_file, file_id)
        except Exception:
            state.cleanup_pending.append(file_id)
            state.cleanup_warnings.append(f"Remote cleanup is still pending for {file_id}")
        state.updated_at = _now()
        store.write_state(state)
        _publish_batch_progress(
            runtime,
            job,
            stage="cleanup",
            detail=f"Deleted {file_index}/{len(pending)} temporary provider files",
            cleanup_done=file_index,
            cleanup_total=len(pending),
        )
    _publish_batch_progress(
        runtime,
        job,
        stage="cleanup",
        detail=(
            "Temporary provider files deleted"
            if not state.cleanup_pending
            else f"{len(state.cleanup_pending)} provider files still need cleanup"
        ),
        cleanup_done=len(pending) - len(state.cleanup_pending),
        cleanup_total=len(pending),
    )


async def _finish_cancelled(
    runtime: LibraryRuntime,
    job: Job,
    store: RecipeRunStore,
    manifest: RecipeRunManifest,
    state: RecipeRunState,
    provider: BatchLLMProvider,
) -> None:
    """Make durable run state agree with a user-cancelled queue job."""
    await _cleanup_remote(runtime, job, store, state, provider)
    state = store.read_state(manifest.run_id)
    successes = sum(
        outcome.ok and custom_id in state.finalized for custom_id, outcome in state.outcomes.items()
    )
    state.phase = RecipeRunPhase.CANCELLED
    state.completed = successes
    state.failed = len(manifest.invocations) - successes
    state.total = len(manifest.invocations)
    state.error = "Recipe Batch was cancelled"
    state.updated_at = _now()
    store.write_state(state)
    local_cleanup = await asyncio.to_thread(_prune_local_payload, store, manifest, state)
    _write_summary_log(store, manifest, state)
    job.log_path = f"{OPERATIONAL_DIR}/{RECIPE_RUNS_DIR}/{manifest.run_id}/summary.log"
    total_cost_usd = _total_cost_usd(state)
    job.meta.update(
        {
            "completed": str(state.completed),
            "failed": str(state.failed),
            "remote_status": state.remote_status or "",
            "total_cost_usd": f"{total_cost_usd:.8f}",
        }
    )
    _publish_batch_progress(
        runtime,
        job,
        stage="done",
        detail="Recipe Batch cancelled",
        install_done=len(state.finalized),
        install_successes=successes,
        install_total=len(manifest.invocations),
        local_cleanup=local_cleanup,
    )


def _prune_local_payload(
    store: RecipeRunStore,
    manifest: RecipeRunManifest,
    state: RecipeRunState,
) -> str:
    """Discard reconstructable local payloads once every result is finalized."""
    expected = {invocation.custom_id for invocation in manifest.invocations}
    if (
        not state.phase.is_terminal
        or state.cleanup_pending
        or (
            state.phase is not RecipeRunPhase.CANCELLED
            and not expected.issubset(set(state.finalized))
        )
    ):
        return "Recovery files retained"
    try:
        removed_bytes = store.prune_payload(manifest.run_id)
    except OSError as error:
        warning = f"Local recipe workspace cleanup failed: {error}"
        if warning not in state.cleanup_warnings:
            state.cleanup_warnings.append(warning)
        state.updated_at = _now()
        store.write_state(state)
        return "Local workspace cleanup needs attention"
    if removed_bytes >= 1024 * 1024:
        amount = f"{removed_bytes / (1024 * 1024):.1f} MiB"
    elif removed_bytes >= 1024:
        amount = f"{removed_bytes / 1024:.1f} KiB"
    else:
        amount = f"{removed_bytes} bytes"
    return f"{amount} of working files removed"


def _install_attempt_log(
    session: PaperSession,
    run_id: str,
    invocation: RecipeInvocation,
    outcome: CollectedRecipeResult,
    *,
    status: str,
) -> str:
    relative = f"{PAPERS_DIR}/{session.citekey}/.pp/recipe-{invocation.recipe_name}-{run_id}.log"
    stage = session.stage_dir()
    try:
        staged = stage / "recipe.log"
        hit_rate = outcome.cached_tokens / outcome.prompt_tokens if outcome.prompt_tokens else 0.0
        lines = [
            f"recipe={invocation.recipe_name}",
            f"status={status}",
            f"provider={outcome.provider}",
            f"model={outcome.model}",
            f"prompt_tokens={outcome.prompt_tokens}",
            f"cached_tokens={outcome.cached_tokens}",
            f"cache_write_tokens={outcome.cache_write_tokens}",
            f"cache_hit_rate={hit_rate:.1%}",
            f"completion_tokens={outcome.completion_tokens}",
            f"cost_usd={outcome.cost_usd:.8f}",
        ]
        if outcome.request_id:
            lines.append(f"request_id={outcome.request_id}")
        if outcome.error:
            lines.append(f"error={outcome.error}")
        if outcome.local_error:
            lines.append(f"local_error={outcome.local_error}")
        staged.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        session.install_artifact(staged, relative)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return relative


def _write_summary_log(
    store: RecipeRunStore,
    manifest: RecipeRunManifest,
    state: RecipeRunState,
) -> None:
    lines = [
        f"run_id={manifest.run_id}",
        f"provider={manifest.provider}",
        f"model={manifest.model}",
        f"remote_status={state.remote_status or 'unknown'}",
        f"phase={state.phase.value}",
        f"requests={state.total}",
        f"installed={state.completed}",
        f"failed={state.failed}",
        f"total_cost_usd={_total_cost_usd(state):.8f}",
    ]
    lines.extend(f"cleanup_warning={warning}" for warning in state.cleanup_warnings)
    if state.error:
        lines.append(f"error={state.error}")
    _atomic_text(
        store,
        store.path(manifest.run_id, "summary.log"),
        "\n".join(lines) + "\n",
    )


def _total_cost_usd(state: RecipeRunState) -> float:
    """Return provider spend for every collected result, installed or not."""
    return sum(outcome.cost_usd for outcome in state.outcomes.values())


def _download_atomic(
    store: RecipeRunStore,
    provider: BatchLLMProvider,
    file_id: str,
    destination: Path,
) -> None:
    store.temp_root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="batch-download-",
        suffix=".tmp",
        dir=store.temp_root,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        provider.download_file(file_id, temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_text(store: RecipeRunStore, destination: Path, text: str) -> None:
    store.temp_root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="recipe-result-",
        suffix=".tmp",
        dir=store.temp_root,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _select_recipes(
    definitions: dict[str, RecipeDefinition],
    recipe_names: list[str],
) -> list[RecipeDefinition]:
    selected: list[RecipeDefinition] = []
    outputs: dict[str, str] = {}
    for name in recipe_names:
        try:
            recipe = definitions[name]
        except KeyError as error:
            raise ValueError(f"unknown recipe: {name}") from error
        collision = outputs.get(recipe.output.casefold())
        if collision is not None:
            raise ValueError(
                f"recipes {collision!r} and {recipe.name!r} declare the same "
                f"output filename: {recipe.output}"
            )
        outputs[recipe.output.casefold()] = recipe.name
        selected.append(recipe)
    if not selected:
        raise ValueError("choose at least one recipe")
    return selected


def _reject_duplicate_targets(
    runtime: LibraryRuntime,
    requested: set[str],
) -> None:
    for job in runtime.queue.list_jobs():
        if (
            job.library_key == runtime.library_key
            and job.kind is JobKind.RECIPE_BATCH
            and not job.state.is_terminal
        ):
            overlap = requested.intersection(job.meta.get("target_keys", "").split(","))
            if overlap:
                raise ValueError(f"recipe work is already in flight for {sorted(overlap)[0]}")
    store = RecipeRunStore(runtime.root)
    for run_id in store.list_run_ids():
        try:
            state = store.read_state(run_id)
            manifest = store.read_manifest(run_id)
        except (OSError, ValueError):
            continue
        if state.phase.is_terminal:
            continue
        active = {f"{item.citekey}/{item.recipe_name}" for item in manifest.invocations}
        overlap = requested.intersection(active)
        if overlap:
            raise ValueError(f"recipe work is already in flight for {sorted(overlap)[0]}")


def _remote_phase(status: str) -> RecipeRunPhase:
    return {
        "validating": RecipeRunPhase.VALIDATING,
        "in_progress": RecipeRunPhase.IN_PROGRESS,
        "finalizing": RecipeRunPhase.FINALIZING,
        "completed": RecipeRunPhase.COLLECTING,
        "failed": RecipeRunPhase.COLLECTING,
        "expired": RecipeRunPhase.COLLECTING,
        "cancelling": RecipeRunPhase.IN_PROGRESS,
        "cancelled": RecipeRunPhase.COLLECTING,
    }.get(status, RecipeRunPhase.BLOCKED)


def _publish_batch_progress(
    runtime: LibraryRuntime,
    job: Job,
    *,
    stage: str,
    detail: str,
    **facts: str | int | None,
) -> None:
    """Publish one live Batch update plus structured fields for the jobs UI."""
    stage_index, stage_label = _BATCH_PROGRESS_STAGES[stage]
    job.meta.update(
        {
            "progress_stage": stage,
            "progress_stage_index": str(stage_index),
            "progress_stage_count": str(_BATCH_PROGRESS_STAGE_COUNT),
            "progress_stage_label": stage_label,
            "progress_updated_at": _now().strftime("%H:%M:%S"),
        }
    )
    for name, value in facts.items():
        key = f"progress_{name}"
        if value is None:
            job.meta.pop(key, None)
        else:
            job.meta[key] = str(value)
    runtime.queue.publish_progress(job.id, detail)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
