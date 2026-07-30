from __future__ import annotations

import pytest

from paper_pipeline.library.paths import RESERVED_PAPER_NAMES
from paper_pipeline.recipes.model import load_builtin_recipes, parse_recipe


def recipe_text(**overrides: object) -> str:
    fields: dict[str, object] = {
        "name": "summary",
        "version": "1",
        "input": "transcription",
        "output": "summary.md",
    }
    fields.update(overrides)
    front_matter = "\n".join(f"{key}: {value}" for key, value in fields.items())
    return f"---\n{front_matter}\n---\nPrompt text.\n"


def test_shipped_recipes_satisfy_the_recipe_contract() -> None:
    recipes = load_builtin_recipes()
    reserved = {name.casefold() for name in RESERVED_PAPER_NAMES}

    assert recipes
    for key, recipe in recipes.items():
        assert key == recipe.name
        assert recipe.version >= 1
        assert recipe.input in {"transcription", "pdf"}
        assert recipe.output.endswith(".md")
        assert "/" not in recipe.output and "\\" not in recipe.output
        assert recipe.output.casefold() not in reserved
        assert recipe.prompt.strip()


def test_yaml_scalars_are_interoperable_and_prompt_is_opaque() -> None:
    prompt = "First line.\n\n- Keep spacing  \n{{ no template language }}\n"
    recipe = parse_recipe(
        """---
name: "quoted" # ordinary YAML comment
version: 2
input: 'transcription'
output: "different-file.md"
---
"""
        + prompt
    )

    assert recipe.name == "quoted"
    assert recipe.version == 2
    assert recipe.input == "transcription"
    assert recipe.output == "different-file.md"
    assert recipe.prompt == prompt


@pytest.mark.parametrize("field", ["name", "version", "input", "output"])
def test_declared_fields_are_required(field: str) -> None:
    text = recipe_text()
    text = "\n".join(line for line in text.splitlines() if not line.startswith(f"{field}:"))

    with pytest.raises(ValueError, match=field):
        parse_recipe(text)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("name", "", id="empty-name"),
        pytest.param("name", "Uppercase", id="non-lowercase-name"),
        pytest.param("name", "../summary", id="unsafe-name"),
        pytest.param("version", "0", id="non-positive-version"),
        pytest.param("version", '"1"', id="non-integer-version"),
        pytest.param("input", "source", id="unsupported-input"),
        pytest.param("output", "summary.txt", id="non-markdown-output"),
        pytest.param("output", ".md", id="empty-output-stem"),
        pytest.param("output", "generated/summary.md", id="posix-subdirectory"),
        pytest.param("output", r"generated\summary.md", id="windows-subdirectory"),
        pytest.param("output", "transcription.md", id="reserved-output"),
        pytest.param("output", "TRANSCRIPTION.MD", id="case-insensitive-reserved-output"),
    ],
)
def test_field_constraints_reject_unsafe_or_ambiguous_values(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        parse_recipe(recipe_text(**{field: value}))


@pytest.mark.parametrize("prompt", ["", " \n\t"])
def test_prompt_must_be_non_empty(prompt: str) -> None:
    text = "---\nname: empty\nversion: 1\ninput: pdf\noutput: empty.md\n---\n" + prompt

    with pytest.raises(ValueError, match=r"prompt.*non-empty"):
        parse_recipe(text)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("name: missing\n", "front matter", id="missing-opening-delimiter"),
        pytest.param("---\nname: missing\n", "front matter", id="missing-closing-delimiter"),
        pytest.param(
            "---\n- name\n- version\n---\nprompt",
            "front matter",
            id="front-matter-is-not-a-mapping",
        ),
        pytest.param(
            recipe_text(provider="openai"),
            "provider",
            id="unknown-field",
        ),
        pytest.param(
            recipe_text().replace("name: summary", "name: summary\nname: other"),
            "name",
            id="duplicate-field",
        ),
        pytest.param(
            recipe_text(output="[summary.md]"),
            "front matter",
            id="non-scalar-field",
        ),
    ],
)
def test_ambiguous_or_extended_front_matter_is_rejected(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_recipe(text)
