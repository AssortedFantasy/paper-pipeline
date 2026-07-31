"""Versioned LLM provider contracts.

The default dev loop and test suite use a fake provider. Real calls require
credentials and are only exercised by tests marked ``llm``.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class RemoteFile:
    id: str


@dataclass(frozen=True)
class RemoteBatch:
    id: str
    status: str
    input_file_id: str
    output_file_id: str | None = None
    error_file_id: str | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    created_at: datetime | None = None


@dataclass(frozen=True)
class BatchLineResult:
    custom_id: str
    ok: bool
    text: str = ""
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    request_id: str | None = None
    error: str | None = None


class BatchLLMProvider(Protocol):
    """External-edge operations required by durable Batch orchestration."""

    name: str

    def request_body(
        self,
        *,
        prompt: str,
        model: str,
        input_sha256: str,
        file_id: str | None = None,
        text_input: str | None = None,
    ) -> dict[str, Any]:
        """Build one underlying endpoint request body."""
        ...

    def upload_input(self, path: Path, *, expires_after_seconds: int) -> RemoteFile:
        """Upload one model input with a server-side safety expiry."""
        ...

    def upload_batch_file(self, path: Path) -> RemoteFile:
        """Upload one JSONL file with purpose=batch."""
        ...

    def create_batch(
        self,
        *,
        input_file_id: str,
        endpoint: str,
        metadata: dict[str, str],
    ) -> RemoteBatch:
        """Create one remote Batch. Callers must treat timeout as ambiguous."""
        ...

    def retrieve_batch(self, batch_id: str) -> RemoteBatch:
        """Return the latest remote Batch state."""
        ...

    def cancel_batch(self, batch_id: str) -> RemoteBatch:
        """Request asynchronous remote cancellation."""
        ...

    def download_file(self, file_id: str, destination: Path) -> None:
        """Download a remote result file to an existing staged destination."""
        ...

    def delete_file(self, file_id: str) -> None:
        """Delete one provider-side file idempotently."""
        ...

    def list_batches(self, *, limit: int = 100) -> list[RemoteBatch]:
        """List recent remote Batches for uncertain-submission reconciliation."""
        ...

    def parse_batch_line(self, value: dict[str, Any]) -> BatchLineResult:
        """Translate one output/error JSONL line without exposing raw payloads."""
        ...
