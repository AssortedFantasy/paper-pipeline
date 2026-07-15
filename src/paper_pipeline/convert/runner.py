"""Run each converter attempt in an isolated spawned child process.

Backend modules are imported only by :func:`_child_entry`, keeping optional
GPU dependencies out of the application process.
"""

from __future__ import annotations

import contextlib
import importlib
import multiprocessing
import os
import shutil
import signal
import subprocess
import tempfile
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from paper_pipeline.convert.contract import ConversionRequest, ConversionResult, Converter


@dataclass(frozen=True)
class ConverterSpec:
    """Importable converter class and JSON-like constructor arguments.

    ``module_path`` accepts either ``"package.module:ClassName"`` or the
    equivalent ``"package.module.ClassName"`` form.
    """

    module_path: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class CancellationSignal(Protocol):
    """Minimal interface accepted from the job system's cancellation token."""

    def is_set(self) -> bool:
        """Return whether cancellation has been requested."""
        ...


@dataclass(frozen=True)
class _ChildMessage:
    result: ConversionResult | None = None
    error: str | None = None


def run_conversion(
    converter_spec: ConverterSpec,
    request: ConversionRequest,
    *,
    cancel_event: CancellationSignal | None = None,
) -> ConversionResult:
    """Run one conversion in a fresh process and validate its staged outputs."""
    started = time.monotonic()
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)

    with tempfile.TemporaryDirectory(prefix="paper-pipeline-convert-") as diagnostics_dir:
        stdout_path = Path(diagnostics_dir) / "stdout.txt"
        stderr_path = Path(diagnostics_dir) / "stderr.txt"
        process = context.Process(
            target=_child_entry,
            args=(converter_spec, request, send_connection, stdout_path, stderr_path),
            name="paper-pipeline-converter",
        )
        try:
            process.start()
            send_connection.close()
            message, stopped_reason = _wait_for_child(
                process,
                receive_connection,
                request.timeout_seconds,
                cancel_event,
            )
        except BaseException:
            if process.pid is not None and process.is_alive():
                _terminate_process_tree(process)
            _clean_staging(request.staging_dir)
            raise
        finally:
            send_connection.close()
            receive_connection.close()

        diagnostics = {
            "stdout": _read_diagnostic(stdout_path),
            "stderr": _read_diagnostic(stderr_path),
        }

    if stopped_reason is not None:
        _clean_staging(request.staging_dir)
        return _failure_result(
            converter_spec,
            started,
            stopped_reason,
            diagnostics,
        )

    if message is None:
        _clean_staging(request.staging_dir)
        exit_code = process.exitcode
        return _failure_result(
            converter_spec,
            started,
            f"converter process exited without a result (exit code {exit_code})",
            diagnostics,
        )

    if message.error is not None:
        _clean_staging(request.staging_dir)
        return _failure_result(
            converter_spec,
            started,
            f"converter raised an exception: {message.error}",
            diagnostics,
        )

    if message.result is None:
        _clean_staging(request.staging_dir)
        return _failure_result(
            converter_spec,
            started,
            "converter child returned an invalid response",
            diagnostics,
        )

    result = replace(
        message.result,
        diagnostics={**message.result.diagnostics, **diagnostics},
    )
    validation_error = _validate_result(result, request.staging_dir)
    if not result.ok or validation_error is not None:
        _clean_staging(request.staging_dir)
        if validation_error is not None:
            return replace(result, ok=False, error=validation_error)
    return result


def _child_entry(
    converter_spec: ConverterSpec,
    request: ConversionRequest,
    connection: Any,
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    if os.name != "nt":
        os.setsid()

    with (
        stdout_path.open("w", encoding="utf-8", buffering=1) as stdout,
        stderr_path.open("w", encoding="utf-8", buffering=1) as stderr,
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        try:
            converter = _load_converter(converter_spec)
            result = converter.convert(request)
            connection.send(_ChildMessage(result=result))
        except BaseException as exc:
            traceback.print_exc()
            with contextlib.suppress(Exception):
                connection.send(_ChildMessage(error=f"{type(exc).__name__}: {exc}"))
        finally:
            connection.close()


def _load_converter(converter_spec: ConverterSpec) -> Converter:
    module_name, separator, class_name = converter_spec.module_path.partition(":")
    if not separator:
        module_name, separator, class_name = converter_spec.module_path.rpartition(".")
    if not module_name or not class_name:
        raise ValueError(
            "converter module_path must be 'package.module:ClassName' or 'package.module.ClassName'"
        )

    module = importlib.import_module(module_name)
    converter_class = getattr(module, class_name)
    return converter_class(**converter_spec.kwargs)


def _wait_for_child(
    process: Any,
    connection: Any,
    timeout_seconds: int,
    cancel_event: CancellationSignal | None,
) -> tuple[_ChildMessage | None, str | None]:
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        if connection.poll(0.02):
            message = _receive_message(connection)
            process.join(timeout=1)
            if process.is_alive():
                _terminate_process_tree(process)
                return None, "converter process did not exit after returning a result"
            return message, None
        if not process.is_alive():
            process.join()
            if connection.poll():
                return _receive_message(connection), None
            return None, None
        if cancel_event is not None and cancel_event.is_set():
            _terminate_process_tree(process)
            return None, "conversion cancelled"
        if time.monotonic() >= deadline:
            _terminate_process_tree(process)
            return None, f"conversion timed out after {timeout_seconds} seconds"


def _receive_message(connection: Any) -> _ChildMessage | None:
    try:
        return connection.recv()
    except EOFError:
        return None


def _terminate_process_tree(process: Any) -> None:
    if process.pid is None:
        return

    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=creation_flags,
            text=True,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def _validate_result(result: ConversionResult, staging_dir: Path) -> str | None:
    if not result.ok:
        return None
    if result.transcription_path is None:
        return "converter reported success without a transcription path"
    if not _is_inside(result.transcription_path, staging_dir):
        return "converter returned a transcription path outside the staging directory"
    if not result.transcription_path.is_file() or result.transcription_path.stat().st_size == 0:
        return "converter reported success without a non-empty transcription"
    for figure_path in result.figure_paths:
        if not _is_inside(figure_path, staging_dir):
            return "converter returned a figure path outside the staging directory"
        if not figure_path.is_file():
            return f"converter reported a missing figure: {figure_path.name}"
    return None


def _is_inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _clean_staging(staging_dir: Path) -> None:
    if not staging_dir.exists():
        return
    for child in staging_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


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


def _read_diagnostic(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
