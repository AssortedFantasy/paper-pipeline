"""Shared mechanics for running one backend attempt in a fresh process.

This module deliberately knows nothing about converters or page renderers. The
backend is imported by :func:`_child_entry` only, so importing the application
never loads optional backend dependencies.
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
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ImportSpec:
    """Importable class and JSON-like constructor arguments."""

    module_path: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class CancellationSignal(Protocol):
    """Minimal cancellation interface accepted from the job system."""

    def is_set(self) -> bool:
        """Return whether cancellation has been requested."""
        ...


class ProcessFailureKind(StrEnum):
    """Ways an isolated attempt can fail before backend validation."""

    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    DID_NOT_EXIT = "did_not_exit"
    NO_RESULT = "no_result"
    CHILD_EXCEPTION = "child_exception"


@dataclass(frozen=True)
class ProcessFailure:
    kind: ProcessFailureKind
    detail: str | None = None


@dataclass(frozen=True)
class IsolatedProcessOutcome:
    result: Any | None
    failure: ProcessFailure | None
    exit_code: int | None
    diagnostics: dict[str, str]


@dataclass(frozen=True)
class _ChildMessage:
    result: Any | None = None
    error: str | None = None


def run_in_fresh_process(
    spec: ImportSpec,
    request: Any,
    *,
    method_name: str,
    timeout_seconds: int,
    cancel_event: CancellationSignal | None,
    process_name: str,
    diagnostics_prefix: str,
) -> IsolatedProcessOutcome:
    """Invoke one backend method in a spawned process and capture diagnostics."""
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)

    with tempfile.TemporaryDirectory(prefix=diagnostics_prefix) as diagnostics_dir:
        stdout_path = Path(diagnostics_dir) / "stdout.txt"
        stderr_path = Path(diagnostics_dir) / "stderr.txt"
        process = context.Process(
            target=_child_entry,
            args=(spec, request, method_name, send_connection, stdout_path, stderr_path),
            name=process_name,
        )
        try:
            process.start()
            send_connection.close()
            message, failure = _wait_for_child(
                process,
                receive_connection,
                timeout_seconds,
                cancel_event,
            )
        except BaseException:
            if process.pid is not None and process.is_alive():
                _terminate_process_tree(process)
            raise
        finally:
            send_connection.close()
            receive_connection.close()

        diagnostics = {
            "stdout": _read_diagnostic(stdout_path),
            "stderr": _read_diagnostic(stderr_path),
        }

    if failure is None and message is None:
        failure = ProcessFailure(ProcessFailureKind.NO_RESULT)
    elif failure is None and message is not None and message.error is not None:
        failure = ProcessFailure(ProcessFailureKind.CHILD_EXCEPTION, message.error)

    return IsolatedProcessOutcome(
        result=message.result if message is not None else None,
        failure=failure,
        exit_code=process.exitcode,
        diagnostics=diagnostics,
    )


def clean_staging_directory(staging_dir: Path) -> None:
    """Remove the contents of a failed attempt's staging directory."""
    if not staging_dir.exists():
        return
    for child in staging_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def is_path_inside_without_symlinks(path: Path, directory: Path) -> bool:
    """Return whether path is below directory without traversing a symlink."""
    try:
        relative = path.absolute().relative_to(directory.absolute())
    except ValueError:
        return False
    current = directory
    if current.is_symlink():
        return False
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return False
    return True


def _child_entry(
    spec: ImportSpec,
    request: Any,
    method_name: str,
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
            backend = _load_backend(spec)
            result = getattr(backend, method_name)(request)
            connection.send(_ChildMessage(result=result))
        except BaseException as exc:
            traceback.print_exc()
            with contextlib.suppress(Exception):
                connection.send(_ChildMessage(error=f"{type(exc).__name__}: {exc}"))
        finally:
            connection.close()


def _load_backend(spec: ImportSpec) -> Any:
    module_name, separator, class_name = spec.module_path.partition(":")
    if not separator:
        module_name, separator, class_name = spec.module_path.rpartition(".")
    if not module_name or not class_name:
        raise ValueError(
            "backend module_path must be 'package.module:ClassName' or 'package.module.ClassName'"
        )

    module = importlib.import_module(module_name)
    backend_class = getattr(module, class_name)
    return backend_class(**spec.kwargs)


def _wait_for_child(
    process: Any,
    connection: Any,
    timeout_seconds: int,
    cancel_event: CancellationSignal | None,
) -> tuple[_ChildMessage | None, ProcessFailure | None]:
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        if connection.poll(0.02):
            message = _receive_message(connection)
            process.join(timeout=1)
            if process.is_alive():
                _terminate_process_tree(process)
                return None, ProcessFailure(ProcessFailureKind.DID_NOT_EXIT)
            return message, None
        if not process.is_alive():
            process.join()
            if connection.poll():
                return _receive_message(connection), None
            return None, None
        if cancel_event is not None and cancel_event.is_set():
            _terminate_process_tree(process)
            return None, ProcessFailure(ProcessFailureKind.CANCELLED)
        if time.monotonic() >= deadline:
            _terminate_process_tree(process)
            return None, ProcessFailure(ProcessFailureKind.TIMED_OUT)


def _receive_message(connection: Any) -> _ChildMessage | None:
    try:
        message = connection.recv()
    except EOFError:
        return None
    return message if isinstance(message, _ChildMessage) else None


def _terminate_process_tree(process: Any) -> None:
    if process.pid is None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            text=True,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def _read_diagnostic(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
