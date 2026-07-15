# Release checklist

This is the honest acceptance record for the first useful Paper Pipeline v2
release. “Delivered” means the behavior exists with automated evidence;
owner-deferred and optional external workflows are kept separate from the
offline release gates.

## Current decision

**No known first-useful-release product blockers remain.** Direct dashboard
library lifecycle and maintenance controls and selected-batch retry are now
delivered with browser coverage. The owner approved removal of the transitional
implementation artifacts after preserving its two PDF-analysis prompts as
packaged recipes, moving the license to the repository root, and relocating
the credential file outside the repository.

Local Marker/OCR execution on the target laptop is explicitly deferred by
owner safety direction. The three-document, SHA-verified structural golden
suite remains available for an approved stronger or remote machine and emits
`GOLDEN_RUNTIME` report lines, but its execution is not an offline release
gate. The environment-gated real-SSH smoke passed on `noesis`.

## Requirement audit

### Library creation and maintenance

| Requirement | Status and evidence |
| --- | --- |
| Create a library in a selected directory | Delivered through service, API, and the dashboard setup panel, with browser coverage. |
| Open and inspect an existing library | Delivered through service, API, and dashboard controls; paper browsing/detail is covered by browser tests. |
| Import Zotero RDF metadata and attachments | Delivered by RDF parser and import services with fixture coverage. |
| Preview additions and metadata refreshes | Delivered in service/API/import dashboard tests. |
| Add later exports without rebuilding | Delivered; additive re-import preserves absent existing papers. |
| Replace Zotero-owned metadata on refresh | Delivered with provenance-preservation tests. |
| Detect missing attachments and invalid paper directories | Delivered by import problems and library validation. |
| Avoid silent duplicate entries | Delivered as advisory DOI/title duplicate candidates in preview; no automatic merge. |
| Validate with actionable problems | Delivered in validator, service, API, and CLI. |
| Rebuild indexes and generated guidance | Delivered in service, API, and CLI. |
| Distinguish managed, source, and disposable state | Delivered by the versioned layout, generated guidance, and `.pp/` policy. |

### Document conversion

| Requirement | Status and evidence |
| --- | --- |
| Marker backend after representative quality/runtime evaluation | Delivered as the adapter, manifest, bounded GPU smoke, and structural golden suite. Local execution is owner-deferred for laptop safety and remains available on an approved stronger/remote machine. |
| Produce non-empty `transcription.md` | Delivered and validated from staged artifacts before success. |
| Preserve headings, equations, tables, figures, captions, references where practical | Implemented through Marker Markdown normalization with versioned structural expectations; local real-corpus execution is owner-deferred. |
| Preserve extracted figures | Delivered by safe figure installation and contract tests. |
| Generate representative PDF page images | Delivered as validated 96-DPI `pages/pageN.png` files in the atomic conversion bundle. |
| Support native text and attempt scanned PDFs | Delivered through the general Marker adapter; scanned quality is backend-dependent and not a byte-level promise. |
| Process one, selected, or all pending papers | Delivered by processing selection services and API/dashboard actions. |
| Fresh child process per conversion | Delivered with failure, crash, timeout, cancellation, cleanup, and orphan-process tests. |
| Record state, timing, backend version, and diagnostics | Delivered in records, attempts, and disposable logs. |
| Retry failed papers | Delivered for individual failed/interrupted jobs. |
| Validate artifacts rather than terminal output | Delivered by runner and completion validation. |

### Agent-oriented indexes and retrieval

| Requirement | Status and evidence |
| --- | --- |
| Generate root `AGENTS.md` with search guidance | Delivered. |
| Keep transcription and generated output searchable with `rg` | Delivered as ordinary UTF-8 Markdown. |
| Generate small rebuildable text indexes | Delivered for titles, authors, summaries, and status. |
| Include title, citekey, authors, and summaries | Delivered with deterministic index tests. |
| Visibly separate source-derived and LLM-generated content | Delivered by `paper.json`: it declares flat recipe outputs and their provenance. |
| Stable citekey-to-content paths | Delivered as `papers/<citekey>/`. |
| Generate VCS guidance for operational noise | Delivered as a deterministic `.gitignore`. |

### Analysis and enrichment recipes

| Requirement | Status and evidence |
| --- | --- |
| Built-in analysis recipes | Delivered as packaged `summary`, `contributions`, `intro`, and `method` recipes; the latter two preserve the v1 PDF-native review prompts. |
| Run over one or selected papers | Delivered as the selected recipe/paper cartesian product in one queued action. |
| Simple template defining input and prompt | Delivered as Markdown plus validated YAML front matter. |
| Predictable Markdown output per paper | Delivered under `papers/<citekey>/`. |
| Record recipe/provider/model/input/usage provenance | Delivered in `paper.json`, without credentials or output frontmatter. |
| Show recipe progress/failures with conversion | Delivered live per paper and in the shared Jobs dashboard. |
| Retry failed recipes | Delivered for individual jobs. |
| Keep execution linear | Delivered; recipes are sequential within a paper lane. |

### Jobs and operations

| Requirement | Status and evidence |
| --- | --- |
| Present queued/running/succeeded/failed/cancelled work | Delivered in shared job models, API, SSE, and dashboard. |
| Serialize heavy local conversion | Delivered with global conversion concurrency of one by default. |
| Use fresh conversion children | Delivered. |
| Concurrent cross-paper provider work, sequential same-paper work | Delivered and covered by scheduling/cache-reuse tests. |
| Cancel current and queued work | Delivered with process-tree cancellation tests. |
| Retry individual failures or selected batches | Delivered, including selected failed/cancelled batch retry with browser coverage and individual interrupted-attempt retry. |
| Keep useful ignorable logs and diagnostics | Delivered under `.pp/`. |
| Browser closure does not cancel jobs | Delivered by server-owned runtime and SSE disconnect tests. |
| Validate disk outputs before completion | Delivered. |
| Recover conservatively after interruption | Delivered with disposable attempt markers and recovery tests. |

### Browser UI and engineering

| Requirement | Status and evidence |
| --- | --- |
| Choose or create a library | Delivered through the dashboard library setup panel with browser coverage. |
| Preview/apply Zotero RDF | Delivered, including problems, duplicates, explicit replacements, progress, and safe cancel. |
| Browse/filter by metadata and processing state | Delivered. |
| Select and launch conversion/recipes | Delivered with all/none/pending controls and multi-recipe checklists. |
| Show queue, live progress, failures, and logs | Delivered through Jobs, SSE, and safe log tails. |
| Retry/cancel work | Delivered for individual work and selected failed/cancelled retry batches; interrupted attempts can be retried individually. |
| Inspect metadata, transcription, recipes, and figures | Delivered by the stable paper detail view. |
| Inspect source PDF or representative pages | The dashboard provides a safe source-PDF link; conversion also emits directly inspectable `pages/pageN.png` files. |
| Rebuild and validate from the dashboard | Delivered through active-library maintenance controls with browser coverage. |
| Single owner for library/job UI state | Delivered with server-rendered htmx fragments and process runtime. |
| Avoid duplicated client truth and full/detail behavior | Delivered; client scripts handle connection presentation only. |
| Reuse statuses/actions and stable URLs | Delivered through shared fragments and `/papers`, `/import`, `/jobs`, `/papers/<citekey>`. |
| Designed empty/loading/error/disconnected/cancelled/interrupted states | Delivered with browser and visual coverage. |
| Real-browser and visual regression coverage | Delivered with Playwright tests and committed baselines. |
| No out-of-model inline DOM manipulation | Delivered; connection-state behavior is isolated in one static script. |

### Portability, configuration, and secrets

| Requirement | Status and evidence |
| --- | --- |
| Copy a complete library without breaking it | Delivered; serialized paths are relative POSIX paths. |
| Remain useful without Paper Pipeline running | Delivered as ordinary JSON, Markdown, PDFs, images, and text indexes. |
| Rebuild derived indexes after changes | Delivered. |
| Ignore disposable noise in version control | Delivered by generated `.gitignore`. |
| Make essential/derived/disposable content clear | Delivered in generated guidance and the layout contract. |
| Keep application configuration outside libraries | Delivered through environment/user-level config. |
| Never store credentials in artifacts, logs, or provenance | Delivered with safe provider errors and provenance tests. |
| Ship recipe definitions with the application | Delivered. |
| Keep configuration small and default-driven | Delivered; provider and converter settings are the primary options. |

### Release-bucket summary

| Requirement | Status |
| --- | --- |
| Create/open/validate/rebuild library | Delivered through service, API, CLI, and dashboard surfaces as appropriate. |
| Preview and repeatedly import RDF | Delivered. |
| Copy source PDFs into paper source directories | Delivered. |
| Convert with Marker in isolated children | Delivered; local laptop golden execution is explicitly owner-deferred, not an offline gate. |
| Produce high-quality transcription and figure assets | Delivered through Marker normalization and structural acceptance assets; approved stronger/remote hardware may run the optional golden suite. |
| Run summary and contribution recipes | Delivered. |
| Generate indexes, root guidance, and ignore rules | Delivered. |
| Reliable dashboard with status/logs/cancel/retry | Delivered for individual and selected-batch recovery flows. |
| Inspect metadata and generated artifacts | Delivered. |
| Automated domain, invariant, contract, and critical-UI tests | Delivered, including offline smoke and visual baselines. |

## Verification record

The release candidate should not be tagged until the required checked commands
pass from the intended dependency profile and the remaining release steps are
complete.

- [x] Fresh default `uv sync` plus `uv run python scripts/smoke.py`: recorded
  by the clean-environment work, 1 smoke test passed; no optional extras.
- [x] Offline formatting, lint, type checking, and default tests: covered by
  the implementation verification; rerun after artifact removal.
- [x] Browser suite and committed visual baselines: covered by the UI work;
  lifecycle, maintenance, and selected-batch retry fixes include browser tests.
- [x] Supplied real-world RDF slice: two papers from the owner-provided
  Thesis2026 export imported, reindexed, and validated successfully. No source
  path or paper content is retained in this repository.
- [x] Real OpenAI provider connectivity: a tiny `gpt-5.6-luna` smoke passed on
  2026-07-15. Credentials and provider payloads were not recorded.
- [x] Real GPT-5.6 prompt-cache verification: an explicit-prefix pair recorded
  cache-write tokens on the first call and cached-read tokens on the second.
- [ ] `uv run pytest -m gpu`: intentionally not run on the target laptop by
  owner safety direction. Optional on an approved stronger/remote machine;
  not an offline release blocker.
- [ ] Full `uv run pytest -m llm`: intentionally not run because it may spend
  money. The small real-provider connectivity smoke above passed.
- [x] Real remote-host smoke: the production fresh-child path passed against
  `noesis` (Ubuntu 24.04.4, RTX 3090, Marker 1.10.2) on 2026-07-15.
- [x] Confirmed no real corpus PDFs, credentials, `.pp/`, or temporary output
  are in the release commits; only the established tiny RDF test-fixture PDFs
  are tracked.
- [x] All applicable checks passed after the approved transitional-artifact
  removal: formatting, lint, type checking, default tests, browser tests,
  visual baselines, lock verification, and the fake end-to-end smoke.
