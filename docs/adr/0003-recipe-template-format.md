# ADR-0003: Recipe template format

Status: Accepted

## Context

Built-in LLM recipes need a small, inspectable format that declares their input
and output.

## Decision

A recipe is Markdown with YAML front matter:

This is a simplified example, and is a poor prompt for a real recipe.

```markdown
---
name: contributions
version: 1
input: transcription
output: contributions.md
---
Extract the paper's key contributions.
```

`input` is `transcription` or `pdf`. `output` is a Markdown filename directly
inside the paper directory and must not collide with reserved library names.
The body is sent to the provider as the prompt; there is no template language.

Recipes ship with the application. A successful result is non-empty text,
installed atomically at the declared output path. `paper.json` records the
recipe version, input and output paths, hashes, provider, model, usage, and
cost. Generated Markdown contains no provenance front matter.

Stored provenance excludes credentials, API endpoints, and raw provider
payloads.

## Consequences

Recipe names and output filenames are independent contract fields. Libraries
contain recipe results but not recipe definitions. Structured outputs,
chaining, and user-authored recipes require a separate design decision.
