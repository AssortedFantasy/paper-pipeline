# AGENTS.md — Paper Pipeline v2

Operational rules for agents developing Paper Pipeline. Read this fully
before changing anything.

## What this project is

Paper Pipeline builds portable, folder-based paper libraries from Zotero RDF
exports: metadata, high-quality `transcription.md` files, LLM enrichment
outputs, and small text indexes that agents can search with `rg`.

**The generated library is the product.** The application is only the tool
that builds it. Paper Pipeline is a library builder — not a research
workspace, not a search service, not a note manager.

## Sources of truth

| Question | Where to look | Lifetime |
| --- | --- | --- |
| What should the product do? | `REFACTOR.md` (requirements; do not contradict it) | scaffolding — deleted at WP-5.4 |
| What work exists and in what order? | `PLAN.md` (work packages) | scaffolding — deleted at WP-5.4 |
| How were contested decisions settled? | `docs/adr/` | permanent |
| How do I operate in this repo? | This file | permanent |
| Old implementation | `v1/` — **reference only. Never import from it, never modify it, never copy its patterns uncritically.** | scaffolding — deleted at WP-5.4 |

**Scaffolding rule:** `REFACTOR.md`, `PLAN.md`, and `v1/` exist only to build
v2 and will be deleted when the plan completes (WP-5.4). Do not reference
them from source code or docstrings — permanent references may only target
`AGENTS.md` or `docs/adr/`. Transient `WP-x.y` markers in docstrings are
allowed during development and are scrubbed at WP-5.4.

If REFACTOR.md and code disagree, REFACTOR.md wins. If you need a decision
that is not covered, write an ADR proposal rather than improvising a
convention.

## Commands

All commands run from the repository root. Use `uv`, never bare `pip`.

| Task | Command |
| --- | --- |
| Setup (core + dev tools) | `uv sync` |
| Setup with Marker/GPU | `uv sync --extra marker` |
| Setup with LLM SDK | `uv sync --extra llm` |
| Format | `uv run ruff format .` |
| Lint (fix) | `uv run ruff check --fix .` |
| Type check | `uv run pyright` |
| Fast tests (default) | `uv run pytest` |
| Clean-environment smoke | `uv run python scripts/smoke.py` |
| Browser/UI tests | `uv run pytest -m browser` (after `uv run playwright install chromium`) |
| GPU/Marker tests | `uv run pytest -m gpu` (explicit only; needs `marker` extra + GPU) |
| Real-LLM tests | `uv run pytest -m llm` (explicit only; needs credentials; costs money) |
| CLI | `uv run paper-pipeline <serve|doctor|validate|reindex>` |

The default `uv run pytest` must always pass with **no GPU, no network, no
credentials, and no extras installed**. Never move a test that needs those
into the default set; mark it `gpu`, `llm`, or `browser`.

## Architecture rules

Dependency direction (imports may only point downward):

```text
web client (templates/static)
    -> web API (paper_pipeline.web)
        -> application services (paper_pipeline.services)
            -> library | ingest | convert | recipes | jobs | indexes
                -> external edges: rdflib, Marker, LLM SDK, filesystem
```

- `paper_pipeline.library` imports nothing from other subpackages and never
  imports FastAPI, rdflib, Marker, or LLM SDKs.
- Business rules live in `services`. Routes, templates, JS, and CLI handlers
  translate; they never decide.
- Marker/torch may only be imported inside the conversion child-process
  entry point. `import paper_pipeline.<anything>` must work without extras.
- Conversion and recipes share the single job system in `paper_pipeline.jobs`.
  Never build a second queue, worker loop, or status store.

### Versioned contracts

These are deliberate compatibility boundaries, not permanently frozen designs:

- Library layout and file names — `library/paths.py` (ADR-0002)
- Serialized formats `library.json` / `paper.json` — `library/model.py` (ADR-0002)
- Converter contract — `convert/contract.py`
- LLM provider contract — `recipes/provider.py`
- Recipe template format — `recipes/model.py` (ADR-0003)
- Job model and scheduling policies — `jobs/model.py` (ADR-0004)

They may change as implementation feedback arrives. Change them deliberately:
update or supersede the relevant ADR, review the library format version when
serialized data changes, and update contract/compatibility tests in the same
work package. Do not let parallel tracks independently drift the same contract.

## Library and state ownership

Three kinds of state — keep them distinct:

1. **Library content** (essential): `library.json`, `papers/<citekey>/paper.json`,
   `source/`, `transcription.md`, `figures/`. Never regenerated or replaced
   silently. Explicit reruns/source replacement install atomically; import
   never deletes papers merely because they disappeared from a later export.
2. **Derived content** (rebuildable): `indexes/`, library `AGENTS.md`,
   library `.gitignore`, `generated/*`. Must be deterministically
   rebuildable from library content; deleting them loses nothing permanent.
3. **Operational state** (disposable): everything under any `.pp/`
   directory — logs, staging temp dirs, diagnostics. Deleting every `.pp/`
   must always be safe.

Hard rules:

- **No second database.** Durable artifact truth lives in `paper.json` and the
  artifacts. In-flight attempt markers under `.pp/attempts/` are disposable
  operational hints used to report interrupted work; losing them must not make
  a valid artifact invalid.
- **All stored paths are library-relative POSIX paths.** Writing an absolute
  path into any library file is a bug, always.
- **All writes are atomic**: stage under the library `.pp/tmp`, validate,
  rename into place. An output that is not validated and installed does not
  exist.
- **No secrets in libraries** — not in provenance, not in logs written under
  the library, not in generated files. Secrets come only from `AppConfig`
  (environment or the user-level config under `~/.paper-pipeline/`; never a
  library-local `.env`).

## Heavy work: GPU, child processes, concurrency

- Every conversion runs in a **fresh child process**; one conversion at a
  time globally (default). Cancellation must kill the whole process tree.
- Every paper mutation goes through the shared job system's **paper lane**.
  A lane is exclusive across conversion, recipe batches, and import apply;
  callers cannot opt out and must not write `paper.json` directly.
- Recipe batches are concurrent across papers and **strictly sequential within
  one paper lane** so the provider can reuse the same input context. Do not
  "optimize" this away.
- Never mark work successful from terminal output. Validate the expected
  artifacts on disk (non-empty `transcription.md`, declared `generated/`
  file) before recording success.
- Do not run real Marker or real LLM calls in the default dev loop or in
  unmarked tests. Use `tests/fakes.py`.
- Never leave a test or script holding a spawned child process; tests must
  clean up processes and temp dirs even on failure.

## Required checks by change class

Run these and ensure they pass before considering work done:

| Change | Required checks |
| --- | --- |
| Any code change | `uv run ruff format .` && `uv run ruff check --fix .` && `uv run pyright` && `uv run pytest` |
| Library format / schema / paths | Above + ADR updated or added + format-version review + atomic-write and validation tests updated |
| Ingestion | Above + fixture-based import tests (first import, re-import, refresh, missing attachment, duplicate candidate) |
| Conversion / jobs | Above + child-process failure/timeout/cancellation tests with the fake converter |
| Recipes / providers | Above + fake-provider tests incl. provenance and per-paper sequencing |
| Web API | Above + API contract tests |
| UI (templates/static/routes) | Above + `uv run pytest -m browser` incl. visual regression snapshots |
| Dependencies (`pyproject.toml`) | Above + confirm default `uv sync` stays GPU-free + `uv lock` committed |

## Definition of done

A work package is done when:

1. Scope matches PLAN.md — nothing missing, nothing extra.
2. All required checks above pass locally, from a clean `uv sync`.
3. New behavior is covered by tests in the correct marker category.
4. Success was verified from durable artifacts (files on disk), not logs.
5. Versioned contract changes include the ADR, format-version review, and
   compatibility-test updates they require.
6. PLAN.md's status column for the work package is updated.
7. No stray files: temp outputs, real PDFs, or credentials are not committed.

## Working style

- Small, reviewable increments; one work package per branch/PR unless
  PLAN.md says otherwise.
- Do not create documentation files beyond what a work package specifies.
- Do not add configuration options, plugins, or abstractions for
  hypothetical futures — REFACTOR.md's principle: complexity must earn its
  place.
- When blocked by an ambiguity, prefer the smallest decision consistent with
  REFACTOR.md, record it in the PR description, and flag it for review.
