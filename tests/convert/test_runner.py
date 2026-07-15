import multiprocessing
import os
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


class HardExitConverter:
    name = "hard-exit"

    def convert(self, request: ConversionRequest) -> ConversionResult:
        del request
        os._exit(7)


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


def test_success_runs_in_spawned_child_and_preserves_outputs(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(
        ConverterSpec(FAKE_SPEC, {"figure_count": 2}),
        request,
    )

    assert result.ok is True
    assert result.transcription_path is not None
    assert result.transcription_path.is_file()
    assert len(result.figure_paths) == 2
    assert result.diagnostics == {"stdout": "", "stderr": ""}
    assert_no_converter_children()


def test_converter_failure_is_returned_and_staging_is_cleaned(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(ConverterSpec(FAKE_SPEC, {"mode": "failure"}), request)

    assert result.ok is False
    assert result.error == "fake converter failure"
    assert list(request.staging_dir.iterdir()) == []
    assert_no_converter_children()


def test_child_exception_becomes_failed_result_with_traceback(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(ConverterSpec(FAKE_SPEC, {"mode": "crash"}), request)

    assert result.ok is False
    assert result.error == "converter raised an exception: RuntimeError: fake converter crash"
    assert "RuntimeError: fake converter crash" in result.diagnostics["stderr"]
    assert list(request.staging_dir.iterdir()) == []
    assert_no_converter_children()


def test_timeout_kills_child_and_cleans_staging(tmp_path: Path) -> None:
    request = make_request(tmp_path, timeout_seconds=1)
    started = time.monotonic()

    result = run_conversion(
        ConverterSpec(FAKE_SPEC, {"mode": "hang", "hang_seconds": 30}),
        request,
    )

    assert result.ok is False
    assert result.error == "conversion timed out after 1 seconds"
    assert time.monotonic() - started < 10
    assert list(request.staging_dir.iterdir()) == []
    assert_no_converter_children()


def test_cancellation_kills_child_and_cleans_staging(tmp_path: Path) -> None:
    request = make_request(tmp_path, timeout_seconds=30)
    cancel_event = threading.Event()
    result_holder = []

    thread = threading.Thread(
        target=lambda: result_holder.append(
            run_conversion(
                ConverterSpec(FAKE_SPEC, {"mode": "hang", "hang_seconds": 30}),
                request,
                cancel_event=cancel_event,
            )
        )
    )
    thread.start()
    time.sleep(0.3)
    cancel_event.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(result_holder) == 1
    assert result_holder[0].ok is False
    assert result_holder[0].error == "conversion cancelled"
    assert list(request.staging_dir.iterdir()) == []
    assert_no_converter_children()


def test_stdout_and_stderr_are_captured(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(ConverterSpec("tests.convert.test_runner:PrintingConverter"), request)

    assert result.ok is True
    assert result.diagnostics["stdout"] == "fake stdout\n"
    assert result.diagnostics["stderr"] == "fake stderr\n"


def test_empty_output_failure_is_cleaned(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(ConverterSpec(FAKE_SPEC, {"mode": "empty"}), request)

    assert result.ok is False
    assert result.error == "fake converter produced an empty transcription"
    assert list(request.staging_dir.iterdir()) == []


def test_invalid_module_path_becomes_failed_result(tmp_path: Path) -> None:
    result = run_conversion(ConverterSpec("not-a-module-path"), make_request(tmp_path))

    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("converter raised an exception: ValueError:")


def test_nonzero_child_exit_becomes_failed_result(tmp_path: Path) -> None:
    result = run_conversion(
        ConverterSpec("tests.convert.test_runner:HardExitConverter"), make_request(tmp_path)
    )

    assert result.ok is False
    assert result.error == "converter process exited without a result (exit code 7)"
    assert_no_converter_children()


def test_false_success_with_empty_artifact_is_rejected(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(
        ConverterSpec("tests.convert.test_runner:EmptySuccessConverter"), request
    )

    assert result.ok is False
    assert result.error == "converter reported success without a non-empty transcription"
    assert list(request.staging_dir.iterdir()) == []


def test_false_success_with_noncanonical_transcription_is_rejected(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(
        ConverterSpec("tests.convert.test_runner:NonCanonicalSuccessConverter"), request
    )

    assert not result.ok
    assert result.error == "converter must return the canonical staging transcription.md path"
    assert list(request.staging_dir.iterdir()) == []


def test_false_success_with_figure_outside_figures_directory_is_rejected(
    tmp_path: Path,
) -> None:
    request = make_request(tmp_path)

    result = run_conversion(
        ConverterSpec("tests.convert.test_runner:NonCanonicalFigureConverter"), request
    )

    assert not result.ok
    assert result.error == (
        "converter must return figure paths inside the staging figures directory"
    )
    assert list(request.staging_dir.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink privileges are environment-specific")
def test_false_success_with_symlinked_transcription_is_rejected(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    result = run_conversion(
        ConverterSpec("tests.convert.test_runner:SymlinkTranscriptionConverter"), request
    )

    assert not result.ok
    assert result.error == "converter must return the canonical staging transcription.md path"
    assert list(request.staging_dir.iterdir()) == []
