"""Provider-neutral durable models for recipe Batch orchestration."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecipeRunPhase(StrEnum):
    PLANNING = "planning"
    SNAPSHOTTING = "snapshotting"
    UPLOADING = "uploading"
    SUBMISSION_READY = "submission_ready"
    SUBMISSION_ATTEMPTED = "submission_attempted"
    VALIDATING = "validating"
    IN_PROGRESS = "in_progress"
    FINALIZING = "finalizing"
    COLLECTING = "collecting"
    INSTALLING = "installing"
    CLEANING_UP = "cleaning_up"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    BLOCKED = "blocked"

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.COMPLETED,
            self.PARTIAL,
            self.FAILED,
            self.CANCELLED,
        }


class RecipeInvocation(BaseModel):
    """One expected Batch line and its local installation contract."""

    model_config = ConfigDict(extra="forbid")

    custom_id: str
    citekey: str
    recipe_name: str
    recipe_version: int
    recipe_prompt: str
    prompt_sha256: str
    output_filename: str
    input_kind: Literal["pdf", "transcription"]
    input_artifact: str
    input_sha256: str
    snapshot_filename: str
    overwrite: bool = False


class RecipeRunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str
    provider: str
    model: str
    endpoint: Literal["/v1/responses"] = "/v1/responses"
    created_at: datetime
    invocations: list[RecipeInvocation]


class CollectedRecipeResult(BaseModel):
    """One remote request outcome after result-file parsing and validation."""

    model_config = ConfigDict(extra="forbid")

    custom_id: str
    ok: bool
    text_filename: str | None = None
    provider: str
    model: str
    prompt_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    request_id: str | None = None
    error: str | None = None
    local_error: str | None = None


class RecipeRunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    run_id: str
    phase: RecipeRunPhase = RecipeRunPhase.PLANNING
    updated_at: datetime
    input_file_id: str | None = None
    batch_id: str | None = None
    remote_status: str | None = None
    output_file_id: str | None = None
    error_file_id: str | None = None
    uploads: dict[str, str] = Field(default_factory=dict)
    outcomes: dict[str, CollectedRecipeResult] = Field(default_factory=dict)
    finalized: list[str] = Field(default_factory=list)
    cleanup_pending: list[str] = Field(default_factory=list)
    cleanup_warnings: list[str] = Field(default_factory=list)
    total: int = 0
    completed: int = 0
    failed: int = 0
    error: str | None = None
