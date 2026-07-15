"""Generated library AGENTS.md and .gitignore templates.

Implemented by WP-2E.2. The generated AGENTS.md is written for
a *consumer* agent inside a generated library — not for developers of Paper
Pipeline. It must briefly explain: layout, citekey-to-directory lookup,
which files are source-derived vs LLM-generated, the indexes, and that
``.pp/`` is ignorable noise. Target: under ~60 lines.

The generated .gitignore excludes: ``.pp/`` (all levels) and
``papers/*/source/``.
"""

import shutil

from paper_pipeline.library.paths import AGENTS_FILE, GITIGNORE_FILE
from paper_pipeline.library.storage import Library

_AGENTS_MD = """# Paper Library Guide

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
- `transcription.md` and `figures/` are source-derived converter outputs.
- `generated/` contains LLM-generated recipe outputs, not source text. Each
  Markdown file begins with provenance identifying its recipe, model, input,
  input hash, and creation time.
- `paper.json` records hashes used to determine whether outputs are current.

## Operational files

- Every `.pp/` directory is disposable operational noise (logs, staging, and
  interruption hints). Ignore it during research and search.
- Deleting `.pp/` never removes canonical paper content.
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
