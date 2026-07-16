"""Golden and determinism tests for generated library support files."""

from pathlib import Path

from paper_pipeline.indexes.agents_md import (
    render_agents_md,
    render_gitignore,
    write_library_support_files,
)
from paper_pipeline.library.storage import create_library

EXPECTED_GITIGNORE = """**/.pp/
papers/*/source/
"""

EXPECTED_AGENTS_MD = """# Paper Library

This folder contains a library of papers converted into formats that agents and
text-processing tools can read directly.

## Finding papers

A paper's citekey maps directly to `papers/<citekey>/`.

Use the indexes in `indexes/` to discover papers and citekeys. Every index line
has the form `<citekey>: <value>`.

- `titles.md`: Paper titles.
- `authors.md`: Paper authors.
- `years.md`: Publication years.
- `venues.md`: Publication venues.
- `summaries.md`: One-sentence generated summaries.

Indexes are compact representations for efficient lookups. Use them to find
relevant citekeys.

## Paper directories

A paper directory can contain:

- `paper.json`: Bibliographic metadata and processing records.
- `source/*.pdf`: The original PDF.
- `transcription.md`: A complete transcription of the paper.
- `figures/*.png`: Figures extracted from the PDF.
- `pages/pageN.png`: Rendered PDF pages.
- `summary.md`: A generated one-sentence summary.
- `contributions.md`: Generated notes on the paper's main contributions.
- `intro_filtered.md`: Generated notes on the introduction, prior work, thesis,
  methods, and key references.
- `method_filtered.md`: Generated notes on the approach, setup, and evaluation.
- `.pp/`: Ignorable operational files.

Some generated or source-derived files may be absent when that processing step
has not yet completed.
"""


def test_generated_agents_md_matches_golden_content() -> None:
    content = render_agents_md()

    assert content == EXPECTED_AGENTS_MD
    assert content.startswith("# Paper Library\n")
    assert "`papers/<citekey>/`" in content
    assert "compact representations for efficient lookups" in content
    assert "`summary.md`" in content
    assert "`method_filtered.md`" in content
    assert "`.pp/`" in content
    assert all(
        name in content
        for name in ("titles.md", "authors.md", "years.md", "venues.md", "summaries.md")
    )
    assert "status.md" not in content
    assert len(content.splitlines()) <= 60
    assert "\r" not in content


def test_generated_gitignore_matches_golden() -> None:
    assert render_gitignore() == EXPECTED_GITIGNORE


def test_regeneration_is_byte_deterministic(library_root: Path) -> None:
    library = create_library(library_root)

    write_library_support_files(library)
    first_agents = (library_root / "AGENTS.md").read_bytes()
    first_ignore = (library_root / ".gitignore").read_bytes()
    write_library_support_files(library)

    assert (library_root / "AGENTS.md").read_bytes() == first_agents
    assert (library_root / ".gitignore").read_bytes() == first_ignore
    assert first_agents == render_agents_md().encode()
    assert first_ignore == EXPECTED_GITIGNORE.encode()
