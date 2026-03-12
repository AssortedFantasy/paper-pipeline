# Architecture

This document is currently a handoff-style architecture note.

It is meant to capture:

- what the repo does today
- what architectural choices exist in the current MVP
- what problems have already shown up
- what kinds of improvements we likely want next
- a brainstorm-oriented task list for future work

This is intentionally not a full detailed design spec yet.
Some future directions here are deliberately vague and should be refined later.

## Purpose

The repo turns a Zotero RDF export plus attached PDFs into a local paper database that is easy for humans and agents to inspect.

Today the main outputs are:

- `papers/<citekey>/meta.md`
- `papers/<citekey>/transcribed.md`
- `papers/<citekey>/status.json`
- `papers/<citekey>/nougat_raw/`

The main runtime paths are:

- CLI via `paper-pipeline`
- Web UI via `paper-gui`

The main transcription backend is Nougat.

## Current MVP Architecture

At a high level, the system currently works like this:

1. Parse a specific Zotero RDF export.
2. Resolve linked attachments from the local export structure.
3. Build per-paper folders under `papers/`.
4. Generate `meta.md` and a placeholder `transcribed.md`.
5. Run Nougat one paper at a time.
6. Store raw Nougat output and update lightweight status files.
7. Present the results in a local web dashboard.

Important architectural characteristics of the current MVP:

- The database format is intentionally simple and file-based.
- CLI and GUI share the same Nougat execution path.
- Long-running GPU work is serialized to reduce overlap and instability.
- A workspace lock is used to prevent overlapping runs.
- The UI is server-rendered HTML with Alpine, htmx fragments, SSE updates, and custom JavaScript.
- The paper metadata displayed by the app is still derived from the RDF rather than from a fully self-contained database manifest.

## What The Current Design Gets Right

- The project has a pragmatic and inspectable file layout.
- The generated paper folders are easy to browse manually.
- The CLI and GUI do not have fully separate transcription logic.
- The serial GPU job model matches the durability and safety concerns of Nougat on this machine.
- Raw outputs and logs are kept on disk, which helps recovery and debugging.

These are important strengths and should generally be preserved while the architecture evolves.

## Main Architectural Frictions Seen So Far

The MVP works, but several design limitations are already visible.

### 1. State Is Split Across Too Many Sources

Current state is spread across:

- RDF-derived metadata in memory
- generated Markdown files
- `status.json`
- raw Nougat files
- UI bootstrap JSON in templates
- browser-side state in Alpine and custom JavaScript

This makes it harder to answer simple questions cleanly, such as:

- what is the canonical metadata for a paper
- what outputs exist for a paper
- which workflows have run successfully
- which settings were used
- what should the UI trust as current truth

### 2. The Paper Database Is Not Fully Self-Contained

The `papers/` directory is useful, but the application still depends heavily on reparsing the RDF export.

That creates long-term questions around:

- how a future user points the tool at an arbitrary Zotero RDF export
- how repeated RDF re-exports are reconciled with the existing paper database
- how metadata regeneration should work
- which files are generated artifacts versus user-edited content

### 3. Workflow Abstraction Is Only Partially Generalized

The project has a step abstraction, but the runtime still mostly assumes one main workflow: transcription.

That is fine for MVP, but future goals likely include multiple workflow types such as:

- transcription
- summarization
- extraction
- prompt-based LLM runs
- review or annotation steps
- metadata refresh operations

Without a more general workflow model, those future additions will likely duplicate job logic, state handling, and UI patterns.

### 4. UI State And Layout Are Too Fragmented

The current UI stack is workable, but ownership of UI state is not clean.

At the moment, behavior is split across:

- server-rendered templates
- Alpine local state
- htmx fragment injection
- SSE event handling
- polling
- direct DOM manipulation in some cases

This has already shown up in real bugs.

Examples:

- the detail side panel can drift into a broken reopen state after closing
- layout and overflow issues have appeared in the dashboard shell and panel structure

These are not just isolated mistakes. They are signals that the UI architecture currently makes it easy for state, layout, and rendered DOM to drift apart.

## Direction A: State Model And Schema Improvements

We likely want to move toward a more explicit and extensible manifest-based state model.

The rough direction is:

- a top-level `papers/manifest.json`
- a per-paper `papers/<citekey>/manifest.json`

The purpose of these manifests would be to provide structured data that is easier to evolve than inferring state from Markdown files and scattered artifacts.

The exact schema should be designed later, but the high-level goals are clear:

- one more canonical place for app-readable state
- better organization of outputs and workflow results
- easier UI consumption
- better recovery after interruptions
- clearer distinction between structured metadata and user-facing documents
- better extensibility for future workflows

This should not mean removing Markdown artifacts.
The Markdown files are still useful as human-readable outputs.
The point is to stop treating them as the primary state model.

Open design direction:

- keep the schema flexible enough for future workflow-specific outputs
- avoid baking in only Nougat-specific assumptions
- make it possible to update manifests incrementally as work completes
- keep the format easy to inspect and edit if needed

Brainstorm task area:

- define the role of top-level versus per-paper manifests
- identify which current facts belong in structured manifests
- decide which data is authoritative and which is derived
- decide how run history, logs, artifacts, and statuses should be represented at a high level
- define how manifests are created, updated, repaired, and regenerated

## Direction B: Paper Database Self-Containment And RDF Lifecycle

The interaction between the paper database and the original RDF export needs a broader architectural pass.

Today the repo is effectively tuned around `Thesis2026.rdf`, but the longer-term direction should be more general:

- work with arbitrary Zotero RDF exports
- support repeated re-exports over time
- handle metadata refresh without rebuilding everything blindly on startup

This implies that RDF ingestion should be treated as an explicit workflow or set of workflows, not just something that silently happens whenever the app boots.

The likely organizational direction is to think in terms of operations such as:

- import or register an RDF export
- create missing paper folders
- remap or refresh attachment references
- regenerate `meta.md`
- detect removed or changed items
- preserve downstream outputs where appropriate
- reconcile new exports against an existing database

This area also raises important policy questions:

- what counts as stable paper identity across re-exports
- how to handle citekey changes or metadata drift
- how much regeneration is automatic versus user-invoked
- what user edits must never be overwritten
- what startup should do versus what explicit commands should do

The key architectural idea is that database maintenance should become a deliberate lifecycle with explicit steps, rather than a side effect of app startup.

Brainstorm task area:

- separate import/update workflows from runtime viewing workflows
- define the lifecycle for first import versus later re-import
- decide how folder creation, metadata regeneration, and cleanup should be triggered
- decide how much of the original RDF should remain a runtime dependency
- define what the self-contained database needs in order to stand on its own

## Direction C: Workflow Abstraction Improvements

The project likely needs a more general workflow model than the current transcription-first structure.

The future architecture should allow multiple workflow types to coexist without each one reinventing:

- queueing
- status tracking
- configuration
- artifact recording
- logging
- error handling
- UI exposure

The main idea is not to overdesign a giant framework immediately.
The goal is to make sure future steps can plug into a shared conceptual model.

Possible future workflow categories:

- ingest
- metadata refresh
- transcription
- summarization
- classification or tagging
- prompt-template LLM jobs
- export or packaging tasks

Useful architectural questions for later:

- what is a workflow versus what is a step
- whether steps compose into pipelines, batches, or ad hoc actions
- whether every workflow writes outputs into the paper folder, a shared run store, or both
- how workflow-specific config is stored and surfaced
- how users inspect past runs and outputs

Brainstorm task area:

- define a lightweight shared workflow vocabulary
- decouple workflow state from only `status.json`
- decide how per-paper versus cross-paper workflows should differ
- identify the minimum shared abstractions for job execution and artifact recording
- make sure new workflows do not fork the CLI and GUI paths again

## Direction D: UI Organization And Anti-Drift Improvements

The current UI should be treated as a useful MVP, but not yet as the architectural standard for the next phase.

The main concern is drift.
Different pieces of the UI currently own overlapping parts of the truth.
That makes it too easy to produce bugs where:

- the DOM says one thing and application state says another
- a panel looks closed but the app still thinks it is open
- fragment content is replaced while other state stays stale
- layout assumptions break when new controls or longer content are added

The detail-panel reopen bug is a concrete example of this class of problem.
The overflow issues are another warning that the page layout model is too brittle.

The future UI architecture should aim for:

- clearer ownership of UI state
- fewer overlapping state mechanisms
- less ad hoc DOM manipulation
- more explicit layout rules for what scrolls, wraps, truncates, and resizes
- UI structures that support richer workflows without constant regressions

This does not force a specific frontend framework decision yet.
The important part is architectural clarity.

Two broad directions are plausible:

- keep the current lightweight stack but make state ownership much more disciplined
- eventually adopt a more explicit client-side state model if the UI grows significantly more complex

Either way, the next phase should avoid fragmented ownership.

Brainstorm task area:

- define who owns major UI state such as selection, detail view, active workflow, and job progress
- reduce mixing of inline DOM manipulation with framework-managed state
- define a shell layout contract for panels, table regions, logs, and detail views
- identify which interactions should be fragment-driven versus state-driven
- add tests for the bug classes already seen, especially reopen and overflow regressions

## Suggested Near-Term Architecture Themes

These themes cut across all the areas above.

### Make State More Explicit

Move away from implicit inference from placeholder content and scattered artifacts.
Prefer structured manifests and explicit lifecycle updates.

### Separate Import, Processing, And Viewing

The system currently blurs some of these concerns together.
Longer term, it will be healthier if RDF import/update, workflow execution, and UI browsing are treated as related but distinct concerns.

### Preserve The File-Based Nature Of The Project

The repo should stay easy to inspect locally.
This architecture does not need a heavy database server to become more disciplined.
The likely direction is better structured files, not a fundamentally different deployment model.

### Keep Durability Constraints Front And Center

The current serial execution and workspace locking are not inconveniences to remove casually.
They reflect real Nougat and GPU reliability constraints.
Future architecture should build on those constraints rather than pretending they do not exist.

## Brainstorm-Oriented Task List

This is not a final roadmap.
It is a list of architecture topics worth exploring.

- Sketch a manifest-based state model for the database root and for each paper.
- Decide what the canonical self-contained paper record should be independent of live RDF parsing.
- Define the lifecycle for importing and re-importing Zotero RDF exports.
- Decide how metadata regeneration should be invoked and what it may overwrite.
- Clarify the difference between artifacts, state, metadata, and user-authored outputs.
- Generalize the workflow model beyond transcription.
- Decide how future workflows record configs, outputs, and histories.
- Clean up UI state ownership so detail panels and similar interactions have one source of truth.
- Define a more robust layout contract for the dashboard shell.
- Add tests for the classes of UI regressions already seen.
- Decide how this document should evolve from handoff notes into a living architecture reference.

## What This Document Is Not Yet

This is not yet:

- a formal schema spec
- a final workflow framework design
- a migration plan
- a fully prioritized implementation roadmap

Those should come later.
For now, this document exists to preserve architectural context and guide the next round of design discussion.

## Later Evolution Of This Document

This file should eventually become a living architecture document.

That later version may grow to include:

- canonical data model definitions
- explicit workflow concepts
- UI state ownership rules
- lifecycle diagrams for RDF import and database refresh
- migration notes as the architecture changes
- architecture decisions and tradeoffs recorded over time

For now, the primary value is clarity during handoff and brainstorming.