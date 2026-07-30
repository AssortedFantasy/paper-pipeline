"""Run each page-render attempt in an isolated spawned child process."""

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

from paper_pipeline.pages.contract import PageRenderer, PageRenderRequest, PageRenderResult


@dataclass(frozen=True)
class PageRendererSpec:
    """Importable page-renderer class and JSON-like constructor arguments."""

    module_path: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class _ChildMessage:
    result: PageRenderResult | None = None
    error: str | None = None


def run_page_render(
    renderer_spec: PageRendererSpec,
    request: PageRenderRequest,
    *,
    cancel_event: CancellationSignal | None = None,
) -> PageRenderResult:
    """Run one local page render in a fresh process and validate its output."""
    started = time.monotonic()
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)

    with tempfile.TemporaryDirectory(prefix="paper-pipeline-pages-") as diagnostics_dir:
        stdout_path = Path(diagnostics_dir) / "stdout.txt"
        stderr_path = Path(diagnostics_dir) / "stderr.txt"
        process = context.Process(
            target=_child_entry,
            args=(renderer_spec, request, send_connection, stdout_path, stderr_path),
            name="paper-pipeline-page-renderer",
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
        return _failure_result(renderer_spec, started, stopped_reason, diagnostics)
    if message is None:
        _clean_staging(request.staging_dir)
        return _failure_result(
            renderer_spec,
            started,
            f"page-render process exited without a result (exit code {process.exitcode})",
            diagnostics,
        )
    if message.error is not None:
        _clean_staging(request.staging_dir)
        return _failure_result(
            renderer_spec,
            started,
            f"page renderer raised an exception: {message.error}",
            diagnostics,
        )
    if message.result is None:
        _clean_staging(request.staging_dir)
        return _failure_result(
            renderer_spec,
            started,
            "page-render child returned an invalid response",
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
    renderer_spec: PageRendererSpec,
    request: PageRenderRequest,
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
            renderer = _load_renderer(renderer_spec)
            connection.send(_ChildMessage(result=renderer.render(request)))
        except BaseException as exc:
            traceback.print_exc()
            with contextlib.suppress(Exception):
                connection.send(_ChildMessage(error=f"{type(exc).__name__}: {exc}"))
        finally:
            connection.close()


def _load_renderer(renderer_spec: PageRendererSpec) -> PageRenderer:
    module_name, separator, class_name = renderer_spec.module_path.partition(":")
    if not separator:
        module_name, separator, class_name = renderer_spec.module_path.rpartition(".")
    if not module_name or not class_name:
        raise ValueError(
            "page renderer module_path must be "
            "'package.module:ClassName' or 'package.module.ClassName'"
        )
    module = importlib.import_module(module_name)
    renderer_class = getattr(module, class_name)
    return renderer_class(**renderer_spec.kwargs)


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
                return None, "page-render process did not exit after returning a result"
            return message, None
        if not process.is_alive():
            process.join()
            if connection.poll():
                return _receive_message(connection), None
            return None, None
        if cancel_event is not None and cancel_event.is_set():
            _terminate_process_tree(process)
            return None, "page rendering cancelled"
        if time.monotonic() >= deadline:
            _terminate_process_tree(process)
            return None, f"page rendering timed out after {timeout_seconds} seconds"


def _receive_message(connection: Any) -> _ChildMessage | None:
    try:
        return connection.recv()
    except EOFError:
        return None


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
        if not _is_inside_without_symlinks(page_path, pages_dir):
            return "page renderer must return paths inside the staging pages directory"
        if page_path.parent.absolute() != pages_dir.absolute():
            return "page renderer must return flat page images directly inside pages"
        if page_path.suffix.casefold() != ".png":
            return f"page renderer returned a non-PNG image: {page_path.name}"
        if not page_path.is_file() or page_path.stat().st_size == 0:
            return f"page renderer reported a missing or empty image: {page_path.name}"
    return None


def _is_inside_without_symlinks(path: Path, directory: Path) -> bool:
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


def _clean_staging(staging_dir: Path) -> None:
    if not staging_dir.exists():
        return
    for child in staging_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


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


def _read_diagnostic(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
