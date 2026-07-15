"""OpenAI-compatible LLM provider adapter.

The optional ``openai`` SDK is imported only when a real client is first
needed, so importing Paper Pipeline never requires the ``llm`` extra.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from paper_pipeline.config import AppConfig
from paper_pipeline.recipes.provider import ProviderRequest, ProviderResult


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
            content = self._content(client, request)
            response = client.responses.create(
                model=model,
                input=[{"role": "user", "content": content}],
            )
            return ProviderResult(
                ok=True,
                text=getattr(response, "output_text", "") or "",
                provider=self.name,
                model=model,
            )
        except ModuleNotFoundError:
            return self._failure(model, "OpenAI provider unavailable; install the 'llm' extra")
        except Exception:
            return self._failure(
                model,
                "OpenAI request failed; check credentials, model, endpoint, and network",
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

    def _content(self, client: Any, request: ProviderRequest) -> list[dict[str, str]]:
        if request.text_input is not None:
            return [
                {"type": "input_text", "text": request.text_input},
                {"type": "input_text", "text": request.prompt},
            ]

        pdf_path = request.pdf_input
        assert pdf_path is not None
        file_id = self._uploaded_file_id(client, pdf_path, request.input_sha256)
        return [
            {"type": "input_file", "file_id": file_id},
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
