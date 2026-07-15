# ADR-0002: Generated library layout

Status: Accepted, amended (2026-07-15)

## Context

REFACTOR.md sketches citekey directories directly at the library root and
defers the exact layout. It requires: citekey as paper identity, source PDFs
always included, relative paths, clear essential/derived/disposable
classification, and durable per-paper processing status for interruption
recovery.

## Decision

```text
<library>/
    library.json            # format_version + identity        [essential]
    AGENTS.md               # generated agent guide            [derived]
    .gitignore              # generated VCS policy             [derived]
    indexes/                # titles/authors/summaries/status  [derived]
    .pp/                    # library-level logs, tmp staging  [disposable]
    papers/
        <citekey>/
            paper.json          # metadata + processing record [essential]
            source/<file>.pdf   # source PDF                   [essential, git-ignored]
            transcription.md    # converter output             [essential output]
            figures/            # converter assets             [essential output]
            generated/          # LLM recipe outputs           [derived]
            .pp/                # diagnostics, logs, raw output[disposable]
```

Key choices:

1. **Papers live under `papers/`, not at the library root.** This deviates
   from the REFACTOR.md sketch. Rationale: citekeys share a namespace with
   nothing — no collision with `indexes/`, `.pp/`, or future reserved names;
   the generated `.gitignore` stays trivial (`**/.pp/`,
   `papers/*/source/`); and agent lookup is still one predictable hop
   (`papers/<citekey>/`), documented in the generated AGENTS.md.
2. **`paper.json` is the single canonical per-paper file**: bibliographic
   metadata, the current source PDF hash, installed artifact provenance, and
   the latest completed attempt for conversion and each recipe. Live job state
   does not overwrite completed artifact truth; disposable in-flight markers
   live under `.pp/attempts/` as specified by ADR-0004.
3. **Source-derived vs LLM-generated separation is by placement**:
   `transcription.md`/`figures/` come from the paper; everything under
   `generated/` comes from an LLM and carries YAML front matter provenance
   (ADR-0003).
4. **All disposable content lives in `.pp/` directories** (library-level and
   per-paper). Deleting every `.pp/` is always safe.
5. **Citekeys must match** `^[A-Za-z0-9](?:[A-Za-z0-9_.+-]*[A-Za-z0-9_+-])?$`
   (no trailing dot — Windows silently strips trailing dots from directory
   names) and must not be Windows-reserved names. Imports with hostile
   citekeys are rejected in the preview with a clear message — never
   silently renamed.
6. `library.json` and `paper.json` carry `format_version` (currently 1).
   Readers reject newer versions with an actionable message.
7. The source PDF's SHA-256 is recorded in `paper.json`. Conversion records
   store the source hash they consumed; recipe records store the hash of the
   PDF or transcription they consumed. Freshness is derived by comparing
   hashes, so a replaced PDF cannot silently leave downstream work current.
8. The schema is versioned, not frozen. Before the first stable release,
   implementation feedback may amend format version 1 while no user libraries
   exist. Once libraries have been produced for use, incompatible serialized
   changes bump `format_version` and add a compatibility or migration test.

## Consequences

- The generated AGENTS.md must state the `papers/<citekey>/` convention
  since it differs from the flat sketch some may expect.
- Deleting a paper directory manually leaves stale index lines; the
  validator reports this and `reindex` repairs it (per REFACTOR.md).
- A Git clone lacks `source/` PDFs; the validator classifies this as "not
  reprocessable", not an error.
- Re-importing identical PDF bytes is a metadata-only refresh. Different bytes
  are shown as a source replacement in the import preview; once accepted, hash
  comparison makes existing conversion and recipe outputs visibly stale
  without deleting them.
