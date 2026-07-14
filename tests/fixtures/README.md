# Test fixtures

## PDF corpus (`corpus/`)

A small representative PDF corpus is required for converter and golden tests.
It must cover:

- native single-column text
- multi-column layout
- heavy equations
- tables
- figures with captions
- a scanned (image-only) document

PDFs are git-ignored (see the repository `.gitignore`); each entry must be
listed in `corpus/manifest.json` with a source URL or provenance note so any
developer can re-download it. Tests that require the corpus must skip with a
clear message when a file is missing, not fail.

Populated by WP-2B.0.

## Zotero exports (`zotero/`)

Small, hand-trimmed Zotero RDF exports (RDF file + `files/` attachments using
tiny dummy PDFs) used by ingestion tests. These are committed — keep them
minimal. Populated by WP-2A.1.
