from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Request, Response
from openai import APIConnectionError, APITimeoutError, BadRequestError, RateLimitError

from paper_pipeline.config import AppConfig
from paper_pipeline.recipes.openai_provider import OpenAIProvider
from paper_pipeline.recipes.provider import ProviderRequest
from tests.fakes import FakeLLMProvider


class RecordingFiles:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(id="file-test")


class RecordingResponses:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            output_text="Generated response",
            usage=SimpleNamespace(
                input_tokens=1_000,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=800,
                    cache_write_tokens=200,
                ),
                output_tokens=100,
            ),
        )


class RecordingClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.files = RecordingFiles()
        self.responses = RecordingResponses(error=error)


def config(**overrides: str | None) -> AppConfig:
    values: dict[str, Any] = {
        "llm_api_key": "test-secret-key",
        "llm_model": "gpt-5.6-luna",
    }
    values.update(overrides)
    return AppConfig.model_construct(**values)


def test_fake_provider_returns_canned_response_and_records_calls() -> None:
    provider = FakeLLMProvider(response="Canned result")
    request = ProviderRequest(prompt="Summarize", text_input="Paper", model="test-model")

    result = provider.generate(request)

    assert result.ok
    assert result.text == "Canned result"
    assert result.provider == "fake"
    assert result.model == "test-model"
    assert provider.calls == [request]


def test_fake_provider_failure_and_delay() -> None:
    provider = FakeLLMProvider(fail=True, delay_seconds=0.01)
    request = ProviderRequest(prompt="Summarize", text_input="Paper", model="test-model")

    started = time.perf_counter()
    result = provider.generate(request)

    assert time.perf_counter() - started >= 0.009
    assert not result.ok
    assert result.error == "fake provider failure"
    assert provider.calls == [request]


def test_openai_text_request_uses_responses_api() -> None:
    client = RecordingClient()
    provider = OpenAIProvider(config(), client=client)

    result = provider.generate(
        ProviderRequest(prompt="Recipe prompt", text_input="Transcription", input_sha256="abc")
    )

    assert result.ok
    assert result.text == "Generated response"
    assert result.prompt_tokens == 1_000
    assert result.cached_tokens == 800
    assert result.cache_write_tokens == 200
    assert result.completion_tokens == 100
    # Luna: 200 cache-write tokens at 1.25x $1/MTok, 800 cached at
    # $0.10/MTok, and 100 output at $6/MTok.
    assert result.cost_usd == 0.00093
    assert client.responses.calls == [
        {
            "model": "gpt-5.6-luna",
            "prompt_cache_key": hashlib.sha256(b"paper-pipeline:abc").hexdigest(),
            "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Transcription",
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        },
                        {"type": "input_text", "text": "Recipe prompt"},
                    ],
                }
            ],
        }
    ]
    assert client.files.calls == []


def test_openai_pdf_upload_is_cached_by_input_hash(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    client = RecordingClient()
    provider = OpenAIProvider(config(), client=client)

    first = provider.generate(
        ProviderRequest(prompt="Summary", pdf_input=pdf, input_sha256="same-hash")
    )
    second = provider.generate(
        ProviderRequest(prompt="Contributions", pdf_input=pdf, input_sha256="same-hash")
    )

    assert first.ok and second.ok
    assert len(client.files.calls) == 1
    upload = client.files.calls[0]
    assert upload["purpose"] == "user_data"
    assert client.responses.calls[0]["input"][0]["content"] == [
        {
            "type": "input_file",
            "file_id": "file-test",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        },
        {"type": "input_text", "text": "Summary"},
    ]
    assert (
        client.responses.calls[0]["prompt_cache_key"]
        == hashlib.sha256(b"paper-pipeline:same-hash").hexdigest()
    )
    assert client.responses.calls[1]["input"][0]["content"][0] == {
        "type": "input_file",
        "file_id": "file-test",
        "prompt_cache_breakpoint": {"mode": "explicit"},
    }


def test_openai_request_model_overrides_configured_model() -> None:
    client = RecordingClient()
    provider = OpenAIProvider(config(), client=client)

    result = provider.generate(
        ProviderRequest(prompt="Prompt", text_input="Input", model="request-model")
    )

    assert result.model == "request-model"
    assert client.responses.calls[0]["model"] == "request-model"


@pytest.mark.parametrize(
    "provider_request",
    [
        ProviderRequest(prompt="Prompt"),
        ProviderRequest(prompt="Prompt", text_input="Text", pdf_input=Path("paper.pdf")),
    ],
)
def test_openai_rejects_requests_without_exactly_one_input(
    provider_request: ProviderRequest,
) -> None:
    client = RecordingClient()

    result = OpenAIProvider(config(), client=client).generate(provider_request)

    assert not result.ok
    assert "exactly one input" in (result.error or "")
    assert client.responses.calls == []


def test_openai_requires_a_model() -> None:
    result = OpenAIProvider(config(llm_model=None), client=RecordingClient()).generate(
        ProviderRequest(prompt="Prompt", text_input="Input")
    )

    assert not result.ok
    assert result.error == "OpenAI model is not configured"


def test_openai_requires_an_api_key_for_a_real_client() -> None:
    result = OpenAIProvider(config(llm_api_key=None)).generate(
        ProviderRequest(prompt="Prompt", text_input="Input")
    )

    assert not result.ok
    assert result.error == "OpenAI API key is not configured"


def test_openai_errors_are_safe() -> None:
    secret = "super-secret-api-key"
    client = RecordingClient(error=RuntimeError(f"request with {secret} failed"))
    request = ProviderRequest(prompt="full private prompt", text_input="private paper")

    result = OpenAIProvider(config(llm_api_key=secret), client=client).generate(request)

    assert not result.ok
    assert result.error is not None
    assert secret not in result.error
    assert request.prompt not in result.error
    assert (request.text_input or "") not in result.error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RateLimitError(
                "private response body",
                response=Response(
                    429,
                    request=Request("POST", "https://api.openai.com/v1/responses"),
                    headers={"x-request-id": "req_safe-123"},
                ),
                body={"private": "response"},
            ),
            "OpenAI rate limit or quota was exceeded after automatic retries "
            "(HTTP 429, request_id=req_safe-123)",
        ),
        (
            BadRequestError(
                "private input excerpt",
                response=Response(
                    400,
                    request=Request("POST", "https://api.openai.com/v1/responses"),
                ),
                body={"private": "response"},
            ),
            "OpenAI rejected the request (check model parameters and input limits) (HTTP 400)",
        ),
        (
            APITimeoutError(Request("POST", "https://api.openai.com/v1/responses")),
            "OpenAI request timed out",
        ),
        (
            APIConnectionError(request=Request("POST", "https://api.openai.com/v1/responses")),
            "OpenAI connection failed after automatic retries",
        ),
    ],
)
def test_openai_errors_include_safe_actionable_diagnostics(error: Exception, expected: str) -> None:
    client = RecordingClient(error=error)

    result = OpenAIProvider(config(), client=client).generate(
        ProviderRequest(prompt="private prompt", text_input="private paper")
    )

    assert result.error == expected
    assert result.error is not None
    assert "private" not in result.error


def test_openai_client_creation_is_lazy_and_respects_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, str]] = []
    client = RecordingClient()

    class OpenAIModule:
        @staticmethod
        def OpenAI(**kwargs: str) -> RecordingClient:
            created.append(kwargs)
            return client

    def fake_import(name: str) -> OpenAIModule:
        assert name == "openai"
        return OpenAIModule()

    monkeypatch.setattr("importlib.import_module", fake_import)
    provider = OpenAIProvider(config(llm_base_url="https://compatible.example/v1"))
    assert created == []

    result = provider.generate(ProviderRequest(prompt="Prompt", text_input="Input"))

    assert result.ok
    assert created == [
        {
            "api_key": "test-secret-key",
            "base_url": "https://compatible.example/v1",
        }
    ]


def test_openai_missing_optional_sdk_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_openai(name: str) -> None:
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr("importlib.import_module", missing_openai)

    result = OpenAIProvider(config()).generate(ProviderRequest(prompt="Prompt", text_input="Input"))

    assert not result.ok
    assert result.error == "OpenAI provider unavailable; reinstall Paper Pipeline"


def test_openai_rejects_missing_usage_and_unknown_pricing() -> None:
    missing_usage = RecordingClient()
    missing_usage.responses.create = lambda **_kwargs: SimpleNamespace(output_text="text")
    result = OpenAIProvider(config(), client=missing_usage).generate(
        ProviderRequest(prompt="Prompt", text_input="Input")
    )
    assert not result.ok
    assert result.error == "OpenAI response contained no token usage"

    unknown = OpenAIProvider(config(llm_model="compatible-model"), client=RecordingClient())
    result = unknown.generate(ProviderRequest(prompt="Prompt", text_input="Input"))
    assert not result.ok
    assert result.error == "OpenAI pricing is not known for model 'compatible-model'"


def test_gpt_56_cost_includes_long_context_and_cache_write_multipliers() -> None:
    client = RecordingClient()
    client.responses.create = lambda **_kwargs: SimpleNamespace(
        output_text="text",
        usage=SimpleNamespace(
            input_tokens=300_000,
            input_tokens_details=SimpleNamespace(
                cached_tokens=200_000,
                cache_write_tokens=100_000,
            ),
            output_tokens=1_000,
        ),
    )

    result = OpenAIProvider(config(), client=client).generate(
        ProviderRequest(prompt="Prompt", text_input="Input")
    )

    # Luna long-context request: writes 100k * $1 * 1.25 * 2,
    # reads 200k * $0.10 * 2, output 1k * $6 * 1.5.
    assert result.ok
    assert result.cost_usd == 0.299


@pytest.mark.llm
def test_real_openai_text_smoke() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY is not set")
    provider = OpenAIProvider(AppConfig(llm_api_key=api_key, llm_model="gpt-5.6-luna"))

    result = provider.generate(
        ProviderRequest(
            prompt="Reply with exactly: provider smoke ok",
            text_input="This is a connectivity smoke test.",
        )
    )

    assert result.ok, result.error
    assert result.text.strip()


@pytest.mark.llm
@pytest.mark.skipif(
    os.environ.get("PAPER_PIPELINE_CACHE_TEST") != "1",
    reason="set PAPER_PIPELINE_CACHE_TEST=1 to spend a small amount verifying real caching",
)
def test_real_openai_reuses_explicit_transcription_cache() -> None:
    config = AppConfig()
    assert config.llm_api_key and config.llm_model
    text = "Stable academic transcription sentence for cache verification.\n" * 600
    input_hash = hashlib.sha256(text.encode()).hexdigest()
    provider = OpenAIProvider(config)

    first = provider.generate(
        ProviderRequest(
            prompt="Reply with exactly: first cache check",
            text_input=text,
            input_sha256=input_hash,
        )
    )
    second = provider.generate(
        ProviderRequest(
            prompt="Reply with exactly: second cache check",
            text_input=text,
            input_sha256=input_hash,
        )
    )

    assert first.ok, first.error
    assert second.ok, second.error
    assert first.cache_write_tokens > 0
    assert second.cached_tokens > 0
