"""Resolve and validate the filesystem input declared by a recipe."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol

from paper_pipeline.library.paths import PAPERS_DIR, paper_dir
from paper_pipeline.recipes.model import RecipeDefinition


class RecipeInputStorage(Protocol):
    """Library root access needed while preparing or finalizing a recipe."""

    @property
    def root(self) -> Path: ...


def resolve_recipe_input(
    library: RecipeInputStorage,
    citekey: str,
    recipe: RecipeDefinition,
    source_pdf: str | None,
) -> tuple[Path, str]:
    """Return a validated input path and its library-relative artifact name."""

    paper_root = paper_dir(library.root, citekey)
    source_root: Path | None = None
    if recipe.input == "transcription":
        input_path = paper_root / "transcription.md"
        input_artifact = f"{PAPERS_DIR}/{citekey}/transcription.md"
    else:
        if source_pdf is None:
            raise ValueError(f"paper {citekey!r} has no source PDF for recipe {recipe.name!r}")
        source_parts = PurePosixPath(source_pdf).parts
        input_path = library.root.joinpath(*source_parts)
        source_root = paper_root / "source"
        input_artifact = input_path.relative_to(library.root).as_posix()

    root = library.root.resolve()
    current = root
    for part in input_path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(
                f"recipe input for paper {citekey!r} must not contain symlinks: {input_artifact}"
            )
    try:
        resolved_input = input_path.resolve(strict=True)
    except FileNotFoundError:
        resolved_input = input_path
    if not resolved_input.is_relative_to(root) or not resolved_input.is_file():
        raise ValueError(
            f"missing {recipe.input} input for recipe {recipe.name!r} on paper {citekey!r}: "
            f"{input_artifact}"
        )
    if source_root is not None and not resolved_input.is_relative_to(source_root.resolve()):
        raise ValueError(f"paper {citekey!r} source PDF is outside its dedicated source directory")
    return resolved_input, input_artifact
