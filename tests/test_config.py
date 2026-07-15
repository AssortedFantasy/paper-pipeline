"""Configuration loading and precedence tests."""

from pathlib import Path

from paper_pipeline import config


def test_defaults_without_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "USER_CONFIG_FILE", tmp_path / "missing.env")
    monkeypatch.delenv("PAPER_PIPELINE_LLM_MODEL", raising=False)
    monkeypatch.delenv("PAPER_PIPELINE_LLM_API_KEY", raising=False)

    settings = config.load_config()

    assert settings.llm_model is None
    assert settings.llm_api_key is None
    assert settings.llm_concurrency == 4


def test_home_env_file_is_loaded(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PAPER_PIPELINE_LLM_MODEL=file-model\nPAPER_PIPELINE_LLM_API_KEY=file-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "USER_CONFIG_FILE", env_file)
    monkeypatch.delenv("PAPER_PIPELINE_LLM_MODEL", raising=False)
    monkeypatch.delenv("PAPER_PIPELINE_LLM_API_KEY", raising=False)

    settings = config.load_config()

    assert settings.llm_model == "file-model"
    assert settings.llm_api_key == "file-secret"


def test_environment_takes_precedence_over_home_file(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PAPER_PIPELINE_LLM_MODEL=file-model\n", encoding="utf-8")
    monkeypatch.setattr(config, "USER_CONFIG_FILE", env_file)
    monkeypatch.setenv("PAPER_PIPELINE_LLM_MODEL", "environment-model")

    assert config.load_config().llm_model == "environment-model"
