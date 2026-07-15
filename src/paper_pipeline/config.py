"""Application configuration.

Responsibilities:

- Load user-level application settings from environment variables and
  ``~/.paper-pipeline/.env`` (``PAPER_PIPELINE_*`` env vars take precedence).
- Hold LLM provider credentials, model selection, and converter/GPU settings.
- Secrets are only ever read from here. They must never be written into a
  library, into logs stored in a library, or into recipe provenance.

Libraries carry no configuration. Prefer sensible defaults over new settings.

Implemented by WP-0.2.
"""

from pathlib import Path
from typing import Any, cast

from pydantic_settings import BaseSettings, SettingsConfigDict

USER_CONFIG_FILE = Path.home() / ".paper-pipeline" / ".env"


class AppConfig(BaseSettings):
    """User-level application settings. Never stored inside a library."""

    model_config = SettingsConfigDict(
        env_prefix="PAPER_PIPELINE_",
        env_file=USER_CONFIG_FILE,
        extra="ignore",
    )

    # LLM provider (used by recipes; optional for core operation)
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    # Required to run recipes; there is no default model. `doctor` and the
    # recipe services fail with an actionable message when unset.
    llm_model: str | None = None
    # Max concurrent remote recipe jobs across papers (same-paper is always sequential).
    llm_concurrency: int = 4

    # Conversion
    converter_timeout_seconds: int = 1800
    conversion_concurrency: int = 1  # keep 1 unless testing proves otherwise


def load_config() -> AppConfig:
    """Load user configuration, with environment variables taking precedence."""
    return AppConfig(**cast(Any, {"_env_file": USER_CONFIG_FILE}))
