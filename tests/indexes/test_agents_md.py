"""Boundary coverage for generated library support files."""

from pathlib import Path

from paper_pipeline.indexes.agents_md import write_library_support_files
from paper_pipeline.library.storage import create_library


def test_library_support_files_are_written(library_root: Path) -> None:
    library = create_library(library_root)

    write_library_support_files(library)

    for filename in ("AGENTS.md", ".gitignore"):
        artifact = library_root / filename
        assert artifact.is_file()
        assert artifact.read_text(encoding="utf-8").strip()
