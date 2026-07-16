# ADR-0002: Generated library layout

Status: Accepted, amended (2026-07-15)

## Context

The library needs citekey-based paper identity, included source PDFs, relative
paths, clear essential/derived/disposable classification, and durable
per-paper processing status for interruption recovery.

## Decision

```text
<library>/
    library.json            # format_version + identity        [essential]
    AGENTS.md               # generated agent guide            [derived]
    .gitignore              # generated VCS policy             [derived]
    indexes/                # titles/authors/years/venues/summaries [derived]
    .pp/                    # library-level logs, tmp staging  [disposable]
    papers/
        <citekey>/
            paper.json          # metadata + processing record [essential]
            source/<file>.pdf   # source PDF                   [essential, git-ignored]
            transcription.md    # converter output             [essential output]
            figures/            # converter assets             [essential output]
            pages/page1.png      # 96-DPI PDF page image         [essential output]
            summary.md          # declared LLM recipe output  [derived]
            contributions.md    # declared LLM recipe output  [derived]
            .pp/                # diagnostics, logs, raw output[disposable]
```

Key choices:

1. **Papers live under `papers/`, not at the library root.** Citekeys share a namespace with
   nothing — no collision with `indexes/`, `.pp/`, or future reserved names;
   the generated `.gitignore` stays trivial (`**/.pp/`,
   `papers/*/source/`); and agent lookup is still one predictable hop
   (`papers/<citekey>/`), documented in the generated AGENTS.md.
2. **`paper.json` is the single canonical per-paper file**: bibliographic
   metadata, the current source PDF hash, installed artifact provenance, and
   the latest completed attempt for conversion and each recipe. Live job state
   does not overwrite completed artifact truth; disposable in-flight markers
   live under `.pp/attempts/` as specified by ADR-0004.
3. **Recipe outputs are flat and declared.** They sit directly beside
   `transcription.md`, avoiding an extra lookup hop. `paper.json` is the
   authority that distinguishes regenerable LLM output from essential content
   and carries provenance (ADR-0003). Recipe output names cannot collide,
   case-insensitively, with `paper.json`, `transcription.md`, `source/`,
   `figures/`, `pages/`, or `.pp/`.
4. **All disposable content lives in `.pp/` directories** (library-level and
   per-paper). Deleting every `.pp/` is always safe.
5. **Citekeys must match** `^[A-Za-z0-9](?:[A-Za-z0-9_.+-]*[A-Za-z0-9_+-])?$`
   (no trailing dot — Windows silently strips trailing dots from directory
   names) and must not be Windows-reserved names. Imports with hostile
   citekeys are rejected in the preview with a clear message — never
   silently renamed.
6. `library.json` and `paper.json` carry `format_version` (currently 2).
   Readers reject newer versions with an actionable message.
7. The source PDF's SHA-256 is recorded in `paper.json`. Conversion records
   store the source hash they consumed; recipe records store the hash of the
   PDF or transcription they consumed. Freshness is derived by comparing
   hashes, so a replaced PDF cannot silently leave downstream work current.
   `source_pdf` must name exactly one file under that same paper's
   `papers/<citekey>/source/` directory; cross-paper and non-source references
   are invalid.
8. Recipe provenance records both its library-relative input and installed
   output artifact paths; output filenames are not inferred from recipe names.
9. Format 2 adds flat recipe outputs and recipe usage/spend fields. Older
   experimental libraries are rejected and may be rebuilt from their Zotero
   export; no in-application migration surface is maintained.
10. Conversion also installs deterministic 96-DPI PNGs at
    `pages/page1.png`, `page2.png`, and so on. They are source-derived members
    of the same validated atomic conversion bundle as the transcription and
    figures.
11. Format remains 2 for the page directory and the additive
    `cache_write_tokens` recipe field. Existing format-2 libraries remain
    valid: older recipe records default the new counter to zero, and a normal
    explicit conversion rerun adds pages. No migration path or compatibility
    shim is introduced.
12. The root `AGENTS.md` is a consumer-agent guide, not application or operator
    documentation. It makes the library a self-describing reading interface by
    explaining citekey lookup, the `rg`-friendly indexes, useful artifacts, and
    disposable noise. It names the fixed standard recipe outputs directly;
    those filenames may remain hardcoded while custom recipes are out of scope.
13. The supported indexes are `titles.md`, `authors.md`, `years.md`,
    `venues.md`, and `summaries.md`. Each contains one
    `<citekey>: <value>` line per paper with explicit placeholders for missing
    metadata. Rebuilding reconciles `indexes/` to this set, removing obsolete
    derived indexes such as the former `status.md`.

## Consequences

- The generated AGENTS.md must state the `papers/<citekey>/` convention
  since it differs from the flat sketch some may expect.
- The generated guide optimizes paper discovery and reading rather than
  explaining provenance internals or processing health.
- Operational processing state remains available in `paper.json` and the
  dashboard rather than occupying a research-discovery index.
- Deleting a paper directory manually leaves stale index lines; the
  validator reports this and `reindex` repairs it.
- A Git clone lacks `source/` PDFs; the validator classifies this as "not
  reprocessable", not an error.
- Re-importing identical PDF bytes is a metadata-only refresh. Different bytes
  are shown as a source replacement in the import preview; once accepted, hash
  comparison makes existing conversion and recipe outputs visibly stale
  without deleting them.
