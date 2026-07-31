from __future__ import annotations

from pathlib import Path

import pytest

from paper_pipeline.library.model import PaperMetadata, PaperRecord
from paper_pipeline.library.paths import FORMAT_VERSION
from paper_pipeline.library.storage import Library, create_library
from paper_pipeline.recipes.input import resolve_recipe_input
from paper_pipeline.recipes.model import RecipeDefinition, RecipeInput


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


def recipe(input_kind: RecipeInput) -> RecipeDefinition:
    return RecipeDefinition(
        name="summary",
        version=1,
        input=input_kind,
        output="summary.md",
        prompt="Summarize this paper.",
    )


@pytest.mark.parametrize("input_kind", ["transcription", "pdf"])
def test_declared_input_resolves_to_its_library_artifact(
    library: Library, input_kind: RecipeInput
) -> None:
    paper = library.read_paper("Smith2024")

    path, artifact = resolve_recipe_input(
        library,
        "Smith2024",
        recipe(input_kind),
        paper.source_pdf,
    )

    assert path == library.root / artifact
    assert path.is_file()


def test_missing_input_is_rejected(library: Library) -> None:
    (library.root / "papers" / "Smith2024" / "transcription.md").unlink()

    with pytest.raises(ValueError, match="missing transcription input"):
        resolve_recipe_input(library, "Smith2024", recipe("transcription"), None)


def test_pdf_outside_the_paper_source_directory_is_rejected(library: Library) -> None:
    outside = library.root / "shared.pdf"
    outside.write_bytes(b"%PDF")

    with pytest.raises(ValueError, match="outside its dedicated source directory"):
        resolve_recipe_input(
            library,
            "Smith2024",
            recipe("pdf"),
            "papers/Smith2024/source/../../../shared.pdf",
        )


@pytest.mark.parametrize("input_kind", ["transcription", "pdf"])
def test_symlinked_input_is_rejected(
    library: Library, tmp_path: Path, input_kind: RecipeInput
) -> None:
    paper_root = library.root / "papers" / "Smith2024"
    input_path = (
        paper_root / "transcription.md"
        if input_kind == "transcription"
        else paper_root / "source" / "paper.pdf"
    )
    external = tmp_path / f"external{input_path.suffix}"
    external.write_bytes(b"external private content")
    input_path.unlink()
    try:
        input_path.symlink_to(external)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(ValueError, match="must not contain symlinks"):
        resolve_recipe_input(
            library,
            "Smith2024",
            recipe(input_kind),
            library.read_paper("Smith2024").source_pdf,
        )
