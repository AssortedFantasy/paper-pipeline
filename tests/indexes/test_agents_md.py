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

EXPECTED_AGENTS_MD = """# Paper Library Guide

This folder is a portable Paper Pipeline library. The files in it are the
product; no running application or external database is needed to read them.

## Finding papers

- A paper's citekey maps directly to `papers/<citekey>/`.
- `papers/<citekey>/paper.json` is the canonical metadata and provenance record.
- `indexes/titles.md` and `indexes/authors.md` provide quick bibliographic lookup.
- `indexes/summaries.md` contains one-line generated summaries when available.
- `indexes/status.md` flags missing, stale, or most-recently-failed processing.
- Indexes are derived navigation aids. If they disagree with paper directories,
  trust paper content and rebuild the indexes.

## Paper content

- `source/` contains the original PDF. It is essential source content but is
  ignored by Git, so a clone may be readable without being reprocessable.
- `transcription.md`, `figures/`, and low-resolution `pages/pageN.png` images
  are source-derived converter outputs.
- Other top-level Markdown files (for example `summary.md`) are LLM-generated
  recipe outputs. Their content contains only the useful result.
- `paper.json` identifies generated files and records their provenance, token
  usage, spend, and hashes used to determine whether outputs are current.

## Operational files

- Every `.pp/` directory is disposable operational noise (logs, staging, and
  interruption hints). Ignore it during research and search.
- Deleting `.pp/` never removes canonical paper content.
"""


def test_generated_agents_md_matches_golden_content() -> None:
    content = render_agents_md()

    assert content == EXPECTED_AGENTS_MD
    assert content.startswith("# Paper Library Guide\n")
    assert "`papers/<citekey>/`" in content
    assert "Other top-level Markdown files" in content
    assert "provenance" in content
    assert "`.pp/`" in content
    assert all(name in content for name in ("titles.md", "authors.md", "summaries.md", "status.md"))
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
