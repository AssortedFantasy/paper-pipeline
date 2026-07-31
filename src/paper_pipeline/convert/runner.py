"""Run each converter attempt in an isolated spawned child process.

Backend modules are imported only by the shared child-process entry point,
keeping optional GPU dependencies out of the application process.
"""

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
from paper_pipeline.convert.contract import ConversionRequest, ConversionResult


@dataclass(frozen=True)
class ConverterSpec:
    """Importable converter class and JSON-like constructor arguments.

    ``module_path`` accepts either ``"package.module:ClassName"`` or the
    equivalent ``"package.module.ClassName"`` form.
    """

    module_path: str
    kwargs: dict[str, Any] = field(default_factory=dict)


def run_conversion(
    converter_spec: ConverterSpec,
    request: ConversionRequest,
    *,
    cancel_event: CancellationSignal | None = None,
) -> ConversionResult:
    """Run one conversion in a fresh process and validate its staged outputs."""
    started = time.monotonic()
    try:
        outcome = run_in_fresh_process(
            ImportSpec(converter_spec.module_path, converter_spec.kwargs),
            request,
            method_name="convert",
            timeout_seconds=request.timeout_seconds,
            cancel_event=cancel_event,
            process_name="paper-pipeline-converter",
            diagnostics_prefix="paper-pipeline-convert-",
        )
    except BaseException:
        clean_staging_directory(request.staging_dir)
        raise

    if outcome.failure is not None:
        clean_staging_directory(request.staging_dir)
        return _failure_result(
            converter_spec,
            started,
            _failure_message(outcome, request.timeout_seconds),
            outcome.diagnostics,
        )

    if outcome.result is None:
        clean_staging_directory(request.staging_dir)
        return _failure_result(
            converter_spec,
            started,
            "converter child returned an invalid response",
            outcome.diagnostics,
        )

    child_result = cast(ConversionResult, outcome.result)
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
            return "conversion cancelled"
        case ProcessFailureKind.TIMED_OUT:
            return f"conversion timed out after {timeout_seconds} seconds"
        case ProcessFailureKind.DID_NOT_EXIT:
            return "converter process did not exit after returning a result"
        case ProcessFailureKind.NO_RESULT:
            return f"converter process exited without a result (exit code {outcome.exit_code})"
        case ProcessFailureKind.CHILD_EXCEPTION:
            return f"converter raised an exception: {failure.detail}"


def _validate_result(result: ConversionResult, staging_dir: Path) -> str | None:
    if not result.ok:
        return None
    if result.transcription_path is None:
        return "converter reported success without a transcription path"
    expected_transcription = (staging_dir / "transcription.md").absolute()
    if (
        result.transcription_path.absolute() != expected_transcription
        or result.transcription_path.is_symlink()
    ):
        return "converter must return the canonical staging transcription.md path"
    if not result.transcription_path.is_file() or result.transcription_path.stat().st_size == 0:
        return "converter reported success without a non-empty transcription"
    figures_dir = staging_dir / "figures"
    if figures_dir.is_symlink():
        return "converter staging figures directory must not be a symlink"
    for figure_path in result.figure_paths:
        if not is_path_inside_without_symlinks(figure_path, figures_dir):
            return "converter must return figure paths inside the staging figures directory"
        if not figure_path.is_file():
            return f"converter reported a missing figure: {figure_path.name}"
    return None


def _failure_result(
    converter_spec: ConverterSpec,
    started: float,
    error: str,
    diagnostics: dict[str, str],
) -> ConversionResult:
    return ConversionResult(
        ok=False,
        backend=converter_spec.module_path,
        backend_version="unknown",
        duration_seconds=time.monotonic() - started,
        error=error,
        diagnostics=diagnostics,
    )
