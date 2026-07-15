# PLAN.md — Paper Pipeline v2 Implementation Plan

This plan decomposes the REFACTOR.md requirements into work packages (WPs)
that agents can execute with minimal coordination. Read `AGENTS.md` first;
it defines the commands, rules, and definition of done that apply to every
WP here.

## How to work this plan

1. Pick the lowest-numbered WP whose **Depends on** entries are all `done`
   and whose status is `todo`.
2. Set its status to `in-progress` (edit the table below), do the work, run
   the required checks, set it to `done`.
3. Stay inside the WP's **Owns** file set. If you must touch a file owned by
   another in-flight WP, coordinate or wait — do not create merge conflicts.
4. Versioned contracts (see AGENTS.md) are shared boundaries, not untouchable
   skeletons. If feedback requires a change, amend/supersede the ADR, review
   the format version, and update compatibility tests before dependent tracks
   proceed.
5. Every WP includes its own tests. A WP without passing tests is not done.

## Status board

| WP | Title | Depends on | Status |
| --- | --- | --- | --- |
| 0.1 | Skeleton, tooling, contracts | — | done |
| 0.2 | Config + `doctor` command | 0.1 | todo |
| 0.3 | Contract feedback pass: lanes, recovery, source identity | 0.1 | done |
| 1.1 | Library storage core | 0.3 | todo |
| 1.2 | Staging + atomic artifact install | 1.1 | todo |
| 1.3 | Library validator | 1.1 | todo |
| 2A.1 | Zotero RDF parsing + fixtures | 0.3 | todo |
| 2A.2 | Import planning (preview) | 2A.1, 1.1 | todo |
| 2B.0 | Marker corpus, pins, runtime characterization | 0.1 | todo |
| 2B.1 | Fake converter + contract tests | 0.1 | todo |
| 2B.2 | Child-process conversion runner | 2B.1 | todo |
| 2B.3 | Marker adapter + GPU smoke test | 2B.0, 2B.2 | todo |
| 2B.4 | Remote conversion over SSH (conditional) | 2B.3 | conditional |
| 2C.1 | Recipe template parsing + built-ins | 0.3 | todo |
| 2C.2 | LLM providers (fake + OpenAI) | 0.3 | todo |
| 2C.3 | Recipe runner + provenance | 2C.1, 2C.2, 1.2 | todo |
| 2D.1 | Job queue, state machine, events | 0.3 | todo |
| 2D.2 | Scheduling policies, cancel, retry | 2D.1 | todo |
| 2D.3 | Attempt markers + completion validation | 2D.2, 1.1 | todo |
| 2E.1 | Index builders | 1.1 | todo |
| 2E.2 | Generated library AGENTS.md + .gitignore | 1.1 | todo |
| 3.0 | Library runtime registry + paper-session boundary | 1.2, 2D.3 | todo |
| 3.1 | Library services + CLI (`validate`, `reindex`) | 3.0, 1.3, 2E.1, 2E.2 | todo |
| 3.2 | Import services (preview + apply) | 3.0, 2A.2 | todo |
| 3.3 | Processing services (convert, recipes, cancel, retry) | 3.0, 2B.2, 2C.3 | todo |
| 4.1 | Web API + SSE | 3.1, 3.2, 3.3 | todo |
| 4.2 | UI shell + papers list + launch actions | 4.1 | todo |
| 4.3 | Import UI (preview/apply) | 4.1 | todo |
| 4.4 | Jobs dashboard | 4.1 | todo |
| 4.5 | Paper detail view | 4.1 | todo |
| 4.6 | Visual regression + designed edge states | 4.2–4.5 | todo |
| 5.1 | Clean-environment smoke test | 3.x, 4.1 | todo |
| 5.2 | End-to-end golden run (GPU) | 2B.3, 3.3 | todo |
| 5.3 | Docs polish + release checklist | all | todo |
| 5.4 | Decommission scaffolding (v1/, REFACTOR.md, PLAN.md) | 5.1–5.3 | todo |

### Parallelization guide

- After **1.1** lands, tracks **A, B, C, D, E** are mutually independent —
  up to five agents can run concurrently, one per track.
- **2B.0** (Marker corpus + pins) touches no shared code and can start
  immediately, in parallel with everything.
- After **3.0** establishes the one runtime/mutation boundary, Phase 3's
  user-facing service WPs are independent. Phase 4 WPs 4.2–4.5 are
  independent after 4.1.
- File ownership: track A owns `ingest/`, B owns `convert/`, C owns
  `recipes/`, D owns `jobs/`, E owns `indexes/`. `tests/fakes.py` is shared
  between B (FakeConverter) and C (FakeLLMProvider) — keep the classes
  disjoint.

---

## Phase 0 — Foundations

### WP-0.1 Skeleton, tooling, contracts — DONE

Delivered: `pyproject.toml`, package skeleton with responsibility docstrings,
versioned contracts (`library/paths.py`, `library/model.py`,
`convert/contract.py`, `recipes/provider.py`, `recipes/model.py`,
`jobs/model.py`), built-in recipe templates, ADRs 0001–0004, AGENTS.md, this
plan, sanity tests.

### WP-0.2 Config + `doctor` command

- **Owns:** `config.py`, `cli.py` (doctor path only), `tests/test_config.py`,
  `tests/test_doctor.py`.
- **Goal:** finished `AppConfig` loading (environment over
  `~/.paper-pipeline/.env`) and a
  `paper-pipeline doctor` command that reports, with actionable messages:
  Python version, package version, whether the `marker` extra is importable,
  whether LLM credentials and a model slug (`llm_model`) are configured
  (presence only — never print the key), and writability of a target
  directory if given.
- **Requirements:** doctor never triggers heavy imports at module scope;
  probe the `marker` extra in a subprocess or via `importlib.util.find_spec`.
  Exit code 0 when core is healthy even if optional extras are absent.
- **Tests:** config precedence (environment over home-directory `.env` over default); doctor output
  with and without extras (monkeypatched); secrets never appear in output.

### WP-0.3 Contract feedback pass: lanes, recovery, source identity — DONE

Amended ADR-0002/0004 and the skeleton contracts before implementation:
mandatory paper lanes across job categories, recipe batches as the cache-reuse
unit, disposable in-flight markers instead of durable `running` rewrites,
separate installed-artifact provenance from the latest attempt, and SHA-256
input identity so source replacement makes dependent outputs stale by
comparison rather than scattered invalidation code.

---

## Phase 1 — Library core (keystone; blocks the tracks that write libraries)

### WP-1.1 Library storage core

- **Owns:** `library/storage.py`, `tests/library/test_storage.py`.
- **Goal:** implement the surface sketched in the `storage.py` docstring:
  `create_library`, `open_library`, `Library.list_papers`, `read_paper`,
  `write_paper`, `operational_dir`.
- **Requirements:**
  - `create_library` writes `library.json` (format_version 1), creates
    `papers/`, `indexes/`, `.pp/`; refuses a non-empty directory.
  - `open_library` validates format version; a newer version fails with an
    actionable message.
  - `write_paper` is atomic: serialize to `.pp/tmp/<rand>`, `os.replace`
    into place. Citekey validated against `paths.CITEKEY_PATTERN` plus
    Windows reserved names.
  - All serialized paths are relative POSIX; add an invariant test that
    validates path-typed fields (bibliographic text is not a path).
  - Source files are hashed with SHA-256 while being staged; `paper.json`
    stores `source_sha256`. Helpers derive conversion/recipe freshness by
    comparing recorded input hashes, never by manually toggling stale flags.
  - Storage's raw write methods are infrastructure APIs. Phase 3 services may
    mutate papers only through a `PaperSession` supplied inside a paper lane.
  - `list_papers` tolerates and reports (not raises) invalid paper dirs.
- **Tests:** round-trips, atomicity under simulated interruption (kill
  between temp write and rename → library still opens, no partial files),
  invalid citekeys rejected, relative-path invariant, opening a library
  copied to a different absolute location works unchanged.

### WP-1.2 Staging + atomic artifact install

- **Owns:** artifact-install portion of `library/storage.py`,
  `tests/library/test_artifacts.py`.
- **Goal:** `Library.stage_dir()` (fresh temp dir under `.pp/tmp`) and atomic
  installation for a single artifact or a declared conversion bundle
  (`transcription.md` plus `figures/`), with validation hooks and hashes.
- **Requirements:** install is rename-based on the same filesystem; replaces
  existing artifact atomically; cleans up abandoned staging dirs older than
  the current process on request (`Library.clean_stale_staging()`).
- **Tests:** file and bundle installs, replacement, interrupted install leaves
  the prior recorded artifact valid or a detectable hash mismatch (never a
  false success), staging cleanup never touches installed content.

### WP-1.3 Library validator

- **Owns:** `library/validation.py`, `tests/library/test_validation.py`.
- **Goal:** implement the checks listed in the module docstring, returning a
  structured report (`problems: [{severity, citekey?, message, action}]`).
- **Requirements:** validation is read-only; distinguishes `error`
  (corruption/invariant breach), `warning` (stale index, missing source in a
  clone), and `info`. Missing `source/` PDFs are "not reprocessable"
  warnings, not errors.
- **Tests:** healthy library; each problem class synthesized in a temp
  library; manually deleted paper dir detected as index staleness (with a
  hand-written index file until 2E.1 lands).

---

## Phase 2 — Parallel tracks

### Track A — Zotero ingestion

#### WP-2A.1 Zotero RDF parsing + fixtures

- **Owns:** `ingest/rdf.py`, `tests/fixtures/zotero/`, `tests/ingest/test_rdf.py`.
- **Goal:** parse a Zotero RDF export directory into `ImportRecord` objects
  (define the dataclass in `ingest/rdf.py`: `metadata: PaperMetadata`,
  `attachment_path: Path | None`, `attachment_sha256: str | None`,
  `problems: list[str]`). Hash the selected attachment while reading it.
- **Requirements:**
  - Support journal articles, conference papers, preprints, books/chapters;
    map to `PaperMetadata` fields; unknown item types produce a record with
    a problem note, never a crash.
  - Citekey source: Zotero's Better BibTeX citation key if present in the
    export; otherwise flag the record as problem "no citekey" (do not
    invent citekeys silently).
  - Locate PDF attachments via the export's `files/` links; missing files
    are per-record problems.
  - Build 2–3 small committed fixture exports (tiny dummy PDFs) covering:
    clean import, missing attachment, duplicate DOI pair, odd item type.
- **Tests:** fixture-driven parse assertions; malformed RDF fails with a
  clear message; no absolute fixture paths leak into `PaperMetadata`.

#### WP-2A.2 Import planning (preview)

- **Owns:** `ingest/plan.py`, `tests/ingest/test_plan.py`.
- **Goal:** `build_import_plan(library, records) -> ImportPlan` with
  `additions`, metadata-only `refreshes`, explicit `source_replacements`,
  `problems`, and duplicate candidates (same DOI or normalized-title match
  under a different citekey). Identical source hashes are metadata-only;
  changed bytes are never hidden inside an ordinary refresh.
- **Requirements:** pure function over data; never touches the filesystem
  beyond reading the library; plan is fully serializable for the preview UI.
- **Tests:** first import, identical re-import, metadata-only refresh, changed
  PDF surfaced as source replacement, mixed plan, duplicate candidate surfaced
  not merged, invalid citekey routed to problems.

### Track B — Conversion

#### WP-2B.0 Marker corpus, pins, runtime characterization

- **Owns:** `tests/fixtures/corpus/manifest.json`, `marker` extra pins in
  `pyproject.toml`, a short report posted in the PR (not committed as a doc
  file).
- **Goal:** Marker quality has already been informally validated on one
  paper and looks good; this WP broadens that to a representative corpus and
  nails down the environment. Assemble the PDF corpus (native text,
  multi-column, equations, tables, figures, scanned; record source URLs in
  `manifest.json`), run Marker across it on the target GPU machine, and
  report: per-class quality notes, runtime per paper, VRAM peak, and the
  exact torch/CUDA pinning the `marker` extra needs (v1 required a cu128
  index on Windows).
- **Outcome:** committed corpus manifest + committed extra pins. Only
  escalate (ADR-0001 amendment) if a whole document class fails badly —
  full no-go is considered unlikely. The measured per-paper runtimes also
  give the owner the data to decide whether local conversion is fast
  enough or WP-2B.4 (remote delegation) is warranted.

#### WP-2B.1 Fake converter + contract tests

- **Owns:** `FakeConverter` in `tests/fakes.py`, `tests/convert/test_contract.py`.
- **Goal:** a configurable fake implementing `Converter`: writes a
  deterministic `transcription.md` (+ optional figure files) into
  `staging_dir`; modes for success, failure (`ok=False`), crash (raises),
  hang (sleeps past timeout), empty output.
- **Tests:** contract semantics — `ok=True` implies non-empty transcription
  in staging; result paths are inside `staging_dir`.

#### WP-2B.2 Child-process conversion runner

- **Owns:** `convert/runner.py`, `tests/convert/test_runner.py`.
- **Goal:** `run_conversion(converter_spec, request) -> ConversionResult`
  executing the converter in a fresh child process (spawn), with timeout
  kill, cancellation (kill process tree), exit-code/exception mapping to
  `ok=False` results, and stdout/stderr captured into `diagnostics`.
- **Requirements:** the child entry point imports the backend lazily; the
  parent never imports Marker. Works with `FakeConverter` via a
  module-path + kwargs spec so tests never need Marker.
- **Tests (use FakeConverter):** success, converter failure, child crash,
  timeout kill, cancellation mid-run, staging dir cleaned on failure, no
  orphan processes after each test.

#### WP-2B.3 Marker adapter + GPU smoke test

- **Owns:** `convert/marker.py`, `tests/convert/test_marker_gpu.py`
  (marked `gpu`).
- **Goal:** Marker adapter per the module docstring, using the pins decided
  in WP-2B.0; normalize Marker output (markdown, images, metadata) into
  `ConversionResult`; capture backend version.
- **Tests:** `gpu`-marked smoke test converting one small corpus PDF
  end-to-end through the runner, asserting non-empty transcription and
  figure handling; skipped cleanly when extra/GPU absent.

#### WP-2B.4 Remote conversion over SSH (conditional)

- **Status:** conditional — the owner decides go/no-go using WP-2B.0's
  runtime measurements; requires a small ADR (ADR-0005) before
  implementation.
- **Motivation:** the development machine is a laptop with a weak NVIDIA
  GPU, and Marker may be too slow on it. The owner has an Ubuntu server
  (reachable as `ssh noesis`, RTX 3090) and wants the option to delegate
  conversion there. This is one remote *backend* behind the existing
  converter contract — not distributed processing, which remains a
  REFACTOR.md non-goal.
- **Owns:** `convert/remote.py`, `tests/convert/test_remote.py`, ADR-0005.
- **Goal:** run Marker on the remote host instead of a local child
  process. The converter contract already supports this shape: a remote
  runner copies `request.pdf_path` to the host, runs the same
  child-process entry point there, and syncs results back into
  `request.staging_dir`; validation and atomic install are unchanged. The
  job queue still sees one `CONVERSION` category at concurrency 1.
- **Requirements:** host/alias comes from `AppConfig` (never stored in the
  library); cancellation kills the SSH client *and* the remote process
  (`ssh -tt` or a remote pidfile + kill); a dead connection maps to
  `ok=False` with a clear error, never a hang past timeout.
- **Tests:** default-suite tests fake the transport (local "remote" via
  subprocess); an opt-in marked test exercises a real host if configured.

### Track C — Recipes

#### WP-2C.1 Recipe template parsing + built-ins

- **Owns:** parsing code in `recipes/model.py`, `recipes/builtin/`,
  `tests/recipes/test_model.py`.
- **Goal:** `parse_recipe(text) -> RecipeDefinition` and
  `load_builtin_recipes() -> dict[str, RecipeDefinition]` (loaded via
  `importlib.resources`).
- **Requirements:** strict validation (known fields only, `output` must be a
  bare `.md` filename with no path separators, body non-empty); clear error
  messages naming the offending field.
- **Tests:** both built-ins parse; each validation failure mode; parsed
  prompt preserves the body verbatim.

#### WP-2C.2 LLM providers (fake + OpenAI)

- **Owns:** `FakeLLMProvider` in `tests/fakes.py`, OpenAI adapter in
  `recipes/openai_provider.py` (a separate module — `recipes/provider.py`
  is the versioned contract module and stays adapter-free),
  `tests/recipes/test_providers.py`.
- **Goal:** fake provider (canned responses, call recording, failure/delay
  modes) and an OpenAI-compatible adapter supporting text input and PDF
  input (file upload), configured from `AppConfig`.
- **Requirements:** the adapter imports `openai` lazily (`llm` extra);
  errors map to `ok=False` with a safe message — never echo the API key or
  full request. Respect `llm_base_url` for compatible endpoints. Provider
  instances live on `LibraryRuntime`; the adapter may reuse uploaded PDF IDs
  by `ProviderRequest.input_sha256` during sequential recipe batches.
- **Tests:** fake provider behavior and mocked-adapter request/cache behavior
  run in the default suite; a real adapter smoke is marked `llm`.

#### WP-2C.3 Recipe runner + provenance

- **Owns:** `recipes/runner.py`, `tests/recipes/test_runner.py`.
- **Goal:** implement the 5-step flow in the module docstring, producing a
  staged output file for `Library.install_artifact` and a `RecipeRecord`.
- **Requirements:** missing declared input fails fast with a clear error;
  front matter exactly per ADR-0003; provenance recorded in the returned
  `RecipeRecord`, including input/output SHA-256; output validated non-empty
  before staging succeeds.
- **Tests (FakeLLMProvider):** success with front matter asserted, missing
  transcription input, provider failure, empty response rejected, provenance
  contains no credential-like config values.

### Track D — Jobs

#### WP-2D.1 Job queue, state machine, events

- **Owns:** `jobs/queue.py`, `jobs/events.py`, `tests/jobs/test_queue.py`.
- **Goal:** an async in-process `JobQueue`: resource-aware enqueue -> `Job`,
  legal state transitions only, and a subscription-based event bus.
- **Requirements:** no HTTP or library imports; expose separate
  `enqueue_paper`, `enqueue_library_read`, and `enqueue_library_write` methods.
  `enqueue_paper` acquires the `(library_key, citekey)` lane internally before
  invoking an injected async callable. There is no unlocked paper enqueue API.
- **Tests:** transition legality, event ordering, subscriber isolation
  (slow subscriber cannot block the queue).

#### WP-2D.2 Scheduling policies, cancel, retry

- **Owns:** scheduling in `jobs/queue.py`, `tests/jobs/test_scheduling.py`.
- **Goal:** ADR-0004 policies: conversion concurrency 1; recipe-batch
  concurrency N across papers; every operation within a paper lane is
  exclusive across categories; library-write barriers exclude paper lanes;
  library reads remain non-exclusive. Cancel queued immediately and running
  work through a cooperative token plus process-tree kill hook.
- **Tests:** two conversions never overlap; a conversion, recipe batch, and
  import for one paper can never overlap; different paper recipe batches do;
  recipe names inside one batch run sequentially on the same provider; reindex
  excludes paper lanes while validate does not; cancel/retry and clean shutdown.

#### WP-2D.3 Attempt markers + completion validation

- **Owns:** `jobs/recovery.py`, queue completion hooks,
  `tests/jobs/test_recovery.py` (no library-model imports; storage callbacks
  are injected and wired by WP-3.0).
- **Goal:** a job may only reach `SUCCEEDED` after its completion validator
  confirms and hashes expected artifacts. Atomically create a disposable
  `.pp/attempts/<job-id>.json` before external work; remove it only after the
  completed attempt/artifact record is durable. Startup scans leftover markers
  into interrupted, retryable views without rewriting `paper.json`.
- **Tests:** missing/empty or hash-mismatched artifact fails completion; crash
  before terminal record leaves a marker and preserves the prior artifact
  record; startup synthesizes interrupted work; a marker whose attempt is
  already terminal is cleaned as stale; deleting `.pp/` loses only diagnostics.

### Track E — Indexes and generated guidance

#### WP-2E.1 Index builders

- **Owns:** `indexes/build.py`, `tests/indexes/test_build.py`.
- **Goal:** `rebuild_indexes(library)` producing `indexes/titles.md`,
  `authors.md`, `summaries.md` (from `generated/summary.md`: front matter
  stripped, then the first body line — which the summary recipe requires
  to be a one-sentence TL;DR), `status.md` (papers with missing, stale, or
  most-recently-failed outputs). Freshness is derived from recorded input
  hashes. Format: one line
  per paper, `<citekey>: <value>`, sorted by citekey, LF endings.
- **Requirements:** deterministic (byte-identical on unchanged input);
  written atomically; entries for missing paper dirs dropped; wholly derived
  from paper content.
- **Tests:** determinism, staleness repair after deleting a paper dir,
  empty library produces valid empty indexes, summaries index distinguishes
  "no summary yet".

#### WP-2E.2 Generated library AGENTS.md + .gitignore

- **Owns:** `indexes/agents_md.py`, `tests/indexes/test_agents_md.py`.
- **Goal:** generate the library's root `AGENTS.md` (per the module
  docstring: layout, citekey lookup under `papers/`, source-derived vs
  LLM-generated distinction, index catalog, `.pp/` is ignorable; ≤ ~60
  lines) and `.gitignore` (`**/.pp/`, `papers/*/source/`).
- **Tests:** golden-file comparison; regeneration is deterministic; content
  mentions `papers/<citekey>/` and the generated/ provenance rule.

---

## Phase 3 — Application services (integration; one implementation for CLI and web)

### WP-3.0 Library runtime registry + paper-session boundary

- **Owns:** `services/runtime.py`, `tests/services/test_runtime.py`.
- **Goal:** a process-wide registry owns one shared `JobQueue`/event bus and,
  keyed by resolved library root, returns one `LibraryRuntime` per open
  library. Each runtime owns provider instances and its raw `Library` storage
  object while using the application queue.
- **Requirements:** user-facing services receive a runtime, not an independently
  opened storage object. `LibraryRuntime.enqueue_paper(...)` delegates to the
  queue and supplies the worker a citekey-scoped `PaperSession` only after its
  lane is held. `PaperSession` is the service-layer surface for record updates,
  staging, and artifact installs. Library-read/write barrier methods are also
  exposed here. Reopening the same resolved path reuses the runtime.
- **Tests:** same path (including equivalent relative/case variants) returns the
  same runtime; different libraries do not share paper lanes; service modules
  outside `runtime.py` do not open storage directly; deliberate cross-category
  read-modify-write races preserve every field.

### WP-3.1 Library services + CLI (`validate`, `reindex`)

- **Owns:** `services/` (library ops), `cli.py` wiring,
  `tests/services/test_library_services.py`.
- **Goal:** `create_library`, `open_library`, `validate_library`,
  `rebuild_indexes` services (thin orchestration over Phase 1/2E), wired to
  `paper-pipeline validate <path>` and `paper-pipeline reindex <path>`.
- **Requirements:** reindex also regenerates `AGENTS.md` and `.gitignore` and
  uses the runtime's library-write barrier; validate uses its read operation.
- **Tests:** service-level (no HTTP); CLI exit codes and human-readable
  output for healthy and problematic libraries.

### WP-3.2 Import services (preview + apply)

- **Owns:** `services/` (import ops), `tests/services/test_import_services.py`.
- **Goal:** `preview_import(runtime, export_path) -> ImportPlan` and
  `apply_import(runtime, plan) -> ImportReport`. Apply schedules one paper-lane
  operation per accepted record: create/refresh metadata and stage/copy PDFs.
  Ordinary refresh preserves processing records. An accepted source replacement
  updates `source_sha256`; existing outputs remain but automatically read stale
  because their recorded input hashes no longer match.
- **Requirements:** import never deletes papers; re-running the same apply
  is idempotent; a failed copy leaves no half-created paper dir.
- **Tests:** first import, additive re-import, metadata-only refresh preserving
  artifact provenance, explicit source replacement becomes stale without
  deleting old outputs, missing attachment skip, idempotency, and interruption
  mid-apply leaves the library valid; import cannot overlap conversion for the
  same citekey.

### WP-3.3 Processing services (convert, recipes, cancel, retry)

- **Owns:** `services/` (processing ops), `tests/services/test_processing_services.py`.
- **Goal:** `queue_conversion(runtime, citekeys)`,
  `queue_recipes(runtime, recipe_names, citekeys)`, `cancel_job(id)`,
  `retry_job(id)`; selection helpers for "all pending". Wires runner +
  recipes + jobs + storage: create attempt marker → execute within paper lane →
  validate/hash → atomic install → record completed artifact/attempt → remove
  marker. It never writes `running` into `paper.json`.
- **Tests (fakes only):** full conversion and recipe flows through the real
  queue into a temp library; failure recorded in `paper.json` with log path
  under `.pp/`; cancel mid-conversion kills the child and records
  `cancelled`; a failed rerun preserves last-good artifact provenance; retry
  succeeds after transient fake failure; recipe batches reuse one provider
  instance/input context per paper.

---

## Phase 4 — Web API and UI

### WP-4.1 Web API + SSE

- **Owns:** `web/app.py`, `web/api.py`, `tests/web/test_api.py`.
- **Goal:** JSON/fragment routes over services: library open/create/validate/
  reindex; papers list with filter params; paper detail; import preview and
  apply; job list; queue/cancel/retry; `GET /events` SSE stream forwarding
  the job event bus. `paper-pipeline serve` starts uvicorn.
- **Requirements:** routes contain no business logic; API responses are
  service models serialized, not ad-hoc dicts; SSE stream survives client
  disconnect without affecting jobs. The app uses the shared runtime registry,
  never a per-request queue or independently opened library. The server binds
  localhost by default.
- **Tests:** API contract tests with httpx + fakes; SSE delivers transition
  events; disconnecting a client does not cancel a running job.

### WP-4.2 UI shell + papers list + launch actions

- **Owns:** `web/templates/` (base, papers), `web/static/` (vendored htmx,
  `app.css`), `tests/web/test_ui_papers.py` (marked `browser`).
- **Goal:** base layout with navigation and SSE-driven job status strip;
  papers table with filters (text, conversion state, recipe state), row
  selection, actions: convert selected, run selected recipe batch, select-all
  -pending. Stable URLs: `/`, `/papers`.
- **Requirements:** selection is the only client-owned state; all data
  rendering is server-side fragments; designed empty and error states; no
  inline DOM manipulation outside htmx swaps.
- **Tests (browser):** load, filter, select, launch (against fakes), empty
  library state.

### WP-4.3 Import UI

- **Owns:** `web/templates/` (import), `tests/web/test_ui_import.py` (browser).
- **Goal:** `/import`: pick export path + library, render the ImportPlan
  preview (additions, refreshes, problems, duplicate candidates), apply with
  a progress/result report.
- **Tests (browser):** preview render for a fixture export, apply flow,
  problems prominently displayed, cancel-before-apply leaves library
  untouched.

### WP-4.4 Jobs dashboard

- **Owns:** `web/templates/` (jobs), `tests/web/test_ui_jobs.py` (browser).
- **Goal:** `/jobs`: queued/running/terminal lists updating via SSE,
  per-job log tail view, cancel and retry buttons; interrupted work rows
  synthesized from `.pp/attempts/` markers (ADR-0004), labeled and retryable.
- **Tests (browser):** live update on state change, cancel and retry flows,
  disconnected state (SSE dropped) shows a designed indicator and recovers.

### WP-4.5 Paper detail view

- **Owns:** `web/templates/` (paper), `tests/web/test_ui_paper.py` (browser).
- **Goal:** `/papers/{citekey}`: metadata, processing status, rendered
  transcription, `generated/` outputs with their provenance front matter
  displayed as "LLM-generated" badges, figures gallery, link to source PDF
  served if present.
- **Tests (browser):** full paper, unconverted paper (designed empty
  states), missing-source clone case.

### WP-4.6 Visual regression + designed edge states

- **Owns:** `tests/web/test_visual.py` (browser), snapshot assets.
- **Goal:** Playwright screenshot baselines for: papers table (filled +
  empty), paper detail, jobs view (running + failed + interrupted), import
  preview, and error/disconnected states. Document the snapshot-update
  command in AGENTS.md's UI row.
- **Tests:** the snapshots themselves; a documented, deterministic viewport
  and dataset (seeded fake library) so diffs are meaningful.

---

## Phase 5 — Hardening and release

### WP-5.1 Clean-environment smoke test

- **Owns:** `tests/test_smoke.py` (marked `slow`), CI-ready script.
- **Goal:** from a clean `uv sync` in a temp checkout: create a library,
  apply a fixture import, run a fake-converter conversion and fake-provider
  recipe through the real CLI/services, reindex, validate — using only
  commands documented in AGENTS.md.
- **Tests:** the smoke test is the deliverable; it must not depend on
  extras, network, or GPU.

### WP-5.2 End-to-end golden run (GPU)

- **Owns:** `tests/test_golden_gpu.py` (marked `gpu`), golden assets.
- **Goal:** convert 2–3 corpus PDFs with real Marker, assert structural
  expectations (headings present, table/figure counts within tolerance)
  rather than byte equality; record runtime as a report line.
- **Tests:** the golden run; skipped cleanly without GPU/extra.

### WP-5.3 Docs polish + release checklist

- **Owns:** `README.md`, `AGENTS.md` final pass, `docs/`.
- **Goal:** verify every command in AGENTS.md verbatim on a clean machine;
  fill in anything WPs changed; confirm REFACTOR.md "First Useful Version"
  bullets are all either delivered or explicitly deferred with a note;
  update the status board to reflect reality.

### WP-5.4 Decommission scaffolding

- **Owns:** repository root cleanup. **Requires explicit owner sign-off
  before deleting anything.**
- **Goal:** the repo's transitional artifacts exist only to build v2;
  keeping them past completion is tech debt. After WP-5.3:
  1. Migrate any still-binding requirement statements from REFACTOR.md into
     AGENTS.md or a new ADR (most are already covered by ADRs 0001–0004).
  2. Delete `v1/` and `REFACTOR.md`.
  3. Delete PLAN.md (git history is the archive).
  4. Scrub transient references from the codebase: `rg -l "REFACTOR|PLAN\.md|WP-[0-9]|v1/" src tests docs README.md AGENTS.md`
     must come back clean afterwards (ADR history sections excepted).
  5. Update AGENTS.md's sources-of-truth table to its permanent form.
- **Tests:** full required-checks pass after deletion; the `rg` sweep above
  is the acceptance check.

---

## Out of scope (do not build, per REFACTOR.md non-goals)

MCP support, agent CLI for reading libraries, semantic/vector search,
citation graphs, saved searches, cross-paper research workflows, note
management, v1 migration, live Zotero sync, metadata editing UI, workflow
DAGs, multi-machine execution, daemon mode, plugin discovery.
