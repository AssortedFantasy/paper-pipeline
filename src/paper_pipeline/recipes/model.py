"""Recipe definitions. Template contract is FROZEN — changes require an ADR.

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

Parsing and validation implemented by WP-2C.1 (see PLAN.md).
"""

from dataclasses import dataclass
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
