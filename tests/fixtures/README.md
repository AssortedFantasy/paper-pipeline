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

The explicit GPU golden test uses the `native-text`, `tables`, and `figures`
entries. Set `PAPER_PIPELINE_GOLDEN_NATIVE_TEXT_PDF`,
`PAPER_PIPELINE_GOLDEN_TABLES_PDF`, and
`PAPER_PIPELINE_GOLDEN_FIGURES_PDF` to verified local copies, then run the
documented GPU test command. Structural bounds live in
`corpus/golden_expectations.json`; the PDFs remain local and uncommitted.

## Zotero exports (`zotero/`)

Small, hand-trimmed Zotero RDF exports (RDF file + `files/` attachments using
tiny dummy PDFs) used by ingestion tests. These are committed — keep them
minimal.
