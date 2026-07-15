from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
        unexpected_download: bool = False,
    ) -> None:
        self.execution = execution or CommandResult(0)
        self.timeout = timeout
        self.unexpected_download = unexpected_download
        self.calls: list[tuple[str, str]] = []
        self.run_dir = ""

    def prepare(self, host: str, run_dir: str, timeout: float) -> None:
        assert timeout > 0
        self.run_dir = run_dir
        self.calls.append(("prepare", host))

    def upload(self, host: str, source: Path, remote_path: str, timeout: float) -> None:
        assert source.read_bytes() == b"%PDF fake"
        assert remote_path == f"{self.run_dir}/input.pdf"
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
        assert run_dir == self.run_dir
        assert remote_python == "python3"
        assert conversion_timeout == 5
        assert timeout > 0
        self.calls.append(("execute", host))
        if self.timeout:
            raise RemoteTransportTimeout("simulated")
        return self.execution

    def download(self, host: str, remote_dir: str, local_dir: Path, timeout: float) -> None:
        assert remote_dir == f"{self.run_dir}/output"
        assert timeout > 0
        self.calls.append(("download", host))
        (local_dir / "result.json").write_text(
            json.dumps({"ok": True, "backend_version": "1.10.2"}),
            encoding="utf-8",
        )
        (local_dir / "transcription.md").write_text("# Remote result\n", encoding="utf-8")
        figures = local_dir / "figures"
        figures.mkdir()
        (figures / "figure.png").write_bytes(b"figure")
        if self.unexpected_download:
            (local_dir / "undeclared.txt").write_text("bad", encoding="utf-8")

    def terminate(self, host: str, run_dir: str, timeout: float) -> None:
        assert run_dir == self.run_dir
        assert 0 < timeout <= 5
        self.calls.append(("terminate", host))

    def cleanup(self, host: str, run_dir: str, timeout: float) -> None:
        assert run_dir == self.run_dir
        assert 0 < timeout <= 5
        self.calls.append(("cleanup", host))


class LocalSubprocessTransport(FakeTransport):
    """Hardware-free local stand-in for the remote process and file sync."""

    def __init__(self, remote: Path) -> None:
        super().__init__()
        self.remote = remote

    def prepare(self, host: str, run_dir: str, timeout: float) -> None:
        super().prepare(host, run_dir, timeout)
        (self.remote / "output").mkdir(parents=True)

    def upload(self, host: str, source: Path, remote_path: str, timeout: float) -> None:
        super().upload(host, source, remote_path, timeout)
        shutil.copy2(source, self.remote / "input.pdf")

    def execute(
        self,
        host: str,
        run_dir: str,
        remote_python: str,
        conversion_timeout: int,
        timeout: float,
    ) -> CommandResult:
        super().execute(
            host,
            run_dir,
            remote_python,
            conversion_timeout,
            timeout,
        )
        code = """
import json
import pathlib
import sys
output = pathlib.Path(sys.argv[1])
(output / 'result.json').write_text(json.dumps({'ok': True, 'backend_version': 'fake'}))
(output / 'transcription.md').write_text('# Local remote subprocess\\n')
(output / 'figures').mkdir()
(output / 'figures' / 'figure.png').write_bytes(b'figure')
"""
        completed = subprocess.run(
            [sys.executable, "-c", code, str(self.remote / "output")],
            timeout=timeout,
            check=False,
        )
        return CommandResult(completed.returncode)

    def download(self, host: str, remote_dir: str, local_dir: Path, timeout: float) -> None:
        assert remote_dir == f"{self.run_dir}/output"
        assert timeout > 0
        self.calls.append(("download", host))
        shutil.copytree(self.remote / "output", local_dir, dirs_exist_ok=True)


def request(tmp_path: Path) -> ConversionRequest:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF fake")
    staging = tmp_path / "staging"
    staging.mkdir()
    return ConversionRequest(source, staging, timeout_seconds=5)


def test_fake_transport_returns_only_canonical_local_paths(tmp_path: Path) -> None:
    transport = LocalSubprocessTransport(tmp_path / "fake-remote")

    result = RemoteConverter("gpu-host", transport=transport).convert(request(tmp_path))

    assert result.ok
    assert result.backend == "remote-marker"
    assert result.backend_version == "fake"
    transcription = result.transcription_path
    assert transcription is not None
    assert transcription == tmp_path / "staging" / "transcription.md"
    assert transcription.read_text(encoding="utf-8") == "# Local remote subprocess\n"
    assert result.figure_paths == [tmp_path / "staging" / "figures" / "figure.png"]
    assert [name for name, _host in transport.calls] == [
        "prepare",
        "upload",
        "execute",
        "download",
        "cleanup",
    ]
    assert len(Path(transport.run_dir).name) == 32


def test_timeout_is_bounded_and_requests_remote_termination(tmp_path: Path) -> None:
    transport = FakeTransport(timeout=True)

    result = RemoteConverter("gpu-host", transport=transport).convert(request(tmp_path))

    assert not result.ok
    assert result.error == "remote conversion timed out"
    assert [name for name, _host in transport.calls][-2:] == ["terminate", "cleanup"]
    assert list((tmp_path / "staging").iterdir()) == []


def test_dead_connection_is_a_clear_failure_not_a_hang(tmp_path: Path) -> None:
    transport = FakeTransport(execution=CommandResult(255, connection_lost=True))

    result = RemoteConverter("gpu-host", transport=transport).convert(request(tmp_path))

    assert not result.ok
    assert result.error == "remote SSH connection was lost"
    assert "download" not in [name for name, _host in transport.calls]
    assert [name for name, _host in transport.calls][-2:] == ["terminate", "cleanup"]


def test_unexpected_download_never_enters_conversion_staging(tmp_path: Path) -> None:
    result = RemoteConverter(
        "gpu-host",
        transport=FakeTransport(unexpected_download=True),
    ).convert(request(tmp_path))

    assert not result.ok
    assert result.error == "remote conversion returned invalid artifacts"
    assert list((tmp_path / "staging").iterdir()) == []


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


def test_remote_shell_protocol_uses_pidfile_process_group_and_hangup_trap() -> None:
    run_dir = "/tmp/paper-pipeline/0123456789abcdef0123456789abcdef"

    execution = _remote_execution_script(run_dir, "python3", 30)
    cleanup = _remote_cleanup_script(run_dir)

    assert "setsid python3 -m paper_pipeline.convert.remote worker" in execution
    assert "worker.pid" in execution
    assert "trap 'terminate_worker; rm -rf --" in execution
    assert "HUP INT TERM" in execution
    assert 'kill -TERM -- "-$target"' in execution
    assert 'kill -KILL -- "-$pid"' in cleanup
    assert run_dir in cleanup


def test_converter_is_constructed_from_user_config_only(tmp_path: Path) -> None:
    config = AppConfig(
        remote_converter_host="configured-host",
        remote_converter_root="/srv/paper-pipeline",
        remote_converter_python="/opt/paper-pipeline/bin/python",
    )
    transport = LocalSubprocessTransport(tmp_path / "fake-remote")

    converter = RemoteConverter.from_config(config, transport=transport)

    assert converter.host == "configured-host"
    assert converter.remote_root == "/srv/paper-pipeline"
    assert converter.remote_python == "/opt/paper-pipeline/bin/python"


@pytest.mark.gpu
@pytest.mark.skipif(
    os.environ.get("PAPER_PIPELINE_REMOTE_TEST") != "1",
    reason="set PAPER_PIPELINE_REMOTE_TEST=1 and a real PDF path to opt in",
)
def test_real_remote_host_opt_in(tmp_path: Path) -> None:
    config = AppConfig()
    pdf = Path(os.environ["PAPER_PIPELINE_REMOTE_TEST_PDF"])
    assert config.remote_converter_host
    staging = tmp_path / "staging"
    staging.mkdir()

    result = RemoteConverter(
        config.remote_converter_host,
        config.remote_converter_root,
        config.remote_converter_python,
    ).convert(ConversionRequest(pdf, staging, config.converter_timeout_seconds))

    assert result.ok, result.error
    assert result.transcription_path is not None
    assert result.transcription_path.stat().st_size > 0
