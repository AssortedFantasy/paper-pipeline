"""OpenAI-compatible LLM provider adapter with usage and spend accounting."""

from __future__ import annotations

import hashlib
import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paper_pipeline.config import AppConfig
from paper_pipeline.recipes.provider import (
    BatchLineResult,
    RemoteBatch,
    RemoteFile,
)

# Standard-processing USD per million tokens. GPT-5.6 cache writes are billed
# at 1.25x ordinary input; cached reads retain the 90% discount. Snapshot
# suffixes inherit their family's rates. Keep this deliberately small and fail
# visibly for unknown pricing instead of silently reporting a false zero cost.
_PRICING: tuple[tuple[str, float, float, float, float], ...] = (
    ("gpt-5.6-sol", 5.0, 0.5, 30.0, 1.25),
    ("gpt-5.6-terra", 2.5, 0.25, 15.0, 1.25),
    ("gpt-5.6-luna", 1.0, 0.1, 6.0, 1.25),
    ("gpt-5.6", 5.0, 0.5, 30.0, 1.25),
    ("gpt-5.5", 5.0, 0.5, 30.0, 1.0),
    ("gpt-5.4", 2.5, 0.25, 15.0, 1.0),
    ("gpt-5", 1.25, 0.125, 10.0, 1.0),
)


class OpenAIProvider:
    """OpenAI Batch API adapter."""

    name = "openai"

    def __init__(self, config: AppConfig, *, client: Any | None = None) -> None:
        self._config = config
        self._client = client

    def request_body(
        self,
        *,
        prompt: str,
        model: str,
        input_sha256: str,
        file_id: str | None = None,
        text_input: str | None = None,
    ) -> dict[str, Any]:
        """Build one stateless Responses request for a Batch JSONL line."""
        if not model:
            raise ValueError("OpenAI model is not configured")
        if (file_id is None) == (text_input is None):
            raise ValueError("batch request must contain exactly one input")
        breakpoint = (
            {"prompt_cache_breakpoint": {"mode": "explicit"}}
            if _uses_explicit_cache_breakpoints(model)
            else {}
        )
        stable = (
            {"type": "input_file", "file_id": file_id, **breakpoint}
            if file_id is not None
            else {"type": "input_text", "text": text_input, **breakpoint}
        )
        body: dict[str, Any] = {
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
        if input_sha256:
            body["prompt_cache_key"] = _cache_key_for_hash(input_sha256)
        if _uses_explicit_cache_breakpoints(model):
            body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
        return body

    def upload_input(self, path: Path, *, expires_after_seconds: int) -> RemoteFile:
        if expires_after_seconds < 3600:
            raise ValueError("OpenAI input expiry must be at least one hour")
        try:
            client = self._get_client()
            with path.open("rb") as source:
                uploaded = client.files.create(
                    file=source,
                    purpose="user_data",
                    expires_after={
                        "anchor": "created_at",
                        "seconds": expires_after_seconds,
                    },
                )
            return RemoteFile(_required_id(uploaded, "OpenAI input upload"))
        except Exception as error:
            raise RuntimeError(_safe_request_failure(error)) from None

    def upload_batch_file(self, path: Path) -> RemoteFile:
        try:
            client = self._get_client()
            with path.open("rb") as source:
                uploaded = client.files.create(file=source, purpose="batch")
            return RemoteFile(_required_id(uploaded, "OpenAI Batch input upload"))
        except Exception as error:
            raise RuntimeError(_safe_request_failure(error)) from None

    def create_batch(
        self,
        *,
        input_file_id: str,
        endpoint: str,
        metadata: dict[str, str],
    ) -> RemoteBatch:
        client = self._get_client()
        # Batch creation has no documented idempotency guarantee. Disable SDK
        # retries so one ambiguous transport failure cannot silently issue a
        # second paid submission.
        submit_client = (
            client.with_options(max_retries=0) if hasattr(client, "with_options") else client
        )
        try:
            batch = submit_client.batches.create(
                input_file_id=input_file_id,
                endpoint=endpoint,
                completion_window="24h",
                metadata=metadata,
            )
            return _remote_batch(batch)
        except Exception as error:
            raise RuntimeError(_safe_request_failure(error)) from None

    def retrieve_batch(self, batch_id: str) -> RemoteBatch:
        try:
            return _remote_batch(self._get_client().batches.retrieve(batch_id))
        except Exception as error:
            raise RuntimeError(_safe_request_failure(error)) from None

    def cancel_batch(self, batch_id: str) -> RemoteBatch:
        try:
            return _remote_batch(self._get_client().batches.cancel(batch_id))
        except Exception as error:
            raise RuntimeError(_safe_request_failure(error)) from None

    def download_file(self, file_id: str, destination: Path) -> None:
        try:
            response = self._get_client().files.content(file_id)
            if hasattr(response, "write_to_file"):
                response.write_to_file(destination)
                return
            content = getattr(response, "content", None)
            if isinstance(content, bytes):
                destination.write_bytes(content)
                return
            text = getattr(response, "text", None)
            if isinstance(text, str):
                destination.write_text(text, encoding="utf-8", newline="\n")
                return
            raise ValueError("OpenAI file download returned unsupported content")
        except Exception as error:
            raise RuntimeError(_safe_request_failure(error)) from None

    def delete_file(self, file_id: str) -> None:
        try:
            self._get_client().files.delete(file_id)
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise RuntimeError(_safe_request_failure(error)) from None

    def list_batches(self, *, limit: int = 100) -> list[RemoteBatch]:
        try:
            page = self._get_client().batches.list(limit=limit)
            data = getattr(page, "data", page)
            return [_remote_batch(item) for item in data]
        except Exception as error:
            raise RuntimeError(_safe_request_failure(error)) from None

    def parse_batch_line(self, value: dict[str, Any]) -> BatchLineResult:
        custom_id = value.get("custom_id")
        if not isinstance(custom_id, str) or not custom_id:
            raise ValueError("OpenAI Batch result contained no custom_id")
        response = value.get("response")
        error = value.get("error")
        if not isinstance(response, dict):
            return BatchLineResult(
                custom_id=custom_id,
                ok=False,
                provider=self.name,
                error=_safe_batch_error(error),
            )
        status_code = response.get("status_code")
        body = response.get("body")
        if status_code != 200 or not isinstance(body, dict) or error:
            return BatchLineResult(
                custom_id=custom_id,
                ok=False,
                provider=self.name,
                request_id=_safe_diagnostic_value(response.get("request_id")),
                error=_safe_batch_error(error, status_code=status_code),
            )
        if body.get("status") not in (None, "completed"):
            return BatchLineResult(
                custom_id=custom_id,
                ok=False,
                provider=self.name,
                request_id=_safe_diagnostic_value(response.get("request_id")),
                error=f"OpenAI response ended with status {body.get('status')!r}",
            )
        text = _batch_output_text(body)
        if not text:
            return BatchLineResult(
                custom_id=custom_id,
                ok=False,
                provider=self.name,
                request_id=_safe_diagnostic_value(response.get("request_id")),
                error="OpenAI response contained no output text",
            )
        usage = body.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("OpenAI Batch response contained no token usage")
        details = usage.get("input_tokens_details") or {}
        prompt = _nonnegative_int(usage.get("input_tokens"), "input tokens")
        cached = _nonnegative_int(details.get("cached_tokens", 0), "cached tokens")
        cache_write = _nonnegative_int(
            details.get("cache_write_tokens", 0),
            "cache write tokens",
        )
        if cached + cache_write > prompt:
            raise ValueError("OpenAI Batch response reported invalid input token details")
        completion = _nonnegative_int(usage.get("output_tokens"), "output tokens")
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("OpenAI Batch response contained no model")
        return BatchLineResult(
            custom_id=custom_id,
            ok=True,
            text=text,
            provider=self.name,
            model=model,
            prompt_tokens=prompt,
            cached_tokens=cached,
            cache_write_tokens=cache_write,
            completion_tokens=completion,
            cost_usd=_cost_usd(
                model,
                prompt_tokens=prompt,
                cached_tokens=cached,
                cache_write_tokens=cache_write,
                completion_tokens=completion,
                batch=True,
            ),
            request_id=_safe_diagnostic_value(response.get("request_id")),
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._config.llm_api_key:
            raise ValueError("OpenAI API key is not configured")

        openai = importlib.import_module("openai")
        kwargs: dict[str, str] = {"api_key": self._config.llm_api_key}
        if self._config.llm_base_url:
            kwargs["base_url"] = self._config.llm_base_url
        self._client = openai.OpenAI(**kwargs)
        return self._client


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"OpenAI response contained invalid {label}")
    return value


def _cost_usd(
    model: str,
    *,
    prompt_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    completion_tokens: int,
    batch: bool = False,
) -> float:
    normalized = model.casefold()
    pricing = next(
        (
            rates
            for prefix, *rates in _PRICING
            if normalized == prefix or normalized.startswith(prefix + "-")
        ),
        None,
    )
    if pricing is None:
        raise ValueError(f"OpenAI pricing is not known for model {model!r}")
    input_rate, cached_rate, output_rate, cache_write_multiplier = pricing
    ordinary_tokens = prompt_tokens - cached_tokens - cache_write_tokens
    long_context = normalized.startswith("gpt-5.6") and prompt_tokens > 272_000
    input_multiplier = 2.0 if long_context else 1.0
    output_multiplier = 1.5 if long_context else 1.0
    total = (
        ordinary_tokens * input_rate * input_multiplier
        + cache_write_tokens * input_rate * cache_write_multiplier * input_multiplier
        + cached_tokens * cached_rate * input_multiplier
        + completion_tokens * output_rate * output_multiplier
    ) / 1_000_000
    if batch:
        total *= 0.5
    return round(total, 10)


def _cache_key_for_hash(input_sha256: str) -> str:
    return hashlib.sha256(f"paper-pipeline:{input_sha256}".encode()).hexdigest()


def _required_id(value: Any, label: str) -> str:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, str) or not identifier:
        raise ValueError(f"{label} returned no ID")
    return identifier


def _remote_batch(value: Any) -> RemoteBatch:
    counts = getattr(value, "request_counts", None)
    created_at = getattr(value, "created_at", None)
    return RemoteBatch(
        id=_required_id(value, "OpenAI Batch"),
        status=str(getattr(value, "status", "")),
        input_file_id=str(getattr(value, "input_file_id", "")),
        output_file_id=_optional_string(getattr(value, "output_file_id", None)),
        error_file_id=_optional_string(getattr(value, "error_file_id", None)),
        total=_optional_nonnegative_int(getattr(counts, "total", 0)),
        completed=_optional_nonnegative_int(getattr(counts, "completed", 0)),
        failed=_optional_nonnegative_int(getattr(counts, "failed", 0)),
        created_at=(
            datetime.fromtimestamp(created_at, tz=UTC)
            if isinstance(created_at, int) and not isinstance(created_at, bool)
            else None
        ),
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _batch_output_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in body.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if (
                isinstance(content, dict)
                and content.get("type") == "output_text"
                and isinstance(content.get("text"), str)
            ):
                parts.append(content["text"])
    return "\n".join(parts).strip()


def _safe_batch_error(value: Any, *, status_code: Any = None) -> str:
    if status_code == 429:
        return "OpenAI rate limit or queued-token quota was exceeded"
    if isinstance(status_code, int) and status_code >= 500:
        return "OpenAI service failed this Batch request"
    if isinstance(value, dict):
        code = _safe_diagnostic_value(value.get("code"))
        if code:
            return f"OpenAI Batch request failed ({code})"
    return "OpenAI Batch request failed"


def _uses_explicit_cache_breakpoints(model: str) -> bool:
    return model.casefold().startswith("gpt-5.6")


def _safe_request_failure(error: Exception) -> str:
    """Describe an SDK failure without copying request or response content."""

    status = getattr(error, "status_code", None)
    error_name = type(error).__name__
    if status == 400:
        message = "OpenAI rejected the request (check model parameters and input limits)"
    elif status == 401:
        message = "OpenAI authentication failed"
    elif status == 403:
        message = "OpenAI denied access to the requested model or resource"
    elif status == 404:
        message = "OpenAI model or endpoint was not found"
    elif status == 408 or error_name == "APITimeoutError":
        message = "OpenAI request timed out"
    elif status == 409:
        message = "OpenAI request conflicted with current resource state"
    elif status == 422:
        message = "OpenAI could not process the request parameters"
    elif status == 429:
        message = "OpenAI rate limit or quota was exceeded"
    elif isinstance(status, int) and status >= 500:
        message = "OpenAI service failed the request"
    elif error_name == "APIConnectionError":
        message = "OpenAI connection failed"
    else:
        message = "OpenAI request failed; check credentials, model, endpoint, and network"

    diagnostics: list[str] = []
    if isinstance(status, int) and 100 <= status <= 599:
        diagnostics.append(f"HTTP {status}")
    request_id = _safe_diagnostic_value(getattr(error, "request_id", None))
    if request_id:
        diagnostics.append(f"request_id={request_id}")
    if diagnostics:
        return f"{message} ({', '.join(diagnostics)})"
    return message


def _safe_diagnostic_value(value: Any) -> str:
    """Allow only the conservative character set used by request IDs."""

    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return ""
    allowed = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:")
    return value if all(character in allowed for character in value) else ""
