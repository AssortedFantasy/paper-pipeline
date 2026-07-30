from __future__ import annotations

from pathlib import Path, PurePosixPath

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


@pytest.mark.parametrize("input_kind", ["transcription", "pdf"])
def test_declared_input_is_selected_and_hashed(library: Library, input_kind: RecipeInput) -> None:
    provider = FakeLLMProvider()
    recipe = summary_recipe(input_kind=input_kind)

    result = run_recipe(library, "Smith2024", recipe, provider, model="test-model")

    paper = library.root / "papers" / "Smith2024"
    expected_input = (
        paper / "transcription.md"
        if input_kind == "transcription"
        else paper / "source" / "paper.pdf"
    )
    expected_artifact = expected_input.relative_to(library.root).as_posix()
    input_hash = sha256_file(expected_input)
    request = provider.calls[0]

    assert result.record.input_artifact == expected_artifact
    assert result.record.input_sha256 == input_hash
    assert request.input_sha256 == input_hash
    if input_kind == "transcription":
        assert request.text_input == expected_input.read_text(encoding="utf-8")
        assert request.pdf_input is None
    else:
        assert request.pdf_input == expected_input
        assert request.text_input is None


def test_success_stages_clean_output_and_records_safe_provenance(library: Library) -> None:
    provider = FakeLLMProvider(response="\n- Main result\n- Limitation\n\n")
    recipe = summary_recipe()

    result = run_recipe(library, "Smith2024", recipe, provider, model="test-model")

    staged_content = result.staged_path.read_text(encoding="utf-8")
    assert staged_content == provider.response.strip() + "\n"
    assert staged_content.strip()
    assert result.staged_path.is_relative_to(library.operational_dir())
    assert result.record.completed_at is not None
    assert result.record.recipe_version == 2
    assert result.record.provider == provider.name
    assert result.record.model == "test-model"
    assert result.record.output_artifact == result.destination
    assert result.record.output_sha256 == sha256_file(result.staged_path)
    assert {
        "prompt": result.record.prompt_tokens,
        "cached": result.record.cached_tokens,
        "cache_write": result.record.cache_write_tokens,
        "completion": result.record.completion_tokens,
        "cost": result.record.cost_usd,
    } == {
        "prompt": provider.prompt_tokens,
        "cached": provider.cached_tokens,
        "cache_write": provider.cache_write_tokens,
        "completion": provider.completion_tokens,
        "cost": provider.cost_usd,
    }

    installed_hash = library.install_artifact(result.staged_path, result.destination)
    assert installed_hash == result.record.output_sha256


def test_output_filename_is_recorded_independently_from_recipe_name(library: Library) -> None:
    recipe = RecipeDefinition(
        name="custom",
        version=1,
        input="transcription",
        output="different-name.md",
        prompt="Respond.",
    )

    result = run_recipe(library, "Smith2024", recipe, FakeLLMProvider(), model="test-model")

    destination = PurePosixPath(result.destination)
    assert destination.parts[:2] == ("papers", "Smith2024")
    assert destination.name == recipe.output
    assert destination.name != f"{recipe.name}.md"
    assert result.record.output_artifact == result.destination


def test_missing_transcription_fails_before_provider_call(library: Library) -> None:
    (library.root / "papers" / "Smith2024" / "transcription.md").unlink()
    provider = FakeLLMProvider()

    with pytest.raises(RecipeRunError):
        run_recipe(library, "Smith2024", summary_recipe(), provider)

    assert provider.calls == []


@pytest.mark.parametrize("input_kind", ["transcription", "pdf"])
def test_symlinked_input_is_rejected_before_provider_call(
    library: Library, tmp_path: Path, input_kind: RecipeInput
) -> None:
    paper = library.root / "papers" / "Smith2024"
    input_path = (
        paper / "transcription.md"
        if input_kind == "transcription"
        else paper / "source" / "paper.pdf"
    )
    external = tmp_path / f"external{input_path.suffix}"
    external.write_bytes(b"external private content")
    input_path.unlink()
    try:
        input_path.symlink_to(external)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    provider = FakeLLMProvider()

    with pytest.raises(RecipeRunError):
        run_recipe(
            library,
            "Smith2024",
            summary_recipe(input_kind=input_kind),
            provider,
        )

    assert provider.calls == []


@pytest.mark.parametrize("failure_mode", ["provider-failure", "blank-response"])
def test_provider_failure_or_blank_response_leaves_no_staged_output(
    library: Library, failure_mode: str
) -> None:
    provider = (
        FakeLLMProvider(fail=True)
        if failure_mode == "provider-failure"
        else FakeLLMProvider(response=" \n\t")
    )
    before = {
        path.relative_to(library.operational_dir()) for path in library.operational_dir().rglob("*")
    }

    with pytest.raises(RecipeRunError):
        run_recipe(library, "Smith2024", summary_recipe(), provider)

    after = {
        path.relative_to(library.operational_dir()) for path in library.operational_dir().rglob("*")
    }
    assert after == before
    assert len(provider.calls) == 1


def test_provenance_excludes_credentials_and_input_content(library: Library) -> None:
    secret = "sk-secret-value"
    private_input = "private transcription contents"
    transcription = library.root / "papers" / "Smith2024" / "transcription.md"
    transcription.write_text(private_input, encoding="utf-8")
    provider = FakeLLMProvider(response="Safe output")
    provider.__dict__["api_key"] = secret

    result = run_recipe(library, "Smith2024", summary_recipe(), provider, model="safe-model")

    serialized_provenance = result.record.model_dump_json()
    staged_content = result.staged_path.read_text(encoding="utf-8")
    for sensitive_value in (secret, private_input, summary_recipe().prompt):
        assert sensitive_value not in serialized_provenance
        assert sensitive_value not in staged_content
