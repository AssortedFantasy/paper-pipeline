"""Canonical on-disk layout of a generated library.

The single source of truth for every path inside a library. All other code
must build library paths through these constants and helpers — never with
hard-coded strings. All paths are relative to the library root.

Layout (ADR-0002):

    <library>/
        library.json            # format version + library identity (essential)
        AGENTS.md               # generated agent guide (derived, rebuildable)
        .gitignore              # generated VCS policy (derived, rebuildable)
        indexes/                # small text indexes (derived, rebuildable)
        .pp/                    # operational noise: logs, temp dirs (disposable)
            attempts/           # in-flight recovery hints, never artifact truth
        papers/
            <citekey>/
                paper.json          # metadata + processing record (essential)
                source/             # source PDF (essential; git-ignored)
                transcription.md    # source-derived converted text (essential output)
                figures/            # source-derived converter assets (essential output)
                generated/          # LLM recipe outputs (derived; provenance in front matter)
                .pp/                # per-paper diagnostics and logs (disposable)
"""

from pathlib import Path, PurePosixPath

# Library root entries
LIBRARY_FILE = "library.json"
AGENTS_FILE = "AGENTS.md"
GITIGNORE_FILE = ".gitignore"
INDEXES_DIR = "indexes"
PAPERS_DIR = "papers"
OPERATIONAL_DIR = ".pp"
ATTEMPTS_DIR = "attempts"

# Per-paper entries
PAPER_FILE = "paper.json"
SOURCE_DIR = "source"
TRANSCRIPTION_FILE = "transcription.md"
FIGURES_DIR = "figures"
GENERATED_DIR = "generated"

# Current library format version. Bump requires an ADR.
FORMAT_VERSION = 1

# Citekeys become directory names. Keep this conservative and portable.
# No trailing dot: Windows silently strips trailing dots from directory names.
CITEKEY_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9_.+-]*[A-Za-z0-9_+-])?$"


def paper_dir(library_root: Path, citekey: str) -> Path:
    """Absolute path of a paper directory (for application use only)."""
    return library_root / PAPERS_DIR / citekey


def relative_paper_dir(citekey: str) -> PurePosixPath:
    """Library-relative paper directory, as stored inside library files."""
    return PurePosixPath(PAPERS_DIR) / citekey
