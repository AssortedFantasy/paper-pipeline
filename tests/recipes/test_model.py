from __future__ import annotations

import pytest

from paper_pipeline.recipes.model import load_builtin_recipes, parse_recipe


def recipe_text(**overrides: str) -> str:
    fields = {
        "name": "summary",
        "version": "1",
        "input": "transcription",
        "output": "summary.md",
    }
    fields.update(overrides)
    front_matter = "\n".join(f"{key}: {value}" for key, value in fields.items())
    return f"---\n{front_matter}\n---\nPrompt text.\n"


def test_builtins_parse() -> None:
    recipes = load_builtin_recipes()

    assert set(recipes) == {"contributions", "intro", "method", "summary"}
    assert recipes["summary"].output == "summary.md"
    assert recipes["contributions"].input == "pdf"
    assert recipes["intro"].input == "pdf"
    assert recipes["intro"].output == "intro_filtered.md"
    assert "Motivation & Problem Context" in recipes["intro"].prompt
    assert recipes["method"].input == "pdf"
    assert recipes["method"].output == "method_filtered.md"
    assert "Approach" in recipes["method"].prompt


def test_prompt_body_is_preserved_verbatim() -> None:
    prompt = "First line.\n\n- Keep spacing  \nLast line.\n"
    text = "---\nname: exact\nversion: 2\ninput: pdf\noutput: exact.md\n---\n" + prompt

    assert parse_recipe(text).prompt == prompt


@pytest.mark.parametrize("field", ["name", "version", "input", "output"])
def test_required_fields(field: str) -> None:
    text = recipe_text()
    text = "\n".join(line for line in text.splitlines() if not line.startswith(f"{field}:"))

    with pytest.raises(ValueError, match=field):
        parse_recipe(text)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"unknown.*provider"):
        parse_recipe(recipe_text(provider="openai"))


def test_duplicate_field_is_rejected() -> None:
    text = recipe_text().replace("name: summary", "name: summary\nname: other")

    with pytest.raises(ValueError, match=r"duplicate.*name"):
        parse_recipe(text)


@pytest.mark.parametrize("name", ["", "Has Spaces", "Uppercase", "../summary"])
def test_invalid_name_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        parse_recipe(recipe_text(name=name))


@pytest.mark.parametrize("version", ["zero", "0", "-1", "1.5", '"1"'])
def test_invalid_version_is_rejected(version: str) -> None:
    with pytest.raises(ValueError, match="version"):
        parse_recipe(recipe_text(version=version))


def test_yaml_quotes_and_inline_comments_are_supported() -> None:
    recipe = parse_recipe(
        """---
name: "quoted" # ordinary YAML comment
version: 1 # positive integer
input: 'transcription'
output: "different-file.md"
---
Keep this prompt.
"""
    )

    assert recipe.name == "quoted"
    assert recipe.version == 1
    assert recipe.input == "transcription"
    assert recipe.output == "different-file.md"


def test_invalid_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="input"):
        parse_recipe(recipe_text(input="source"))


@pytest.mark.parametrize(
    "output",
    ["summary.txt", ".md", "generated/summary.md", "generated\\summary.md", "../summary.md"],
)
def test_output_must_be_a_bare_markdown_filename(output: str) -> None:
    with pytest.raises(ValueError, match=r"output.*bare \.md filename"):
        parse_recipe(recipe_text(output=output))


@pytest.mark.parametrize("prompt", ["", "\n", " \n\t"])
def test_prompt_must_be_non_empty(prompt: str) -> None:
    text = "---\nname: empty\nversion: 1\ninput: pdf\noutput: empty.md\n---\n" + prompt

    with pytest.raises(ValueError, match=r"prompt.*non-empty"):
        parse_recipe(text)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name: missing\n", "start"),
        ("---\nname: missing\n", "end"),
        ("---\nnot-a-field\n---\nprompt", "front matter"),
    ],
)
def test_malformed_front_matter_is_rejected(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_recipe(text)
