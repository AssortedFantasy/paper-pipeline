from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import Library, create_library, sha256_file
from paper_pipeline.recipes.model import RecipeDefinition, RecipeInput
from paper_pipeline.recipes.runner import RecipeRunError, run_recipe
from tests.fakes import FakeLLMProvider


@pytest.fixture
def library(tmp_path: Path) -> Library:
    result = create_library(tmp_path / "library")
    result.write_paper(
        PaperRecord(
            format_version=FORMAT_VERSION,
            metadata=PaperMetadata(citekey="Smith2024", title="Test paper"),
            source_pdf="papers/Smith2024/source/paper.pdf",
        )
    )
    paper_root = result.root / "papers" / "Smith2024"
    (paper_root / "source").mkdir()
    (paper_root / "source" / "paper.pdf").write_bytes(b"%PDF-1.4 fake paper")
    (paper_root / "transcription.md").write_text(
        "# Paper\n\nTranscribed content.\n", encoding="utf-8"
    )
    return result


def summary_recipe(*, input_kind: RecipeInput = "transcription") -> RecipeDefinition:
    return RecipeDefinition(
        name="summary",
        version=2,
        input=input_kind,
        output="summary.md",
        prompt="Summarize this paper.",
    )


def test_success_stages_clean_output_and_records_provenance_usage(library: Library) -> None:
    provider = FakeLLMProvider(response="- Main result\n- Limitation")
    recipe = summary_recipe()

    result = run_recipe(library, "Smith2024", recipe, provider, model="test-model")

    transcription = library.root / "papers" / "Smith2024" / "transcription.md"
    input_hash = sha256_file(transcription)
    assert result.record.completed_at is not None
    expected = "- Main result\n- Limitation\n"
    assert result.staged_path.read_text(encoding="utf-8") == expected
    assert result.destination == "papers/Smith2024/summary.md"
    assert result.record.recipe_version == 2
    assert result.record.provider == "fake"
    assert result.record.model == "test-model"
    assert result.record.input_artifact == "papers/Smith2024/transcription.md"
    assert result.record.input_sha256 == input_hash
    assert result.record.output_artifact == "papers/Smith2024/summary.md"
    assert result.record.output_sha256 == hashlib.sha256(expected.encode()).hexdigest()
    assert result.record.prompt_tokens == 100
    assert result.record.cached_tokens == 0
    assert result.record.completion_tokens == 20
    assert result.record.cost_usd == 0.001
    assert provider.calls[0].text_input == transcription.read_text(encoding="utf-8")
    assert provider.calls[0].input_sha256 == input_hash

    installed_hash = library.install_artifact(result.staged_path, result.destination)
    assert installed_hash == result.record.output_sha256


def test_pdf_input_uses_source_path_and_hash(library: Library) -> None:
    provider = FakeLLMProvider()

    result = run_recipe(
        library,
        "Smith2024",
        summary_recipe(input_kind="pdf"),
        provider,
        model="test-model",
    )

    source = library.root / "papers" / "Smith2024" / "source" / "paper.pdf"
    assert provider.calls[0].pdf_input == source
    assert provider.calls[0].text_input is None
    assert provider.calls[0].input_sha256 == sha256_file(source)
    assert result.record.input_artifact == "papers/Smith2024/source/paper.pdf"
    assert result.staged_path.read_text(encoding="utf-8") == "Fake LLM response.\n"


def test_output_filename_is_recorded_independently_from_recipe_name(library: Library) -> None:
    recipe = RecipeDefinition(
        name="custom",
        version=1,
        input="transcription",
        output="different-name.md",
        prompt="Respond.",
    )

    result = run_recipe(library, "Smith2024", recipe, FakeLLMProvider(), model="test-model")

    assert result.destination == "papers/Smith2024/different-name.md"
    assert result.record.output_artifact == result.destination


def test_missing_transcription_fails_before_provider_call(library: Library) -> None:
    (library.root / "papers" / "Smith2024" / "transcription.md").unlink()
    provider = FakeLLMProvider()

    with pytest.raises(RecipeRunError, match=r"missing transcription.*summary.*Smith2024"):
        run_recipe(library, "Smith2024", summary_recipe(), provider)

    assert provider.calls == []


def test_symlinked_transcription_is_rejected_before_provider_call(
    library: Library, tmp_path: Path
) -> None:
    transcription = library.root / "papers" / "Smith2024" / "transcription.md"
    external = tmp_path / "external.md"
    external.write_text("external private text", encoding="utf-8")
    transcription.unlink()
    try:
        transcription.symlink_to(external)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    provider = FakeLLMProvider()

    with pytest.raises(RecipeRunError, match="must not contain symlinks"):
        run_recipe(library, "Smith2024", summary_recipe(), provider)

    assert provider.calls == []


def test_provider_failure_produces_no_staged_output(library: Library) -> None:
    provider = FakeLLMProvider(fail=True, failure_message="safe provider failure")

    with pytest.raises(RecipeRunError, match="safe provider failure"):
        run_recipe(library, "Smith2024", summary_recipe(), provider)

    assert list((library.root / ".pp" / "tmp").iterdir()) == []


@pytest.mark.parametrize("response", ["", " ", "\n\t"])
def test_empty_provider_response_is_rejected(library: Library, response: str) -> None:
    provider = FakeLLMProvider(response=response)

    with pytest.raises(RecipeRunError, match="empty response"):
        run_recipe(library, "Smith2024", summary_recipe(), provider)

    assert list((library.root / ".pp" / "tmp").iterdir()) == []


def test_provenance_excludes_credentials_and_input_content(library: Library) -> None:
    secret = "sk-secret-value"
    private_input = "private transcription contents"
    transcription = library.root / "papers" / "Smith2024" / "transcription.md"
    transcription.write_text(private_input, encoding="utf-8")
    provider = FakeLLMProvider(response="Safe output")
    provider.__dict__["api_key"] = secret

    result = run_recipe(library, "Smith2024", summary_recipe(), provider, model="safe-model")

    serialized = result.record.model_dump_json() + result.staged_path.read_text(encoding="utf-8")
    assert secret not in serialized
    assert private_input not in serialized
    assert "Summarize this paper." not in serialized
