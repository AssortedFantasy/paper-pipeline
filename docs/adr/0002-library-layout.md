# ADR-0002: Generated library layout

Status: Accepted

## Context

Generated libraries must be portable, directly readable from the filesystem,
and safe to update after interrupted work.

## Decision

```text
<library>/
    library.json
    AGENTS.md
    .gitignore
    indexes/
    .pp/
    papers/
        <citekey>/
            paper.json
            source/<hash>.pdf
            transcription.md
            figures/
            pages/
            <recipe-output>.md
            .pp/
```

`library.json` and `paper.json` use library format version 2. Citekeys identify
papers and must be safe as cross-platform directory names. Serialized models
reject unknown fields so typos and schema drift fail visibly instead of being
silently discarded.

Library state has four classes:

- Essential: library and paper records, source PDFs, transcriptions, and figures
- Optional source-derived: independently rendered pages
- Derived: indexes, generated guidance, ignore rules, and declared recipe
  outputs
- Disposable: diagnostics, staging files, and attempt markers under `.pp/`

Disposable runtime acceleration, such as the Papers-view inspection cache,
may also live under `.pp/`. These files are never canonical records and every
feature using them must rebuild correctly when `.pp/` is absent or malformed
(ADR-0007).

All serialized paths are library-relative POSIX paths. `paper.json` is the
canonical paper record and declares installed artifacts and their provenance.
Source and input hashes determine whether conversion and recipe outputs are
current.

Writes are staged under `.pp/`, validated, and atomically installed. Import is
additive: papers missing from a later Zotero export are retained, and replacing
a source PDF requires explicit approval.

## Consequences

Libraries can be copied without rewriting metadata and read without the
application. Deleting `.pp/` loses diagnostics but not valid artifacts.
Deleting derived files loses no source material; `reindex` rebuilds indexes and
generated guidance. Deleting `.pp/` also discards runtime caches, which are
rebuilt from canonical library files when needed.
