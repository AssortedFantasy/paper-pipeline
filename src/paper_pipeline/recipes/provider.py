"""Versioned LLM provider contract.

The default dev loop and test suite use a fake provider; the OpenAI-compatible
adapter requires the ``llm`` extra and real credentials, and is only
exercised by tests marked ``llm``.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ProviderRequest:
    """One recipe invocation against one paper input."""

    prompt: str
    # Exactly one of text_input / pdf_input is set, per the recipe's declared input.
    text_input: str | None = None
    pdf_input: Path | None = None
    # Hash of the selected PDF or transcription. Adapters may use it to reuse
    # uploaded files/provider context across one paper's sequential recipe batch.
    input_sha256: str = ""
    model: str = ""


@dataclass(frozen=True)
class ProviderResult:
    ok: bool
    text: str = ""
    provider: str = ""
    model: str = ""
    error: str | None = None


class LLMProvider(Protocol):
    """Implemented by each provider adapter (and the test fake)."""

    name: str

    def generate(self, request: ProviderRequest) -> ProviderResult:
        """Run one generation. Must not raise for ordinary failures; return ok=False."""
        ...
