"""Run each page-render attempt in an isolated spawned child process."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from paper_pipeline._isolated_process import (
    CancellationSignal,
    ImportSpec,
    IsolatedProcessOutcome,
    ProcessFailureKind,
    clean_staging_directory,
    is_path_inside_without_symlinks,
    run_in_fresh_process,
)
from paper_pipeline.pages.contract import PageRenderRequest, PageRenderResult


@dataclass(frozen=True)
class PageRendererSpec:
    """Importable page-renderer class and JSON-like constructor arguments."""

    module_path: str
    kwargs: dict[str, Any] = field(default_factory=dict)


def run_page_render(
    renderer_spec: PageRendererSpec,
    request: PageRenderRequest,
    *,
    cancel_event: CancellationSignal | None = None,
) -> PageRenderResult:
    """Run one local page render in a fresh process and validate its output."""
    started = time.monotonic()
    try:
        outcome = run_in_fresh_process(
            ImportSpec(renderer_spec.module_path, renderer_spec.kwargs),
            request,
            method_name="render",
            timeout_seconds=request.timeout_seconds,
            cancel_event=cancel_event,
            process_name="paper-pipeline-page-renderer",
            diagnostics_prefix="paper-pipeline-pages-",
        )
    except BaseException:
        clean_staging_directory(request.staging_dir)
        raise

    if outcome.failure is not None:
        clean_staging_directory(request.staging_dir)
        return _failure_result(
            renderer_spec,
            started,
            _failure_message(outcome, request.timeout_seconds),
            outcome.diagnostics,
        )
    if outcome.result is None:
        clean_staging_directory(request.staging_dir)
        return _failure_result(
            renderer_spec,
            started,
            "page-render child returned an invalid response",
            outcome.diagnostics,
        )

    child_result = cast(PageRenderResult, outcome.result)
    result = replace(
        child_result,
        diagnostics={**child_result.diagnostics, **outcome.diagnostics},
    )
    validation_error = _validate_result(result, request.staging_dir)
    if not result.ok or validation_error is not None:
        clean_staging_directory(request.staging_dir)
        if validation_error is not None:
            return replace(result, ok=False, error=validation_error)
    return result


def _failure_message(outcome: IsolatedProcessOutcome, timeout_seconds: int) -> str:
    failure = outcome.failure
    if failure is None:
        raise AssertionError("failure message requested for a successful process")
    match failure.kind:
        case ProcessFailureKind.CANCELLED:
            return "page rendering cancelled"
        case ProcessFailureKind.TIMED_OUT:
            return f"page rendering timed out after {timeout_seconds} seconds"
        case ProcessFailureKind.DID_NOT_EXIT:
            return "page-render process did not exit after returning a result"
        case ProcessFailureKind.NO_RESULT:
            return f"page-render process exited without a result (exit code {outcome.exit_code})"
        case ProcessFailureKind.CHILD_EXCEPTION:
            return f"page renderer raised an exception: {failure.detail}"


def _validate_result(result: PageRenderResult, staging_dir: Path) -> str | None:
    if not result.ok:
        return None
    if not result.page_paths:
        return "page renderer reported success without page images"
    pages_dir = staging_dir / "pages"
    if pages_dir.is_symlink():
        return "page-render staging pages directory must not be a symlink"
    expected_names = {f"page{index}.png" for index in range(1, len(result.page_paths) + 1)}
    if {path.name for path in result.page_paths} != expected_names:
        return "page renderer must return one contiguous pageN.png sequence"
    actual_files = {path.absolute() for path in pages_dir.rglob("*") if path.is_file()}
    if actual_files != {path.absolute() for path in result.page_paths}:
        return "page renderer result must declare every staged page image"
    for page_path in result.page_paths:
        if not is_path_inside_without_symlinks(page_path, pages_dir):
            return "page renderer must return paths inside the staging pages directory"
        if page_path.parent.absolute() != pages_dir.absolute():
            return "page renderer must return flat page images directly inside pages"
        if page_path.suffix.casefold() != ".png":
            return f"page renderer returned a non-PNG image: {page_path.name}"
        if not page_path.is_file() or page_path.stat().st_size == 0:
            return f"page renderer reported a missing or empty image: {page_path.name}"
    return None


def _failure_result(
    renderer_spec: PageRendererSpec,
    started: float,
    error: str,
    diagnostics: dict[str, str],
) -> PageRenderResult:
    return PageRenderResult(
        ok=False,
        renderer=renderer_spec.module_path,
        renderer_version="unknown",
        duration_seconds=time.monotonic() - started,
        error=error,
        diagnostics=diagnostics,
    )
