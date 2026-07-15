# Paper Pipeline Refactor Workshop

This document defines the product and architectural direction for the next
version of Paper Pipeline.

It is not yet a detailed implementation specification. The purpose of this
pass is to establish what the product should do, which capabilities belong in
the first useful version, and what responsibilities the code must separate.
Exact schemas, directory layouts, frameworks, and module trees will be designed
after these requirements are stable.

## How to Use This Document

- Treat statements under **Decided**, **Resolved**, or **First Useful
  Version** as current requirements.
- Treat **Open Questions** and deferred decisions as work that still needs
  resolution.
- Keep later possibilities visible only when they affect an early design
  boundary.
- Do not introduce infrastructure solely for a speculative future feature.
- Record durable implementation decisions as ADRs once concrete design begins.

# 1. Basic Context

## What Paper Pipeline Is

Paper Pipeline is a local library builder for academic papers.

It consumes a collection managed in Zotero and produces a portable,
folder-based library that humans and LLM agents can browse with ordinary
filesystem tools. The generated library is the product. The Paper Pipeline
application is the tool used to build and maintain it.

The motivating observation is that agents are unreliable when asked to recall
the literature, but much more useful when they can search dependable local
sources with tools such as `rg`. PDFs remain valuable source documents, but are
a poor primary interface for this kind of agent workflow. Paper Pipeline turns
them into concise metadata, searchable Markdown, useful indexes, and optional
derived summaries.

Paper Pipeline is not a research workspace. It does not perform literature
reviews, manage a writing project, or prescribe how a generated library will be
used. A library should be usable by arbitrary downstream human and agent
workflows without Paper Pipeline running.

## Why the Old Version Is Being Replaced

The old version proved the basic idea, but its design was shaped too closely by
its first implementation:

- Nougat became the center of the pipeline instead of one document conversion
  implementation.
- Metadata, processing state, generated files, and UI state did not have clear
  ownership.
- Each new workflow required workflow-specific state, worker, API, and UI code.
- The browser UI mixed several state-management approaches and was easy for
  agents to break while extending it.
- The project lacked tests and sufficiently precise operational guidance for
  reliable agent-driven development.

The old code lives under `v1/` as reference material only.

## Decided Product Direction

- This is a greenfield redevelopment.
- There is no migration requirement for v1 databases.
- The initial product is personal, local software, not a commercial or
  multi-tenant service.
- Paper Pipeline is exclusively a library builder, not a general research
  workbench.
- Zotero is an import source. A user can import later exports to add papers and
  refresh Zotero-owned metadata, but continuous two-way synchronization is not
  required.
- Zotero RDF is an acceptable first import format.
- The generated library is independent of Paper Pipeline and can be copied to
  another location or machine.
- Persistent library state belongs inside the library. Paper Pipeline must not
  depend on a separate hidden database elsewhere on the machine.
- Paths stored inside the library must be relative.
- The primary agent interface is the filesystem.
- Agents are expected to read libraries, not modify them.
- Generated content is primarily an artifact for agent use. Manual editing is
  possible but is not a supported authoring workflow.
- The UI is an operational dashboard with lightweight document inspection, not
  an editing or research environment.
- Libraries never contain secrets or machine-specific configuration.
  Application configuration lives outside the library.
- Automated tests, dependable development commands, and a strong root
  `AGENTS.md` are first-class requirements.

Greenfield development removes v1 compatibility work, but does not remove the
need for versioned formats and safe evolution of newly generated libraries.

## Product Principles

1. **The library is the product.** The application exists to produce and
   maintain a useful standalone artifact.
2. **Agent search is the primary use case.** Content should be easy to locate
   and read with ordinary filesystem tools.
3. **Human operation should be safe and legible.** The UI should make imports,
   processing, failures, and retries understandable.
4. **Generated and source-derived content must be distinguishable.** An agent
   should not mistake an LLM summary for text extracted from the paper.
5. **Derived features must be rebuildable.** Indexes, summaries, and generated
   views must not become irreplaceable sources of truth.
6. **Processing technology must be replaceable.** Marker is the preferred
   first converter, but the product model must not be Marker-specific.
7. **Expensive dependencies must be contained.** GPU processing should be
   isolated so failures and memory leaks do not destabilize the application.
8. **Reliable extension beats clever abstraction.** A new recipe or converter
   should have an obvious home and should not require unrelated cross-layer
   edits.
9. **Complexity must earn its place.** Do not build a plugin platform,
   distributed scheduler, semantic database, or general workflow graph before
   a real feature requires it.

## Intended Shape of a Generated Library

The exact layout remains undecided, but the public shape is deliberately
simple:

```text
<library>/
    AGENTS.md
    indexes/
        ...small agent-oriented text indexes...
    <citekey>/
        ...metadata, source-derived content, and generated artifacts...
    <citekey>/
        ...
```

Each paper directory contains bibliographic metadata, a high-quality
`transcription.md`, and the source PDF in a dedicated subdirectory. It may
also contain figures, rendered page images, recipe outputs, and clearly
separated diagnostic metadata.

The library may contain internal operational files, but they must be clearly
marked, safe for agents to ignore, and safe to exclude from version control
when appropriate. Deleting rebuildable indexes or disposable diagnostics must
not destroy the underlying papers.

## Resolved Product Questions

These questions were raised during workshopping and are now decided. Treat
them as requirements, equivalent to the **Decided Product Direction** above.

- **Source PDFs are always included.** Each paper directory contains a
  dedicated subdirectory holding the source PDF. The original library folder
  is therefore always fully reprocessable from its own contents.
- **The citekey is the paper identity.** Citekeys are what appear in `.tex`
  files, so using them as directory names lets an agent find a paper in one
  hop instead of two. The design assumes well-behaved citekeys: unique within
  a library and stable across Zotero exports. Paper Pipeline builds no
  rename-tracking machinery; if a citekey changes upstream, re-import treats
  it as a new paper, with the import preview and duplicate detection as the
  safety net.
- **Papers removed from a later Zotero export are retained.** Import never
  deletes library papers. Cleanup policies, if ever needed, are a deliberate
  later feature.
- **Version control policy:** everything is committable except logs,
  intermediates, and the source PDFs, which the generated `.gitignore`
  excludes by default. Consequence: a Git *clone* of a library is readable
  and searchable but cannot be reprocessed from source; only the original
  folder or a full file copy retains that ability. This is accepted.
- **Agents do not cite the library.** Citation happens in LaTeX through a
  BibTeX file and citekey, outside Paper Pipeline's scope. The library exists
  so an agent can read the text behind a citation: look up the citekey, find
  the matching directory, `rg` the transcription, and verify a claim or
  inspect a table. No local citation or evidence-reference format is needed.

# 2. Feature Plans

## Core User Journey

1. Select a Zotero RDF export and a destination library.
2. Preview the papers that will be added or refreshed.
3. Import bibliographic metadata and the chosen source files.
4. Convert selected or pending PDFs into searchable Markdown.
5. Inspect successes, failures, logs, and representative output.
6. Run selected enrichment recipes such as summary or key-claim extraction.
7. Rebuild the library indexes and generated `AGENTS.md`.
8. Copy, version, or use the generated library independently of Paper Pipeline.

The same library can later receive another Zotero RDF import. New papers are
added and Zotero-owned metadata is replaced from the newer export. This is
repeatable import, not live synchronization.

## Library Creation and Maintenance

### First Useful Version

- Create a new library in a user-selected directory.
- Open and inspect an existing library.
- Import metadata and attachments from Zotero RDF.
- Preview additions and metadata refreshes before applying an import.
- Add papers from later RDF exports without rebuilding the whole library.
- Replace Zotero-owned metadata during a refresh.
- Detect missing source attachments and invalid paper directories.
- Avoid silently creating two entries for the same imported paper.
- Validate the library and report actionable problems.
- Rebuild all derived indexes and generated library guidance.
- Clearly distinguish managed outputs, source inputs, and disposable
  diagnostics.

### Later or Conditional

- More sophisticated duplicate detection and merge assistance.
- Additional Zotero export formats.
- Non-Zotero ingestion, if a real use case appears.
- Removal, archival, and cleanup policies beyond a conservative default.

The ingestion boundary should normalize Zotero data before it reaches the rest
of the application. This is a modest design seam, not a commitment to a general
connector or plugin framework.

## Document Conversion

High-quality PDF-to-Markdown conversion is a central feature.

### First Useful Version

- Use Marker as the first conversion backend, subject to a representative
  quality and runtime test before implementation is finalized.
- Produce `transcription.md` for each successfully converted paper.
- Preserve useful structure supported by the converter, including headings,
  equations, tables, figures, captions, and references where practical.
- Preserve extracted figures when Marker produces useful assets.
- Support native-text PDFs and attempt scanned PDFs through the capabilities of
  the selected backend.
- Process one paper, an explicit selection, or all pending papers.
- Run each conversion in a fresh process so GPU memory and backend failures are
  forcibly contained at paper boundaries.
- Record success, failure, timing, backend version, and enough diagnostic
  information to understand a failed run.
- Allow failed papers to be retried.
- Never treat terminal output alone as proof of success; validate expected
  artifacts before marking a conversion complete.

Markdown is the required agent-facing result. Structured converter output may
be retained when it is cheap and useful for debugging or future features, but
it is not currently a required public library interface.

### Later or Conditional

- Render every PDF page as an image for quick visual inspection.
- Select a different converter for exceptional documents.
- Delegate conversion to a single user-controlled remote host over SSH
  (e.g. a home server with a stronger GPU) when the local GPU is too slow.
  This is one remote backend behind the converter contract, not
  distributed processing.
- Compare converter outputs or configurations.
- Expose advanced conversion settings in the UI.
- Manual correction or approval workflows.
- Rich page-level provenance.

The first version should not prevent alternate converters, but it does not need
runtime plugin discovery or multiple implemented backends.

## Agent-Oriented Indexes and Retrieval

The library is searched directly from the filesystem. Paper Pipeline does not
need to become a search service.

### First Useful Version

- Generate a short root `AGENTS.md` explaining the library structure and how to
  search it.
- Keep transcriptions and recipe outputs searchable with `rg`.
- Generate small, rebuildable, text-based indexes that reduce the amount of
  directory exploration needed by an agent.
- Include at least title, citekey, authors, and available generated summaries in
  appropriate indexes.
- Make source-derived transcription and LLM-generated analysis visibly
  different through naming or placement.
- Use stable, predictable paths so an agent can go from citekey to content
  without directory exploration.
- Generate a `.gitignore` or equivalent guidance for disposable logs, caches,
  and other operational noise.

Candidate indexes include:

- `titles`: citekey to full paper title.
- `authors`: citekey to author list.
- `summaries`: citekey to a short generated summary.
- `keywords`: citekey to imported or generated keywords, if useful.
- `status`: papers missing required outputs or containing failures.

The precise file format is deferred. The important properties are that indexes
are concise, easy to search, easy for agents to understand, and completely
rebuildable from paper directories.

### Explicit Non-Goals

- MCP support.
- A special agent CLI for reading the library.
- Semantic or vector search in the initial product.
- Citation-graph navigation.
- Saved search collections.
- Cross-paper research or comparison workflows.
- Agent-authored notes managed by Paper Pipeline.

Downstream agents may build any of these on top of a generated library. They do
not belong to the library builder itself unless future experience demonstrates
a need.

## Analysis and Enrichment Recipes

Recipes produce compact, agent-oriented Markdown artifacts using an external
LLM provider. They enrich the library; they do not perform research on behalf
of the user.

### First Useful Version

- Include a small set of useful built-in recipes, including a concise summary
  and key-claim or key-contribution extraction.
- Allow a recipe to run over one paper or a selected set of papers.
- Represent a recipe as a simple template that defines its input and prompt.
- Produce a predictably named Markdown file in the corresponding paper
  directory.
- Record enough metadata to identify the recipe, model/provider, prompt version,
  and source used to create an output.
- Show recipe progress and failures in the same operational UI as conversion.
- Retry failed recipe runs.
- Keep the execution model linear.

A conceptual recipe might look like:

```text
input: pdf
output: contributions.md

Extract the key contributions in this paper.
Format them as a bulleted list.
Output only the contributions.
```

The actual template syntax is deferred. It must eventually answer:

- Which input artifact is sent: source PDF, transcription, or another output?
- What output filename and media type are expected?
- Which prompt and provider settings apply?
- What constitutes a valid result?

### Scheduling Requirement

LLM providers cache large prompt inputs such as an uploaded PDF. Two recipes
sent concurrently for the same paper defeat that cache; the same two recipes
sent back-to-back let the second hit it. The scheduler must therefore:

- run recipes for any single paper sequentially, so later recipes against the
  same input reuse the provider's cache;
- allow concurrency across different papers, up to provider limits; and
- keep GPU conversion serialized and isolated from recipe scheduling.

The first implementation can use simple task categories and policies. It does
not require a general workflow graph.

### Later or Conditional

- User-created recipes through the UI.
- Structured JSON recipe outputs.
- Multiple retained results from different prompt or model versions.
- Recipes that consume outputs from other recipes.
- Additional built-ins for methods, datasets, metrics, and results.

## Jobs and Operations

### First Useful Version

- Present queued, running, completed, failed, and cancelled work.
- Run heavy local conversion at a safe concurrency, initially one conversion at
  a time unless testing proves otherwise.
- Use fresh child processes for conversion so memory is reclaimed after each
  paper.
- Permit concurrent remote API jobs across papers according to provider
  limits, while sequencing same-paper recipes to preserve provider cache
  reuse.
- Cancel current and queued work safely.
- Retry individual failures or selected batches.
- Keep useful logs and diagnostic metadata in a clearly ignorable location.
- Keep browser closure independent of active work: closing a browser tab must
  not cancel server-side jobs.
- Validate outputs on disk before reporting completion.
- Recover conservatively from application interruption, without trusting a
  stale in-memory status.

The UI server itself does not need to be a persistent operating-system service
in the first version. If the application process exits, active child work may
be stopped and later classified as interrupted.

### Later or Conditional

- Resume partially completed backend work.
- Persistent daemon operation after the main application exits.
- Rich dependency, GPU, provider, and cost diagnostics.
- Work estimates before large batches.
- Formal cross-process library locks if real concurrent-writer use requires
  them.
- Multi-machine execution beyond the conditional single-host SSH
  conversion delegation noted under Document Conversion.

## Browser UI and UX

The browser UI is an operations dashboard with lightweight inspection.

### First Useful Version

- Choose or create a library.
- Preview and apply a Zotero RDF import.
- Browse and filter the paper list by basic metadata and processing state.
- Select papers and launch conversion or enrichment recipes.
- Show the job queue, current work, progress events, failures, and logs.
- Retry or cancel work.
- Open a paper detail view with metadata, transcription, recipe outputs, and
  available figures.
- Provide a lightweight way to inspect the source PDF or representative page
  images if doing so is technically straightforward.
- Rebuild indexes and validate the library.

### UI Engineering Requirements

- Each piece of UI state has one explicit owner.
- Server-owned library and job state is not duplicated as independent client
  truth.
- Full pages and detail views do not implement the same behavior separately.
- Repeated statuses and actions use shared components.
- Important views have stable URLs.
- Empty, loading, error, disconnected, cancelled, and interrupted states are
  intentionally designed.
- Layout and interaction behavior is tested in a real browser.
- Important layouts receive visual regression coverage.
- Inline DOM manipulation outside the chosen UI state model is prohibited.

The UI does not edit bibliographic metadata, manage notes, conduct research, or
need comprehensive PDF annotation tools.

## Portability and Version Control

### First Useful Version

- A complete library folder can be copied without breaking internal paths.
- The library remains useful without Paper Pipeline installed or running.
- All managed internal references use relative paths.
- Derived indexes can be rebuilt after papers are added or removed.
- A generated `.gitignore` keeps disposable runtime noise out of version
  control and ordinary searches.
- The library format makes it clear which content is essential, derived, or
  disposable.

Removing a paper directory manually may temporarily make indexes stale. The
validator and index rebuild operation should make this safe and unsurprising
rather than requiring a central database repair.

### Decided Source-PDF Policy

Source PDFs are always copied into a dedicated subdirectory of each paper
directory and are git-ignored by default. The original library folder is fully
self-contained and reprocessable; a Git clone is readable and searchable but
cannot rerun conversion from source. A library must never depend on an
absolute path into a Zotero export.

## Configuration and Secrets

The library must stay portable and committable, so it cannot carry secrets or
machine-specific settings.

### First Useful Version

- Application configuration lives outside the library, in a small user-level
  config file and/or environment variables: LLM provider credentials, model
  selection, and converter/GPU settings.
- Secrets are only ever read from application configuration or the
  environment. They are never written into a library, into logs stored in the
  library, or into recipe provenance metadata.
- Recipe definitions are the main configuration surface. Built-in recipes
  ship with the application, not with the library. Recipe provenance recorded
  in the library names the recipe, prompt version, and model — never
  credentials.
- Beyond provider settings and recipes, the application should aim for
  near-zero configuration. Prefer sensible defaults over new settings.

### Later or Conditional

- Per-library recipe selection or overrides, if a real need appears.

## Candidate Release Buckets

### First Useful Version

- Create, open, validate, and rebuild a library.
- Preview and repeatedly import Zotero RDF exports.
- Copy source PDFs into each paper's dedicated source subdirectory.
- Convert selected papers with Marker in isolated child processes.
- Produce high-quality `transcription.md` and useful figure assets.
- Run built-in summary and key-claim/contribution recipes.
- Generate concise text indexes, root `AGENTS.md`, and `.gitignore`.
- Operate batches through a reliable dashboard with status, logs, cancellation,
  and retry.
- Inspect paper metadata and generated artifacts in the browser.
- Cover domain rules, library invariants, processing contracts, and critical UI
  journeys with automated tests.

### Likely Next

- More built-in recipes, especially methods, datasets, metrics, and results.
- User-authored recipe templates.
- Better PDF or page-image inspection.
- Richer import reconciliation and duplicate detection.
- Improved interruption recovery and diagnostics.
- Structured recipe output where it provides a concrete benefit.

### Explore Later

- Alternate conversion backends.
- Non-Zotero import sources.
- Advanced conversion comparison and configuration.
- Persistent daemon operation.
- More sophisticated provenance conventions.
- Selective export of a subset of papers.

### Explicit Non-Goals

- Migration from the v1 database format.
- Commercial, hosted, or multi-tenant operation.
- Two-way or live Zotero synchronization.
- A research workspace or writing environment.
- Metadata and note editing in the UI.
- MCP support or a special library-reading CLI.
- Semantic search as a foundational feature.
- Citation graphs, saved searches, or cross-paper research workflows.
- A general workflow DAG system.
- Distributed or multi-machine processing. (Delegating conversion to one
  user-controlled SSH host is a conditional feature, not this.)

# 3. Code Organization Plans

The feature plan now implies several clear responsibility boundaries. This
section defines those boundaries without yet choosing the repository tree or
the exact types used to implement them.

## Central Architectural Rule

The generated library format is the contract between Paper Pipeline and every
downstream consumer.

The application may use in-memory state, rebuildable indexes, and disposable
runtime files to operate efficiently. It must not keep a second authoritative
paper database outside the library. Restarting the application should recover
truth by inspecting durable library artifacts, not by trusting browser state or
an unrelated application database.

## Responsibility Boundaries

### Library Model and Storage

Responsible for:

- The versioned library format and its invariants.
- Paper identity, citekeys, metadata, and source ownership.
- Relative path handling.
- Safe reads and atomic writes.
- Classification of essential, derived, and disposable files.
- Validation and repair reporting.

Not responsible for Zotero parsing, Marker execution, LLM calls, HTTP routes, or
UI state.

### Zotero Ingestion

Responsible for:

- Parsing RDF exports and locating exported attachments.
- Normalizing Zotero-specific data into library import records.
- Comparing an import snapshot with the current library.
- Producing a previewable add/refresh plan.

Not responsible for directly scattering files around the library or deciding
how the UI renders an import preview.

### Document Conversion

Responsible for:

- A small converter contract expressed in product terms.
- Preparing a per-paper conversion request.
- Launching Marker through process isolation.
- Collecting, normalizing, and validating its output.
- Returning declared artifacts and diagnostics to library storage.

Marker-specific flags and output quirks remain inside the Marker adapter. The
rest of the application deals with conversion requests and results.

### Enrichment Recipes

Responsible for:

- Loading and validating recipe definitions.
- Resolving declared paper inputs.
- Calling the configured LLM provider.
- Validating and returning the declared output.
- Recording recipe and provider provenance.

Recipe definitions describe work; they do not implement queueing, HTTP APIs, or
paper storage themselves.

### Job Execution

Responsible for shared operational behavior:

- Queueing and task state transitions.
- Process lifecycle and cancellation.
- Task-category concurrency limits.
- Per-paper sequencing of remote recipe work to preserve provider cache
  reuse.
- Logging, retries, interruption detection, and completion validation.
- Publishing progress events to interested interfaces.

Conversion and LLM recipes may have different execution policies, but they must
not grow separate job systems.

### Index Generation

Responsible for:

- Reading canonical paper content.
- Producing concise text indexes and root agent instructions.
- Rebuilding indexes deterministically.
- Detecting stale entries caused by manually removed paper directories.

Indexes never become the canonical paper registry.

### Application Services

Responsible for user-level operations such as:

- Preview import.
- Apply import.
- Convert selected papers.
- Run a recipe.
- Cancel or retry work.
- Validate a library.
- Rebuild indexes.

Both the browser API and any maintenance CLI use these services. Business rules
must not be duplicated in routes, templates, JavaScript, or command handlers.

### Web API and Client

The API translates HTTP requests and job events into application-service calls
and response models. The client renders those models and owns only ephemeral
interaction state.

The client must not infer durable state from placeholder content, manually
patch DOM owned by another state system, or maintain an independent paper/job
database in the browser.

## Dependency Direction

The desired dependency direction is:

```text
Web client
    -> Web API
        -> Application services
            -> Library, ingestion, conversion, recipes, jobs, indexing
                -> Marker, Zotero RDF, LLM provider, filesystem
```

Domain and library rules do not import FastAPI, frontend code, Marker, or an
LLM SDK. External integrations sit at the edges and are exercised through
small contracts.

## State and Artifact Ownership

The design must distinguish at least three kinds of state:

1. **Library content:** durable, portable content needed by downstream agents.
2. **Derived library content:** indexes and generated enrichment outputs that
   can be rebuilt or rerun.
3. **Operational state:** logs, temporary files, child-process metadata, and
   active-job information used while Paper Pipeline is operating.

The exact placement is deferred, but ownership must be explicit. An output is
not complete until it has been validated and atomically installed into its
final library location.

## Extension Rules

- Adding a built-in recipe should primarily require a recipe definition, not a
  new worker, status schema, API family, and UI component family.
- Adding a converter should implement the converter contract and its own output
  normalization without changing the library model.
- Adding an index should not change paper storage or import behavior.
- Adding a UI view should consume existing application contracts rather than
  reach directly into filesystem internals.
- Large GPU and provider dependencies should remain optional at import time and
  isolated from the core library code.

## Decisions Still Deferred

- Exact library directory layout and filenames beyond the public expectations
  recorded above.
- Library and per-paper metadata schemas.
- Exact recipe template syntax.
- Python package and module tree.
- Frontend framework and component library.
- API style and progress-event transport.
- Job-state persistence representation.
- Exact operational-file and `.gitignore` conventions.

# 4. Development and Project Meta

## Testing Strategy

Tests are mandatory because the project will be extended by agents and v1
showed how easily cross-layer behavior can regress.

### Library Tests

- Unit tests for identity, metadata replacement, path, and validation rules.
- Invariant tests proving generated paths are relative and libraries do not
  depend on external application state.
- Import tests covering first import, later additions, metadata refresh, missing
  attachments, and duplicate candidates.
- Index rebuild tests, including manually removed paper directories.
- Atomic-write and interrupted-write tests.

### Processing Tests

- A small representative PDF corpus covering native text, multiple columns,
  equations, tables, figures, and scanned content.
- Converter contract tests with a cheap fake converter.
- A slower Marker smoke or golden test separated from the default fast suite.
- Child-process failure, timeout, cancellation, and cleanup tests.
- Recipe parsing, input resolution, output validation, retry, and provenance
  tests using a fake LLM provider.
- Scheduler tests for GPU serialization, API concurrency, and per-paper recipe
  ordering.

### Application and UI Tests

- Application-service tests independent of HTTP.
- API contract tests.
- Browser tests for import, selection, conversion launch, recipe launch,
  cancellation, retry, and paper inspection.
- Visual regression tests for the library table, detail view, job state, and
  important error/empty states.
- A clean-environment smoke test using only documented commands.

The required checks for each class of change must be documented in
`AGENTS.md`, and the fast default test suite must not require a GPU or paid API.

## Agent Development Operations

The new root `AGENTS.md` should be written as soon as the first concrete project
structure and commands exist. It should be concise and operational.

It must include:

- The product boundary: library builder, not research workspace.
- The dependency direction and canonical sources of truth.
- Generated, derived, user-owned, and disposable file rules.
- Exact setup, formatting, linting, type-checking, test, and smoke commands.
- Rules for GPU work, child processes, concurrency, cancellation, and cleanup.
- How to validate successful work from durable artifacts.
- Required checks for backend, UI, schema, recipe, and dependency changes.
- A definition of done.

Nested `AGENTS.md` files may be added only when a subproject has genuinely
different commands or safety constraints. Long architectural explanations
belong in dedicated documentation and ADRs.

## Development and Dependency Expectations

- The default development loop should be fast and should not launch Marker,
  consume API credits, or require a GPU.
- External services and heavy processing backends need fake implementations for
  normal tests.
- Dependency groups should keep core development, UI development, Marker/GPU
  support, and optional providers separable where practical.
- Setup and health checks should fail with actionable explanations.
- Formatting, linting, type-checking, and tests should have single documented
  entry points suitable for humans, agents, and CI.
- Long-running commands must make their process and artifact behavior clear.

## Decisions Required Before Detailed Structure Design

The next workshop should resolve:

1. The minimum canonical per-paper metadata and status information, including
   where durable per-paper processing status lives so interruption recovery
   has a defined source of truth.
2. Essential versus optional files in a paper directory.
3. The recipe template contract and first built-in recipes.
4. The operational-state and logging model.
5. The concrete backend/frontend technology choices.

Source-PDF ownership and paper identity were resolved during this pass; see
**Resolved Product Questions** in section 1.

Once those are answered, the repository tree, library schema, APIs, and first
implementation milestones can be designed without guessing at the product.
