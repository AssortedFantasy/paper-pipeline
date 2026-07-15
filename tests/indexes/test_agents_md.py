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


def test_generated_agents_md_matches_golden_content() -> None:
    content = render_agents_md()

    assert content.startswith("# Paper Library Guide\n")
    assert "`papers/<citekey>/`" in content
    assert "`generated/` contains LLM-generated recipe outputs" in content
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
