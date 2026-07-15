"""Recipe execution: resolve input, call provider, validate, add provenance.

Implemented by WP-2C.3. For one (paper, recipe) pair:

1. Resolve the declared input (transcription text or source PDF); fail
   fast with a clear error if it is missing.
2. Call the configured provider.
3. Validate the result (non-empty, plausibly Markdown).
4. Prepend YAML front matter provenance (recipe name/version, provider,
   model, created timestamp, input artifact — never credentials).
5. Return the staged output for atomic installation into ``generated/``.

Scheduling and same-paper recipe batching for provider cache reuse are the job
layer's responsibility, not this module's.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from paper_pipeline.library.model import PaperRecord, RecipeRecord
from paper_pipeline.library.paths import PAPERS_DIR, paper_dir
from paper_pipeline.library.storage import sha256_file
from paper_pipeline.recipes.model import RecipeDefinition
from paper_pipeline.recipes.provider import LLMProvider, ProviderRequest


class RecipeRunError(RuntimeError):
    """A recipe could not produce a valid staged artifact."""


class RecipeStorage(Protocol):
    """Minimal citekey-scoped storage needed by the recipe runner."""

    @property
    def root(self) -> Path: ...

    def read_paper(self, citekey: str) -> PaperRecord: ...

    def stage_dir(self) -> Path: ...


@dataclass(frozen=True)
class RecipeRunResult:
    """Validated recipe output ready for atomic artifact installation."""

    staged_path: Path
    destination: str
    record: RecipeRecord


def run_recipe(
    library: RecipeStorage,
    citekey: str,
    recipe: RecipeDefinition,
    provider: LLMProvider,
    *,
    model: str = "",
) -> RecipeRunResult:
    """Run one recipe without installing or recording it in ``paper.json``."""

    paper = library.read_paper(citekey)
    input_path, input_artifact = _resolve_input(library, citekey, recipe, paper.source_pdf)
    input_sha256 = sha256_file(input_path)

    if recipe.input == "transcription":
        try:
            text_input = input_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise RecipeRunError(
                f"recipe input transcription for paper {citekey!r} is not valid UTF-8"
            ) from error
        if not text_input.strip():
            raise RecipeRunError(f"recipe input transcription for paper {citekey!r} is empty")
        request = ProviderRequest(
            prompt=recipe.prompt,
            text_input=text_input,
            input_sha256=input_sha256,
            model=model,
        )
    else:
        request = ProviderRequest(
            prompt=recipe.prompt,
            pdf_input=input_path,
            input_sha256=input_sha256,
            model=model,
        )

    provider_result = provider.generate(request)
    if not provider_result.ok:
        detail = provider_result.error or "provider returned no error details"
        raise RecipeRunError(f"recipe {recipe.name!r} provider failed: {detail}")
    body = provider_result.text.strip()
    if not body:
        raise RecipeRunError(f"recipe {recipe.name!r} provider returned an empty response")
    provider_name = provider_result.provider or provider.name
    provider_model = provider_result.model or model
    if not provider_name:
        raise RecipeRunError(f"recipe {recipe.name!r} provider returned no provider identity")
    if not provider_model:
        raise RecipeRunError(f"recipe {recipe.name!r} provider returned no model identity")
    if any(character in value for value in (provider_name, provider_model) for character in "\r\n"):
        raise RecipeRunError("provider and model identities must each fit on one line")

    completed_at = datetime.now(UTC).replace(microsecond=0)
    content = _render_output(
        recipe=recipe,
        provider=provider_name,
        model=provider_model,
        input_artifact=input_artifact,
        input_sha256=input_sha256,
        completed_at=completed_at,
        body=body,
    )
    staging_dir = library.stage_dir()
    staged_path = staging_dir / recipe.output
    try:
        staged_path.write_text(content, encoding="utf-8", newline="\n")
        if not staged_path.is_file() or staged_path.stat().st_size == 0:
            raise RecipeRunError(f"recipe {recipe.name!r} produced no staged output")
        output_sha256 = sha256_file(staged_path)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return RecipeRunResult(
        staged_path=staged_path,
        destination=f"{PAPERS_DIR}/{citekey}/generated/{recipe.output}",
        record=RecipeRecord(
            recipe_version=recipe.version,
            provider=provider_name,
            model=provider_model,
            input_artifact=input_artifact,
            input_sha256=input_sha256,
            output_artifact=f"{PAPERS_DIR}/{citekey}/generated/{recipe.output}",
            output_sha256=output_sha256,
            completed_at=completed_at,
        ),
    )


def _resolve_input(
    library: RecipeStorage,
    citekey: str,
    recipe: RecipeDefinition,
    source_pdf: str | None,
) -> tuple[Path, str]:
    paper_root = paper_dir(library.root, citekey)
    if recipe.input == "transcription":
        input_path = paper_root / "transcription.md"
        input_artifact = f"{PAPERS_DIR}/{citekey}/transcription.md"
    else:
        if source_pdf is None:
            raise RecipeRunError(f"paper {citekey!r} has no source PDF for recipe {recipe.name!r}")
        source_parts = PurePosixPath(source_pdf).parts
        input_path = library.root.joinpath(*source_parts)
        source_root = paper_root / "source"
        if not input_path.is_relative_to(source_root):
            raise RecipeRunError(
                f"paper {citekey!r} source PDF is outside its dedicated source directory"
            )
        input_artifact = input_path.relative_to(library.root).as_posix()

    root = library.root.resolve()
    current = root
    for part in input_path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise RecipeRunError(
                f"recipe input for paper {citekey!r} must not contain symlinks: {input_artifact}"
            )
    try:
        resolved_input = input_path.resolve(strict=True)
    except FileNotFoundError:
        resolved_input = input_path
    if not resolved_input.is_relative_to(root) or not resolved_input.is_file():
        raise RecipeRunError(
            f"missing {recipe.input} input for recipe {recipe.name!r} on paper {citekey!r}: "
            f"{input_artifact}"
        )
    return resolved_input, input_artifact


def _render_output(
    *,
    recipe: RecipeDefinition,
    provider: str,
    model: str,
    input_artifact: str,
    input_sha256: str,
    completed_at: datetime,
    body: str,
) -> str:
    created = completed_at.isoformat().replace("+00:00", "Z")
    return (
        "---\n"
        "generated_by: paper-pipeline\n"
        f"recipe: {recipe.name}\n"
        f"recipe_version: {recipe.version}\n"
        f"provider: {provider}\n"
        f"model: {model}\n"
        f"input: {input_artifact}\n"
        f"input_sha256: {input_sha256}\n"
        f"created: {created}\n"
        "---\n"
        f"{body}\n"
    )
