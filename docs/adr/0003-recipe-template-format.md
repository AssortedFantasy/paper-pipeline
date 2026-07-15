# ADR-0003: Recipe template format

Status: Accepted, amended (2026-07-15)

## Context

REFACTOR.md requires recipes to be simple templates declaring input, output,
and prompt, with recorded provenance and a deferred concrete syntax.

## Decision

A recipe is a Markdown file with YAML front matter:

```markdown
---
name: contributions        # identifier; also the key in paper.json recipes map
version: 1                 # bump when the prompt changes meaningfully
input: transcription       # "transcription" | "pdf"
output: contributions.md   # filename inside the paper's generated/ directory
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
  non-empty text; the runner writes it to `generated/<output>` with YAML
  front matter provenance:

```markdown
---
generated_by: paper-pipeline
recipe: contributions
recipe_version: 1
provider: openai
model: gpt-5
input: papers/smith2024/transcription.md
input_sha256: <sha256-of-transcription>
created: 2026-07-14T12:00:00Z
---
- The paper introduces ...
```

- Provenance never includes credentials, API endpoints, or raw provider
  payloads. `paper.json` records the same provenance in its `recipes` map.
- The `input` path in generated front matter and `paper.json` is always a
  library-relative POSIX path (`papers/<citekey>/transcription.md` or
  `papers/<citekey>/source/<file>.pdf`), consistent with ADR-0002. The original
  paper-relative example was clarified before v2 library production. This does
  not change the serialized schema, so format version 1 remains appropriate;
  storage compatibility tests enforce the clarified invariant.

## Consequences

- Structured JSON outputs, recipe chaining, and user-authored recipes are
  future ADRs; nothing here precludes them.
- The front matter means `generated/` files are self-describing: an agent
  can always tell LLM output from source-derived text.
