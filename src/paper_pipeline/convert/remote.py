"""Optional Marker conversion on one SSH host behind the converter contract.

The default test suite supplies a fake :class:`RemoteTransport`; importing this
module never opens a connection or imports Marker. The module's ``worker`` CLI
is invoked only on a configured remote machine where the Marker extra is
installed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from paper_pipeline.config import AppConfig
from paper_pipeline.convert.contract import ConversionRequest, ConversionResult

_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.@-]*[A-Za-z0-9_@.-])?$")
_COMMAND = re.compile(r"^[A-Za-z0-9_./-]+$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST = "result.json"


class RemoteTransportError(RuntimeError):
    """A bounded SSH/SCP operation failed."""


class RemoteTransportTimeout(RemoteTransportError):
    """A transport operation exhausted the conversion deadline."""


@dataclass(frozen=True)
class CommandResult:
    """Minimal non-sensitive transport outcome."""

    returncode: int
    connection_lost: bool = False


class RemoteTransport(Protocol):
    """Injectable SSH edge used by the adapter and fake tests."""

    def prepare(self, host: str, run_dir: str, timeout: float) -> None: ...

    def upload(self, host: str, source: Path, remote_path: str, timeout: float) -> None: ...

    def execute(
        self,
        host: str,
        run_dir: str,
        remote_python: str,
        conversion_timeout: int,
        timeout: float,
    ) -> CommandResult: ...

    def download(self, host: str, remote_dir: str, local_dir: Path, timeout: float) -> None: ...

    def terminate(self, host: str, run_dir: str, timeout: float) -> None: ...

    def cleanup(self, host: str, run_dir: str, timeout: float) -> None: ...


class SubprocessSshTransport:
    """System OpenSSH transport with bounded subprocesses and no shell=True."""

    def prepare(self, host: str, run_dir: str, timeout: float) -> None:
        root = str(PurePosixPath(run_dir).parent)
        script = (
            "umask 077; "
            f"mkdir -p -- {shlex.quote(root)} && "
            f"test ! -L {shlex.quote(root)} && test -O {shlex.quote(root)} && "
            f"chmod 700 -- {shlex.quote(root)} && "
            f"mkdir -- {shlex.quote(run_dir)} && "
            f"mkdir -- {shlex.quote(run_dir + '/output')}"
        )
        self._require_success(["ssh", "-o", "BatchMode=yes", host, script], timeout)

    def upload(self, host: str, source: Path, remote_path: str, timeout: float) -> None:
        self._require_success(
            ["scp", "-o", "BatchMode=yes", str(source), f"{host}:{remote_path}"],
            timeout,
        )

    def execute(
        self,
        host: str,
        run_dir: str,
        remote_python: str,
        conversion_timeout: int,
        timeout: float,
    ) -> CommandResult:
        script = _remote_execution_script(run_dir, remote_python, conversion_timeout)
        result = self._run(
            ["ssh", "-tt", "-o", "BatchMode=yes", host, script],
            timeout,
        )
        return CommandResult(returncode=result, connection_lost=result == 255)

    def download(self, host: str, remote_dir: str, local_dir: Path, timeout: float) -> None:
        self._require_success(
            ["scp", "-r", "-o", "BatchMode=yes", f"{host}:{remote_dir}/.", str(local_dir)],
            timeout,
        )

    def terminate(self, host: str, run_dir: str, timeout: float) -> None:
        self._require_success(
            ["ssh", "-o", "BatchMode=yes", host, _remote_cleanup_script(run_dir)],
            timeout,
        )

    def cleanup(self, host: str, run_dir: str, timeout: float) -> None:
        script = f"rm -rf -- {shlex.quote(run_dir)}"
        self._require_success(["ssh", "-o", "BatchMode=yes", host, script], timeout)

    def _require_success(self, argv: list[str], timeout: float) -> None:
        returncode = self._run(argv, timeout)
        if returncode == 255:
            raise RemoteTransportError("remote SSH connection was lost")
        if returncode != 0:
            raise RemoteTransportError("SSH transport command failed")

    def _run(self, argv: list[str], timeout: float) -> int:
        if timeout <= 0:
            raise RemoteTransportTimeout("remote conversion timed out")
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _kill_local_process_tree(process)
            raise RemoteTransportTimeout("remote conversion timed out") from error


class RemoteConverter:
    """Copy, execute, and retrieve one canonical Marker conversion over SSH."""

    name = "remote-marker"

    def __init__(
        self,
        host: str,
        remote_root: str = "/tmp/paper-pipeline",
        remote_python: str = "python3",
        *,
        transport: RemoteTransport | None = None,
    ) -> None:
        _validate_settings(host, remote_root, remote_python)
        self.host = host
        self.remote_root = remote_root.rstrip("/")
        self.remote_python = remote_python
        self.transport = transport or SubprocessSshTransport()

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        transport: RemoteTransport | None = None,
    ) -> RemoteConverter:
        """Build the backend only from user-level application configuration."""
        if not config.remote_converter_host:
            raise ValueError("remote converter host is not configured")
        return cls(
            config.remote_converter_host,
            config.remote_converter_root,
            config.remote_converter_python,
            transport=transport,
        )

    def convert(self, request: ConversionRequest) -> ConversionResult:
        started = time.monotonic()
        if not request.pdf_path.is_file():
            return self._failure(started, "source PDF does not exist")
        if not request.staging_dir.is_dir():
            return self._failure(started, "conversion staging directory does not exist")
        if request.timeout_seconds <= 0:
            return self._failure(started, "remote conversion timeout must be positive")

        run_id = uuid4().hex
        assert _RUN_ID.fullmatch(run_id)
        run_dir = f"{self.remote_root}/{run_id}"
        remote_input = f"{run_dir}/input.pdf"
        remote_output = f"{run_dir}/output"
        deadline = started + request.timeout_seconds
        prepared = True
        execution_completed = False
        transport_timings: dict[str, str] = {}
        phase_started = started
        try:
            self.transport.prepare(self.host, run_dir, _remaining(deadline))
            phase_finished = time.monotonic()
            transport_timings["timing_ssh_prepare_seconds"] = _duration(
                phase_started, phase_finished
            )
            phase_started = phase_finished
            self.transport.upload(
                self.host,
                request.pdf_path,
                remote_input,
                _remaining(deadline),
            )
            phase_finished = time.monotonic()
            transport_timings["timing_ssh_upload_seconds"] = _duration(
                phase_started, phase_finished
            )
            phase_started = phase_finished
            execution = self.transport.execute(
                self.host,
                run_dir,
                self.remote_python,
                request.timeout_seconds,
                _remaining(deadline),
            )
            phase_finished = time.monotonic()
            transport_timings["timing_remote_worker_seconds"] = _duration(
                phase_started, phase_finished
            )
            phase_started = phase_finished
            execution_completed = not execution.connection_lost
            if execution.connection_lost:
                raise RemoteTransportError("remote SSH connection was lost")
            if execution.returncode != 0:
                raise RemoteTransportError("remote conversion process failed")

            with tempfile.TemporaryDirectory(prefix="paper-pipeline-remote-result-") as temp:
                download = Path(temp)
                self.transport.download(
                    self.host,
                    remote_output,
                    download,
                    _remaining(deadline),
                )
                phase_finished = time.monotonic()
                transport_timings["timing_ssh_download_seconds"] = _duration(
                    phase_started, phase_finished
                )
                return _install_download(
                    download,
                    request.staging_dir,
                    started,
                    transport_timings,
                )
        except RemoteTransportTimeout:
            return self._failure(started, "remote conversion timed out")
        except RemoteTransportError as error:
            return self._failure(started, str(error))
        except OSError:
            return self._failure(started, "SSH transport is unavailable")
        except (ValueError, json.JSONDecodeError):
            return self._failure(started, "remote conversion returned invalid artifacts")
        finally:
            if prepared:
                # Cleanup shares the request deadline. A transport timeout closes
                # the forced PTY and triggers the remote trap even when no time
                # remains for the secondary pidfile command.
                if not execution_completed:
                    cleanup_timeout = _bounded_cleanup_timeout(deadline)
                    if cleanup_timeout is not None:
                        with contextlib.suppress(Exception):
                            self.transport.terminate(self.host, run_dir, cleanup_timeout)
                cleanup_timeout = _bounded_cleanup_timeout(deadline)
                if cleanup_timeout is not None:
                    with contextlib.suppress(Exception):
                        self.transport.cleanup(self.host, run_dir, cleanup_timeout)

    def _failure(self, started: float, error: str) -> ConversionResult:
        return ConversionResult(
            ok=False,
            backend=self.name,
            backend_version="unknown",
            duration_seconds=time.monotonic() - started,
            error=error,
        )


def _install_download(
    download: Path,
    staging: Path,
    started: float,
    transport_timings: dict[str, str] | None = None,
) -> ConversionResult:
    if any(path.is_symlink() for path in download.rglob("*")):
        raise ValueError("download contains symlinks")
    allowed = {_MANIFEST, "transcription.md", "figures", "pages"}
    if any(path.name not in allowed for path in download.iterdir()):
        raise ValueError("download contains unexpected entries")
    manifest_path = download / _MANIFEST
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RemoteTransportError("remote Marker conversion failed")
    backend_version = payload.get("backend_version")
    if not isinstance(backend_version, str) or not backend_version:
        raise ValueError("manifest has no backend version")
    diagnostics = payload.get("diagnostics", {})
    if not isinstance(diagnostics, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in diagnostics.items()
    ):
        raise ValueError("manifest has invalid diagnostics")
    transcription = download / "transcription.md"
    if not transcription.is_file() or transcription.stat().st_size == 0:
        raise ValueError("download has no transcription")
    figures = download / "figures"
    if figures.exists():
        if not figures.is_dir():
            raise ValueError("figures is not a directory")
        if any(not path.is_file() and not path.is_dir() for path in figures.rglob("*")):
            raise ValueError("figures contains an unsupported entry")
    pages = download / "pages"
    if pages.exists():
        if not pages.is_dir():
            raise ValueError("pages is not a directory")
        page_files = sorted(path for path in pages.rglob("*") if path.is_file())
        if not page_files or any(path.suffix.casefold() != ".png" for path in page_files):
            raise ValueError("pages contains invalid page images")

    destination = staging / "transcription.md"
    shutil.copy2(transcription, destination)
    figure_paths: list[Path] = []
    if figures.exists():
        destination_figures = staging / "figures"
        shutil.copytree(figures, destination_figures)
        figure_paths = sorted(path for path in destination_figures.rglob("*") if path.is_file())
    page_paths: list[Path] = []
    if pages.exists():
        destination_pages = staging / "pages"
        shutil.copytree(pages, destination_pages)
        page_paths = sorted(path for path in destination_pages.rglob("*") if path.is_file())
    diagnostics.update(transport_timings or {})
    return ConversionResult(
        ok=True,
        backend="remote-marker",
        backend_version=backend_version,
        duration_seconds=time.monotonic() - started,
        transcription_path=destination,
        figure_paths=figure_paths,
        page_paths=page_paths,
        diagnostics=diagnostics,
    )


def _remote_execution_script(run_dir: str, remote_python: str, timeout: int) -> str:
    pidfile = f"{run_dir}/worker.pid"
    worker = " ".join(
        (
            shlex.quote(remote_python),
            "-m paper_pipeline.convert.remote worker",
            f"--input {shlex.quote(run_dir + '/input.pdf')}",
            f"--output {shlex.quote(run_dir + '/output')}",
            f"--timeout {timeout}",
        )
    )
    quoted_pidfile = shlex.quote(pidfile)
    quoted_run = shlex.quote(run_dir)
    return (
        "set +e; "
        "terminate_worker() { target=${pid:-}; "
        f'if [ -z "$target" ] && [ -f {quoted_pidfile} ]; then '
        f"target=$(cat {quoted_pidfile}); fi; "
        "case $target in (*[!0-9]*|'') return;; esac; "
        'kill -TERM -- "-$target" 2>/dev/null || true; '
        'sleep 1; kill -KILL -- "-$target" 2>/dev/null || true; }; '
        f"trap 'terminate_worker; rm -rf -- {quoted_run}; exit 143' HUP INT TERM; "
        f"setsid {worker} & pid=$!; printf '%s\\n' \"$pid\" > {quoted_pidfile}; "
        'wait "$pid"; status=$?; '
        f"rm -f -- {quoted_pidfile}; trap - HUP INT TERM; exit $status"
    )


def _remote_cleanup_script(run_dir: str) -> str:
    pidfile = shlex.quote(f"{run_dir}/worker.pid")
    quoted_run = shlex.quote(run_dir)
    return (
        f"if [ -f {pidfile} ]; then pid=$(cat {pidfile}); "
        "case $pid in (*[!0-9]*|'') exit 2;; esac; "
        'kill -TERM -- "-$pid" 2>/dev/null || true; '
        'sleep 1; kill -KILL -- "-$pid" 2>/dev/null || true; fi; '
        f"rm -rf -- {quoted_run}"
    )


def _validate_settings(host: str, remote_root: str, remote_python: str) -> None:
    if not _HOST.fullmatch(host) or host.startswith("-"):
        raise ValueError("remote converter host must be a safe SSH host or alias")
    root = PurePosixPath(remote_root)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or str(root) != remote_root.rstrip("/")
        or remote_root == "/"
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in root.parts[1:])
    ):
        raise ValueError("remote converter root must be a safe absolute POSIX path")
    if not _COMMAND.fullmatch(remote_python) or remote_python.startswith("-"):
        raise ValueError("remote converter Python must be one executable path without arguments")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RemoteTransportTimeout("remote conversion timed out")
    return remaining


def _bounded_cleanup_timeout(deadline: float) -> float | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(5.0, remaining)


def _kill_local_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        # Keep SSH in the conversion child's process group so runner-level
        # cancellation kills both. For this transport's own timeout, killing
        # the SSH client is sufficient to close the forced PTY and trigger the
        # remote HUP trap.
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


def _write_remote_manifest(input_path: Path, output_dir: Path, timeout: int) -> int:
    from paper_pipeline.convert.marker import MarkerConverter

    output_dir.mkdir(parents=True, exist_ok=True)
    result = MarkerConverter().convert(
        ConversionRequest(input_path, output_dir, timeout_seconds=timeout)
    )
    payload = {
        "ok": result.ok,
        "backend_version": result.backend_version,
        "diagnostics": {
            name: value
            for name, value in result.diagnostics.items()
            if name == "page_count" or name.startswith("timing_")
        },
    }
    (output_dir / _MANIFEST).write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


def _duration(started: float, finished: float) -> str:
    return f"{finished - started:.3f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", choices=["worker"])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args(argv)
    return _write_remote_manifest(args.input, args.output, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
