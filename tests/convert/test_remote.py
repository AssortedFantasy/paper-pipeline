from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from paper_pipeline.config import AppConfig
from paper_pipeline.convert.contract import ConversionRequest
from paper_pipeline.convert.remote import (
    CommandResult,
    RemoteConverter,
    RemoteTransportTimeout,
    _remote_cleanup_script,
    _remote_execution_script,
)


class FakeTransport:
    def __init__(
        self,
        *,
        execution: CommandResult | None = None,
        timeout: bool = False,
        invalid_download: str | None = None,
    ) -> None:
        self.execution = execution or CommandResult(0)
        self.timeout = timeout
        self.invalid_download = invalid_download
        self.calls: list[tuple[str, str]] = []
        self.run_dir = ""

    def prepare(self, host: str, run_dir: str, timeout: float) -> None:
        assert timeout > 0
        self.run_dir = run_dir
        self.calls.append(("prepare", host))

    def upload(self, host: str, source: Path, remote_path: str, timeout: float) -> None:
        assert source.read_bytes() == b"%PDF fake"
        assert timeout > 0
        self.calls.append(("upload", host))

    def execute(
        self,
        host: str,
        run_dir: str,
        remote_python: str,
        conversion_timeout: int,
        timeout: float,
    ) -> CommandResult:
        assert remote_python
        assert conversion_timeout > 0
        assert timeout > 0
        self.calls.append(("execute", host))
        if self.timeout:
            raise RemoteTransportTimeout("simulated")
        return self.execution

    def download(self, host: str, remote_dir: str, local_dir: Path, timeout: float) -> None:
        assert timeout > 0
        self.calls.append(("download", host))
        (local_dir / "result.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "backend_version": "1.10.2",
                    "diagnostics": {"timing_conversion_seconds": "1.250"},
                }
            ),
            encoding="utf-8",
        )
        (local_dir / "transcription.md").write_text("# Remote result\n", encoding="utf-8")
        figures = local_dir / "figures"
        figures.mkdir()
        (figures / "figure.png").write_bytes(b"figure")
        pages = local_dir / "pages"
        pages.mkdir()
        (pages / "page1.png").write_bytes(b"page")
        if self.invalid_download == "unexpected-entry":
            (local_dir / "undeclared.txt").write_text("bad", encoding="utf-8")
        elif self.invalid_download == "invalid-page":
            (pages / "page1.png").rename(pages / "page1.txt")
        elif self.invalid_download == "missing-transcription":
            (local_dir / "transcription.md").unlink()
        elif self.invalid_download == "empty-transcription":
            (local_dir / "transcription.md").write_bytes(b"")
        elif self.invalid_download == "malformed-manifest":
            (local_dir / "result.json").write_text("not-json", encoding="utf-8")
        elif self.invalid_download == "symlink":
            transcription = local_dir / "transcription.md"
            transcription.unlink()
            outside = local_dir.parent / "outside-transcription.md"
            outside.write_text("outside", encoding="utf-8")
            try:
                transcription.symlink_to(outside)
            except OSError as error:
                pytest.skip(f"file symlinks unavailable: {error}")

    def terminate(self, host: str, run_dir: str, timeout: float) -> None:
        assert 0 < timeout <= 5
        self.calls.append(("terminate", host))

    def cleanup(self, host: str, run_dir: str, timeout: float) -> None:
        assert 0 < timeout <= 5
        self.calls.append(("cleanup", host))


def request(tmp_path: Path) -> ConversionRequest:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF fake")
    staging = tmp_path / "staging"
    staging.mkdir()
    return ConversionRequest(source, staging, timeout_seconds=5)


def test_success_installs_only_canonical_local_artifacts_and_cleans_up(tmp_path: Path) -> None:
    transport = FakeTransport()
    conversion_request = request(tmp_path)

    result = RemoteConverter("gpu-host", transport=transport).convert(conversion_request)

    assert result.ok
    assert result.backend == "remote-marker"
    assert result.figure_paths
    assert result.page_paths
    installed = [
        result.transcription_path,
        *result.figure_paths,
        *result.page_paths,
    ]
    assert all(
        path is not None and path.is_file() and path.is_relative_to(conversion_request.staging_dir)
        for path in installed
    )
    operations = {name for name, _host in transport.calls}
    assert {"prepare", "upload", "execute", "download", "cleanup"} <= operations
    assert "terminate" not in operations


@pytest.mark.parametrize("failure", ["timeout", "disconnection"])
def test_incomplete_remote_execution_is_terminated_and_cleaned_up(
    tmp_path: Path, failure: str
) -> None:
    transport = (
        FakeTransport(timeout=True)
        if failure == "timeout"
        else FakeTransport(execution=CommandResult(255, connection_lost=True))
    )

    result = RemoteConverter("gpu-host", transport=transport).convert(request(tmp_path))

    assert not result.ok
    assert result.error
    operations = {name for name, _host in transport.calls}
    assert {"terminate", "cleanup"} <= operations
    assert "download" not in operations
    assert list((tmp_path / "staging").iterdir()) == []


@pytest.mark.parametrize(
    "problem",
    [
        "unexpected-entry",
        "invalid-page",
        "missing-transcription",
        "empty-transcription",
        "malformed-manifest",
        "symlink",
    ],
)
def test_invalid_download_never_enters_conversion_staging(tmp_path: Path, problem: str) -> None:
    transport = FakeTransport(invalid_download=problem)
    result = RemoteConverter("gpu-host", transport=transport).convert(request(tmp_path))

    assert not result.ok
    assert result.error
    assert list((tmp_path / "staging").iterdir()) == []
    assert "cleanup" in {name for name, _host in transport.calls}


@pytest.mark.parametrize(
    ("host", "root", "python"),
    [
        ("-oProxyCommand=bad", "/tmp/paper-pipeline", "python3"),
        ("gpu-host", "relative/root", "python3"),
        ("gpu-host", "/tmp/../escape", "python3"),
        ("gpu-host", "/tmp/paper-pipeline", "python3 -c"),
    ],
)
def test_remote_settings_reject_command_and_path_injection(
    host: str, root: str, python: str
) -> None:
    with pytest.raises(ValueError):
        RemoteConverter(host, root, python, transport=FakeTransport())


def test_remote_shell_protocol_can_terminate_the_worker_process_group() -> None:
    run_dir = "/tmp/paper-pipeline/0123456789abcdef0123456789abcdef"

    execution = _remote_execution_script(run_dir, "python3", 30)
    cleanup = _remote_cleanup_script(run_dir)

    assert "setsid" in execution
    assert all(signal in execution for signal in ("HUP", "INT", "TERM"))
    pidfile_pattern = r"[A-Za-z0-9_./-]+\.pid"
    assert set(re.findall(pidfile_pattern, execution)) & set(re.findall(pidfile_pattern, cleanup))
    assert all(
        f"kill -{signal}" in script
        for script in (execution, cleanup)
        for signal in ("TERM", "KILL")
    )
    assert re.search(r"""kill -TERM\b[^;]*["']-\$""", execution)
    assert re.search(r"""kill -KILL\b[^;]*["']-\$""", cleanup)


def test_converter_is_constructed_from_user_config_only() -> None:
    config = AppConfig(
        remote_converter_host="configured-host",
        remote_converter_root="/srv/paper-pipeline",
        remote_converter_python="/opt/paper-pipeline/bin/python",
    )
    transport = FakeTransport()

    converter = RemoteConverter.from_config(config, transport=transport)

    assert converter.host == "configured-host"
    assert converter.remote_root == "/srv/paper-pipeline"
    assert converter.remote_python == "/opt/paper-pipeline/bin/python"
