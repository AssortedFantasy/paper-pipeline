# ADR-0003: Recipe template format

Status: Accepted, amended (2026-07-15)

## Context

Recipes need to be simple templates declaring input, output, and prompt, with
recorded provenance and a concrete, inspectable syntax.

## Decision

A recipe is a Markdown file with YAML front matter:

```markdown
---
name: contributions        # identifier; also the key in paper.json recipes map
version: 1                 # bump when the prompt changes meaningfully
input: transcription       # "transcription" | "pdf"
output: contributions.md   # filename directly inside the paper directory
---
Extract the key contributions in this paper.
Format them as a bulleted list.
Output only the contributions.
```

- Built-in recipes ship inside the application package
  (`paper_pipeline/recipes/builtin/`). Libraries never contain recipe
  definitions.
- The body is the literal prompt. No templating language in v1; the input
  artifact is attached/appended by the runner according to `input`.
- A result is valid when the provider call succeeds and the response is
  non-empty text. The runner writes only that output to
  `papers/<citekey>/<output>`; generated Markdown has no provenance
  frontmatter.

- Provenance never includes credentials, API endpoints, or raw provider
  payloads. `paper.json` records the same provenance in its `recipes` map.
- The `input` path in `paper.json` is always a
  library-relative POSIX path (`papers/<citekey>/transcription.md` or
  `papers/<citekey>/source/<file>.pdf`), consistent with ADR-0002. Storage
  validation enforces this invariant.
- `paper.json` also records the library-relative installed `output_artifact`.
  Recipe names and output filenames are intentionally independent contract
  fields, so validators and indexers never infer one from the other. Tests
  cover non-matching names.

## Consequences

- Structured JSON outputs, recipe chaining, and user-authored recipes are
  future ADRs; nothing here precludes them.
- `paper.json`, not the Markdown body or directory placement, identifies which
  flat files are generated and records their hashes and provenance.
