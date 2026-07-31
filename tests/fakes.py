"""Fake implementations of external contracts for the fast test suite.

Expanded by the work packages that need them:

- ``FakeConverter``: implements ``convert.contract.Converter``. Writes a tiny
  deterministic transcription and optional figure files into staging.
- ``FakePageRenderer``: writes deterministic page images independently of
  transcription conversion.
- ``FakeLLMProvider``: implements ``recipes.provider.LLMProvider``. Returns
  canned text; records calls so scheduler tests can assert per-paper
  sequencing. Configurable to fail or delay.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from paper_pipeline.convert.contract import ConversionRequest, ConversionResult
from paper_pipeline.pages.contract import PageRenderRequest, PageRenderResult
from paper_pipeline.recipes.provider import (
    BatchLineResult,
    ProviderRequest,
    ProviderResult,
    RemoteBatch,
    RemoteFile,
)

FakeConverterMode = Literal["success", "failure", "crash", "hang", "empty"]


@dataclass
class FakeConverter:
    """Deterministic converter double usable from spawned child processes."""

    name: str = field(default="fake", init=False)

    mode: FakeConverterMode = "success"
    figure_count: int = 0
    hang_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.figure_count < 0:
            raise ValueError("figure_count must not be negative")

    def convert(self, request: ConversionRequest) -> ConversionResult:
        """Produce the configured outcome without reading the input PDF."""
        started = time.perf_counter()

        if self.mode == "crash":
            raise RuntimeError("fake converter crash")
        if self.mode == "hang":
            delay = self.hang_seconds
            time.sleep(delay if delay is not None else request.timeout_seconds + 1.0)
        if self.mode == "failure":
            return self._result(
                started=started,
                ok=False,
                error="fake converter failure",
            )

        transcription_path = request.staging_dir / "transcription.md"
        if self.mode == "empty":
            transcription_path.write_text("", encoding="utf-8")
            return self._result(
                started=started,
                ok=False,
                transcription_path=transcription_path,
                error="fake converter produced an empty transcription",
            )

        transcription_path.write_text(
            "# Fake transcription\n\nDeterministic converter output.\n",
            encoding="utf-8",
        )
        figure_paths = self._write_figures(request.staging_dir)
        return self._result(
            started=started,
            ok=True,
            transcription_path=transcription_path,
            figure_paths=figure_paths,
        )

    def _write_figures(self, staging_dir: Path) -> list[Path]:
        if self.figure_count == 0:
            return []

        figures_dir = staging_dir / "figures"
        figures_dir.mkdir()
        paths = [figures_dir / f"figure-{index}.png" for index in range(1, self.figure_count + 1)]
        for index, path in enumerate(paths, start=1):
            path.write_bytes(f"fake figure {index}\n".encode())
        return paths

    @staticmethod
    def _result(
        *,
        started: float,
        ok: bool,
        transcription_path: Path | None = None,
        figure_paths: list[Path] | None = None,
        error: str | None = None,
    ) -> ConversionResult:
        return ConversionResult(
            ok=ok,
            backend="fake",
            backend_version="1.0",
            duration_seconds=time.perf_counter() - started,
            transcription_path=transcription_path,
            figure_paths=figure_paths or [],
            error=error,
        )


@dataclass
class FakePageRenderer:
    """Deterministic local page-renderer double usable from spawned processes."""

    name: str = field(default="fake-pages", init=False)
    mode: FakeConverterMode = "success"
    page_count: int = 1
    hang_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.page_count < 0:
            raise ValueError("page_count must not be negative")

    def render(self, request: PageRenderRequest) -> PageRenderResult:
        started = time.perf_counter()
        if self.mode == "crash":
            raise RuntimeError("fake page renderer crash")
        if self.mode == "hang":
            delay = self.hang_seconds
            time.sleep(delay if delay is not None else request.timeout_seconds + 1.0)
        if self.mode == "failure":
            return self._result(started, ok=False, error="fake page renderer failure")

        pages_dir = request.staging_dir / "pages"
        pages_dir.mkdir()
        paths = [pages_dir / f"page{index}.png" for index in range(1, self.page_count + 1)]
        for index, path in enumerate(paths, start=1):
            path.write_bytes(f"fake page {index}\n".encode())
        if self.mode == "empty" and paths:
            paths[0].write_bytes(b"")
        return self._result(started, ok=True, page_paths=paths)

    @staticmethod
    def _result(
        started: float,
        *,
        ok: bool,
        page_paths: list[Path] | None = None,
        error: str | None = None,
    ) -> PageRenderResult:
        return PageRenderResult(
            ok=ok,
            renderer="fake-pages",
            renderer_version="1.0",
            duration_seconds=time.perf_counter() - started,
            page_paths=page_paths or [],
            error=error,
        )


@dataclass
class FakeLLMProvider:
    """Deterministic LLM double with call recording and failure/delay modes."""

    name: str = field(default="fake", init=False)
    response: str = "Fake LLM response."
    fail: bool = False
    fail_prompts: set[str] = field(default_factory=set)
    delay_seconds: float = 0.0
    batch_statuses: list[str] = field(default_factory=list)
    retain_deleted_files: bool = False
    failure_message: str = "fake provider failure"
    prompt_tokens: int = 100
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    completion_tokens: int = 20
    cost_usd: float = 0.001
    calls: list[ProviderRequest] = field(default_factory=list, init=False)
    _files: dict[str, bytes] = field(default_factory=dict, init=False)
    _file_paths: dict[str, Path] = field(default_factory=dict, init=False)
    _batches: dict[str, RemoteBatch] = field(default_factory=dict, init=False)
    _batch_poll_statuses: dict[str, list[str]] = field(default_factory=dict, init=False)
    deleted_file_ids: list[str] = field(default_factory=list, init=False)
    input_upload_count: int = field(default=0, init=False)
    created_batch_count: int = field(default=0, init=False)

    def generate(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request)
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        if self.fail:
            return ProviderResult(
                ok=False,
                provider=self.name,
                model=request.model,
                error=self.failure_message,
            )
        return ProviderResult(
            ok=True,
            text=self.response,
            provider=self.name,
            model=request.model,
            prompt_tokens=self.prompt_tokens,
            cached_tokens=self.cached_tokens,
            cache_write_tokens=self.cache_write_tokens,
            completion_tokens=self.completion_tokens,
            cost_usd=self.cost_usd,
        )

    def request_body(
        self,
        *,
        prompt: str,
        model: str,
        input_sha256: str,
        file_id: str | None = None,
        text_input: str | None = None,
    ) -> dict[str, Any]:
        del input_sha256
        stable = (
            {"type": "input_file", "file_id": file_id}
            if file_id is not None
            else {"type": "input_text", "text": text_input}
        )
        return {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        stable,
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
            "store": False,
        }

    def upload_input(self, path: Path, *, expires_after_seconds: int) -> RemoteFile:
        assert expires_after_seconds >= 3600
        self.input_upload_count += 1
        identifier = self._next_file_id()
        self._files[identifier] = path.read_bytes()
        self._file_paths[identifier] = path
        return RemoteFile(identifier)

    def upload_batch_file(self, path: Path) -> RemoteFile:
        identifier = self._next_file_id()
        self._files[identifier] = path.read_bytes()
        self._file_paths[identifier] = path
        return RemoteFile(identifier)

    def create_batch(
        self,
        *,
        input_file_id: str,
        endpoint: str,
        metadata: dict[str, str],
    ) -> RemoteBatch:
        del endpoint, metadata
        self.created_batch_count += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        lines = [
            json.loads(line)
            for line in self._files[input_file_id].decode("utf-8").splitlines()
            if line.strip()
        ]
        output_lines: list[dict[str, Any]] = []
        error_lines: list[dict[str, Any]] = []
        for line in lines:
            body = line["body"]
            content = body["input"][0]["content"]
            stable = content[0]
            prompt = content[1]["text"]
            file_id = stable.get("file_id")
            request = ProviderRequest(
                prompt=prompt,
                text_input=stable.get("text"),
                pdf_input=self._file_paths.get(file_id) if file_id else None,
                model=body["model"],
            )
            self.calls.append(request)
            if self.fail or prompt in self.fail_prompts:
                error_lines.append(
                    {
                        "custom_id": line["custom_id"],
                        "response": None,
                        "error": {"code": "fake_failure"},
                    }
                )
                continue
            output_lines.append(
                {
                    "custom_id": line["custom_id"],
                    "response": {
                        "status_code": 200,
                        "request_id": f"req_{line['custom_id']}",
                        "body": {
                            "status": "completed",
                            "model": body["model"],
                            "output": [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": self.response}],
                                }
                            ],
                            "usage": {
                                "input_tokens": self.prompt_tokens,
                                "input_tokens_details": {
                                    "cached_tokens": self.cached_tokens,
                                    "cache_write_tokens": self.cache_write_tokens,
                                },
                                "output_tokens": self.completion_tokens,
                            },
                        },
                    },
                    "error": None,
                }
            )
        output_file_id = self._store_jsonl(output_lines) if output_lines else None
        error_file_id = self._store_jsonl(error_lines) if error_lines else None
        batch_id = f"batch-{len(self._batches) + 1}"
        batch = RemoteBatch(
            id=batch_id,
            status="completed",
            input_file_id=input_file_id,
            output_file_id=output_file_id,
            error_file_id=error_file_id,
            total=len(lines),
            completed=len(output_lines),
            failed=len(error_lines),
            created_at=datetime.now(UTC),
        )
        self._batches[batch_id] = batch
        if self.batch_statuses:
            self._batch_poll_statuses[batch_id] = list(self.batch_statuses)
            return RemoteBatch(
                id=batch.id,
                status=self.batch_statuses[0],
                input_file_id=batch.input_file_id,
                total=batch.total,
                created_at=batch.created_at,
            )
        return batch

    def retrieve_batch(self, batch_id: str) -> RemoteBatch:
        batch = self._batches[batch_id]
        statuses = self._batch_poll_statuses.get(batch_id, [])
        if not statuses:
            return batch
        status = statuses.pop(0)
        if status in {"completed", "failed", "expired", "cancelled"}:
            return RemoteBatch(**{**batch.__dict__, "status": status})
        return RemoteBatch(
            id=batch.id,
            status=status,
            input_file_id=batch.input_file_id,
            total=batch.total,
            created_at=batch.created_at,
        )

    def cancel_batch(self, batch_id: str) -> RemoteBatch:
        batch = self._batches[batch_id]
        cancelled = RemoteBatch(**{**batch.__dict__, "status": "cancelled"})
        self._batches[batch_id] = cancelled
        return cancelled

    def download_file(self, file_id: str, destination: Path) -> None:
        destination.write_bytes(self._files[file_id])

    def delete_file(self, file_id: str) -> None:
        self.deleted_file_ids.append(file_id)
        if not self.retain_deleted_files:
            self._files.pop(file_id, None)
            self._file_paths.pop(file_id, None)

    def list_batches(self, *, limit: int = 100) -> list[RemoteBatch]:
        return list(self._batches.values())[-limit:]

    def parse_batch_line(self, value: dict[str, Any]) -> BatchLineResult:
        custom_id = value["custom_id"]
        response = value.get("response")
        if response is None:
            return BatchLineResult(
                custom_id=custom_id,
                ok=False,
                provider=self.name,
                error=self.failure_message,
            )
        body = response["body"]
        usage = body["usage"]
        details = usage["input_tokens_details"]
        return BatchLineResult(
            custom_id=custom_id,
            ok=True,
            text=body["output"][0]["content"][0]["text"],
            provider=self.name,
            model=body["model"],
            prompt_tokens=usage["input_tokens"],
            cached_tokens=details["cached_tokens"],
            cache_write_tokens=details["cache_write_tokens"],
            completion_tokens=usage["output_tokens"],
            cost_usd=self.cost_usd,
            request_id=response["request_id"],
        )

    def _next_file_id(self) -> str:
        return f"file-{len(self._files) + 1}"

    def _store_jsonl(self, lines: list[dict[str, Any]]) -> str:
        identifier = self._next_file_id()
        self._files[identifier] = (
            "\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n"
        ).encode()
        return identifier
