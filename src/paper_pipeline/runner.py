from __future__ import annotations

import contextlib
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pypdf


@dataclass
class SubprocessResult:
    """Normalized subprocess outcome for long-running Nougat jobs.

    The GUI needs cancellation, the CLI needs timeout visibility, and both need
    a single result type that does not overload `None` with multiple meanings.
    """

    returncode: int | None
    timed_out: bool = False
    cancelled: bool = False


def get_pdf_page_count(pdf_path: Path) -> int | None:
    try:
        return len(pypdf.PdfReader(str(pdf_path)).pages)
    except Exception as exc:
        print(f"warning: failed to count pages for {pdf_path}: {exc}")
        return None


def default_nougat_command(workspace_root: Path) -> str:
    # Derive Scripts/bin dir from the running Python interpreter first
    python_dir = Path(sys.executable).parent
    candidates = [
        python_dir / "nougat.exe",  # Windows venv
        python_dir / "nougat",  # Unix venv
        workspace_root / ".venv" / "Scripts" / "nougat.exe",
        workspace_root / ".venv" / "bin" / "nougat",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    # Last resort: check PATH
    found = shutil.which("nougat")
    if found:
        return found
    return "nougat"


def build_nougat_command(
    nougat_cmd: str,
    pdf_path: Path,
    output_dir: Path,
    *,
    model: str | None = None,
    batchsize: int | None = None,
    no_skipping: bool = False,
    recompute: bool = False,
    pages: str | None = None,
) -> list[str]:
    command = [
        nougat_cmd,
        str(pdf_path),
        "-o",
        str(output_dir),
        "--markdown",
    ]
    if model:
        command.extend(["-m", model])
    if batchsize:
        command.extend(["-b", str(batchsize)])
    if no_skipping:
        command.append("--no-skipping")
    if recompute:
        command.append("--recompute")
    if pages:
        command.extend(["--pages", pages])
    return command


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)


def _stream_output(stream, output_queue: queue.Queue[str | None]) -> None:
    try:
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            output_queue.put(line)
    finally:
        output_queue.put(None)


def run_nougat_subprocess(
    command: list[str],
    log_path: Path,
    timeout_seconds: int | None,
    on_output: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SubprocessResult:
    env = os.environ.copy()
    env.setdefault("NO_ALBUMENTATIONS_UPDATE", "1")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    start_new_session = os.name != "nt"

    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        output_queue: queue.Queue[str | None] = queue.Queue()
        reader_thread = threading.Thread(
            target=_stream_output,
            args=(process.stdout, output_queue),
            daemon=True,
        )
        reader_thread.start()

        deadline = None
        if timeout_seconds is not None:
            deadline = time.monotonic() + timeout_seconds

        stream_closed = False
        while True:
            while True:
                try:
                    item = output_queue.get_nowait()
                except queue.Empty:
                    break

                if item is None:
                    stream_closed = True
                    continue

                log_file.write(item)
                log_file.flush()
                if on_output:
                    on_output(item.rstrip())

            if should_cancel and should_cancel():
                log_file.write("\n[paper-pipeline] cancelled by user\n")
                log_file.flush()
                _terminate_process_tree(process)
                process.wait(timeout=10)
                reader_thread.join(timeout=1)
                return SubprocessResult(
                    returncode=process.returncode,
                    cancelled=True,
                )

            if deadline is not None and time.monotonic() >= deadline:
                log_file.write(
                    f"\n[paper-pipeline] timed out after {timeout_seconds} seconds\n"
                )
                log_file.flush()
                _terminate_process_tree(process)
                process.wait(timeout=10)
                reader_thread.join(timeout=1)
                return SubprocessResult(
                    returncode=process.returncode,
                    timed_out=True,
                )

            if process.poll() is not None and stream_closed:
                break

            time.sleep(0.1)

        reader_thread.join(timeout=1)

        while True:
            try:
                item = output_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            log_file.write(item)
            if on_output:
                on_output(item.rstrip())

        log_file.flush()
    return SubprocessResult(returncode=process.returncode)


def combine_markdown_chunks(markdown_chunks: list[str]) -> str:
    cleaned = [chunk.strip() for chunk in markdown_chunks if chunk.strip()]
    if not cleaned:
        return ""
    return "\n\n".join(cleaned).rstrip() + "\n"
