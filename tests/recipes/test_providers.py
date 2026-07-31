from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from paper_pipeline.config import AppConfig
from paper_pipeline.recipes.openai_provider import OpenAIProvider


def config(**overrides: str | None) -> AppConfig:
    values: dict[str, Any] = {
        "llm_api_key": "test-secret-key",
        "llm_model": "gpt-5.6-luna",
    }
    values.update(overrides)
    return AppConfig.model_construct(**values)


@pytest.mark.parametrize(
    ("input_args", "expected_stable"),
    [
        (
            {"text_input": "Transcription"},
            {"type": "input_text", "text": "Transcription"},
        ),
        (
            {"file_id": "file-pdf"},
            {"type": "input_file", "file_id": "file-pdf"},
        ),
    ],
)
def test_batch_request_keeps_input_cacheable_and_prompt_variable(
    input_args: dict[str, str], expected_stable: dict[str, str]
) -> None:
    body = OpenAIProvider(config()).request_body(
        prompt="Recipe prompt",
        model="gpt-5.6-luna",
        input_sha256="paper-hash",
        **input_args,
    )

    content = body["input"][0]["content"]
    assert content[0] == {
        **expected_stable,
        "prompt_cache_breakpoint": {"mode": "explicit"},
    }
    assert content[1] == {"type": "input_text", "text": "Recipe prompt"}
    assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert body["store"] is False
    assert "paper-hash" not in body["prompt_cache_key"]


@pytest.mark.parametrize(
    "input_args",
    [{}, {"file_id": "file-pdf", "text_input": "Transcription"}],
    ids=["no-input", "two-inputs"],
)
def test_batch_request_requires_exactly_one_input(input_args: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="exactly one input"):
        OpenAIProvider(config()).request_body(
            prompt="Prompt",
            model="gpt-5.6-luna",
            input_sha256="hash",
            **input_args,
        )


def test_batch_result_parser_records_usage_at_batch_pricing() -> None:
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

    result = OpenAIProvider(config()).parse_batch_line(line)

    assert result.ok
    assert result.text == "Generated"
    assert result.request_id == "req_test"
    assert result.cost_usd == 0.000465


def test_batch_result_rejects_inconsistent_token_details() -> None:
    line = {
        "custom_id": "r000001",
        "response": {
            "status_code": 200,
            "body": {
                "model": "gpt-5.6-luna",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Generated"}],
                    }
                ],
                "usage": {
                    "input_tokens": 1,
                    "input_tokens_details": {
                        "cached_tokens": 1,
                        "cache_write_tokens": 1,
                    },
                    "output_tokens": 1,
                },
            },
        },
    }

    with pytest.raises(ValueError, match="invalid input token details"):
        OpenAIProvider(config()).parse_batch_line(line)


def test_batch_creation_disables_sdk_retries() -> None:
    batch = SimpleNamespace(
        id="batch-1",
        status="in_progress",
        input_file_id="file-1",
        request_counts=SimpleNamespace(total=1, completed=0, failed=0),
    )

    class Client:
        def __init__(self) -> None:
            self.retry_values: list[int] = []
            self.batches = SimpleNamespace(create=lambda **_kwargs: batch)

        def with_options(self, *, max_retries: int) -> Client:
            self.retry_values.append(max_retries)
            return self

    client = Client()

    result = OpenAIProvider(config(), client=client).create_batch(
        input_file_id="file-1",
        endpoint="/v1/responses",
        metadata={"run_id": "run-1"},
    )

    assert result.id == "batch-1"
    assert client.retry_values == [0]


def test_input_upload_has_a_server_side_expiry(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF")
    calls: list[dict[str, Any]] = []
    client = SimpleNamespace(
        files=SimpleNamespace(
            create=lambda **kwargs: (calls.append(kwargs), SimpleNamespace(id="file-1"))[1]
        )
    )

    result = OpenAIProvider(config(), client=client).upload_input(
        source, expires_after_seconds=3600
    )

    assert result.id == "file-1"
    assert calls[0]["purpose"] == "user_data"
    assert calls[0]["expires_after"] == {"anchor": "created_at", "seconds": 3600}
