"""OpenAI-compatible LLM provider adapter with usage and spend accounting."""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Any

from paper_pipeline.config import AppConfig
from paper_pipeline.recipes.provider import ProviderRequest, ProviderResult

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
    """Generate recipe output with the Responses API."""

    name = "openai"

    def __init__(self, config: AppConfig, *, client: Any | None = None) -> None:
        self._config = config
        self._client = client
        self._pdf_file_ids: dict[str, str] = {}

    def generate(self, request: ProviderRequest) -> ProviderResult:
        """Generate text, returning a safe result for ordinary failures."""

        model = request.model or self._config.llm_model or ""
        if not model:
            return self._failure(model, "OpenAI model is not configured")
        if self._client is None and not self._config.llm_api_key:
            return self._failure(model, "OpenAI API key is not configured")
        if (request.text_input is None) == (request.pdf_input is None):
            return self._failure(model, "provider request must contain exactly one input")

        try:
            client = self._get_client()
            content = self._content(client, request, model)
            cache_key = _cache_key(request)
            response_args: dict[str, Any] = {
                "model": model,
                "input": [{"role": "user", "content": content}],
            }
            if cache_key:
                response_args["prompt_cache_key"] = cache_key
            if _uses_explicit_cache_breakpoints(model):
                response_args["prompt_cache_options"] = {
                    "mode": "implicit",
                    "ttl": "30m",
                }
            response = client.responses.create(
                **response_args,
            )
            prompt_tokens, cached_tokens, cache_write_tokens, completion_tokens = _usage(response)
            cost_usd = _cost_usd(
                model,
                prompt_tokens=prompt_tokens,
                cached_tokens=cached_tokens,
                cache_write_tokens=cache_write_tokens,
                completion_tokens=completion_tokens,
            )
            return ProviderResult(
                ok=True,
                text=getattr(response, "output_text", "") or "",
                provider=self.name,
                model=model,
                prompt_tokens=prompt_tokens,
                cached_tokens=cached_tokens,
                cache_write_tokens=cache_write_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
            )
        except ModuleNotFoundError:
            return self._failure(model, "OpenAI provider unavailable; reinstall Paper Pipeline")
        except ValueError as error:
            return self._failure(model, str(error))
        except Exception as error:
            return self._failure(model, _safe_request_failure(error))

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

    def _content(self, client: Any, request: ProviderRequest, model: str) -> list[dict[str, Any]]:
        breakpoint = (
            {"prompt_cache_breakpoint": {"mode": "explicit"}}
            if _uses_explicit_cache_breakpoints(model)
            else {}
        )
        if request.text_input is not None:
            return [
                {"type": "input_text", "text": request.text_input, **breakpoint},
                {"type": "input_text", "text": request.prompt},
            ]

        pdf_path = request.pdf_input
        assert pdf_path is not None
        file_id = self._uploaded_file_id(client, pdf_path, request.input_sha256)
        return [
            {"type": "input_file", "file_id": file_id, **breakpoint},
            {"type": "input_text", "text": request.prompt},
        ]

    def _uploaded_file_id(self, client: Any, pdf_path: Path, input_sha256: str) -> str:
        if input_sha256 and input_sha256 in self._pdf_file_ids:
            return self._pdf_file_ids[input_sha256]

        with pdf_path.open("rb") as pdf_file:
            uploaded = client.files.create(file=pdf_file, purpose="user_data")
        file_id = getattr(uploaded, "id", None)
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("OpenAI file upload returned no file ID")
        if input_sha256:
            self._pdf_file_ids[input_sha256] = file_id
        return file_id

    def _failure(self, model: str, message: str) -> ProviderResult:
        return ProviderResult(
            ok=False,
            provider=self.name,
            model=model,
            error=message,
        )


def _usage(response: Any) -> tuple[int, int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        raise ValueError("OpenAI response contained no token usage")
    prompt = _nonnegative_int(getattr(usage, "input_tokens", None), "input tokens")
    completion = _nonnegative_int(getattr(usage, "output_tokens", None), "output tokens")
    details = getattr(usage, "input_tokens_details", None)
    cached = _nonnegative_int(getattr(details, "cached_tokens", 0), "cached tokens")
    cache_write = _nonnegative_int(getattr(details, "cache_write_tokens", 0), "cache write tokens")
    if cached + cache_write > prompt:
        raise ValueError("OpenAI response reported invalid input token details")
    return prompt, cached, cache_write, completion


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
    return round(total, 10)


def _cache_key(request: ProviderRequest) -> str:
    """Return a stable, non-content cache-routing key for one paper input."""

    if not request.input_sha256:
        return ""
    return hashlib.sha256(f"paper-pipeline:{request.input_sha256}".encode()).hexdigest()


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
        message = "OpenAI rate limit or quota was exceeded after automatic retries"
    elif isinstance(status, int) and status >= 500:
        message = "OpenAI service failed after automatic retries"
    elif error_name == "APIConnectionError":
        message = "OpenAI connection failed after automatic retries"
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
