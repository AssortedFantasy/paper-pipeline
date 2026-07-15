"""Tests for the safe environment doctor command."""

from pathlib import Path
from typing import Any, cast

from paper_pipeline.cli import _run_doctor, main
from paper_pipeline.config import AppConfig


def _config(**values: object) -> AppConfig:
    return AppConfig(**cast(Any, {**values, "_env_file": None}))


def test_doctor_reports_missing_optional_extras(monkeypatch, capsys) -> None:
    monkeypatch.setattr("paper_pipeline.cli.importlib.util.find_spec", lambda _name: None)
    config = _config(llm_api_key=None, llm_model=None)

    assert _run_doctor(None, config) == 0
    output = capsys.readouterr().out
    assert "Marker extra: not installed" in output
    assert "LLM credentials: not configured" in output
    assert "LLM model: not configured" in output


def test_doctor_reports_configured_extras_without_printing_secret(monkeypatch, capsys) -> None:
    monkeypatch.setattr("paper_pipeline.cli.importlib.util.find_spec", lambda _name: object())
    secret = "never-print-this-secret"
    config = _config(llm_api_key=secret, llm_model="test-model")

    assert _run_doctor(None, config) == 0
    output = capsys.readouterr().out
    assert "Marker extra: available" in output
    assert "LLM credentials: configured" in output
    assert "LLM model: configured (test-model)" in output
    assert secret not in output


def test_doctor_checks_target_writability(tmp_path: Path, capsys) -> None:
    config = _config()

    assert _run_doctor(tmp_path / "future-library", config) == 0
    assert "Target directory: writable" in capsys.readouterr().out


def test_doctor_subcommand_accepts_target(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr("paper_pipeline.cli.load_config", _config)
    monkeypatch.setattr("paper_pipeline.cli.importlib.util.find_spec", lambda _name: None)

    assert main(["doctor", str(tmp_path)]) == 0
    assert "Paper Pipeline:" in capsys.readouterr().out
