import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from paper_pipeline.convert.contract import ConversionRequest, ConversionResult
from paper_pipeline.convert.runner import ConverterSpec, run_conversion
from tests.fakes import FakeConverter

FAKE_SPEC = "tests.fakes:FakeConverter"


class PrintingConverter(FakeConverter):
    def convert(self, request: ConversionRequest) -> ConversionResult:
        print("fake stdout", flush=True)
        print("fake stderr", file=sys.stderr, flush=True)
        return super().convert(request)


class ProcessIdentityConverter(FakeConverter):
    def convert(self, request: ConversionRequest) -> ConversionResult:
        result = super().convert(request)
        result.diagnostics["converter_pid"] = str(os.getpid())
        return result


class HardExitConverter:
    name = "hard-exit"

    def convert(self, request: ConversionRequest) -> ConversionResult:
        del request
        os._exit(7)


class GrandchildConverter:
    name = "grandchild"

    def __init__(self, pid_path: str) -> None:
        self.pid_path = Path(pid_path)

    def convert(self, request: ConversionRequest) -> ConversionResult:
        del request
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.pid_path.write_text(str(child.pid), encoding="utf-8")
        time.sleep(30)
        raise AssertionError("grandchild converter should have been terminated")


class EmptySuccessConverter:
    name = "invalid-success"

    def convert(self, request: ConversionRequest) -> ConversionResult:
        transcription_path = request.staging_dir / "transcription.md"
        transcription_path.write_text("", encoding="utf-8")
        return ConversionResult(
            ok=True,
            backend=self.name,
            backend_version="1",
            duration_seconds=0,
            transcription_path=transcription_path,
        )


class NonCanonicalSuccessConverter:
    name = "noncanonical-success"

    def convert(self, request: ConversionRequest) -> ConversionResult:
        transcription_path = request.staging_dir / "nested" / "transcription.md"
        transcription_path.parent.mkdir()
        transcription_path.write_text("text", encoding="utf-8")
        return ConversionResult(
            ok=True,
            backend=self.name,
            backend_version="1",
            duration_seconds=0,
            transcription_path=transcription_path,
        )


class NonCanonicalFigureConverter(FakeConverter):
    def convert(self, request: ConversionRequest) -> ConversionResult:
        result = super().convert(request)
        figure = request.staging_dir / "wrong-place.png"
        figure.write_bytes(b"figure")
        result.figure_paths.append(figure)
        return result


class SymlinkTranscriptionConverter:
    name = "symlink-success"

    def convert(self, request: ConversionRequest) -> ConversionResult:
        outside = request.staging_dir.parent / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        transcription = request.staging_dir / "transcription.md"
        transcription.symlink_to(outside)
        return ConversionResult(
            ok=True,
            backend=self.name,
            backend_version="1",
            duration_seconds=0,
            transcription_path=transcription,
        )


def make_request(tmp_path: Path, *, timeout_seconds: int = 3) -> ConversionRequest:
    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(b"fake pdf")
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    return ConversionRequest(
        pdf_path=pdf_path,
        staging_dir=staging_dir,
        timeout_seconds=timeout_seconds,
    )


def assert_no_converter_children() -> None:
    assert all(
        child.name != "paper-pipeline-converter" for child in multiprocessing.active_children()
    )


def process_is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
                exit_code.value == 259
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        check=False,
        text=True,
    )
    return status.returncode == 0 and not status.stdout.lstrip().startswith("Z")


def terminate_test_process(pid: int) -> None:
    if not process_is_running(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        os.kill(pid, signal.SIGKILL)


def wait_for_pid(pid_path: Path, *, timeout_seconds: float = 5) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            return int(pid_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.02)
    pytest.fail(f"converter did not report its subprocess PID within {timeout_seconds} seconds")


def assert_process_stops(pid: int, *, timeout_seconds: float = 5) -> None:
    deadline = time.monotonic() + timeout_seconds
    while process_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_is_running(pid)


def test_success_runs_in_spawned_child_and_preserves_outputs(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(
        ConverterSpec(
            "tests.convert.test_runner:ProcessIdentityConverter",
            {"figure_count": 2},
        ),
        request,
    )

    assert result.ok
    assert int(result.diagnostics["converter_pid"]) != os.getpid()
    assert result.transcription_path is not None
    assert result.transcription_path == request.staging_dir / "transcription.md"
    assert result.transcription_path.stat().st_size > 0
    assert len(result.figure_paths) == 2
    assert all(
        path.is_file() and path.parent == request.staging_dir / "figures"
        for path in result.figure_paths
    )
    assert not (request.staging_dir / "pages").exists()
    assert_no_converter_children()


def test_converter_failure_is_returned_and_staging_is_cleaned(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(ConverterSpec(FAKE_SPEC, {"mode": "failure"}), request)

    assert not result.ok
    assert result.error
    assert list(request.staging_dir.iterdir()) == []
    assert_no_converter_children()


def test_child_exception_becomes_failed_result_with_traceback(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(ConverterSpec(FAKE_SPEC, {"mode": "crash"}), request)

    assert not result.ok
    assert "RuntimeError" in (result.error or "")
    assert "RuntimeError: fake converter crash" in result.diagnostics["stderr"]
    assert list(request.staging_dir.iterdir()) == []
    assert_no_converter_children()


def test_timeout_kills_process_tree_and_cleans_staging(tmp_path: Path) -> None:
    request = make_request(tmp_path, timeout_seconds=1)
    grandchild_pid_path = tmp_path / "grandchild.pid"
    started = time.monotonic()

    try:
        result = run_conversion(
            ConverterSpec(
                "tests.convert.test_runner:GrandchildConverter",
                {"pid_path": str(grandchild_pid_path)},
            ),
            request,
        )
        grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))

        assert not result.ok
        assert "timed out" in (result.error or "")
        assert time.monotonic() - started < 10
        assert list(request.staging_dir.iterdir()) == []
        assert_no_converter_children()
        assert_process_stops(grandchild_pid)
    finally:
        if grandchild_pid_path.is_file():
            terminate_test_process(int(grandchild_pid_path.read_text(encoding="utf-8")))


def test_cancellation_kills_process_tree_and_cleans_staging(tmp_path: Path) -> None:
    request = make_request(tmp_path, timeout_seconds=30)
    grandchild_pid_path = tmp_path / "grandchild.pid"
    cancel_event = threading.Event()
    result_holder: list[ConversionResult] = []

    thread = threading.Thread(
        target=lambda: result_holder.append(
            run_conversion(
                ConverterSpec(
                    "tests.convert.test_runner:GrandchildConverter",
                    {"pid_path": str(grandchild_pid_path)},
                ),
                request,
                cancel_event=cancel_event,
            )
        )
    )
    thread.start()
    grandchild_pid: int | None = None
    try:
        grandchild_pid = wait_for_pid(grandchild_pid_path)
        cancel_event.set()
        thread.join(timeout=10)

        assert not thread.is_alive()
        assert len(result_holder) == 1
        assert not result_holder[0].ok
        assert "cancel" in (result_holder[0].error or "")
        assert list(request.staging_dir.iterdir()) == []
        assert_no_converter_children()
        assert_process_stops(grandchild_pid)
    finally:
        cancel_event.set()
        thread.join(timeout=10)
        if grandchild_pid is None and grandchild_pid_path.is_file():
            grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))
        if grandchild_pid is not None:
            terminate_test_process(grandchild_pid)


def test_stdout_and_stderr_are_captured(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(ConverterSpec("tests.convert.test_runner:PrintingConverter"), request)

    assert result.ok
    assert "fake stdout" in result.diagnostics["stdout"]
    assert "fake stderr" in result.diagnostics["stderr"]


def test_nonzero_child_exit_becomes_failed_result(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(
        ConverterSpec("tests.convert.test_runner:HardExitConverter"),
        request,
    )

    assert not result.ok
    assert "without a result" in (result.error or "")
    assert list(request.staging_dir.iterdir()) == []
    assert_no_converter_children()


@pytest.mark.parametrize(
    "converter_class",
    [
        "EmptySuccessConverter",
        "NonCanonicalSuccessConverter",
        "NonCanonicalFigureConverter",
    ],
    ids=["empty-transcription", "noncanonical-transcription", "misplaced-figure"],
)
def test_invalid_success_artifacts_are_rejected_and_cleaned(
    tmp_path: Path, converter_class: str
) -> None:
    request = make_request(tmp_path)

    result = run_conversion(
        ConverterSpec(f"tests.convert.test_runner:{converter_class}"),
        request,
    )

    assert not result.ok
    assert result.error
    assert list(request.staging_dir.iterdir()) == []
    assert_no_converter_children()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink privileges are environment-specific")
def test_false_success_with_symlinked_transcription_is_rejected(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(
        ConverterSpec("tests.convert.test_runner:SymlinkTranscriptionConverter"), request
    )

    assert not result.ok
    assert result.error
    assert list(request.staging_dir.iterdir()) == []
    assert_no_converter_children()
