# AGENTS.md — Paper Pipeline v2

Paper Pipeline builds portable, folder-based paper libraries from Zotero RDF
exports: metadata, `transcription.md` files, LLM enrichment
outputs, and small text indexes that agents can search with `rg`.

The generated library is the product. The application is only the tool
that builds it. Paper Pipeline is a library builder — not a research
workspace, not a search service, not a note manager.

## Product scope

- Paper Pipeline is personal, local software. Hosted, commercial, and
  multi-tenant operation and migration of the former implementation's database
  are out of scope.
- Zotero RDF is a repeatable one-way import, not live or two-way sync. Later
  imports retain papers absent from the new export. Citekey is identity; an
  upstream citekey change creates a new paper and has no rename tracking.
- The filesystem is the agent reading interface. Paper Pipeline does not
  support agent-authored notes, metadata or note editing, or manual authoring
  workflows. Citation remains external (for example, BibTeX/LaTeX by citekey);
  the library defines no citation or evidence-reference format.
- Do not add an MCP or library-reading CLI, semantic/vector search, citation
  graphs, saved searches, cross-paper research workflows, workflow DAGs,
  daemon mode, plugin discovery, or multi-machine execution. The accepted
  single user-controlled SSH conversion host is the sole remote-execution
  exception.

## Files of Note

`README.md` - high level overview
`docs/adr/` - architecture decision records

## Commands

All commands run from the repository root. Use `uv`, never bare `pip`.

| Task | Command |
| --- | --- |
| Setup (core + dev tools) | `uv sync` |
| Setup with Marker/GPU | `uv sync --extra marker` |
| Install browser runtime | `uv run playwright install chromium` |
| Format | `uv run ruff format .` |
| Lint (fix) | `uv run ruff check --fix .` |
| Type check | `uv run pyright` |
| Default tests (offline) | `uv run pytest` |
| Clean-environment smoke | `uv run python scripts/smoke.py` |
| Browser/UI tests | `uv run pytest -m browser` |
| Update visual baselines | `uv run pytest -m browser tests/web/test_visual.py --update-snapshots` |
| GPU/Marker tests | `uv run pytest -m gpu` (explicit only; needs `marker` extra + GPU) |
| Real-LLM tests | `uv run pytest -m llm` (explicit only; needs credentials; costs money) |
| Start dashboard | `uv run paper-pipeline serve` |
| Environment diagnostics | `uv run paper-pipeline doctor` |
| Validate library | `uv run paper-pipeline validate <library>` |
| Rebuild indexes | `uv run paper-pipeline reindex <library>` |

Replace `<library>` with the library directory. The GPU suite uses local,
SHA-verified corpus paths documented in `tests/fixtures/README.md`; never add
the PDFs to the repository. Real-provider tests read
`PAPER_PIPELINE_LLM_API_KEY` and `PAPER_PIPELINE_LLM_MODEL` and may spend money.

The default `uv run pytest` must always pass with **no GPU, no network, no
credentials, and no optional extras installed**. Never move a test that needs those
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

## Library Rules

- **No second database.** Durable artifact truth lives in `paper.json` and the
  artifacts.
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
  artifacts on disk (non-empty `transcription.md`, declared flat recipe
  file) before recording success.
- Do not run real Marker or real LLM calls in the default dev loop or in
  unmarked tests. Use `tests/fakes.py`.
- Never leave a test or script holding a spawned child process; tests must
  clean up processes and temp dirs even on failure.

## Tests

- Be intentional about testing.
- Do NOT write tests that amount to checking if the implementation does exactly what it literally does.
- Consider the intentionality of the design and how it should behave in the face of future changes.
- Be mindful of test performance.
