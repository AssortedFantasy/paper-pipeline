"""Generated library AGENTS.md and .gitignore templates.

The generated AGENTS.md is written for a *consumer* agent working directly in
a generated library — not for developers of Paper Pipeline. Its purpose is to
make the filesystem a self-describing reading interface: explain how to find a
paper by citekey, search the indexes, and choose useful paper artifacts while
ignoring operational noise. The standard recipe filenames are intentionally
hardcoded until Paper Pipeline supports custom recipes. Target: under ~60 lines.

The generated .gitignore excludes: ``.pp/`` (all levels) and
``papers/*/source/``.
"""

import shutil

from paper_pipeline.library.paths import AGENTS_FILE, GITIGNORE_FILE
from paper_pipeline.library.storage import Library

_AGENTS_MD = """# Paper Library

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

_GITIGNORE = """**/.pp/
papers/*/source/
"""


def render_agents_md() -> str:
    """Return deterministic consumer guidance for a generated library."""
    return _AGENTS_MD


def render_gitignore() -> str:
    """Return the generated library's deterministic version-control policy."""
    return _GITIGNORE


def write_library_support_files(library: Library) -> None:
    """Atomically regenerate the root AGENTS.md and .gitignore."""
    stage = library.stage_dir()
    try:
        for filename, content in (
            (AGENTS_FILE, render_agents_md()),
            (GITIGNORE_FILE, render_gitignore()),
        ):
            staged = stage / filename
            with staged.open("w", encoding="utf-8", newline="\n") as output:
                output.write(content)
            library.install_artifact(staged, filename)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
