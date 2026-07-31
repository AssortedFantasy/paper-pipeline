from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Request, Response
from openai import APITimeoutError, BadRequestError, RateLimitError

from paper_pipeline.config import AppConfig
from paper_pipeline.recipes.openai_provider import OpenAIProvider
from paper_pipeline.recipes.provider import ProviderRequest


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
        self.output_text = "Generated response"

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            output_text=self.output_text,
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


def test_text_generation_returns_usage_and_preserves_the_cacheable_prefix() -> None:
    client = RecordingClient()
    provider = OpenAIProvider(config(), client=client)

    result = provider.generate(
        ProviderRequest(
            prompt="Recipe prompt",
            text_input="Transcription",
            input_sha256="paper-hash",
        )
    )

    assert result.ok
    assert result.text == client.responses.output_text
    assert result.provider == "openai"
    assert result.model == "gpt-5.6-luna"
    assert (
        result.prompt_tokens,
        result.cached_tokens,
        result.cache_write_tokens,
        result.completion_tokens,
    ) == (1_000, 800, 200, 100)
    # Spend is a persisted product value, so its calculation is a contract:
    # 200 cache-write tokens at 1.25x $1/MTok, 800 cached at $0.10/MTok,
    # and 100 output at $6/MTok.
    assert result.cost_usd == 0.00093

    assert len(client.responses.calls) == 1
    sent = client.responses.calls[0]
    content = sent["input"][0]["content"]
    assert [item["text"] for item in content] == ["Transcription", "Recipe prompt"]
    assert "prompt_cache_breakpoint" in content[0]
    assert "prompt_cache_breakpoint" not in content[1]
    assert sent["prompt_cache_key"]
    assert "paper-hash" not in sent["prompt_cache_key"]
    assert "prompt_cache_options" in sent
    assert sent["prompt_cache_options"]["mode"] == "explicit"
    assert sent["store"] is False
    assert client.files.calls == []


def test_batch_request_uses_pdf_prefix_explicit_cache_and_no_response_storage() -> None:
    provider = OpenAIProvider(config(), client=RecordingClient())

    body = provider.request_body(
        prompt="Recipe prompt",
        model="gpt-5.6-luna",
        input_sha256="paper-hash",
        file_id="file-pdf",
    )

    content = body["input"][0]["content"]
    assert content[0] == {
        "type": "input_file",
        "file_id": "file-pdf",
        "prompt_cache_breakpoint": {"mode": "explicit"},
    }
    assert content[1] == {"type": "input_text", "text": "Recipe prompt"}
    assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert body["store"] is False
    assert "paper-hash" not in body["prompt_cache_key"]


def test_batch_result_parser_applies_batch_pricing() -> None:
    provider = OpenAIProvider(config(), client=RecordingClient())
    line = {
        "custom_id": "r000001",
        "response": {
            "status_code": 200,
            "request_id": "req_test",
            "body": {
                "status": "completed",
                "model": "gpt-5.6-luna",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Generated"}],
                    }
                ],
                "usage": {
                    "input_tokens": 1_000,
                    "input_tokens_details": {
                        "cached_tokens": 800,
                        "cache_write_tokens": 200,
                    },
                    "output_tokens": 100,
                },
            },
        },
        "error": None,
    }

    result = provider.parse_batch_line(line)

    assert result.ok
    assert result.text == "Generated"
    assert result.request_id == "req_test"
    assert result.cost_usd == 0.000465


def test_cache_routing_is_stable_per_input_and_separates_different_inputs() -> None:
    client = RecordingClient()
    provider = OpenAIProvider(config(), client=client)

    for input_hash, prompt in (
        ("same-paper", "Summary"),
        ("same-paper", "Contributions"),
        ("different-paper", "Summary"),
    ):
        result = provider.generate(
            ProviderRequest(
                prompt=prompt,
                text_input="Shared transcription",
                input_sha256=input_hash,
            )
        )
        assert result.ok

    cache_keys = [call["prompt_cache_key"] for call in client.responses.calls]
    assert cache_keys[0] == cache_keys[1]
    assert cache_keys[0] != cache_keys[2]


def test_pdf_upload_is_reused_for_a_sequential_recipe_batch(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    client = RecordingClient()
    provider = OpenAIProvider(config(), client=client)

    results = [
        provider.generate(ProviderRequest(prompt=prompt, pdf_input=pdf, input_sha256="same-paper"))
        for prompt in ("Summary", "Contributions")
    ]

    assert all(result.ok for result in results)
    assert len(client.files.calls) == 1
    assert client.files.calls[0]["purpose"] == "user_data"

    sent_content = [call["input"][0]["content"] for call in client.responses.calls]
    uploaded_inputs = [
        next(item for item in content if item["type"] == "input_file") for content in sent_content
    ]
    assert {item["file_id"] for item in uploaded_inputs} == {"file-test"}
    assert all("prompt_cache_breakpoint" in item for item in uploaded_inputs)
    assert [
        next(item["text"] for item in content if item["type"] == "input_text")
        for content in sent_content
    ] == ["Summary", "Contributions"]
    assert (
        client.responses.calls[0]["prompt_cache_key"]
        == client.responses.calls[1]["prompt_cache_key"]
    )


def test_request_model_takes_precedence_over_the_default() -> None:
    client = RecordingClient()

    result = OpenAIProvider(config(), client=client).generate(
        ProviderRequest(prompt="Prompt", text_input="Input", model="gpt-5.6-terra")
    )

    assert result.ok
    assert result.model == "gpt-5.6-terra"
    assert client.responses.calls[0]["model"] == "gpt-5.6-terra"


@pytest.mark.parametrize(
    "provider_request",
    [
        ProviderRequest(prompt="Prompt"),
        ProviderRequest(prompt="Prompt", text_input="Text", pdf_input=Path("paper.pdf")),
    ],
    ids=["no-input", "two-inputs"],
)
def test_invalid_input_selection_fails_without_contacting_the_provider(
    provider_request: ProviderRequest,
) -> None:
    client = RecordingClient()

    result = OpenAIProvider(config(), client=client).generate(provider_request)

    assert not result.ok
    assert "exactly one input" in (result.error or "")
    assert client.responses.calls == []
    assert client.files.calls == []


@pytest.mark.parametrize(
    ("overrides", "expected_problem"),
    [
        ({"llm_model": None}, "model"),
        ({"llm_api_key": None}, "API key"),
    ],
    ids=["missing-model", "missing-api-key"],
)
def test_missing_required_configuration_is_actionable_and_offline(
    overrides: dict[str, str | None], expected_problem: str
) -> None:
    result = OpenAIProvider(config(**overrides)).generate(
        ProviderRequest(prompt="Prompt", text_input="Input")
    )

    assert not result.ok
    assert "not configured" in (result.error or "")
    assert expected_problem in (result.error or "")


@pytest.mark.parametrize(
    ("error", "expected_fragments"),
    [
        (
            RuntimeError("request with super-secret-api-key and private response failed"),
            ("request failed",),
        ),
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
            ("rate limit", "HTTP 429", "request_id=req_safe-123"),
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
            ("rejected", "HTTP 400"),
        ),
        (
            APITimeoutError(Request("POST", "https://api.openai.com/v1/responses")),
            ("timed out",),
        ),
    ],
    ids=["unexpected", "rate-limit", "bad-request", "timeout"],
)
def test_failures_are_actionable_without_leaking_sensitive_content(
    error: Exception, expected_fragments: tuple[str, ...]
) -> None:
    client = RecordingClient(error=error)

    result = OpenAIProvider(config(), client=client).generate(
        ProviderRequest(prompt="private prompt", text_input="private paper")
    )

    assert not result.ok
    assert result.error is not None
    assert all(fragment in result.error for fragment in expected_fragments)
    assert "private" not in result.error
    assert "super-secret-api-key" not in result.error


def test_client_creation_is_lazy_reuses_the_client_and_honors_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, str]] = []
    client = RecordingClient()

    class OpenAIModule:
        @staticmethod
        def OpenAI(**kwargs: str) -> RecordingClient:
            created.append(kwargs)
            return client

    monkeypatch.setattr(
        "importlib.import_module",
        lambda _name: OpenAIModule(),
    )
    provider = OpenAIProvider(config(llm_base_url="https://compatible.example/v1"))
    assert created == []

    for prompt in ("First", "Second"):
        assert provider.generate(ProviderRequest(prompt=prompt, text_input="Input")).ok

    assert len(created) == 1
    assert created[0]["api_key"] == "test-secret-key"
    assert created[0]["base_url"] == "https://compatible.example/v1"


def test_missing_optional_sdk_returns_an_actionable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_openai(_name: str) -> None:
        raise ModuleNotFoundError(name="openai")

    monkeypatch.setattr("importlib.import_module", missing_openai)

    result = OpenAIProvider(config()).generate(ProviderRequest(prompt="Prompt", text_input="Input"))

    assert not result.ok
    assert "unavailable" in (result.error or "")
    assert "reinstall" in (result.error or "")


@pytest.mark.parametrize(
    ("usage", "expected_problem"),
    [
        (None, "no token usage"),
        (SimpleNamespace(input_tokens=-1, output_tokens=1), "input tokens"),
        (SimpleNamespace(input_tokens=1, output_tokens=True), "output tokens"),
        (
            SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=1,
                    cache_write_tokens=1,
                ),
            ),
            "invalid input token details",
        ),
    ],
    ids=["missing", "negative", "boolean", "details-exceed-total"],
)
def test_invalid_usage_is_not_recorded_as_a_success(
    usage: SimpleNamespace | None, expected_problem: str
) -> None:
    client = RecordingClient()
    response = SimpleNamespace(output_text="text")
    if usage is not None:
        response.usage = usage
    client.responses.create = lambda **_kwargs: response

    result = OpenAIProvider(config(), client=client).generate(
        ProviderRequest(prompt="Prompt", text_input="Input")
    )

    assert not result.ok
    assert expected_problem in (result.error or "")
    assert result.cost_usd == 0


def test_unknown_pricing_fails_instead_of_reporting_false_zero_spend() -> None:
    result = OpenAIProvider(
        config(llm_model="compatible-model"), client=RecordingClient()
    ).generate(ProviderRequest(prompt="Prompt", text_input="Input"))

    assert not result.ok
    assert "pricing is not known" in (result.error or "")
    assert "compatible-model" in (result.error or "")
    assert result.cost_usd == 0


def test_long_context_pricing_applies_input_and_output_multipliers() -> None:
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

    assert result.ok
    # Long-context Luna: writes 100k * $1 * 1.25 * 2,
    # reads 200k * $0.10 * 2, and outputs 1k * $6 * 1.5.
    assert result.cost_usd == 0.299
