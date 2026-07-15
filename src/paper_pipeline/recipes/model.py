"""Recipe definitions and their versioned template contract.

A recipe is a Markdown file with YAML front matter (ADR-0003):

    ---
    name: contributions
    version: 1
    input: transcription        # "transcription" or "pdf"
    output: contributions.md    # filename inside the paper's generated/ dir
    ---
    Extract the key contributions in this paper.
    Format them as a bulleted list.
    Output only the contributions.

The parser deliberately accepts only the scalar front-matter shape above. This
keeps recipe files easy to inspect and prevents YAML features that are not part
of the versioned contract from being interpreted inconsistently.
"""

import re
from dataclasses import dataclass
from importlib import resources
from typing import Literal

RecipeInput = Literal["transcription", "pdf"]


@dataclass(frozen=True)
class RecipeDefinition:
    """A parsed, validated recipe template."""

    name: str
    version: int
    input: RecipeInput
    output: str  # filename inside generated/, must end in .md
    prompt: str


_FIELDS = frozenset({"name", "version", "input", "output"})
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def parse_recipe(text: str) -> RecipeDefinition:
    """Parse and strictly validate one recipe template.

    ``ValueError`` messages name the field responsible for a validation
    failure. Syntax errors that cannot be associated with a field name the
    front matter or prompt instead.
    """

    front_matter, prompt = _split_template(text)
    values = _parse_front_matter(front_matter)

    missing = _FIELDS - values.keys()
    if missing:
        field = sorted(missing)[0]
        raise ValueError(f"recipe field {field!r} is required")

    name = values["name"]
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "recipe field 'name' must be a lowercase identifier containing only "
            "letters, digits, underscores, or hyphens"
        )

    raw_version = values["version"]
    try:
        version = int(raw_version)
    except ValueError as exc:
        raise ValueError("recipe field 'version' must be a positive integer") from exc
    if version < 1 or str(version) != raw_version:
        raise ValueError("recipe field 'version' must be a positive integer")

    recipe_input = values["input"]
    if recipe_input not in ("transcription", "pdf"):
        raise ValueError("recipe field 'input' must be 'transcription' or 'pdf'")

    output = values["output"]
    if (
        not output.endswith(".md")
        or output == ".md"
        or "/" in output
        or "\\" in output
        or output in (".", "..")
    ):
        raise ValueError("recipe field 'output' must be a bare .md filename")

    if not prompt.strip():
        raise ValueError("recipe prompt body must be non-empty")

    return RecipeDefinition(
        name=name,
        version=version,
        input=recipe_input,
        output=output,
        prompt=prompt,
    )


def load_builtin_recipes() -> dict[str, RecipeDefinition]:
    """Load the recipe templates bundled with Paper Pipeline, keyed by name."""

    builtin_dir = resources.files("paper_pipeline.recipes").joinpath("builtin")
    recipes: dict[str, RecipeDefinition] = {}
    for template in sorted(builtin_dir.iterdir(), key=lambda item: item.name):
        if not template.name.endswith(".md"):
            continue
        recipe = parse_recipe(template.read_text(encoding="utf-8"))
        if recipe.name in recipes:
            raise ValueError(f"duplicate built-in recipe name {recipe.name!r}")
        recipes[recipe.name] = recipe
    return recipes


def _split_template(text: str) -> tuple[str, str]:
    normalized = text.removeprefix("\ufeff")
    lines = normalized.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError("recipe front matter must start with '---'")

    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            front_matter = "".join(lines[1:index])
            prompt = "".join(lines[index + 1 :])
            return front_matter, prompt
    raise ValueError("recipe front matter must end with '---'")


def _parse_front_matter(front_matter: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, line in enumerate(front_matter.splitlines(), start=2):
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"invalid recipe front matter on line {line_number}")
        field, value = (part.strip() for part in line.split(":", maxsplit=1))
        if field not in _FIELDS:
            label = field or "<empty>"
            raise ValueError(f"unknown recipe field {label!r}")
        if field in values:
            raise ValueError(f"duplicate recipe field {field!r}")
        if not value:
            raise ValueError(f"recipe field {field!r} must be non-empty")
        values[field] = value
    return values
